/* Volt operator UI. No framework, no build, same-origin only.
 *
 * The screen shows what the server last returned, never a guess. Every action waits
 * for the server and then shows its returned state or its refusal (code + reason).
 * See docs/specs/operator-ui.md.
 */
'use strict';
(function () {
  // ---- endpoints: the only paths this page calls. A test checks each exists on the server.
  const EP = {
    state:       ['GET',  '/api/state'],
    go:          ['POST', '/api/shows/{id}/go'],
    take:        ['POST', '/api/shows/{id}/take'],
    hold:        ['POST', '/api/shows/{id}/hold'],
    release:     ['POST', '/api/shows/{id}/release'],
    reorder:     ['POST', '/api/shows/{id}/reorder'],
    skip:        ['POST', '/api/items/{id}/skip'],
    kill:        ['POST', '/api/items/{id}/kill'],
    recordStart: ['POST', '/api/record/start'],
    recordStop:  ['POST', '/api/record/stop'],
    cleared:     ['POST', '/api/assets/{id}/cleared'],
    legalHold:   ['POST', '/api/assets/{id}/legal-hold'],
    publish:     ['POST', '/api/spans/{id}/publish'],
  };
  // Actions that run one at a time on the server (the sequencing lock). Hold, release, rights
  // and publish do not wait on it, so HOLD stays pressable while a take waits on the room.
  const SEQUENCED = new Set(['go', 'take', 'skip', 'kill', 'reorder', 'recordStart', 'recordStop']);

  const POLL_MS = 2000, POLL_HIDDEN_MS = 6000, TAKE_COOLDOWN_MS = 1500, ARM_MS = 4000;
  const SCREENS = ['show', 'record', 'rights', 'publish'];

  const ERR_LABEL = {
    held: 'Held', not_cleared: 'Not cleared', no_media: 'No media', cue_failed: 'Cue failed',
    cue_unsupported: 'Cue unsupported', no_room: 'No room', room_refused: 'Room refused',
    not_wrapped: 'Not wrapped', publish_failed: 'Publish failed', not_found: 'Not found',
    not_skippable: 'Cannot skip', not_killable: 'Cannot kill', not_attachable: 'Cannot attach',
    bad_input: 'Bad input', unauthorized: 'Token needed', bad_host: 'Wrong host',
    too_large: 'Too large', internal: 'Server error',
  };
  const CUE_LABEL = {
    'overlay.take': 'Lower third', 'stream.start': 'Start stream', 'show-input.take': 'Input to program',
    'record.start': 'Start record', 'record.arm': 'Arm record', 'audio.snapshot': 'Audio snapshot',
  };
  const ITEM_CHIP = {
    'on air': ['air', '●', 'On air'], ready: ['ready', '○', 'Ready'], aired: ['aired', '✓', 'Aired'],
    skipped: ['skipped', '⤳', 'Skipped'], killed: ['killed', '✕', 'Killed'],
  };
  const CUE_CHIP = {
    fired: ['fired', '✓', 'Fired'], failed: ['failed', '✕', 'Failed'], armed: ['armed', '▶', 'Armed'],
    stored: ['stored', '○', 'Stored'], unsupported: ['unsupported', '?', 'Unsupported'],
  };
  const VER_CHIP = {
    done: ['done', '✓', 'Done'], failed: ['failed', '✕', 'Failed'], queued: ['queued', '…', 'Queued'],
  };
  const REC_STEPS = ['requested', 'preparing', 'producing', 'stopping', 'finalizing', 'completed'];
  const REC_ALIAS = { starting: 'preparing', live: 'producing' };

  const S = {
    data: null, show: null, screen: 'show', epoch: 0, seq: false, busy: false,
    pending: new Set(), lastOk: 0, conn: 'connecting', selected: null, armed: null,
    flash: null, cueDetail: {}, cueLast: {}, lastTake: 0, pubFilter: '', sigs: {},
  };

  // ---- tiny DOM helper -----------------------------------------------------
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const k in attrs || {}) {
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? '' : v);
    }
    for (const c of kids.flat(Infinity)) {
      if (c == null || c === false) continue;
      n.append(c.nodeType ? c : document.createTextNode(String(c)));
    }
    return n;
  }
  const $ = (id) => document.getElementById(id);
  const short = (id) => (id || '').slice(0, 8);

  function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return String(iso);
    return d.toLocaleTimeString([], { hour12: false });
  }

  // ---- the one place that knows the /rundown shape: {hold, items} ----------
  function parseRundown(raw) {
    const r = raw || {};
    const hold = r.hold ? { reason: r.hold.reason || '', heldAt: r.hold.heldAt || '' } : null;
    const items = (r.items || []).map((i) => ({
      id: i.id, slug: i.slug, status: i.status, urls: i.urls || [],
      asRunIn: i.asRunIn || null, asRunOut: i.asRunOut || null,
    }));
    return { hold, items };
  }

  // ---- network -------------------------------------------------------------
  function getToken() { try { return sessionStorage.getItem('volt.token') || ''; } catch (e) { return ''; } }
  function setToken(v) {
    try { v ? sessionStorage.setItem('volt.token', v) : sessionStorage.removeItem('volt.token'); } catch (e) { /* private mode */ }
  }

  async function call(kind, id, body) {
    const [method, tpl] = EP[kind];
    let path = tpl.replace('{id}', encodeURIComponent(id || ''));
    if (kind === 'state' && S.show) path += '?show=' + encodeURIComponent(S.show);
    const headers = {};
    const token = getToken();
    if (token) headers.Authorization = 'Bearer ' + token;
    const init = { method, headers, cache: 'no-store' };
    if (method === 'POST') { headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body || {}); }
    const res = await fetch(path, init);
    let data = null;
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    return { status: res.status, data };
  }

  function setConn(state) {
    S.conn = state;
    const n = $('conn');
    const text = { ok: '● Connected', lost: '✕ Connection lost. This screen may be out of date.',
                   token: '⚠ Token needed', connecting: 'Connecting…' }[state];
    if (n.textContent !== text) n.textContent = text;
    n.classList.toggle('bad', state === 'lost' || state === 'token');
  }

  async function refresh() {
    const epoch = S.epoch;
    try {
      const { status, data } = await call('state');
      if (status === 401) { setConn('token'); if (!$('token-dlg').open) openToken(); return; }
      if (status !== 200 || !data) throw new Error('state ' + status);
      if (epoch !== S.epoch) return; // an action finished meanwhile; its own refresh follows
      S.lastOk = Date.now();
      setConn('ok');
      data.rundown = data.rundown ? parseRundown(data.rundown) : null;
      S.data = data;
      if (data.show && S.show !== data.show.id) S.show = data.show.id;
      render();
    } catch (e) {
      setConn('lost');
      render();
    }
  }

  let timer = null;
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(async () => { await refresh(); schedule(); }, document.hidden ? POLL_HIDDEN_MS : POLL_MS);
  }

  // ---- actions -------------------------------------------------------------
  function codeOf(data, status) {
    if (data && data.error && data.error.code) return { code: data.error.code, reason: data.error.reason || '' };
    return { code: 'internal', reason: 'The server answered ' + status + ' with no reason.' };
  }

  function announce(text) {
    const n = $('live');
    n.textContent = '';
    setTimeout(() => { n.textContent = text; }, 30);
  }

  function setFlash(f) {
    S.flash = f;
    if (f && f.kind === 'good') {
      const mine = f;
      setTimeout(() => { if (S.flash === mine) { S.flash = null; render(); } }, 6000);
    }
  }

  /* Run one action. `pendKey` names the button that started it so it can show as busy. */
  async function act(kind, id, body, o) {
    o = o || {};
    const pendKey = o.key || kind + ':' + (id || '');
    const sequenced = SEQUENCED.has(kind);
    if (S.pending.has(pendKey) || (sequenced && S.seq)) return;
    S.pending.add(pendKey);
    if (sequenced) S.seq = true;
    S.epoch++;
    render();
    try {
      const { status, data } = await call(kind, id, body);
      // Show the server's returned state first, then refuse or confirm.
      if (data && data.rundown) { S.data.rundown = parseRundown(data.rundown); }
      if (data && data.asset && S.data) {
        S.data.assets = S.data.assets.map((a) => (a.id === data.asset.id ? Object.assign({}, a, data.asset) : a));
      }
      if (data && data.versions && S.data && o.span) {
        S.data.spans = S.data.spans.map((s) => (s.id === o.span ? Object.assign({}, s, { versions: data.versions }) : s));
      }
      if (status === 401) { setConn('token'); openToken(); }
      if (data && data.ok) {
        if (kind === 'take') (data.results || []).forEach((r) => { delete S.cueLast[r.cue]; delete S.cueDetail[r.cue]; });
        setFlash({ kind: 'good', title: o.okTitle || 'Done', reason: o.ok ? o.ok(data) : '' });
        announce((o.okTitle || 'Done') + ' ' + (o.ok ? o.ok(data) : ''));
      } else {
        const e = codeOf(data, status);
        setFlash(refusal(kind, e, data, o));
        announce((ERR_LABEL[e.code] || 'Refused') + '. ' + e.reason);
      }
    } catch (err) {
      setFlash({ kind: 'bad', title: 'No answer from the server.', code: 'network',
        reason: 'The request may or may not have landed. Check the rundown before trying again.' });
      setConn('lost');
    } finally {
      S.pending.delete(pendKey);
      if (sequenced) S.seq = false;
      S.epoch++;
      render();
      refresh();
    }
  }

  function refusal(kind, e, data, o) {
    const f = { kind: 'bad', title: (ERR_LABEL[e.code] || 'Refused') + '.', code: e.code, reason: e.reason };
    if (kind === 'take' && data && data.results) {
      (data.results || []).forEach((r) => { S.cueDetail[r.cue] = r.detail || r.status; S.cueLast[r.cue] = r.status; });
      const n = data.results.length;
      const at = data.results.findIndex((r) => r.status !== 'fired');
      const air = data.rundown && parseRundown(data.rundown).items.find((i) => i.status === 'on air');
      const parts = [];
      if (at >= 0) parts.push('Cue ' + (at + 1) + ' of ' + n + '.');
      parts.push(air ? air.slug + ' is still on air.' : 'Nothing is on air.');
      parts.push('Fired cues stay fired. Taking again resumes at the failed cue.');
      f.sub = parts.join(' ');
      f.title = 'Take failed.';
    }
    if (kind === 'publish' && data && data.versions) {
      const done = data.versions.filter((v) => v.status === 'done').length;
      f.sub = done + ' of ' + data.versions.length + ' versions done. Publish again retries the failed ones only.';
    }
    if (o && o.hint && !f.sub) f.sub = o.hint;
    return f;
  }

  // ---- derived show state --------------------------------------------------
  function showInfo() {
    const r = S.data && S.data.rundown;
    const items = r ? r.items : [];
    const air = items.find((i) => i.status === 'on air') || null;
    const floor = air ? items.indexOf(air) : -1;
    const next = items.find((i, idx) => idx > floor && i.status === 'ready') || null;
    return { items, air, next, hold: r ? r.hold : null };
  }
  function cuesOf(id) { return (S.data && S.data.cues && S.data.cues[id]) || []; }
  function mediaOf(id) { return (S.data && S.data.media && S.data.media[id]) || {}; }

  function takeBlock() {
    const { next, hold } = showInfo();
    if (!S.data || !S.data.show) return 'No show is open.';
    if (S.conn !== 'ok') return 'No live connection to the server.';
    if (hold) return 'Rundown is held. Release it to take.';
    if (!next) return 'End of show. Nothing left to take.';
    return null;
  }

  function takeNow(viaKey) {
    if (S.seq || takeBlock()) return;
    if (viaKey && Date.now() - S.lastTake < TAKE_COOLDOWN_MS) return;
    S.lastTake = Date.now();
    const { next } = showInfo();
    act('take', S.show, {}, {
      key: 'take', okTitle: 'Taken.',
      ok: (d) => (next ? next.slug : 'Item') + ' is on air. ' + d.results.filter((r) => r.status === 'fired').length +
        ' of ' + d.results.length + ' cues fired.',
    });
  }

  function toggleHold() {
    if (!S.data || !S.data.show) return;
    const { hold } = showInfo();
    if (hold) act('release', S.show, {}, { key: 'hold', okTitle: 'Released.', ok: () => 'The rundown can move again.' });
    else act('hold', S.show, { reason: 'Held from the operator screen' }, { key: 'hold', okTitle: 'Held.', ok: () => 'Take and go are refused until you release.' });
  }

  function armedAction(key, fn) {
    if (S.armed === key) { S.armed = null; fn(); render(); return; }
    S.armed = key;
    render();
    setTimeout(() => { if (S.armed === key) { S.armed = null; render(); } }, ARM_MS);
  }

  function skipItem(id) { act('skip', id, {}, { okTitle: 'Skipped.', ok: () => 'The item will be passed over.' }); }
  function killItem(id) { armedAction('kill:' + id, () => act('kill', id, {}, { okTitle: 'Killed.', ok: () => 'The asset stays in the library.' })); }
  function move(id, dir) {
    const items = showInfo().items;
    const i = items.findIndex((x) => x.id === id), j = i + dir;
    if (i < 0 || j < 0 || j >= items.length) return;
    const ids = items.map((x) => x.id);
    [ids[i], ids[j]] = [ids[j], ids[i]];
    act('reorder', S.show, { items: ids }, { key: 'move:' + id, okTitle: 'Reordered.', ok: () => 'New order saved.' });
  }

  // ---- rendering -----------------------------------------------------------
  function paint(id, sig, build) {
    const host = $(id);
    const s = JSON.stringify(sig);
    if (S.sigs[id] === s) return;
    S.sigs[id] = s;
    const active = document.activeElement;
    const fk = active && host.contains(active) ? active.getAttribute('data-fk') : null;
    const caret = fk && active.selectionStart != null ? [active.selectionStart, active.selectionEnd] : null;
    host.replaceChildren(...[].concat(build()).flat(Infinity).filter((x) => x != null && x !== false));
    if (fk) {
      const n = host.querySelector('[data-fk="' + CSS.escape(fk) + '"]');
      if (n) {
        n.focus({ preventScroll: true });
        if (caret && n.setSelectionRange) n.setSelectionRange(caret[0], caret[1]);
      }
    }
  }

  function chip(map, key, extra) {
    const m = map[key] || ['ready', '?', key];
    return el('span', { class: 'chip ' + m[0] + (extra ? ' ' + extra : '') }, el('span', { 'aria-hidden': 'true', text: m[1] }), m[2]);
  }

  function btn(label, o) {
    o = o || {};
    const dis = o.disabled || S.pending.has(o.key || '\0');
    return el('button', {
      type: 'button', class: 'btn' + (o.cls ? ' ' + o.cls : ''), 'data-fk': o.key, disabled: dis,
      title: o.title, 'aria-label': o.aria, onclick: o.onclick,
    }, label);
  }

  function render() {
    renderChrome();
    if (!S.data) {
      paint('banners', [S.conn], () => (S.conn === 'lost'
        ? el('div', { class: 'banner bad', role: 'alert' }, el('div', { class: 'msg' }, el('strong', { text: 'Cannot reach the server.' }),
          el('span', { class: 'why', text: 'Nothing has loaded yet. Retrying.' })))
        : []));
      return;
    }
    renderBanners();
    if (S.screen === 'show') renderShow();
    if (S.screen === 'record') renderRecord();
    if (S.screen === 'rights') renderRights();
    if (S.screen === 'publish') renderPublish();
  }

  function renderChrome() {
    const sel = $('show-select');
    const shows = (S.data && S.data.shows) || [];
    const sig = JSON.stringify([shows, S.show]);
    if (sel.dataset.sig !== sig) {
      sel.dataset.sig = sig;
      sel.replaceChildren(...(shows.length ? shows.map((s) => el('option', { value: s.id, selected: s.id === S.show }, s.title + (s.held ? ' (held)' : ''))) : [el('option', { value: '' }, 'No shows')]));
      sel.value = S.show || '';
    }
    for (const a of document.querySelectorAll('#screens a')) {
      if (a.dataset.screen === S.screen) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    }
  }

  function renderBanners() {
    const { hold } = showInfo();
    const f = S.flash;
    const room = S.data.room || {};
    paint('banners', [hold, f, S.conn, room.stale], () => {
      const out = [];
      if (S.conn === 'lost') {
        out.push(el('div', { class: 'banner bad', role: 'alert' }, el('div', { class: 'msg' },
          el('strong', { text: 'Connection lost.' }),
          el('span', { class: 'why', text: 'The server did not answer. What you see is the last state it gave. Take is disabled until it answers.' }))));
      }
      if (hold) {
        out.push(el('div', { class: 'banner warn', role: 'status' }, el('div', { class: 'msg' },
          el('strong', { text: '⏸ Rundown held.' }),
          el('span', { class: 'why', text: hold.reason || 'No reason given.' }),
          el('span', { class: 'sub', text: 'Since ' + fmtTime(hold.heldAt) + '. Take and advance are refused until released.' })),
          btn('Release hold', { key: 'hold-banner', cls: 'signal', onclick: toggleHold })));
      }
      if (f) {
        out.push(el('div', { class: 'banner ' + (f.kind === 'good' ? 'good' : f.kind === 'warn' ? 'warn' : 'bad'), role: f.kind === 'good' ? 'status' : 'alert' },
          el('div', { class: 'msg' },
            el('strong', { text: (f.kind === 'good' ? '✓ ' : '✕ ') + f.title }),
            f.reason ? el('span', { class: 'why', text: f.reason }) : null,
            f.sub ? el('span', { class: 'sub', text: f.sub }) : null,
            f.code ? el('span', { class: 'sub', text: 'code: ' + f.code }) : null),
          el('button', { type: 'button', class: 'btn ghost small', 'data-fk': 'dismiss', onclick: () => { S.flash = null; render(); } }, 'Dismiss')));
      }
      return out;
    });
  }

  // ---- Show screen ---------------------------------------------------------
  function renderShow() {
    const { items, air, next, hold } = showInfo();
    const block = takeBlock();
    const takeCues = next ? cuesOf(next.id).length : 0;
    const takeBusy = S.pending.has('take');
    paint('transport', [air, next, hold, block, S.seq, [...S.pending], takeCues, S.data.show], () => [
      el('div', { class: 'nowline air' }, el('span', { class: 'lbl', text: 'On air' }),
        el('span', { class: 'val', text: air ? '● ' + air.slug : 'Nothing on air' })),
      el('div', { class: 'nowline next' }, el('span', { class: 'lbl', text: 'Next' }),
        el('span', { class: 'val', text: next ? '▶ ' + next.slug : 'End of show' })),
      el('span', { class: 'spacer' }),
      el('div', {}, btn(
        [el('span', {}, takeBusy ? 'TAKING…' : 'TAKE'),
         el('small', {}, next ? next.slug + ' · ' + takeCues + (takeCues === 1 ? ' cue' : ' cues') : 'nothing to take'),
         el('small', { class: 'k' }, 'Key ', el('kbd', { text: 'T' }))],
        { key: 'take', cls: 'take', disabled: !!block || S.seq, onclick: () => takeNow(false) })),
      el('div', { class: 'acts' },
        btn(hold ? 'RELEASE HOLD' : 'HOLD', { key: 'hold', cls: hold ? 'signal' : 'ghost', onclick: toggleHold, disabled: !S.data.show, title: 'Key H' }),
        btn('Advance, no cues', { key: 'go', cls: 'ghost', disabled: !!block || S.seq,
          title: 'Moves the rundown on without sending any cues to the room',
          onclick: () => act('go', S.show, {}, { okTitle: 'Advanced.', ok: (d) => (d.over ? 'End of show.' : 'Next item is on air. No cues were sent.') }) })),
      block ? el('span', { class: 'hint', text: block }) : el('span', { class: 'hint' }, 'Hold: ', el('kbd', { text: 'H' }), ' · all keys: ', el('kbd', { text: '?' })),
    ]);
    $('transport').classList.toggle('held', !!hold);

    const sel = S.selected && items.find((i) => i.id === S.selected) ? S.selected : (next || air || items[0] || {}).id;
    S.selected = sel || null;
    paint('rundown', [items, S.data.media, S.selected, S.armed, [...S.pending], S.seq], () => {
      if (!S.data.show) return el('p', { class: 'empty', text: 'No show yet. Create one, then reload.' });
      if (!items.length) return el('p', { class: 'empty', text: 'This show has no items.' });
      return el('ol', { class: 'list' }, items.map((it, idx) => rundownRow(it, idx, items, air, next)));
    });

    const sItem = items.find((i) => i.id === S.selected);
    $('h-cues').textContent = sItem ? 'Cues on ' + sItem.slug : 'Cues';
    paint('cues', [S.selected, S.data.cues, S.cueDetail, S.cueLast], () => {
      if (!sItem) return el('p', { class: 'empty', text: 'Select a rundown row.' });
      const cs = cuesOf(sItem.id);
      if (!cs.length) return el('p', { class: 'empty', text: 'No cues on this item. Taking it only changes the rundown.' });
      return [cs.map((c) => el('div', { class: 'cue' },
        el('div', {}, el('span', { class: 'cmd', text: CUE_LABEL[c.command] || c.command }),
          el('small', { text: c.command + (c.payload ? ' · ' + c.payload : '') + (S.cueDetail[c.id] && c.status !== 'fired' ? ' · last take: ' + S.cueDetail[c.id] : '') })),
        chip(CUE_CHIP, c.status !== 'fired' && S.cueLast[c.id] && S.cueLast[c.id] !== 'fired' ? S.cueLast[c.id] : c.status))),
        el('p', { class: 'note', text: 'Take resumes at the first cue that did not fire. Fired cues are not sent twice.' })];
    });

    paint('room', S.data.room, () => roomPanel(S.data.room));
  }

  function rundownRow(it, idx, items, air, next) {
    const m = mediaOf(it.id);
    const isNext = next && next.id === it.id;
    const meta = [];
    if (m.tcIn) meta.push('clip ' + m.tcIn + ' to ' + m.tcOut);
    if (it.asRunIn) meta.push('as-run ' + fmtTime(it.asRunIn) + ' to ' + (it.asRunOut ? fmtTime(it.asRunOut) : 'now'));
    const blocked = (it.status === 'ready' || it.status === 'on air') && m.blockedBy && m.blockedBy !== 'ok' ? m.blockedBy : null;
    const ready = it.status === 'ready';
    const cls = 'rrow' + (it.status === 'on air' ? ' is-air' : isNext ? ' is-next' : '') + (S.selected === it.id ? ' sel' : '');
    const prev = items[idx - 1];
    const canUp = ready && prev && prev.status !== 'on air' && prev.status !== 'aired';
    const canDown = ready && idx < items.length - 1;
    const killKey = 'kill:' + it.id;
    return el('li', { class: cls },
      el('span', { class: 'n', 'aria-hidden': 'true', text: String(idx + 1) }),
      el('button', { type: 'button', class: 'pick', 'data-fk': 'pick:' + it.id, 'aria-current': S.selected === it.id ? 'true' : null,
          onclick: () => { S.selected = it.id; render(); } },
        el('span', { class: 'title' + (it.status === 'skipped' || it.status === 'killed' ? ' struck' : ''), text: it.slug }),
        el('span', { class: 'meta' }, meta.join(' · ') || (it.status === 'ready' ? 'not yet aired' : ''),
          blocked ? el('span', { class: 'bad' }, (meta.length ? ' · ' : '') + '⚠ media ' + blocked) : null)),
      isNext ? chip({ next: ['next', '▶', 'Next'] }, 'next') : chip(ITEM_CHIP, it.status),
      el('span', { class: 'tools' },
        ready ? [
          btn('↑', { key: 'up:' + it.id, cls: 'ghost small', aria: 'Move ' + it.slug + ' up', title: 'Move up', disabled: !canUp || S.seq, onclick: () => move(it.id, -1) }),
          btn('↓', { key: 'down:' + it.id, cls: 'ghost small', aria: 'Move ' + it.slug + ' down', title: 'Move down', disabled: !canDown || S.seq, onclick: () => move(it.id, 1) }),
          btn('Skip', { key: 'skip-b:' + it.id, cls: 'ghost small', aria: 'Skip ' + it.slug, disabled: S.seq, onclick: () => skipItem(it.id) }),
          btn(S.armed === killKey ? 'Confirm kill' : 'Kill', { key: 'killb:' + it.id, cls: 'danger small' + (S.armed === killKey ? ' armed' : ''),
            aria: (S.armed === killKey ? 'Confirm kill ' : 'Kill ') + it.slug, disabled: S.seq, onclick: () => killItem(it.id) }),
        ] : null));
  }

  function roomPanel(room) {
    room = room || {};
    if (!room.configured) {
      return [el('dl', { class: 'kv' }, el('dt', { text: 'Control' }), el('dd', { text: '✕ no room configured' })),
        el('p', { class: 'note', text: 'Take and record need a room. Set VOLT_ROOM_URL on the server. Advance, no cues still works.' })];
    }
    if (!room.reachable) {
      return [el('dl', { class: 'kv' }, el('dt', { text: 'Control' }), el('dd', { text: '✕ unreachable' }),
        el('dt', { text: 'Reason' }), el('dd', { text: room.error || 'no answer' }))];
    }
    return el('dl', { class: 'kv' },
      el('dt', { text: 'Control' }), el('dd', { text: '✓ reachable' }),
      el('dt', { text: 'Snapshot' }), el('dd', { text: room.stale ? '⚠ stale' : '✓ fresh' }),
      el('dt', { text: 'Recording' }), el('dd', { text: (room.state || 'unknown') + ' · ' + (room.health || 'unknown') }),
      el('dt', { text: 'Session' }), el('dd', { text: room.sessionId || 'none' }),
      room.error ? [el('dt', { text: 'Room error' }), el('dd', { text: room.error })] : null);
  }

  // ---- Record screen -------------------------------------------------------
  function currentRecording() {
    const room = S.data.room || {};
    if (!room.sessionId) return null;
    return S.data.assets.find((a) => a.sourceKey === 'cv:' + room.sessionId) || null;
  }

  function renderRecord() {
    const room = S.data.room || {};
    const cur = currentRecording();
    const sig = [room, cur, S.data.assets, [...S.pending], S.seq];
    paint('record', sig, () => {
      const st = REC_ALIAS[room.state] || room.state;
      const idx = room.reachable ? REC_STEPS.indexOf(st) : -1;
      const dead = room.reachable && (room.state === 'failed' || room.state === 'interrupted');
      const steps = REC_STEPS.map((name, j) => {
        let cls = 'step', glyph = '○';
        if (idx >= 0 && (j < idx || (j === idx && name === 'completed' && room.finalized))) { cls += ' done'; glyph = '✓'; }
        else if (idx >= 0 && j === idx) { cls += ' now'; glyph = '▶'; }
        return el('li', { class: cls, 'aria-current': cls.includes('now') ? 'step' : null },
          el('span', { class: 'g', 'aria-hidden': 'true', text: glyph }), name);
      });
      if (dead) steps.push(el('li', { class: 'step fail', 'aria-current': 'step' }, el('span', { class: 'g', 'aria-hidden': 'true', text: '✕' }), room.state));
      const starting = S.pending.has('recordStart');
      const stopping = cur && S.pending.has('recordStop:' + cur.id);
      return [
        el('section', { class: 'panel', 'aria-labelledby': 'h-recstart' }, el('h2', { id: 'h-recstart', text: 'Start a recording' }),
          el('div', { class: 'form-row' },
            el('div', {}, el('label', { for: 'rec-camera', text: 'Camera or label (optional)' }),
              el('input', { id: 'rec-camera', type: 'text', maxlength: '100', autocomplete: 'off', value: S.recCamera || '', 'data-fk': 'rec-camera',
                oninput: (e) => { S.recCamera = e.target.value; } })),
            btn(starting ? 'STARTING…' : 'START RECORDING', { key: 'recordStart', disabled: !room.configured || S.seq,
              onclick: () => act('recordStart', null, S.recCamera ? { camera: S.recCamera } : {}, { key: 'recordStart', okTitle: 'Recording.', ok: (d) => 'Asset ' + short(d.asset.id) + ' minted once the room showed output.' })}),
            el('span', { class: 'hint', text: room.configured ? 'Mints the asset only after the room shows a file being written.' : 'No room configured. Set VOLT_ROOM_URL on the server.' }))),
        el('section', { class: 'panel', 'aria-labelledby': 'h-lifecycle' },
          el('h2', { id: 'h-lifecycle', text: 'Recording lifecycle' + (cur ? ' · ' + (cur.camera || short(cur.id)) : '') }),
          el('ol', { class: 'steps', 'aria-label': 'Lifecycle steps' }, steps),
          el('dl', { class: 'kv' },
            el('dt', { text: 'State' }), el('dd', { text: room.reachable ? room.state + ' · ' + room.health : room.configured ? 'room unreachable' : 'no room' }),
            el('dt', { text: 'Asset' }), el('dd', { text: cur ? cur.id : 'none yet. It is minted once output is observed.' }),
            el('dt', { text: 'Session' }), el('dd', { text: room.sessionId || 'none' }),
            el('dt', { text: 'File' }), el('dd', { text: cur && cur.file ? cur.file + (cur.open ? ' · open' : ' · closed') : 'none' }),
            el('dt', { text: 'Snapshot' }), el('dd', { text: room.reachable ? (room.stale ? '⚠ stale' : '✓ fresh') : 'none' }),
            el('dt', { text: 'Finalized' }), el('dd', { text: room.reachable ? (room.finalized ? 'yes' : 'no') : 'unknown' }),
            el('dt', { text: 'Checksum' }), el('dd', { text: cur && cur.checksum ? cur.checksum : 'not wrapped' }),
            room.error ? [el('dt', { text: 'Room error' }), el('dd', { text: room.error })] : null),
          el('div', { class: 'bar top-rule' },
            btn(stopping ? [el('span', { class: 'spin', 'aria-hidden': 'true' }), ' WAITING FOR THE ROOM…'] : 'STOP AND WRAP',
              { key: 'recordStop:' + (cur ? cur.id : ''), disabled: !cur || !cur.open || S.seq,
                onclick: () => stopRecording(cur.id) }),
            el('span', { class: 'hint', text: cur && cur.open ? 'Stops, then waits for completed and finalized from the room. Never wraps on a stop ack.' : 'Nothing open to stop.' }))),
        el('section', { class: 'panel', 'aria-labelledby': 'h-assets' }, el('h2', { id: 'h-assets', text: 'Recordings' }),
          assetsTable(S.data.assets.filter((a) => a.sourceKey || a.open), true)),
      ];
    });
  }

  function stopRecording(id) {
    act('recordStop', null, { asset: id }, { key: 'recordStop:' + id, okTitle: 'Wrapped.', ok: (d) => 'Checksum ' + short(d.checksum) + '… Asset ' + short(id) + ' is closed.',
      hint: 'Nothing was wrapped. Stop again to retry.' });
  }

  function assetsTable(list, withStop) {
    if (!list.length) return el('p', { class: 'empty', text: 'No recordings yet.' });
    return el('div', { class: 'tblwrap' }, el('table', {},
      el('caption', { class: 'vh', text: 'Recordings' }),
      el('thead', {}, el('tr', {}, ['Asset', 'Camera', 'File', 'State', 'Checksum', withStop ? 'Action' : null].filter(Boolean).map((h) => el('th', { scope: 'col', text: h })))),
      el('tbody', {}, list.map((a) => el('tr', {},
        el('td', { class: 'mono', text: short(a.id) }), el('td', { text: a.camera || '—' }),
        el('td', { class: 'mono', text: a.file || '—' }),
        el('td', {}, a.open ? chip({ rec: ['warn', '●', 'Recording'] }, 'rec') : chip({ wrapped: ['ok', '✓', 'Wrapped'] }, 'wrapped')),
        el('td', { class: 'mono', text: a.checksum ? short(a.checksum) + '…' : '—' }),
        withStop ? el('td', {}, a.open ? btn('Stop and wrap', { key: 'recordStop:' + a.id, cls: 'ghost small', aria: 'Stop and wrap ' + (a.camera || short(a.id)), disabled: S.seq, onclick: () => stopRecording(a.id) }) : null) : null)))));
  }

  // ---- Rights screen -------------------------------------------------------
  function renderRights() {
    paint('rights', [S.data.assets, S.armed, [...S.pending]], () => {
      const list = S.data.assets;
      return el('section', { class: 'panel', 'aria-labelledby': 'h-assets2' }, el('h2', { id: 'h-assets2', text: 'Assets' }),
        el('p', { class: 'note', text: 'Take, go and publish refuse anything not cleared or on legal hold. The reason shows where the operator is blocked.' }),
        !list.length ? el('p', { class: 'empty', text: 'No assets yet.' }) :
        el('div', { class: 'tblwrap' }, el('table', {},
          el('caption', { class: 'vh', text: 'Assets and their rights' }),
          el('thead', {}, el('tr', {}, ['Asset', 'Camera', 'Cleared', 'Legal hold', 'Can air or publish', 'Actions'].map((h) => el('th', { scope: 'col', text: h })))),
          el('tbody', {}, list.map((a) => {
            const lk = 'lift:' + a.id;
            return el('tr', {},
              el('td', { class: 'mono', text: short(a.id) }), el('td', { text: a.camera || a.show || '—' }),
              el('td', { class: a.cleared ? 'ok' : 'no', text: a.cleared ? '✓ Cleared' : '✕ Not cleared' }),
              el('td', { class: a.legalHold ? 'no' : '', text: a.legalHold ? '⚑ On hold' : 'No' }),
              el('td', { class: a.blockedBy ? 'no' : 'ok', text: a.blockedBy ? '✕ No · ' + a.blockedBy : '✓ Yes' }),
              el('td', {}, el('div', { class: 'acts' },
                btn(a.cleared ? 'Un-clear' : 'Clear', { key: 'cleared:' + a.id, cls: 'ghost small', aria: (a.cleared ? 'Un-clear ' : 'Clear ') + short(a.id),
                  onclick: () => act('cleared', a.id, { cleared: !a.cleared }, { okTitle: a.cleared ? 'Un-cleared.' : 'Cleared.', ok: () => 'Asset ' + short(a.id) }) }),
                a.legalHold
                  ? btn(S.armed === lk ? 'Confirm lift hold' : 'Lift hold', { key: 'lift-b:' + a.id, cls: 'danger small' + (S.armed === lk ? ' armed' : ''), aria: (S.armed === lk ? 'Confirm lift hold on ' : 'Lift hold on ') + short(a.id),
                      onclick: () => armedAction(lk, () => act('legalHold', a.id, { held: false }, { key: 'lift:' + a.id, okTitle: 'Hold lifted.', ok: () => 'Asset ' + short(a.id) })) })
                  : btn('Place hold', { key: 'hold-a:' + a.id, cls: 'ghost small', aria: 'Place hold on ' + short(a.id) + ' (legal hold)',
                      onclick: () => act('legalHold', a.id, { held: true }, { key: 'holda:' + a.id, okTitle: 'Hold placed.', ok: () => 'Asset ' + short(a.id) + ' cannot air or publish.' }) }))));
          })))));
    });
  }

  // ---- Publish screen ------------------------------------------------------
  const KINDS = ['house', 'ott', 'social'];

  function publishList() {
    const used = new Set();
    if (S.data.rundown) {
      for (const i of S.data.rundown.items) { const m = mediaOf(i.id); if (m.span) used.add(m.span); }
    }
    const q = S.pubFilter.trim().toLowerCase();
    const spans = S.data.spans.filter((s) => {
      if (q) return ((s.text || '') + ' ' + s.id + ' ' + (s.camera || '')).toLowerCase().includes(q);
      return used.has(s.id) || s.versions.length > 0;
    });
    return { spans: spans.slice(0, 60), used, more: Math.max(0, spans.length - 60) };
  }

  function renderPublish() {
    const info = publishList();
    paint('publish', [info.spans, info.more, [...S.pending], S.pubFilter, [...info.used]], () => {
      const head = el('div', { class: 'panel' }, el('div', { class: 'form-row' },
        el('div', {}, el('label', { for: 'pub-filter', text: 'Find a clip by word' }),
          el('input', { id: 'pub-filter', type: 'text', value: S.pubFilter, 'data-fk': 'pub-filter', autocomplete: 'off',
            oninput: (e) => { S.pubFilter = e.target.value; renderPublish(); } })),
        el('span', { class: 'hint', text: S.pubFilter ? 'Showing matches from every clip.' : 'Showing clips in this rundown and clips already published.' })));
      const rows = info.spans.map((s) => {
        const key = 'publish:' + s.id;
        const running = S.pending.has(key);
        const done = s.versions.filter((v) => v.status === 'done').length;
        const anyFail = s.versions.some((v) => v.status === 'failed');
        const label = anyFail ? 'PUBLISH AGAIN' : done === 3 ? 'Publish again' : 'PUBLISH';
        return el('tr', {},
          el('td', {}, el('strong', { text: s.text || short(s.id) }), el('div', { class: 'muted mono', text: s.tcIn + ' to ' + s.tcOut + (s.camera ? ' · ' + s.camera : '') })),
          el('td', {}, el('div', { class: 'vers' }, KINDS.map((k) => {
            const v = s.versions.find((x) => x.kind === k);
            const url = v && v.url;
            return el('div', { class: 'ver' }, el('span', { text: k }),
              v ? chip(VER_CHIP, v.status) : el('span', { class: 'chip unsupported' }, el('span', { 'aria-hidden': 'true', text: '–' }), 'Not made'),
              url ? (/^https?:\/\//i.test(url) ? el('a', { class: 'url', href: url, rel: 'noopener', text: url }) : el('span', { class: 'url', text: url })) : el('span', { class: 'url muted', text: '' }));
          })), el('div', { class: 'muted', text: running ? 'Rendering…' : s.versions.length ? done + ' of 3 done' : '' })),
          el('td', {},
            s.blockedBy ? el('div', { class: 'no' }, '✕ Blocked: ' + s.blockedBy, ' ', el('a', { href: '#rights', text: 'Fix on Rights' })) : null,
            btn(running ? [el('span', { class: 'spin', 'aria-hidden': 'true' }), ' RENDERING…'] : label,
              { key, cls: s.blockedBy ? 'ghost' : '', disabled: !!s.blockedBy,
                aria: (running ? 'Rendering' : label) + ' ' + (s.text || short(s.id)),
                onclick: () => act('publish', s.id, {}, { key, span: s.id, okTitle: 'Published.', ok: (d) => d.versions.length + ' versions done.' }) })));
      });
      return [head, el('section', { class: 'panel', 'aria-labelledby': 'h-clips' }, el('h2', { id: 'h-clips', text: 'Clips' }),
        rows.length ? el('div', { class: 'tblwrap' }, el('table', {},
          el('caption', { class: 'vh', text: 'Clips and their published versions' }),
          el('thead', {}, el('tr', {}, ['Clip', 'Versions', 'Action'].map((h) => el('th', { scope: 'col', text: h })))),
          el('tbody', {}, rows))) : el('p', { class: 'empty', text: S.pubFilter ? 'No clip matches.' : 'No clips in this rundown yet.' }),
        info.more ? el('p', { class: 'note', text: info.more + ' more. Type to narrow the list.' }) : null)];
    });
  }

  // ---- dialogs, keys, screens ----------------------------------------------
  function openToken() { const d = $('token-dlg'); if (!d.open) { d.showModal(); $('token-in').focus(); } }
  function anyDialog() { return $('token-dlg').open || $('keys-dlg').open; }

  function setScreen(name) {
    S.screen = SCREENS.includes(name) ? name : 'show';
    for (const s of SCREENS) $('screen-' + s).hidden = s !== S.screen;
    S.sigs = {};
    render();
  }

  function onKey(e) {
    if (e.ctrlKey || e.metaKey || e.altKey || anyDialog()) return;
    const t = e.target;
    if (t && t.matches && t.matches('input,select,textarea,[contenteditable]')) return;
    if (e.key === '?') { $('keys-dlg').showModal(); return; }
    if (e.key >= '1' && e.key <= '4') { location.hash = '#' + SCREENS[+e.key - 1]; return; }
    if (S.screen !== 'show' || !S.data) return;
    const k = e.key.toLowerCase();
    // Space takes only when nothing interactive has focus, so a focused button keeps its own Space.
    // Pointer clicks drop focus (see init), so Space after clicking Hold does not press Hold again.
    if (e.key === ' ' || e.code === 'Space') {
      if (t && t !== document.body && t.closest && t.closest('button,a,summary,select,[role="button"],[tabindex]')) return;
      e.preventDefault();
      if (!e.repeat) takeNow(true);
      return;
    }
    if (k === 't') { if (!e.repeat) takeNow(true); return; }
    if (k === 'h') { if (!e.repeat) toggleHold(); return; }
    if (k === 's' && S.selected && !e.repeat) { const it = showInfo().items.find((i) => i.id === S.selected); if (it && it.status === 'ready') skipItem(it.id); return; }
    if (k === 'x' && S.selected && !e.repeat) { const it = showInfo().items.find((i) => i.id === S.selected); if (it && it.status === 'ready') killItem(it.id); return; }
    if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && t && t.closest && t.closest('#rundown')) {
      const picks = [...document.querySelectorAll('#rundown .pick')];
      const i = picks.indexOf(t.closest('.pick') || t);
      const j = Math.max(0, Math.min(picks.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)));
      if (picks[j]) { e.preventDefault(); picks[j].focus(); S.selected = picks[j].getAttribute('data-fk').slice(5); render(); }
    }
  }

  function init() {
    try { S.show = sessionStorage.getItem('volt.show') || new URLSearchParams(location.search).get('show'); } catch (e) { S.show = null; }
    S.show = S.show || new URLSearchParams(location.search).get('show');
    $('show-select').addEventListener('change', (e) => {
      S.show = e.target.value; S.selected = null; S.cueDetail = {}; S.cueLast = {}; S.sigs = {};
      try { sessionStorage.setItem('volt.show', S.show); } catch (x) { /* ignore */ }
      refresh();
    });
    $('token-btn').addEventListener('click', openToken);
    $('token-close').addEventListener('click', () => $('token-dlg').close());
    $('token-clear').addEventListener('click', () => { setToken(''); $('token-in').value = ''; $('token-dlg').close(); refresh(); });
    $('token-form').addEventListener('submit', () => { setToken($('token-in').value.trim()); $('token-in').value = ''; setTimeout(refresh, 0); });
    $('keys-btn').addEventListener('click', () => $('keys-dlg').showModal());
    $('keys-close').addEventListener('click', () => $('keys-dlg').close());
    document.addEventListener('keydown', onKey);
    // A mouse click leaves focus on the button it pressed. Drop it so Space is TAKE, not that button again.
    document.addEventListener('click', (e) => {
      const b = e.target && e.target.closest && e.target.closest('button');
      if (b && e.detail > 0) setTimeout(() => { if (document.activeElement === b) b.blur(); }, 0);
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { if (S.armed) { S.armed = null; render(); } else if (document.activeElement && document.activeElement !== document.body && !anyDialog()) document.activeElement.blur(); } });
    window.addEventListener('hashchange', () => setScreen(location.hash.slice(1)));
    document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); schedule(); } });
    const tick = () => { $('clock').textContent = new Date().toLocaleTimeString([], { hour12: false }); };
    tick(); setInterval(tick, 1000);
    setConn('connecting');
    setScreen(location.hash.slice(1));
    refresh().then(schedule);
  }

  init();
})();
