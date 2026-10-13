/* Volt operator UI. No framework, no build, same-origin only.
 *
 * The screen shows what the server last returned, never a guess. Every action waits
 * for the server and then shows its returned state or its refusal (code + reason).
 * See docs/specs/operator-ui.md.
 */
'use strict';
(function () {
  // ---- timecode: the page's twin of volt/timecode.py (a test compares them on real rates) ------------------
  // A rate is {n, d}, an exact ratio. Labels are counted in whole frames, so nothing drifts.
  const NAMED_RATES = [[24000, 1001], [30000, 1001], [60000, 1001], [120000, 1001]];
  function parseRate(r) {
    if (r == null || r === '') return null;
    const s = String(r).trim();
    let n, d;
    if (s.includes('/')) { const p = s.split('/'); n = Number(p[0]); d = Number(p[1]); }
    else {
      const v = Number(s);
      for (const [nn, dd] of NAMED_RATES) if (Math.abs(v - nn / dd) < 0.0015) return { n: nn, d: dd };
      if (!Number.isInteger(v)) return null;
      n = v; d = 1;
    }
    return n > 0 && d > 0 && isFinite(n) && isFinite(d) ? { n, d } : null;
  }
  const nominalFps = (r) => Math.round(r.n / r.d);
  function dropPerMinute(r) {
    if (r.n === 30000 && r.d === 1001) return 2;
    if (r.n === 60000 && r.d === 1001) return 4;
    return 0;
  }
  const isDropLabel = (tc) => /[;.]\d+$/.test(String(tc || '').trim());
  const pad2 = (n) => String(n).padStart(2, '0');
  function framesToTc(frames, r, drop) {
    const nom = nominalFps(r);
    let n = Math.max(0, Math.floor(frames));
    let sep = ':';
    const d = drop ? dropPerMinute(r) : 0;
    if (d) {
      const perMin = nom * 60 - d, per10 = nom * 600 - 9 * d;
      const tens = Math.floor(n / per10), rem = n % per10;
      n += 9 * d * tens + (rem >= d ? d * Math.floor((rem - d) / perMin) : 0);
      sep = ';';
    }
    const f = n % nom, secs = Math.floor(n / nom);
    return pad2(Math.floor(secs / 3600)) + ':' + pad2(Math.floor(secs / 60) % 60) + ':' + pad2(secs % 60) + sep + pad2(f);
  }
  // Frames since 00:00:00:00, or null for a label that does not parse or does not exist on this timebase.
  function tcToFrames(tc, r, drop) {
    const m = /^(\d{2,}):(\d{2}):(\d{2})([:;.])(\d{2,3})$/.exec(String(tc).trim());
    if (!m || !r) return null;
    const h = +m[1], mi = +m[2], sec = +m[3], f = +m[5], nom = nominalFps(r);
    if (mi > 59 || sec > 59 || f >= nom) return null;
    const total = ((h * 60 + mi) * 60 + sec) * nom + f;
    if (drop == null) drop = m[4] === ';' || m[4] === '.';
    const d = drop ? dropPerMinute(r) : 0;
    if (!d) return total;
    if (sec === 0 && f < d && mi % 10 !== 0) return null;
    const minutes = h * 60 + mi;
    return total - d * (minutes - Math.floor(minutes / 10));
  }
  // A length is a count of frames, not a place on the clock: nominal timebase, no drop, whatever the clip is.
  const lengthTc = (frames, r) => framesToTc(frames, r, false);
  const fmtRate = (r) => (r.d === 1 ? String(r.n) : (r.n / r.d).toFixed(3).replace(/0+$/, '').replace(/\.$/, '')) + ' fps';

  if (typeof document === 'undefined') {   // loaded by a test under node: export the pure functions and stop
    if (typeof module !== 'undefined') module.exports = { parseRate, framesToTc, tcToFrames, isDropLabel, lengthTc, fmtRate };
    return;
  }
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
    attach:      ['POST', '/api/items/{id}/attach'],
    spans:       ['GET',  '/api/assets/{id}/spans'],
    transcript:  ['GET',  '/api/assets/{id}/transcript'],
    proxy:       ['GET',  '/api/assets/{id}/proxy'],
    thumb:       ['GET',  '/api/spans/{id}/thumb'],
    spanCreate:  ['POST', '/api/assets/{id}/spans'],
    spanTrim:    ['POST', '/api/spans/{id}/trim'],
    spanLabel:   ['POST', '/api/spans/{id}/label'],
    spanDelete:  ['POST', '/api/spans/{id}/delete'],
    spanSplit:   ['POST', '/api/spans/{id}/split'],
    spanMerge:   ['POST', '/api/spans/{id}/merge'],
    detect:      ['POST', '/api/assets/{id}/shots/detect'],
    spanFrames:  ['POST', '/api/assets/{id}/span-frames'],
  };
  // Actions that run one at a time on the server (the sequencing lock). Hold, release, rights
  // and publish do not wait on it, so HOLD stays pressable while a take waits on the room.
  const SEQUENCED = new Set(['go', 'take', 'skip', 'kill', 'reorder', 'recordStart', 'recordStop', 'attach']);

  const POLL_MS = 2000, POLL_HIDDEN_MS = 6000, TAKE_COOLDOWN_MS = 1500, ARM_MS = 4000;
  const SCREENS = ['show', 'record', 'rights', 'publish', 'library'];

  const ERR_LABEL = {
    held: 'Held', not_cleared: 'Not cleared', no_media: 'No media', cue_failed: 'Cue failed',
    cue_unsupported: 'Cue unsupported', no_room: 'No room', room_refused: 'Room refused',
    not_wrapped: 'Not wrapped', publish_failed: 'Publish failed', not_found: 'Not found',
    not_skippable: 'Cannot skip', not_killable: 'Cannot kill', not_attachable: 'Cannot attach',
    bad_input: 'Bad input', unauthorized: 'Token needed', bad_host: 'Wrong host',
    too_large: 'Too large', internal: 'Server error',
    overlaps_shot: 'Shots overlap', span_in_use: 'Span in use', bad_timecode: 'Bad timecode',
    outside_source: 'Outside the source', no_proxy: 'No proxy', not_deletable: 'Cannot delete',
    not_editable: 'Cannot edit', conflict: 'Try again', thumb_failed: 'Thumbnail failed',
    thumb_timeout: 'Thumbnail timed out', bad_range: 'Bad range',
    detect_busy: 'Detection already running', essence_open: 'Still recording', not_adjacent: 'Not neighbours',
    indexing: 'Still indexing', engine_missing: 'ffmpeg missing',
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

  // `query` is an object of GET parameters (library reads); `body` is the JSON of a POST.
  async function call(kind, id, body, query) {
    const [method, tpl] = EP[kind];
    let path = tpl.replace('{id}', encodeURIComponent(id || ''));
    if (kind === 'state' && S.show) path += '?show=' + encodeURIComponent(S.show);
    if (query) path += '?' + new URLSearchParams(query).toString();
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

  // Bytes (a thumbnail, the proxy) with the bearer header, for <img> and <video> which cannot send one.
  // `onProgress(received, total)` is called while a body streams; `maxBytes` refuses a body before reading it.
  async function fetchBlob(path, o) {
    o = o || {};
    const headers = {};
    const token = getToken();
    if (token) headers.Authorization = 'Bearer ' + token;
    const res = await fetch(path, { headers, cache: o.cache || 'no-store', signal: o.signal });
    if (!res.ok) {
      let data = null;
      try { data = await res.json(); } catch (e) { /* not JSON */ }
      const err = new Error((data && data.error && data.error.reason) || 'The server answered ' + res.status + '.');
      err.status = res.status; err.code = (data && data.error && data.error.code) || 'internal';
      throw err;
    }
    const type = res.headers.get('Content-Type') || '';
    const total = Number(res.headers.get('Content-Length')) || 0;
    if (o.maxBytes && total > o.maxBytes) {
      if (res.body && res.body.cancel) res.body.cancel();
      const err = new Error('too_big'); err.code = 'too_big'; err.total = total; throw err;
    }
    if (!o.onProgress || !res.body || !res.body.getReader) return new Blob([await res.arrayBuffer()], { type });
    const reader = res.body.getReader();
    const parts = [];
    let got = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parts.push(value); got += value.length;
      o.onProgress(got, total);
    }
    return new Blob(parts, { type });
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
      if (S.screen === 'library') libPoll();
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
    if (S.screen === 'library') renderLibrary();
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

  function flashNode(f) {
    return el('div', { class: 'banner ' + (f.kind === 'good' ? 'good' : f.kind === 'warn' ? 'warn' : 'bad'), role: f.kind === 'good' ? 'status' : 'alert' },
      el('div', { class: 'msg' },
        el('strong', { text: (f.kind === 'good' ? '✓ ' : f.kind === 'warn' ? '! ' : '✕ ') + f.title }),
        f.reason ? el('span', { class: 'why', text: f.reason }) : null,
        f.sub ? el('span', { class: 'sub', text: f.sub }) : null,
        f.code ? el('span', { class: 'sub', text: 'code: ' + f.code }) : null),
      f.action ? el('button', { type: 'button', class: 'btn small', 'data-fk': 'flash-action', onclick: () => { S.flash = null; f.action.fn(); } }, f.action.label) : null,
      el('button', { type: 'button', class: 'btn ghost small', 'data-fk': 'dismiss', onclick: () => { S.flash = null; render(); } }, 'Dismiss'));
  }

  function renderBanners() {
    const { hold } = showInfo();
    // On the Library screen the message sits beside the work (see renderLibrary), not at the top of a long page.
    const f = S.screen === 'library' ? null : S.flash;
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
      if (f) out.push(flashNode(f));
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
          el('small', { text: c.command + (c.payload ? ' · ' + c.payload : '') + (c.status !== 'fired' && (S.cueDetail[c.id] || (c.failure && c.failure.detail)) ? ' · last take: ' + (S.cueDetail[c.id] || c.failure.detail) : '') })),
        chip(CUE_CHIP, c.status !== 'fired' && S.cueLast[c.id] && S.cueLast[c.id] !== 'fired' ? S.cueLast[c.id] : (c.status !== 'fired' && c.failure ? c.failure.status : c.status)))),
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

  // ---- Library screen: shots, clips, player, marks, selection -------------------------------------------
  // docs/specs/shot-spans.md (slice 4) and docs/mockups/shot-spans.html. The page shows what the server last
  // returned. Every write goes to the server and the list is read back; nothing is shown optimistically.
  //
  // Media and the token: <img> and <video> cannot send an Authorization header. Thumbnails are therefore always
  // fetched with fetch() (header attached when there is a token) and shown from a blob: URL; that also lets a
  // trim fetch the new frame instead of a cached one. The proxy plays natively (<video src>, byte ranges) when
  // the page has no token, which is the loopback case. When a token is set the proxy is fetched with the header
  // into a blob first (an explicit "Load proxy" button, capped at BLOB_CAP) and then plays from memory.
  // No server change, no token in a URL or a log. See docs/specs/operator-ui.md for what that costs.
  const LIB = { PAGE: 500, MAX_PAGES: 10, WORD_PAGES: 4, BLOB_CAP: 512 * 1024 * 1024, THUMB_PARALLEL: 3, SNIPPET: 90 };
  const ORIGIN_CHIP = { detect: ['det', 'Detected'], manual: ['man', 'Marked'], transcript: ['wrd', 'From words'] };
  const L = {
    assetId: null, load: 'idle', err: null, data: null, words: [], tok: 0, filter: 'all', cur: null, sel: [],
    draft: null, pos: null, raf: 0, warn: '',
    proxy: { for: null, state: 'none', msg: '', pct: 0, mode: 'native', url: null, abort: null },
  };
  const T = { urls: new Map(), failed: new Map(), queue: [], active: 0, inflight: new Set() };

  function store(key, value) {
    try {
      if (value === undefined) return JSON.parse(sessionStorage.getItem(key));
      if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, JSON.stringify(value));
    } catch (e) { /* private mode: the selection lives until the page closes */ }
    return null;
  }

  function libInit() {
    L.assetId = store('volt.lib.asset') || null;
    const sel = store('volt.lib.sel');
    L.sel = Array.isArray(sel) ? sel.filter((x) => x && typeof x.id === 'string' && typeof x.assetId === 'string') : [];
    const v = $('lib-video');
    $('lib-asset').addEventListener('change', (e) => setAsset(e.target.value));
    $('lib-play').addEventListener('click', togglePlay);
    $('lib-back').addEventListener('click', () => stepFrames(-1));
    $('lib-fwd').addEventListener('click', () => stepFrames(1));
    $('lib-prev').addEventListener('click', () => jumpShot(-1));
    $('lib-next').addEventListener('click', () => jumpShot(1));
    $('lib-reload').addEventListener('click', () => proxyLoad());
    $('lib-mark-in').addEventListener('click', () => markAt('lib-in', 'In'));
    $('lib-mark-out').addEventListener('click', () => markAt('lib-out', 'Out'));
    $('lib-mark').addEventListener('submit', (e) => { e.preventDefault(); addSpan('clip'); });
    $('lib-add-shot').addEventListener('click', () => addSpan('shot'));
    for (const ev of ['timeupdate', 'seeked', 'seeking']) v.addEventListener(ev, updatePos);
    v.addEventListener('loadedmetadata', onVideoReady);
    v.addEventListener('play', () => { $('lib-play').firstChild.textContent = 'Pause '; tickPos(); });
    v.addEventListener('pause', () => { $('lib-play').firstChild.textContent = 'Play '; updatePos(); });
    v.addEventListener('error', onVideoError);
  }

  function libEnter() { if (L.assetId) libLoad(L.load === 'ready'); }
  function libLeave() { const v = $('lib-video'); if (v && !v.paused) v.pause(); }

  function libPoll() {
    if (L.assetId && L.load === 'ready' && !document.hidden && ![...S.pending].some((k) => k.startsWith('lib:'))) libLoad(true);
  }

  // ---- data ------------------------------------------------------------------------------------------------
  function setAsset(id) {
    if (id === L.assetId) return;
    proxyRelease();
    L.assetId = id || null;
    store('volt.lib.asset', L.assetId);
    L.data = null; L.words = []; L.cur = null; L.draft = null; L.filter = 'all'; L.pos = null; L.warn = '';
    for (const f of ['lib-in', 'lib-out', 'lib-name']) $(f).value = '';
    thumbsPrune();
    if (L.assetId) libLoad(false); else { L.load = 'idle'; render(); }
  }

  const failure = (code, reason) => Object.assign(new Error(reason), { code });

  async function libLoad(quiet) {
    const id = L.assetId;
    if (!id) { L.load = 'idle'; render(); return; }
    const tok = ++L.tok;
    if (!quiet) { L.load = 'loading'; L.err = null; L.data = null; T.failed.clear(); render(); }
    try {
      const spans = [];
      let head = null, after = null;
      for (let i = 0; i < LIB.MAX_PAGES; i++) {
        const q = { kind: 'shot,clip', limit: String(LIB.PAGE) };
        if (after) q.after = after;
        const { status, data } = await call('spans', id, null, q);
        if (tok !== L.tok) return;
        if (status === 401) { setConn('token'); openToken(); throw failure('unauthorized', 'A token is needed to read this asset.'); }
        if (status !== 200 || !data || !data.ok) { const e = codeOf(data, status); throw failure(e.code, e.reason); }
        head = head || data;
        spans.push(...data.spans);
        after = data.next;
        if (!after) break;
      }
      let words = L.words;
      if (!quiet) {
        words = [];
        let wafter = null;
        for (let i = 0; i < LIB.WORD_PAGES; i++) {   // snippets are a convenience: a failure here is not a failure to list
          const q = { limit: '1000' };
          if (wafter) q.after = wafter;
          const { status, data } = await call('transcript', id, null, q);
          if (tok !== L.tok) return;
          if (status !== 200 || !data || !data.ok) break;
          words.push(...data.words);
          wafter = data.next;
          if (!wafter) break;
        }
      }
      L.words = words;
      L.data = Object.assign({}, head, { spans, truncated: !!after });
      thumbsPrune();
      L.load = 'ready';
      L.err = null;
      libBackground(head);
      if (L.cur && !spans.some((s) => s.id === L.cur)) { L.cur = null; L.draft = null; }
      syncTray(spans, !after);
    } catch (e) {
      if (tok !== L.tok) return;
      if (!quiet) { L.load = 'error'; L.err = { code: e.code || 'internal', reason: e.message || 'The list could not be read.' }; }
    }
    render();
  }

  // The tray keeps its own copy of each span (name, range) so it survives a change of asset. When the whole list
  // of an asset has been read, refresh the copies and drop spans that no longer exist.
  function syncTray(spans, complete) {
    const v = libView();
    const byId = new Map(spans.map((s) => [s.id, s]));
    let changed = false;
    L.sel = L.sel.filter((e) => {
      if (e.assetId !== L.assetId) return true;
      const s = byId.get(e.id);
      if (!s) { if (complete) { changed = true; return false; } return true; }
      const fresh = trayEntry(s, v);
      if (JSON.stringify(fresh) !== JSON.stringify(e)) { Object.assign(e, fresh); changed = true; }
      return true;
    });
    if (changed) store('volt.lib.sel', L.sel);
  }

  // ---- derived ---------------------------------------------------------------------------------------------
  function libView() {
    const d = L.data;
    if (!d) return { d: null, ok: false, list: [] };
    if (L.memo && L.memo.d === d && L.memo.words === L.words) return L.memo.view;
    const view = buildView(d);
    L.memo = { d, words: L.words, view };
    return view;
  }

  function buildView(d) {
    const rate = parseRate(d.rate);
    const drop = isDropLabel(d.start);
    const startF = rate && d.start ? tcToFrames(d.start, rate, drop) : null;
    const ok = !!(rate && startF != null);
    const counts = { shot: 0, clip: 0 };
    const words = L.words;
    const list = d.spans.map((s) => {
      const n = ++counts[s.kind === 'shot' ? 'shot' : 'clip'];
      const tag = s.kind === 'shot' ? 'Shot ' + n : 'Clip C' + n;
      return { s, tag, name: s.label || tag, snippet: ok ? snippetFor(s, words) : '' };
    });
    return { d, rate, drop, startF, ok, list };
  }

  function snippetFor(s, words) {
    let text = s.text || '';
    if (!text && words.length) {
      let lo = 0, hi = words.length;
      while (lo < hi) { const mid = (lo + hi) >> 1; if (words[mid].frameIn < s.frameIn) lo = mid + 1; else hi = mid; }
      const out = [];
      for (let i = lo; i < words.length && words[i].frameIn < s.frameOut && out.join(' ').length < LIB.SNIPPET; i++) out.push(words[i].text);
      text = out.join(' ');
    }
    return text.length > LIB.SNIPPET ? text.slice(0, LIB.SNIPPET - 1).trimEnd() + '…' : text;
  }

  const spanLen = (s) => s.frameOut - s.frameIn;
  const isSelected = (id) => L.sel.some((e) => e.id === id);
  const curSpan = () => (L.data && L.cur ? L.data.spans.find((s) => s.id === L.cur) || null : null);
  const inUse = (s) => s.usedBy.items + s.usedBy.versions;

  function trayEntry(s, v) {
    const item = v.list.find((x) => x.s.id === s.id);
    const asset = ((S.data && S.data.assets) || []).find((a) => a.id === s.assetId);
    return { id: s.id, assetId: s.assetId, name: item ? item.name : (s.label || s.id.slice(0, 8)), tcIn: s.tcIn, tcOut: s.tcOut,
      frames: spanLen(s), rate: v.d ? v.d.rate : null, cam: (asset && (asset.camera || short(asset.id))) || short(s.assetId) };
  }

  function toggleSel(s) {
    const v = libView();
    if (isSelected(s.id)) L.sel = L.sel.filter((e) => e.id !== s.id);
    else L.sel = L.sel.concat([trayEntry(s, v)]);
    store('volt.lib.sel', L.sel);
    announce((isSelected(s.id) ? 'Selected ' : 'Removed ') + (v.list.find((x) => x.s.id === s.id) || {}).name);
    render();
  }

  function moveSel(id, dir) {
    const i = L.sel.findIndex((e) => e.id === id), j = i + dir;
    if (i < 0 || j < 0 || j >= L.sel.length) return;
    const next = L.sel.slice();
    [next[i], next[j]] = [next[j], next[i]];
    L.sel = next;
    store('volt.lib.sel', L.sel);
    render();
  }

  // ---- thumbnails (see the note at the top about why they are fetched) ------------------------------------
  const thumbKey = (s) => s.id + ':' + s.frameIn + ':' + s.frameOut;

  function thumbNode(s) {
    const key = thumbKey(s);
    if (T.failed.has(key)) return el('span', { class: 'thumb none', 'data-tk': key, title: T.failed.get(key) }, 'no frame');
    const img = el('img', { class: 'thumb', alt: '', width: '96', height: '54', 'data-tk': key, 'data-asset': s.assetId });
    const url = T.urls.get(key);
    if (url) img.src = url; else { img.classList.add('wait'); wantThumb(s, key); }
    return img;
  }

  function wantThumb(s, key) {
    if (T.inflight.has(key) || T.queue.some((q) => q.key === key)) return;
    T.queue.push({ id: s.id, key, asset: s.assetId });
    pumpThumbs();
  }

  function pumpThumbs() {
    while (T.active < LIB.THUMB_PARALLEL && T.queue.length) {
      const job = T.queue.shift();
      if (job.asset !== L.assetId) continue;
      T.active++;
      T.inflight.add(job.key);
      fetchBlob('/api/spans/' + encodeURIComponent(job.id) + '/thumb', { cache: 'reload' })
        .then((blob) => {
          if (job.asset !== L.assetId) return;
          const url = URL.createObjectURL(blob);
          T.urls.set(job.key, url);
          for (const n of document.querySelectorAll('img[data-tk="' + CSS.escape(job.key) + '"]')) { n.src = url; n.classList.remove('wait'); }
        })
        .catch((err) => {
          const why = (err.code ? err.code + ': ' : '') + err.message;
          T.failed.set(job.key, why);
          for (const n of document.querySelectorAll('img[data-tk="' + CSS.escape(job.key) + '"]')) n.replaceWith(el('span', { class: 'thumb none', 'data-tk': job.key, title: why }, 'no frame'));
        })
        .finally(() => { T.active--; T.inflight.delete(job.key); pumpThumbs(); });
    }
  }

  // Forget blobs of other assets, and of frames a trim has replaced.
  function thumbsPrune() {
    const live = new Set(L.data ? L.data.spans.map(thumbKey) : []);
    for (const [key, url] of T.urls) if (!live.has(key)) { URL.revokeObjectURL(url); T.urls.delete(key); }
    T.queue = T.queue.filter((q) => q.asset === L.assetId && live.has(q.key));
  }

  // ---- the proxy -------------------------------------------------------------------------------------------
  function proxyRelease() {
    const P = L.proxy, v = $('lib-video');
    if (P.abort) { P.abort.abort(); P.abort = null; }
    if (!v.paused) v.pause();
    v.removeAttribute('src');
    v.load();
    if (P.url) { URL.revokeObjectURL(P.url); P.url = null; }
    P.for = null; P.state = 'none'; P.msg = ''; P.pct = 0;
    L.pos = null;
    cancelAnimationFrame(L.raf);
  }

  // Native: let <video> stream and seek by range. Token: fetch with the header, then play from memory.
  async function proxyLoad() {
    const id = L.assetId, P = L.proxy, v = $('lib-video');
    if (!id) return;
    proxyRelease();
    P.for = id; P.msg = '';
    const path = '/api/assets/' + encodeURIComponent(id) + '/proxy';
    if (!getToken()) {
      P.mode = 'native'; P.state = 'loading';
      v.src = path;
      v.load();
      renderVmsg();
      return;
    }
    P.mode = 'blob'; P.state = 'loading'; P.pct = 0;
    P.abort = new AbortController();
    renderVmsg();
    try {
      const blob = await fetchBlob(path, {
        signal: P.abort.signal, maxBytes: LIB.BLOB_CAP,
        onProgress: (got, total) => { P.pct = total ? Math.floor((got / total) * 100) : 0; P.got = got; renderVmsg(); },
      });
      if (L.assetId !== id) return;
      P.url = URL.createObjectURL(blob);
      v.src = P.url;
      v.load();
    } catch (err) {
      if (err.name === 'AbortError' || L.assetId !== id) return;
      P.state = 'error';
      P.msg = err.code === 'too_big'
        ? 'The proxy is ' + Math.round(err.total / 1048576) + ' MB. With a token it has to be loaded into memory first, and the limit is ' + Math.round(LIB.BLOB_CAP / 1048576) + ' MB. Open Volt on the machine that runs it, without a token, to play a file this size.'
        : (err.code ? err.code + ': ' : '') + err.message;
      renderVmsg();
    }
  }

  function onVideoReady() {
    const P = L.proxy, v = $('lib-video'), view = libView();
    P.state = 'ready';
    L.warn = '';
    if (view.ok && view.d.durationFrames && isFinite(v.duration)) {
      const frames = Math.round(v.duration * view.rate.n / view.rate.d);
      if (Math.abs(frames - view.d.durationFrames) > 2) {
        L.warn = 'The proxy is ' + frames + ' frames long and the source ' + view.d.durationFrames + '. Marks assume the two share a start and a rate; check the out frame before you rely on it.';
      }
    }
    if (L.pos == null && view.ok) L.pos = view.startF;
    render();
    updatePos();
  }

  async function onVideoError() {
    const P = L.proxy, v = $('lib-video');
    if (!v.getAttribute('src')) return;   // we just cleared it
    P.state = 'error';
    P.msg = 'The browser could not play this proxy.';
    if (P.mode === 'native') {
      try {   // the player shows no reason; one byte of the same URL does
        const res = await fetch('/api/assets/' + encodeURIComponent(L.assetId) + '/proxy', { headers: { Range: 'bytes=0-0' }, cache: 'no-store' });
        if (!res.ok) { const data = await res.json(); P.msg = codeOf(data, res.status).code + ': ' + codeOf(data, res.status).reason; }
        else P.msg += ' The file was served, so its format may not be one this browser plays.';
      } catch (e) { /* keep the generic text */ }
    }
    render();
    renderVmsg();
  }

  // The message that sits over the player when there is nothing to play yet.
  function renderVmsg() {
    const P = L.proxy, d = L.data, v = $('lib-video');
    const box = $('lib-vmsg');
    const ready = P.state === 'ready';
    v.hidden = !ready;
    $('lib-overlay').hidden = !ready;
    $('lib-reload').hidden = !(d && d.hasProxy && (ready || P.state === 'error'));
    let nodes = [];
    if (!ready) {
      if (!d || !d.hasProxy) {
        nodes = [el('strong', { text: 'No proxy' }), el('span', { text: 'Thumbnails and the player need a proxy essence, and the proxy job has not produced one for this asset. Marks can still be typed as timecode.' })];
      } else if (P.state === 'idle') {
        nodes = [el('strong', { text: 'Proxy not loaded' }),
          el('span', { text: 'This server needs a token, and a browser cannot send one with a video tag. Loading downloads the whole proxy into memory first, then plays it from there.' }),
          el('button', { type: 'button', class: 'btn', 'data-fk': 'load-proxy', onclick: proxyLoad }, 'Load proxy')];
      } else if (P.state === 'loading') {
        nodes = [el('span', { class: 'spin', 'aria-hidden': 'true' }), el('span', { role: 'status', text: P.mode === 'blob' ? 'Loading the proxy' + (P.pct ? ' · ' + P.pct + '%' : '…') : 'Opening the proxy…' })];
      } else if (P.state === 'error') {
        nodes = [el('strong', { text: 'Proxy not available' }), el('span', { text: P.msg }),
          el('button', { type: 'button', class: 'btn ghost', 'data-fk': 'load-proxy', onclick: proxyLoad }, 'Try again')];
      }
    }
    const sig = JSON.stringify([ready, P.state, P.pct, P.msg, !!(d && d.hasProxy)]);
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    box.replaceChildren(...nodes);
    box.hidden = !nodes.length;
  }

  function setupProxy(view) {
    const P = L.proxy;
    if (P.for === L.assetId && !(P.state === 'none' && view.d.hasProxy)) { renderVmsg(); return; }
    P.for = L.assetId;
    if (!view.d.hasProxy) { P.state = 'none'; renderVmsg(); return; }
    if (getToken()) { P.mode = 'blob'; P.state = 'idle'; renderVmsg(); return; }   // not loaded until asked: it is a whole-file download
    proxyLoad();
  }

  // ---- the playhead ----------------------------------------------------------------------------------------
  const videoReady = () => L.proxy.state === 'ready';

  function frameNow(view) {
    const v = $('lib-video');
    return view.startF + Math.floor(v.currentTime * view.rate.n / view.rate.d + 1e-4);
  }

  function updatePos() {
    const view = libView();
    if (!view.ok || !videoReady()) return;
    L.pos = frameNow(view);
    const tc = framesToTc(L.pos, view.rate, view.drop);
    const shot = view.list.find((x) => x.s.kind === 'shot' && x.s.frameIn <= L.pos && L.pos < x.s.frameOut);
    const text = tc + (shot ? ' · ' + shot.tag : '');
    if ($('lib-overlay').textContent !== text) $('lib-overlay').textContent = text;
    $('lib-pos').textContent = 'Playhead ' + text;
    const total = timelineTotal(view);
    placeHead(total ? (L.pos - view.startF) / total : 0);
  }

  function tickPos() {
    cancelAnimationFrame(L.raf);
    const v = $('lib-video');
    const step = () => { updatePos(); if (!v.paused && !v.ended) L.raf = requestAnimationFrame(step); };
    L.raf = requestAnimationFrame(step);
  }

  function seekTo(frame) {
    const view = libView(), v = $('lib-video');
    if (!view.ok || !videoReady()) return false;
    const max = isFinite(v.duration) ? v.duration : Infinity;
    // Aim a quarter frame in, so the frame shown is the one asked for and not the one before it.
    v.currentTime = Math.min(max, Math.max(0, (frame - view.startF + 0.25) * view.rate.d / view.rate.n));
    L.pos = frame;
    updatePos();
    return true;
  }

  function togglePlay() { const v = $('lib-video'); if (!videoReady()) return; if (v.paused) v.play().catch(() => {}); else v.pause(); }
  function stepFrames(n) {
    const view = libView(), v = $('lib-video');
    if (!view.ok || !videoReady()) return;
    if (!v.paused) v.pause();
    seekTo(Math.max(view.startF, (L.pos == null ? frameNow(view) : L.pos) + n));
  }

  function jumpShot(dir) {
    const view = libView();
    if (!view.ok || !videoReady()) return;
    const here = L.pos == null ? frameNow(view) : L.pos;
    const shots = view.list.filter((x) => x.s.kind === 'shot');
    const target = dir > 0 ? shots.find((x) => x.s.frameIn > here) : [...shots].reverse().find((x) => x.s.frameIn < here);
    if (!target) { announce(dir > 0 ? 'No later shot.' : 'No earlier shot.'); return; }
    L.cur = target.s.id;
    seekTo(target.s.frameIn);
    render();
  }

  function markAt(inputId, word) {
    const view = libView();
    if (!view.ok || !videoReady()) { announce('No frame to mark from. Type a timecode instead.'); return; }
    const tc = framesToTc(L.pos == null ? frameNow(view) : L.pos, view.rate, view.drop);
    $(inputId).value = tc;
    announce(word + ' set to ' + tc);
    updateMarkHint();
  }

  // ---- writing ---------------------------------------------------------------------------------------------
  async function libAct(kind, id, body, o) {
    o = o || {};
    const key = o.key || 'lib:' + kind + ':' + id;
    if (S.pending.has(key)) return;
    S.pending.add(key);
    render();
    try {
      const { status, data } = await call(kind, id, body);
      if (status === 401) { setConn('token'); openToken(); }
      if (data && data.ok) {
        if (o.after) o.after(data);
        const title = typeof o.okTitle === 'function' ? o.okTitle(data) : (o.okTitle || 'Done');
        setFlash({ kind: (o.tone && o.tone(data)) || 'good', title, reason: o.ok ? o.ok(data) : '' });
        announce(title + ' ' + (o.ok ? o.ok(data) : ''));
        await libLoad(true);
      } else {
        const e = codeOf(data, status);
        const f = refusal(kind, e, data, o);
        if (o.decorate) o.decorate(f, e);
        setFlash(f);
        announce((ERR_LABEL[e.code] || 'Refused') + '. ' + (f.reason || e.reason));
      }
    } catch (err) {
      setFlash({ kind: 'bad', title: 'No answer from the server.', code: 'network',
        reason: 'The request may or may not have landed. Check the list before trying again.' });
      setConn('lost');
    } finally {
      S.pending.delete(key);
      render();
    }
  }

  function addSpan(kind) {
    if (!L.assetId || !L.data) return;
    const tcIn = $('lib-in').value.trim(), tcOut = $('lib-out').value.trim(), name = $('lib-name').value.trim();
    if (!tcIn || !tcOut) {
      setFlash({ kind: 'bad', title: 'Set in and out first.', reason: 'Press I and O at the playhead, or type both timecodes.', code: 'bad_input' });
      render();
      return;
    }
    const body = { kind, tc_in: tcIn, tc_out: tcOut };
    if (name) body.label = name;
    libAct('spanCreate', L.assetId, body, {
      key: 'lib:add:' + kind, okTitle: kind === 'shot' ? 'Shot added.' : 'Clip added.',
      ok: (d) => d.span.tcIn + ' to ' + d.span.tcOut + '.',
      after: (d) => { L.cur = d.span.id; L.draft = null; for (const f of ['lib-in', 'lib-out', 'lib-name']) $(f).value = ''; updateMarkHint(); },
      decorate: (f, e) => {
        if (e.code !== 'overlaps_shot') return;
        const other = libView().list.find((x) => e.reason.includes(x.s.id));
        f.reason = tcIn + ' to ' + tcOut + ' overlaps ' + (other ? other.tag + ' (' + other.s.tcIn + ' to ' + other.s.tcOut + ')' : 'another shot') +
          '. A shot cannot overlap another shot. Make it a clip, or trim that shot first.';
        if (kind === 'shot') f.action = { label: 'Add as clip', fn: () => addSpan('clip') };
      },
    });
  }

  function draftFor(s) {
    if (!L.draft || L.draft.id !== s.id) L.draft = { id: s.id, name: s.label || '', tcIn: s.tcIn, tcOut: s.tcOut, item: '' };
    return L.draft;
  }

  function saveName(s) {
    const d = draftFor(s);
    libAct('spanLabel', s.id, { label: d.name.trim() || null }, { key: 'lib:label:' + s.id, okTitle: 'Renamed.', ok: () => (d.name.trim() || 'Name cleared') + '.', after: () => { L.draft = null; } });
  }

  function applyTrim(s) {
    const d = draftFor(s);
    const body = {};
    if (d.tcIn.trim() && d.tcIn.trim() !== s.tcIn) body.tc_in = d.tcIn.trim();
    if (d.tcOut.trim() && d.tcOut.trim() !== s.tcOut) body.tc_out = d.tcOut.trim();
    if (!Object.keys(body).length) {
      setFlash({ kind: 'bad', title: 'Nothing to trim.', reason: 'Change the in or the out first.', code: 'bad_input' });
      render();
      return;
    }
    libAct('spanTrim', s.id, body, { key: 'lib:trim:' + s.id, okTitle: 'Trimmed.', ok: (r) => r.span.tcIn + ' to ' + r.span.tcOut + '.', after: () => { L.draft = null; } });
  }

  function deleteSpan(s) {
    armedAction('del:' + s.id, () => libAct('spanDelete', s.id, {}, {
      key: 'lib:del:' + s.id, okTitle: 'Deleted.', ok: () => 'Nothing else was changed.',
      after: () => {
        L.sel = L.sel.filter((e) => e.id !== s.id);
        store('volt.lib.sel', L.sel);
        if (L.cur === s.id) { L.cur = null; L.draft = null; }
      },
    }));
  }

  function attachSpan(s, itemId, slug) {
    act('attach', itemId, { span: s.id }, { key: 'lib:attach:' + s.id, okTitle: 'Attached.', ok: () => s.id.slice(0, 8) + ' is on "' + slug + '".' });
  }

  // ---- detection, split, merge, indexing (slice 4b of docs/specs/shot-spans.md) -----------------------------
  // Detection is a job a runner executes, so the page only queues it and then reads the job's state back from the
  // span list's header (`detect`, `indexing`) on the poll it already makes. Split and merge are plain writes. Every
  // rule and refusal is the server's (volt/shots.py); this section shows what it said. Rights never block any of it.
  const D = { threshold: '', minFrames: '', open: false, asked: new Set(), seen: null };
  const detActive = (j) => !!j && (j.status === 'queued' || j.status === 'running');
  const mmss = (n) => Math.floor(n / 60) + ':' + String(n % 60).padStart(2, '0');

  // Runs on every successful read of the list: queue the backfill once per asset per page, and say when a job ends.
  function libBackground(head) {
    const id = L.assetId;
    const idx = head.indexing;
    if (head.pendingFrames > 0 && !D.asked.has(id) && (!idx || idx.status === 'failed')) {
      D.asked.add(id);
      call('spanFrames', id, {}).then(() => { if (id === L.assetId) libLoad(true); }).catch(() => { /* the next open asks again */ });
    }
    const j = head.detect;
    if (j) {
      const was = D.seen && D.seen.asset === id && D.seen.id === j.id ? D.seen.status : null;
      if (was && (was === 'queued' || was === 'running')) {
        if (j.status === 'done') announce('Detection finished.');
        else if (j.status === 'failed') announce('Detection failed. ' + (j.error || ''));
      }
      D.seen = { asset: id, id: j.id, status: j.status };
    }
  }

  // What the backfill is doing, in calm words. null when nothing is waiting on it.
  function indexInfo(d) {
    const n = d.pendingFrames;
    if (!n) return null;
    const j = d.indexing;
    const spans = n + ' span' + (n === 1 ? '' : 's');
    if (!j || detActive(j)) return { state: 'working', text: 'Indexing: placing ' + spans + ' on frames. They join the list when it is done.' };
    if (j.status === 'failed') return { state: 'failed', text: 'Indexing did not finish (' + (j.error || 'no reason recorded') + '). ' + spans + ' not listed yet.' };
    return { state: 'stuck', text: spans + ' could not be placed on frames, because their timecodes do not exist on this essence, so ' + (n === 1 ? 'it is' : 'they are') + ' not listed.' };
  }

  function detectInfo(d) {
    const j = d.detect;
    const last = d.lastRun
      ? 'Last detection ' + d.lastRun.createdAt.slice(0, 16).replace('T', ' ') + ' · threshold ' + d.lastRun.threshold + ' · ' + d.lastRun.shotCount + ' shots'
      : 'Shots have not been detected for this asset.';
    if (j && j.status === 'running') {
      return { tone: 'busy', text: 'Detecting shots. Running' + (j.ageSeconds == null ? '' : ' for ' + mmss(j.ageSeconds)) + '.',
        sub: 'The scan reads the whole file in one pass and reports no percentage. The list fills in when it ends.' };
    }
    if (j && j.status === 'queued') {
      const stuck = j.ageSeconds != null && j.ageSeconds > 20;
      return { tone: 'busy', text: 'Detection queued' + (j.ageSeconds == null ? '' : ' ' + mmss(j.ageSeconds) + ' ago') + (j.error ? '. Last note: ' + j.error : '') + '.',
        sub: stuck ? 'No runner has picked it up yet. Detection runs in the runner (python -m volt.runner); it stays queued until one is running.' : 'Waiting for a runner to pick it up.' };
    }
    if (j && j.status === 'failed') return { tone: 'bad', text: 'Detection failed: ' + (j.error || 'no reason recorded') + '.', sub: 'Nothing was changed. ' + last };
    return { tone: '', text: last, sub: '' };
  }

  function paintDetect(view) {
    const d = view.d;
    const info = detectInfo(d), idx = indexInfo(d);
    const detected = view.list.filter((x) => x.s.origin === 'detect');
    const replaceable = detected.filter((x) => !inUse(x.s)).length;
    const hand = view.list.filter((x) => x.s.kind === 'shot' && x.s.origin !== 'detect').length;
    const busy = detActive(d.detect) || S.pending.has('lib:detect');
    const blockedWhy = d.open ? 'Detection waits until the recording is wrapped.' : busy ? 'A detection is already queued or running.' : '';
    const armed = S.armed === 'detect:' + L.assetId;
    const label = armed ? 'Confirm re-detect' : (d.detect && d.detect.status === 'failed') ? 'Try again' : (d.lastRun || detected.length) ? 'Re-detect' : 'Detect shots';
    paint('lib-detectbar', [L.assetId, info, idx, label, blockedWhy, replaceable, hand, armed, D.threshold, D.minFrames, D.open], () => [
      el('div', { class: 'bar flush' },
        el('button', { type: 'button', class: 'btn' + (armed ? ' armed' : ' ghost'), id: 'lib-detect', 'data-fk': 'lib-detect', 'aria-disabled': blockedWhy ? 'true' : null,
          'aria-describedby': blockedWhy ? 'lib-detect-why' : null, onclick: () => { if (!blockedWhy) detectClick(view); } }, label),
        el('details', { class: 'detect-set', open: D.open, ontoggle: (e) => { D.open = e.target.open; } },
          el('summary', { text: 'Settings' }),
          el('div', { class: 'bar flush' },
            el('div', {}, el('label', { for: 'lib-det-t', text: 'Threshold (above 0, below 1)' }),
              el('input', { id: 'lib-det-t', type: 'text', class: 'mono', inputmode: 'decimal', placeholder: '0.13', value: D.threshold, 'data-fk': 'lib-det-t', autocomplete: 'off',
                oninput: (e) => { D.threshold = e.target.value; } })),
            el('div', {}, el('label', { for: 'lib-det-m', text: 'Shortest shot (frames)' }),
              el('input', { id: 'lib-det-m', type: 'text', class: 'mono', inputmode: 'numeric', placeholder: '12', value: D.minFrames, 'data-fk': 'lib-det-m', autocomplete: 'off',
                oninput: (e) => { D.minFrames = e.target.value; } })),
            el('small', { text: 'Blank uses the default. A change of either is a new run.' }))),
        blockedWhy ? el('small', { id: 'lib-detect-why', text: blockedWhy }) : null),
      el('p', { class: 'detect-state' + (info.tone ? ' ' + info.tone : ''), role: 'status', id: 'lib-detect-state' },
        info.tone === 'busy' ? el('progress', { 'aria-label': 'Detection in progress', class: 'detect-bar' }) : null,
        el('span', { text: info.text }), info.sub ? el('small', { text: ' ' + info.sub }) : null),
      replaceable && !blockedWhy ? el('small', { id: 'lib-detect-warn', text: 'Re-detecting replaces ' + replaceable + ' detected shot' + (replaceable === 1 ? '' : 's') +
        '. ' + (hand ? hand + ' shot' + (hand === 1 ? '' : 's') + ' you marked, split or merged by hand and any shot in use are kept.' : 'Any shot you mark, split or merge by hand, and any shot in use, is kept.') }) : null,
      idx ? el('p', { class: 'detect-state ' + (idx.state === 'working' ? 'busy' : 'bad'), role: 'status', id: 'lib-index-state' },
        el('span', { text: idx.text }),
        idx.state === 'failed' ? el('button', { type: 'button', class: 'btn ghost small', 'data-fk': 'lib-idx-retry', onclick: () => { D.asked.delete(L.assetId); libLoad(true); } }, 'Try again') : null) : null,
    ]);
  }

  function detectClick(view) {
    const replaceable = view.list.filter((x) => x.s.origin === 'detect' && !inUse(x.s)).length;
    if (replaceable) armedAction('detect:' + L.assetId, runDetect); else runDetect();
  }

  function runDetect() {
    const body = {};
    const t = D.threshold.trim(), m = D.minFrames.trim();
    if (t) body.threshold = Number(t);
    if (m) body.min_frames = Number(m);
    for (const k in body) {
      if (!isFinite(body[k])) {
        setFlash({ kind: 'bad', title: 'Bad input.', code: 'bad_input', reason: (k === 'threshold' ? 'The threshold' : 'The shortest shot') + ' must be a number, or blank for the default.' });
        render();
        return;
      }
    }
    libAct('detect', L.assetId, body, {
      key: 'lib:detect', okTitle: (r) => (r.alreadyDone ? 'Already detected.' : 'Detection queued.'), tone: (r) => (r.alreadyDone ? 'warn' : 'good'),
      ok: (r) => (r.alreadyDone
        ? 'This asset was already detected with these settings, so nothing was queued. Change the threshold or the shortest shot to run again.'
        : 'A runner will pick it up; this screen follows it. Shots you made by hand are not touched.'),
    });
  }

  // The shot after `s` in frame order (the list is sorted), or null.
  function nextShot(s, view) {
    if (s.kind !== 'shot') return null;
    const i = view.list.findIndex((x) => x.s.id === s.id);
    for (let j = i + 1; i >= 0 && j < view.list.length; j++) if (view.list[j].s.kind === 'shot') return view.list[j];
    return null;
  }

  function splitMergeBlock(s, view, d, used, next) {
    const isShot = s.kind === 'shot';
    const adjacent = !!next && next.s.frameIn === s.frameOut;
    let why;
    if (!isShot) why = 'Only shots can be split or merged. A clip can be trimmed.';
    else if (used) why = 'In use by a rundown item or a version: split and merge are refused. Rename is allowed.';
    else if (!next) why = 'This is the last shot, so there is nothing to merge with. To split, type a timecode inside the shot or use the playhead.';
    else if (!adjacent) why = 'The next shot starts later (there is a gap), so it is not a neighbour. To split, type a timecode inside the shot or use the playhead.';
    else if (inUse(next.s)) why = next.name + ' is in use, so it cannot be merged in.';
    else why = 'Merge folds ' + next.name + ' (' + next.s.tcIn + ' to ' + next.s.tcOut + ') into this shot, which keeps its name. Split needs a timecode strictly inside the shot.';
    const canSplit = isShot && !used, canMerge = isShot && !used && adjacent && !inUse(next.s);
    return el('fieldset', { class: 'grp' }, el('legend', { text: 'Split and merge' }),
      el('label', { class: 'f', for: 'span-split', text: 'Split at' }),
      el('input', { id: 'span-split', type: 'text', class: 'mono', value: d.splitAt || '', 'data-fk': 'span-split', disabled: !canSplit, spellcheck: 'false', 'aria-describedby': 'span-sm-why',
        oninput: (e) => { d.splitAt = e.target.value; },
        onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); splitSpan(s); } } }),
      el('div', { class: 'bar flush' },
        btn('Playhead as split point', { key: 'ph-split', cls: 'ghost small', disabled: !canSplit || !videoReady(), onclick: () => { d.splitAt = playheadTc(view); $('span-split').value = d.splitAt; } }),
        btn('Split', { key: 'lib:split:' + s.id, cls: 'ghost', disabled: !canSplit, onclick: () => splitSpan(s) }),
        btn('Merge with next', { key: 'lib:merge:' + s.id, cls: 'ghost', disabled: !canMerge, onclick: () => mergeNext(s, next) })),
      el('small', { id: 'span-sm-why', class: canSplit ? null : 'no', text: why }));
  }

  function splitSpan(s) {
    const d = draftFor(s);
    const at = (d.splitAt || '').trim();
    if (!at) {
      setFlash({ kind: 'bad', title: 'Set a split point first.', reason: 'Type a timecode inside the shot, or move the playhead and use "Playhead as split point".', code: 'bad_input' });
      render();
      return;
    }
    libAct('spanSplit', s.id, { at }, {
      key: 'lib:split:' + s.id, okTitle: 'Split.',
      ok: (r) => r.spans[0].tcIn + ' to ' + r.spans[0].tcOut + ' and ' + r.spans[1].tcIn + ' to ' + r.spans[1].tcOut + '. Both are now marked by hand.',
      after: () => { L.draft = null; },
    });
  }

  function mergeNext(s, next) {
    const gone = next.name;
    libAct('spanMerge', s.id, { with: next.s.id }, {
      key: 'lib:merge:' + s.id, okTitle: 'Merged.',
      ok: (r) => gone + ' was folded in. The shot now runs ' + r.span.tcIn + ' to ' + r.span.tcOut + '.',
      after: (r) => {
        L.sel = L.sel.filter((e) => e.id !== r.removed);
        store('volt.lib.sel', L.sel);
        if (L.cur === r.removed) L.cur = s.id;
        L.draft = null;
      },
    });
  }

  // ---- keys ------------------------------------------------------------------------------------------------
  // Only on this screen, and only where a control would not already use the key. Space is not TAKE here: on a row's
  // name it toggles the selection (see the row's own handler) and elsewhere it keeps its browser behaviour.
  function libKey(e) {
    if (!L.data) return;
    const t = e.target;
    const k = e.key;
    const onControl = t && t !== document.body && t.closest && t.closest('button,a,summary,select,[role="button"],input,textarea');
    if (k === 'Enter') { if (!onControl && !e.repeat) { e.preventDefault(); addSpan('clip'); } return; }
    const low = k.toLowerCase();
    if (low === 'i' && !e.repeat) { markAt('lib-in', 'In'); return; }
    if (low === 'o' && !e.repeat) { markAt('lib-out', 'Out'); return; }
    if (low === 'k' && !e.repeat) { togglePlay(); return; }
    if (k === ',') { stepFrames(-1); return; }
    if (k === '.') { stepFrames(1); return; }
    if (k === '[' && !e.repeat) { jumpShot(-1); return; }
    if (k === ']' && !e.repeat) { jumpShot(1); return; }
  }

  // ---- rendering -------------------------------------------------------------------------------------------
  // Static markup only (no data goes in), so innerHTML is safe here and needs no namespace string.
  function icon(kind) {
    const tpl = document.createElement('template');
    const stroke = kind === 'warn' ? '#ff4d4d' : '#a0a0a0';
    const shape = kind === 'warn' ? '<path d="M20 6l15 27H5z"/><path d="M20 16v8M20 28v2"/>'
      : '<rect x="6" y="8" width="28" height="24" rx="3"/><path d="M14 8v24M26 8v24M6 16h8M26 16h8M6 24h8M26 24h8"/>';
    tpl.innerHTML = '<svg width="40" height="40" viewBox="0 0 40 40" fill="none" stroke="' + stroke + '" stroke-width="2" aria-hidden="true" focusable="false">' + shape + '</svg>';
    return tpl.content.firstChild;
  }

  function stateCard(title, kind, text, ...actions) {
    return el('section', { class: 'panel state-card', 'aria-labelledby': 'h-state' }, el('h2', { id: 'h-state', text: title }),
      el('div', { class: 'empty' }, el('div', { 'aria-hidden': 'true' }, icon(kind)), el('p', { text }), actions.length ? el('div', { class: 'acts' }, actions) : null));
  }

  function renderLibPicker() {
    const sel = $('lib-asset');
    const assets = ((S.data && S.data.assets) || []).slice();
    // Assets that have a proxy and a closed hi-res come first: those are the ones a shot list is useful on.
    const rank = (a) => (a.hasProxy ? 0 : 2) + (a.open ? 1 : 0);
    assets.sort((a, b) => rank(a) - rank(b));
    const sig = JSON.stringify([assets.map((a) => [a.id, a.camera, a.open, a.hasProxy]), L.assetId]);
    if (sel.dataset.sig === sig) return;
    sel.dataset.sig = sig;
    sel.replaceChildren(el('option', { value: '' }, assets.length ? 'Choose an asset' : 'No assets'),
      ...assets.map((a) => el('option', { value: a.id, selected: a.id === L.assetId },
        (a.camera || a.show || 'Asset') + ' · ' + short(a.id) + (a.open ? ' · recording' : '') + (a.hasProxy ? '' : ' · no proxy'))));
    sel.value = L.assetId && assets.some((a) => a.id === L.assetId) ? L.assetId : '';
  }

  function renderLibrary() {
    renderLibPicker();
    paint('lib-msg', [S.flash], () => (S.flash ? flashNode(S.flash) : []));
    const assets = (S.data && S.data.assets) || [];
    if (L.assetId && !assets.some((a) => a.id === L.assetId)) { L.assetId = null; store('volt.lib.asset', null); L.load = 'idle'; L.data = null; }
    const view = libView();
    let card = null;
    if (!assets.length) card = stateCard('No recordings yet', 'film', 'There is nothing to list. Record something on the Record screen, then come back.');
    else if (!L.assetId) card = stateCard('No asset chosen', 'film', 'Pick an asset to list its shots. Assets with a proxy and a closed hi-res come first.');
    else if (L.load === 'loading' || L.load === 'idle') card = stateCard('Loading', 'film', 'Reading the shots of ' + short(L.assetId) + '…');
    else if (L.load === 'error') {
      card = stateCard('Could not read the shots', 'warn', (L.err.code ? L.err.code + ': ' : '') + L.err.reason,
        el('button', { type: 'button', class: 'btn', 'data-fk': 'lib-retry', onclick: () => libLoad(false) }, 'Try again'));
    } else if (!view.ok) {
      card = stateCard('This asset cannot be marked', 'warn', 'It has no video file with a frame rate recorded, so a timecode cannot be placed on it. Spans need the hi-res essence and its rate.');
    }
    paint('lib-empty', [card ? [L.assetId, L.load, L.err, assets.length] : null], () => card || []);
    $('lib-body').hidden = !!card;
    if (card) return;
    paintHead(view);
    paintFilter(view);
    paintDetect(view);
    paintList(view);
    paintTray(view);
    paintSpan(view);
    renderTimeline(view);
    setupProxy(view);
    updateMarkHint();
    const noFrame = !videoReady();
    for (const id of ['lib-play', 'lib-back', 'lib-fwd', 'lib-prev', 'lib-next', 'lib-mark-in', 'lib-mark-out']) $(id).disabled = noFrame;
    $('lib-add-clip').disabled = S.pending.has('lib:add:clip');
    $('lib-add-shot').disabled = S.pending.has('lib:add:shot');
    updatePos();
  }

  function updateMarkHint() {
    const view = libView();
    if (!view.ok) return;
    const parts = [view.d.hasProxy ? 'I and O mark the playhead; Enter adds a clip when nothing is focused.' : 'There is no proxy to play, so type the in and the out as timecode.'];
    parts.push('Marks are timecode on the source (' + fmtRate(view.rate) + (view.drop ? ', drop-frame' : '') + ').');
    if (view.d.open) parts.push('The asset is still recording: the end of the source is not known, so a mark past it is checked again at wrap.');
    if (L.warn) parts.push(L.warn);
    const text = parts.join(' ');
    if ($('lib-mark-hint').textContent !== text) $('lib-mark-hint').textContent = text;
  }

  function paintHead(view) {
    const d = view.d;
    const assets = (S.data && S.data.assets) || [];
    const a = assets.find((x) => x.id === d.assetId) || {};
    paint('lib-head', [d.assetId, a.camera, d.rate, d.start, d.durationFrames, d.open, d.cleared, d.legalHold, d.hasProxy], () => el('div', { class: 'asset' },
      kv('Asset', short(d.assetId), true), kv('Camera', a.camera || a.show || '—'), kv('Rate', fmtRate(view.rate), true), kv('Start', d.start, true),
      kv('Length', d.durationFrames ? lengthTc(d.durationFrames, view.rate) : (d.open ? 'growing' : 'unknown'), true),
      el('span', { class: d.cleared ? 'ok' : 'no' }, el('span', { 'aria-hidden': 'true', text: d.cleared ? '✓ ' : '✕ ' }), d.cleared ? 'Cleared' : 'Not cleared'),
      d.legalHold ? el('span', { class: 'no' }, el('span', { 'aria-hidden': 'true', text: '⚑ ' }), 'Legal hold') : null,
      d.open ? el('span', { class: 'warn' }, el('span', { 'aria-hidden': 'true', text: '● ' }), 'Recording') : null,
      kv('Proxy', d.hasProxy ? 'ready' : 'none')));
  }
  function kv(k, v, mono) { return el('span', {}, el('span', { class: 'k', text: k }), el('b', { class: mono ? 'mono' : null, text: v })); }

  const FILTERS = [['all', 'All', () => true], ['shot', 'Shots', (s) => s.kind === 'shot'], ['clip', 'Clips', (s) => s.kind === 'clip'],
    ['detect', 'Detected', (s) => s.origin === 'detect'], ['hand', 'Marked by hand', (s) => s.origin !== 'detect']];

  function paintFilter(view) {
    const d = view.d;
    paint('lib-filter', [L.filter, view.list.map((x) => x.s.kind + (x.s.origin || ''))], () => [
      FILTERS.map(([key, label, test]) => el('button', { type: 'button', class: 'pill', 'aria-pressed': String(L.filter === key), 'data-fk': 'f:' + key,
        onclick: () => { L.filter = key; render(); } }, label + ' ' + view.list.filter((x) => test(x.s)).length)),
    ]);
  }

  function paintList(view) {
    const d = view.d;
    const test = FILTERS.find((f) => f[0] === L.filter)[2];
    const rows = view.list.filter((x) => test(x.s));
    const idx = indexInfo(d);
    paint('lib-list', [rows.map((x) => [x.s, x.name, x.snippet, isSelected(x.s.id)]), L.cur, L.filter, d.truncated, d.pendingFrames, d.hasProxy, d.open, idx && idx.state], () => {
      const out = [];
      if (!view.list.length && idx && idx.state === 'working') {
        out.push(stateCard('Indexing', 'film', idx.text));
        return out;
      }
      if (!view.list.length) {
        out.push(stateCard('No shots yet', 'film', (d.lastRun ? 'Detection ran and no shots remain.' : 'This asset has never been scanned.') +
          (videoReady() ? ' Mark by hand: play the proxy and press I, then O.' : ' Mark by hand: type an in and an out above.'),
          el('button', { type: 'button', class: 'btn ghost', 'data-fk': 'mark-by-hand', onclick: () => $('lib-in').focus() }, 'Mark by hand')));
        return out;
      }
      if (!rows.length) { out.push(el('p', { class: 'empty', text: 'Nothing matches this filter.' })); return out; }
      out.push(el('div', { class: 'tblwrap' }, el('table', {},
        el('caption', { class: 'cap', text: 'Sorted by timecode. Space on a name toggles the row. Enter on a name seeks the player.' }),
        el('thead', {}, el('tr', {}, [
          el('th', { scope: 'col' }, el('span', { class: 'vh', text: 'Select' })),
          ...['Frame', 'Name', 'In', 'Out', 'Length', 'Transcript', 'State'].map((h) => el('th', { scope: 'col', text: h }))])),
        el('tbody', {}, rows.map((x) => libRow(x, view))))));
      if (d.truncated) out.push(el('p', { class: 'note', text: 'The list stops at ' + (LIB.MAX_PAGES * LIB.PAGE) + ' spans. Narrow it with a filter.' }));
      return out;
    });
  }

  function libRow(x, view) {
    const s = x.s, on = isSelected(s.id);
    const origin = ORIGIN_CHIP[s.origin] || ORIGIN_CHIP.manual;
    const word = s.kind === 'shot' ? 'shot' : 'clip';
    return el('tr', { class: (on ? 'sel' : '') + (L.cur === s.id ? ' cur' : '') },
      el('td', { class: 'c' }, el('input', { type: 'checkbox', checked: on, 'data-fk': 'sel:' + s.id, 'aria-label': 'Select ' + word + ' ' + x.name,
        onchange: () => toggleSel(s) })),
      el('td', { class: 't' }, thumbNode(s)),
      el('td', {},
        el('button', { type: 'button', class: 'rowname', 'data-fk': 'name:' + s.id, 'aria-current': L.cur === s.id ? 'true' : null,
          onclick: () => { L.cur = s.id; L.draft = null; seekTo(s.frameIn); render(); },
          onkeydown: (e) => { if (e.key === ' ') { e.preventDefault(); toggleSel(s); } },
          onkeyup: (e) => { if (e.key === ' ') e.preventDefault(); } }, x.name),
        el('small', {}, x.tag + ' · ', el('span', { class: 'chip ' + origin[0] }, origin[1]))),
      el('td', { class: 'mono', text: s.tcIn }), el('td', { class: 'mono', text: s.tcOut }),
      el('td', { class: 'mono', text: lengthTc(spanLen(s), view.rate) }),
      el('td', { class: 'tx', text: x.snippet || '—' }),
      el('td', { class: 'st' }, on ? el('span', { class: 'state' }, el('span', { 'aria-hidden': 'true', text: '✓ ' }), 'Selected') : el('span', { class: 'state-off', text: 'Not selected' }),
        inUse(s) ? el('small', { class: 'block', text: 'Used by ' + [s.usedBy.items ? s.usedBy.items + ' item' + (s.usedBy.items > 1 ? 's' : '') : '', s.usedBy.versions ? s.usedBy.versions + ' version' + (s.usedBy.versions > 1 ? 's' : '') : ''].filter(Boolean).join(' and ') }) : null));
  }

  function paintTray(view) {
    const tray = L.sel;
    paint('lib-tray', [tray, [...S.pending]], () => {
      if (!tray.length) return el('p', { class: 'empty', text: 'Nothing selected. Tick rows to build a list; its order is the order of an export.' });
      const rates = new Set(tray.map((e) => e.rate));
      const rate = rates.size === 1 ? parseRate(tray[0].rate) : null;
      const total = rate ? lengthTc(tray.reduce((n, e) => n + e.frames, 0), rate) : 'mixed rates';
      return [
        el('ol', { class: 'tray', 'aria-label': 'Selection, in export order' }, tray.map((e, i) => el('li', {},
          el('span', { class: 'n', 'aria-hidden': 'true', text: String(i + 1) }),
          el('span', { class: 'grow' }, el('b', { text: e.name }), el('small', { class: 'mono', text: e.tcIn + ' · ' + (parseRate(e.rate) ? lengthTc(e.frames, parseRate(e.rate)) : e.frames + ' fr') + (e.assetId !== L.assetId ? ' · ' + e.cam : '') })),
          el('button', { type: 'button', class: 'mini', 'data-fk': 'up:' + e.id, 'aria-label': 'Move ' + e.name + ' up', disabled: i === 0, onclick: () => moveSel(e.id, -1) }, '▲'),
          el('button', { type: 'button', class: 'mini', 'data-fk': 'dn:' + e.id, 'aria-label': 'Move ' + e.name + ' down', disabled: i === tray.length - 1, onclick: () => moveSel(e.id, 1) }, '▼'),
          el('button', { type: 'button', class: 'mini', 'data-fk': 'rm:' + e.id, 'aria-label': 'Remove ' + e.name, onclick: () => { L.sel = L.sel.filter((x) => x.id !== e.id); store('volt.lib.sel', L.sel); render(); } }, '×')))),
        el('dl', { class: 'kv' }, el('dt', { text: 'Total' }), el('dd', { text: total }), el('dt', { text: 'Assets' }), el('dd', { text: String(new Set(tray.map((e) => e.assetId)).size) })),
        el('div', { class: 'bar' },
          el('button', { type: 'button', class: 'btn', disabled: true, 'aria-describedby': 'lib-export-why' }, 'EXPORT TO NLE'),
          el('button', { type: 'button', class: 'btn ghost', 'data-fk': 'tray-clear', onclick: () => { L.sel = []; store('volt.lib.sel', L.sel); render(); } }, 'Clear')),
        el('p', { class: 'note', id: 'lib-export-why', text: 'Export to an NLE is not built yet. The selection is kept for this tab.' }),
      ];
    });
  }

  function paintSpan(view) {
    const s = curSpan();
    if (s) draftFor(s);
    const items = showInfo().items.filter((i) => i.status === 'ready');
    const next = s ? nextShot(s, view) : null;
    paint('lib-span', [s, s ? L.draft.id : null, [...S.pending], S.armed, items.map((i) => i.id), L.pos != null, next && [next.s.id, next.s.frameIn, inUse(next.s)]], () => {
      if (!s) return el('p', { class: 'empty', text: 'Pick a row to rename it, trim it, attach it to a rundown item or delete it.' });
      const d = draftFor(s);
      const used = inUse(s) > 0;
      const delKey = 'del:' + s.id;
      const row = view.list.find((x) => x.s.id === s.id);
      return el('div', { class: 'body' },
        el('dl', { class: 'kv' },
          el('dt', { text: 'Span' }), el('dd', { text: (row ? row.tag : s.kind) + ' · ' + short(s.id) }),
          el('dt', { text: 'Range' }), el('dd', { text: s.tcIn + ' to ' + s.tcOut }),
          el('dt', { text: 'Length' }), el('dd', { text: lengthTc(spanLen(s), view.rate) })),
        el('label', { class: 'f', for: 'span-name', text: 'Name' }),
        el('input', { id: 'span-name', type: 'text', maxlength: '200', value: d.name, 'data-fk': 'span-name', oninput: (e) => { d.name = e.target.value; },
          onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); saveName(s); } } }),
        el('div', { class: 'bar flush' }, btn('Save name', { key: 'lib:label:' + s.id, cls: 'ghost', onclick: () => saveName(s) })),
        el('fieldset', { class: 'grp' }, el('legend', { text: 'Trim' }),
          el('label', { class: 'f', for: 'span-in', text: 'In' }),
          el('input', { id: 'span-in', type: 'text', class: 'mono', value: d.tcIn, 'data-fk': 'span-in', disabled: used, spellcheck: 'false', oninput: (e) => { d.tcIn = e.target.value; } }),
          el('label', { class: 'f', for: 'span-out', text: 'Out' }),
          el('input', { id: 'span-out', type: 'text', class: 'mono', value: d.tcOut, 'data-fk': 'span-out', disabled: used, spellcheck: 'false', oninput: (e) => { d.tcOut = e.target.value; } }),
          el('div', { class: 'bar flush' },
            btn('Playhead as in', { key: 'ph-in', cls: 'ghost small', disabled: used || !videoReady(), onclick: () => { d.tcIn = playheadTc(view); $('span-in').value = d.tcIn; } }),
            btn('Playhead as out', { key: 'ph-out', cls: 'ghost small', disabled: used || !videoReady(), onclick: () => { d.tcOut = playheadTc(view); $('span-out').value = d.tcOut; } }),
            btn('Apply trim', { key: 'lib:trim:' + s.id, cls: 'ghost', disabled: used, onclick: () => applyTrim(s) }))),
        splitMergeBlock(s, view, d, used, next),
        items.length ? el('div', {},
          el('label', { class: 'f', for: 'span-attach', text: 'Attach to a rundown item' }),
          el('select', { id: 'span-attach', 'data-fk': 'span-attach', onchange: (e) => { d.item = e.target.value; } },
            el('option', { value: '' }, 'Choose an item'), items.map((i) => el('option', { value: i.id, selected: d.item === i.id }, i.slug))),
          el('div', { class: 'bar flush' }, btn('Attach', { key: 'lib:attach:' + s.id, cls: 'ghost', disabled: S.seq,
            onclick: () => { const it = items.find((i) => i.id === d.item); if (it) attachSpan(s, it.id, it.slug); else { setFlash({ kind: 'bad', title: 'Choose an item first.', code: 'bad_input', reason: 'Pick the rundown item to attach this span to.' }); render(); } } })),
          s.blockedBy ? el('small', { class: 'no', text: '✕ ' + s.blockedBy + ': attaching works, but this span cannot air or publish until that is fixed on Rights.' }) : null) : el('small', { text: 'No ready rundown item to attach to.' }),
        el('div', { class: 'bar flush' }, btn(S.armed === delKey ? 'Confirm delete' : 'Delete', { key: 'lib:del:' + s.id, cls: 'danger' + (S.armed === delKey ? ' armed' : ''), disabled: used,
          aria: (S.armed === delKey ? 'Confirm delete ' : 'Delete ') + (row ? row.name : short(s.id)), onclick: () => deleteSpan(s) })),
        el('small', { class: used ? 'no' : null, text: used ? 'In use by ' + s.usedBy.items + ' rundown item(s) and ' + s.usedBy.versions + ' version(s): trim and delete are refused. Rename is allowed.' : 'Delete is refused while a rundown item or a version uses the span.' }));
    });
  }
  const playheadTc = (view) => framesToTc(L.pos == null ? view.startF : L.pos, view.rate, view.drop);

  // ---- the timeline: shot segments on top, clips as bars below, one playhead --------------------------------
  function timelineTotal(view) {
    const d = view.d;
    if (d.durationFrames) return d.durationFrames;
    const v = $('lib-video');
    const fromVideo = videoReady() && isFinite(v.duration) ? Math.round(v.duration * view.rate.n / view.rate.d) : 0;
    const fromSpans = view.list.reduce((m, x) => Math.max(m, x.s.frameOut - view.startF), 0);
    return Math.max(fromVideo, fromSpans, 1);
  }

  function place(node, from, to, total) {
    const a = Math.max(0, Math.min(1, from / total)), b = Math.max(a, Math.min(1, to / total));
    node.style.left = (a * 100).toFixed(3) + '%';
    node.style.width = ((b - a) * 100).toFixed(3) + '%';
  }
  function placeHead(frac) { $('lib-head-mark').style.left = (Math.max(0, Math.min(1, frac)) * 100).toFixed(3) + '%'; }

  function renderTimeline(view) {
    const total = timelineTotal(view);
    const box = $('lib-tline');
    const sig = JSON.stringify([view.list.map((x) => [x.s.id, x.s.frameIn, x.s.frameOut, x.s.kind, isSelected(x.s.id)]), total]);
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    const head = $('lib-head-mark');
    const nodes = [];
    let n = 0;
    for (const x of view.list) {
      const s = x.s;
      if (s.kind === 'shot') {
        const seg = el('span', { class: 'seg' + (isSelected(s.id) ? ' on' : '') }, el('i', { text: String(++n) }));
        place(seg, s.frameIn - view.startF, s.frameOut - view.startF, total);
        nodes.push(seg);
      } else {
        const bar = el('span', { class: 'clipbar' + (isSelected(s.id) ? ' on' : '') });
        place(bar, s.frameIn - view.startF, s.frameOut - view.startF, total);
        nodes.push(bar);
      }
    }
    const shots = view.list.filter((x) => x.s.kind === 'shot').length, clips = view.list.length - shots;
    box.setAttribute('aria-label', 'Timeline of ' + shots + ' shot' + (shots === 1 ? '' : 's') + ' and ' + clips + ' clip' + (clips === 1 ? '' : 's') + '. ' + L.sel.filter((e) => e.assetId === L.assetId).length + ' selected.');
    box.replaceChildren(...nodes, head);
    $('lib-scale').replaceChildren(...[0, 0.5, 1].map((f) => el('span', { text: framesToTc(view.startF + Math.round(total * f), view.rate, view.drop) })));
  }

  // ---- dialogs, keys, screens ----------------------------------------------
  function openToken() { const d = $('token-dlg'); if (!d.open) { d.showModal(); $('token-in').focus(); } }
  function anyDialog() { return $('token-dlg').open || $('keys-dlg').open; }

  function setScreen(name) {
    const was = S.screen;
    S.screen = SCREENS.includes(name) ? name : 'show';
    for (const s of SCREENS) $('screen-' + s).hidden = s !== S.screen;
    S.sigs = {};
    if (was === 'library' && S.screen !== 'library') libLeave();
    if (S.screen === 'library' && was !== 'library') libEnter();
    render();
  }

  function onKey(e) {
    if (e.ctrlKey || e.metaKey || e.altKey || anyDialog()) return;
    const t = e.target;
    if (t && t.matches && t.matches('input,select,textarea,[contenteditable]')) return;
    if (e.key === '?') { $('keys-dlg').showModal(); return; }
    if (e.key >= '1' && e.key <= String(SCREENS.length)) { location.hash = '#' + SCREENS[+e.key - 1]; return; }
    if (S.screen === 'library') { libKey(e); return; }   // its own keys; Space is never TAKE here
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
    libInit();
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
