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

  function deriveShow(rundown) {
    const items = rundown && rundown.items ? rundown.items : [];
    const taking = items.find((i) => i.status === 'taking') || null;
    const air = taking || items.find((i) => i.status === 'on air') || null;
    const floor = air ? items.indexOf(air) : -1;
    const next = taking ? null : (items.find((i, idx) => idx > floor && i.status === 'ready') || null);
    return { items, air, next, hold: rundown ? rundown.hold : null, taking };
  }

  // ---- rundown timing: front time (when an item starts) and drift (actual start minus planned front) ----------
  // Planned length of an item in seconds from the server's frame count and exact rate; null when either is unknown.
  function itemSeconds(media) {
    const r = media && media.frames != null ? parseRate(media.rate) : null;
    return r ? media.frames * r.d / r.n : null;
  }
  // A killed or skipped item takes no time, so it is left out of the running sum. An item of unknown length adds zero.
  // The clock is anchored on the first counted item that has started; before that only offsets are known.
  function frontTimes(items, seconds, nowMs) {
    let run = 0;
    const rows = items.map((it) => {
      if (it.status === 'killed' || it.status === 'skipped') return { id: it.id, offset: null, startMs: null };
      const row = { id: it.id, offset: run, startMs: null };
      run += seconds[it.id] || 0;
      return row;
    });
    const at = (it) => (it.asRunIn ? Date.parse(it.asRunIn) : NaN);
    const k = items.findIndex((it, i) => rows[i].offset !== null && !isNaN(at(it)));
    let drift = null;
    if (k >= 0) {
      const anchor = at(items[k]) - rows[k].offset * 1000;
      rows.forEach((r) => { if (r.offset !== null) r.startMs = anchor + r.offset * 1000; });
      const cur = items.findIndex((it, i) => rows[i].offset !== null && (it.status === 'on air' || it.status === 'taking') && !isNaN(at(it)));
      if (cur >= 0) drift = Math.round((at(items[cur]) - rows[cur].startMs) / 1000);
    }
    return { rows, totalSec: run, driftSec: drift };
  }
  function fmtOffset(sec) {
    if (sec == null || isNaN(sec)) return '--:--';
    const t = Math.max(0, Math.round(sec)), h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), x = t % 60;
    const two = (n) => String(n).padStart(2, '0');
    return h ? h + ':' + two(m) + ':' + two(x) : two(m) + ':' + two(x);
  }
  function fmtSigned(sec) {
    if (sec == null || isNaN(sec)) return '';
    const t = Math.round(sec);
    return t === 0 ? '00:00' : (t > 0 ? '+' : '-') + fmtOffset(Math.abs(t));
  }

  // The state language: one mapping, always icon + word + colour. Take is an action, not a state, so it is not here.
  const STATE = {
    onair:   { icon: 'onair',   word: 'On air',  cls: 'st air' },
    ready:   { icon: 'ready',   word: 'Ready',   cls: 'st ok' },
    pending: { icon: 'pending', word: 'Pending', cls: 'st pend' },
    failed:  { icon: 'alert',   word: 'Failed',  cls: 'st fail' },
    locked:  { icon: 'lock',    word: 'Held',    cls: 'st hold' },
    next:    { icon: 'take',    word: 'Next',    cls: 'st next' },
    taking:  { icon: 'pending', word: 'Taking',  cls: 'st pend' },
    aired:   { icon: 'ready',   word: 'Aired',   cls: 'st done' },
    skipped: { icon: 'skip',    word: 'Skipped', cls: 'st done' },
    killed:  { icon: 'close',   word: 'Killed',  cls: 'st done' },
    cleared:   { icon: 'ready',   word: 'Cleared',     cls: 'st ok' },
    uncleared: { icon: 'alert',   word: 'Not cleared', cls: 'st fail' },
    hold:      { icon: 'lock',    word: 'Legal hold',  cls: 'st fail' },
    running:   { icon: 'pending', word: 'Running',     cls: 'st pend' },
    done:      { icon: 'ready',   word: 'Done',        cls: 'st ok' },
    queued:    { icon: 'pending', word: 'Queued',      cls: 'st pend' },


  };
  // A rundown item's status and a cue's status, in the state language. Next is the first ready item after the one on air.
  const itemStateKind = (status, isNext) => (isNext ? 'next' : status === 'on air' ? 'onair' : status);
  const CUE_STATE = { fired: ['ready', 'Fired'], failed: ['failed', 'Failed'], armed: ['pending', 'Armed'], stored: ['pending', 'Stored'], unsupported: ['locked', 'Unsupported'] };
  const cueStateKind = (status) => CUE_STATE[status] || [status, String(status)];
  // A kind this page does not know (a future server state) renders its own word with the neutral pending icon.
  function stateChipSpec(kind, word) {
    const known = Object.prototype.hasOwnProperty.call(STATE, kind) ? STATE[kind] : null;
    return known ? { icon: known.icon, word: word || known.word, cls: known.cls }
      : { icon: STATE.pending.icon, word: word || String(kind), cls: 'st' };
  }

  // Workspaces are saved layouts (which screens the rail shows), not permissions: the server still decides what a user may do.
  const WORKSPACES = {
    producer: ['show', 'rights', 'publish', 'library'],
    editor:   ['library', 'publish', 'rights'],
    operator: ['show', 'record'],
  };
  const DEFAULT_WORKSPACE = 'operator';
  const ALL_SCREENS = ['show', 'record', 'rights', 'publish', 'library'];
  const workspaceName = (stored) => (typeof stored === 'string' && Object.prototype.hasOwnProperty.call(WORKSPACES, stored) ? stored : DEFAULT_WORKSPACE);
  // A hash naming a screen outside the rail keeps the workspace; that screen rides along as the one temporary extra.
  function railFor(workspace, screen) {
    const screens = WORKSPACES[workspaceName(workspace)].slice();
    return { screens, extra: ALL_SCREENS.includes(screen) && !screens.includes(screen) ? screen : null };
  }
  const defaultScreen = (workspace) => WORKSPACES[workspaceName(workspace)][0];

  if (typeof document === 'undefined') {   // loaded by a test under node: export the pure functions and stop
    if (typeof module !== 'undefined') module.exports = { parseRate, framesToTc, tcToFrames, isDropLabel, lengthTc, fmtRate, deriveShow, STATE, stateChipSpec, WORKSPACES, DEFAULT_WORKSPACE, workspaceName, railFor, defaultScreen, itemStateKind, cueStateKind, itemSeconds, frontTimes, fmtOffset, fmtSigned };
    return;
  }
  // ---- endpoints: the only paths this page calls. A test checks each exists on the server.
  const EP = {
    state:       ['GET',  '/api/state'],
    health:      ['GET',  '/api/health'],
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
    mediaTicket: ['GET',  '/api/media-ticket'],
    spanCreate:  ['POST', '/api/assets/{id}/spans'],
    spanTrim:    ['POST', '/api/spans/{id}/trim'],
    spanLabel:   ['POST', '/api/spans/{id}/label'],
    spanDelete:  ['POST', '/api/spans/{id}/delete'],
    spanSplit:   ['POST', '/api/spans/{id}/split'],
    spanMerge:   ['POST', '/api/spans/{id}/merge'],
    detect:      ['POST', '/api/assets/{id}/shots/detect'],
    spanFrames:  ['POST', '/api/assets/{id}/span-frames'],
    exportNle:   ['POST', '/api/exports/nle'],
    showCreate:  ['POST', '/api/shows'],
    itemAdd:     ['POST', '/api/shows/{id}/items'],
    itemRename:  ['POST', '/api/items/{id}/rename'],
    itemRemove:  ['POST', '/api/items/{id}/remove'],
    itemDetach:  ['POST', '/api/items/{id}/detach'],
    cueAdd:      ['POST', '/api/items/{id}/cues'],
    cueRemove:   ['POST', '/api/cues/{id}/remove'],
    cueCommands: ['GET',  '/api/cue-commands'],
    ingest:      ['POST', '/api/ingest'],
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
    forbidden_role: 'Admin only',
    too_large: 'Too large', internal: 'Server error',
    overlaps_shot: 'Shots overlap', span_in_use: 'Span in use', bad_timecode: 'Bad timecode',
    outside_source: 'Outside the source', no_proxy: 'No proxy', not_deletable: 'Cannot delete',
    not_editable: 'Cannot edit', conflict: 'Try again', thumb_failed: 'Thumbnail failed',
    thumb_timeout: 'Thumbnail timed out', bad_range: 'Bad range',
    detect_busy: 'Detection already running', essence_open: 'Still recording', not_adjacent: 'Not neighbours',
    indexing: 'Still indexing', engine_missing: 'ffmpeg missing',
    stale_order: 'The list changed', order_locked: 'Order is locked', not_removable: 'Cannot remove',
    id_conflict: 'Id already used', bad_command: 'Unknown command', cue_fired: 'Cue already fired',
    path_not_allowed: 'Path not allowed', ingest_disabled: 'Ingest is switched off', disk_low: 'Disk too full',
    taking: 'Taking',
  };
  // What each ingest refusal means for the operator, in words. The server's own reason is shown beside it.
  const INGEST_WHY = {
    path_not_allowed: 'That file is not inside a folder this server may ingest from. Move it into an allowed folder, or ask whoever runs the server which folders are allowed (VOLT_INGEST_ROOTS).',
    ingest_disabled: 'The server has no ingest folder configured, so ingest is off. Whoever runs the server sets VOLT_INGEST_ROOTS to turn it on.',
    not_found: 'Nothing was read: there is no file at that path (a folder does not count).',
    bad_input: 'Type the full path to one file, starting at the drive letter or the root.',
    engine_missing: 'ffprobe is not installed on the server, so the file cannot be checked.',
    disk_low: 'The server’s disk is too full to take a copy. Free space there, then try again.',
    unauthorized: 'A token is needed. Press Token at the top, then try again.',
    network: 'The server did not answer. The file may or may not have been taken in; check the asset list before trying again.',
  };
  const CUE_LABEL = {
    'overlay.take': 'Lower third', 'stream.start': 'Start stream', 'show-input.take': 'Input to program',
    'record.start': 'Start record', 'record.arm': 'Arm record', 'audio.snapshot': 'Audio snapshot',
  };
  const REC_STEPS = ['requested', 'preparing', 'producing', 'stopping', 'finalizing', 'completed'];
  const REC_ALIAS = { starting: 'preparing', live: 'producing' };

  const S = {
    data: null, show: null, screen: 'show', epoch: 0, seq: false, busy: false,
    pending: new Set(), lastOk: 0, conn: 'connecting', selected: null, armed: null,
    flash: null, cueDetail: {}, cueLast: {}, lastTake: 0, pubFilter: '', sigs: {},
    cmds: null, cmdsTried: 0, ws: DEFAULT_WORKSPACE,
    // Show building drafts: what is typed but not yet sent. The server's answer replaces the screen, never the draft.
    build: { title: '', slug: '', renameId: null, rename: '', pick: '', cmd: 'overlay.take', payload: '' },
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
  // An icon from the sprite in index.html. Built through a <template> so no namespace URL appears in this file.
  function icon(name, cls) {
    const t = document.createElement('template');
    t.innerHTML = '<svg class="ic' + (cls ? ' ' + cls : '') + '" aria-hidden="true" focusable="false"><use href="#i-' + name + '"/></svg>';
    return t.content.firstElementChild;
  }
  function stateChip(kind, word) {
    const spec = stateChipSpec(kind, word);
    return el('span', { class: spec.cls }, icon(spec.icon, 's'), el('span', { text: spec.word }));
  }
  const short = (id) => (id || '').slice(0, 8);

  function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return String(iso);
    const t = d.toLocaleTimeString([], { hour12: false });
    const ms = /\.(\d{3})Z?$/.exec(String(iso));   // the server's as-run is to the millisecond; older rows are whole seconds
    return ms ? t + '.' + ms[1] : t;
  }

  // ---- the one place that knows the /rundown shape: {hold, items} ----------
  function parseRundown(raw) {
    const r = raw || {};
    const hold = r.hold ? { reason: r.hold.reason || '', heldAt: r.hold.heldAt || '' } : null;
    const items = (r.items || []).map((i) => ({
      id: i.id, slug: i.slug, status: i.status, urls: i.urls || [],
      asRunIn: i.asRunInPrecise || i.asRunIn || null, asRunOut: i.asRunOutPrecise || i.asRunOut || null,
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

  function ticketFrom(url) {
    try { return new URL(url, location.origin).searchParams.get('ticket') || ''; } catch (e) { return ''; }
  }

  async function fetchMediaTicket(asset, kind) {
    const { status, data } = await call('mediaTicket', null, null, { asset, kind });
    if (status === 404) return { missing: true };
    if (status !== 200 || !data || !data.url) {
      const e = codeOf(data, status);
      throw failure(e.code, e.reason);
    }
    return { url: data.url, expires: data.expires, ticket: ticketFrom(data.url) };
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

  // Show who the token belongs to on the Token button. Best effort: an older server has no /api/whoami.
  let whoamiFor = null;
  async function showWhoami() {
    const token = getToken();
    if (!token) { whoamiFor = null; $('token-btn').textContent = 'Token'; $('who').hidden = true; return; }
    if (whoamiFor === token) return;
    whoamiFor = token;
    try {
      const res = await fetch('/api/whoami', { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store' });
      const who = res.ok ? await res.json() : null;
      $('token-btn').textContent = who && who.name ? who.name + ' (' + who.role + ')' : 'Token';
      // The pill names the person and their access level; it stays hidden when the server does not say.
      const pill = $('who');
      pill.hidden = !(who && who.name);
      if (!pill.hidden) pill.replaceChildren(icon('user', 's'), el('span', { text: who.name + ' · ' + who.role }));
    } catch (e) { /* leave the button as it is */ }
  }

  // The footer names the server's version once, from the health report. Best effort: the footer stays blank without it.
  let versionShown = false;
  async function showVersion() {
    if (versionShown) return;
    versionShown = true;
    try {
      const { data } = await call('health');
      if (data && data.version) $('foot-version').textContent = 'v' + data.version;
    } catch (e) { versionShown = false; }
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
      showWhoami();
      showVersion();
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
      if (data && data.orderVersion) S.data.orderVersion = data.orderVersion;
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
    return deriveShow(S.data && S.data.rundown);
  }
  function cuesOf(id) { return (S.data && S.data.cues && S.data.cues[id]) || []; }
  function mediaOf(id) { return (S.data && S.data.media && S.data.media[id]) || {}; }

  function takeBlock() {
    const { next, hold, taking } = showInfo();
    if (!S.data || !S.data.show) return 'No show is open.';
    if (S.conn !== 'ok') return 'No live connection to the server.';
    if (taking) return 'An item is taking. Wait until it is on air.';
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
    // `version` makes the move conditional on the list this screen shows: if another operator changed it, the server refuses.
    act('reorder', S.show, { items: ids, version: S.data.orderVersion || undefined }, { key: 'move:' + id, okTitle: 'Reordered.', ok: () => 'New order saved.',
      hint: 'Nothing moved. The rundown shown is now the server’s current order; press the arrow again if you still want the move.' });
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

  // The rights state of an asset: cleared or not, and legal hold when it is set. Both can show at once.
  function assetChips(d) {
    return [stateChip(d.cleared ? 'cleared' : 'uncleared'), d.legalHold ? stateChip('hold') : null];
  }

  function cueChip(status) {
    const [kind, word] = cueStateKind(status);
    return stateChip(kind, word);
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
    if (S.screen === 'library') renderWordsTab();
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
    const rail = railFor(S.ws, S.screen);
    for (const a of document.querySelectorAll('#screens a')) {
      const scr = a.dataset.screen;
      a.hidden = !rail.screens.includes(scr) && scr !== rail.extra;
      a.classList.toggle('off-rail', scr === rail.extra);
      if (scr === S.screen) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    }
    const items = S.data && S.data.rundown && S.data.rundown.items ? S.data.rundown.items : [];
    const show = shows.find((x) => x.id === S.show);
    $('foot-show').textContent = show ? show.title : 'No show';
    $('foot-count').textContent = S.data ? items.length + (items.length === 1 ? ' item' : ' items') : '';
  }

  // The workspace is remembered per browser. Storage can be missing, blocked or hold junk: any of that means Operator.
  function storedWorkspace() {
    try { return workspaceName(localStorage.getItem('volt.workspace')); } catch (e) { return DEFAULT_WORKSPACE; }
  }
  function setWorkspace(name, go) {
    S.ws = workspaceName(name);
    try { localStorage.setItem('volt.workspace', S.ws); } catch (e) { /* works without it */ }
    for (const w of Object.keys(WORKSPACES)) document.body.classList.toggle('ws-' + w, w === S.ws);
    for (const b of document.querySelectorAll('#workspaces button')) b.setAttribute('aria-pressed', String(b.dataset.ws === S.ws));
    // An explicit switch lands on the new workspace's first screen unless the screen already open belongs to it.
    if (go && !railFor(S.ws, S.screen).screens.includes(S.screen)) { location.hash = '#' + defaultScreen(S.ws); return; }
    render();
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
    const { items, air, next, hold, taking } = showInfo();
    const block = takeBlock();
    const takeCues = next ? cuesOf(next.id).length : 0;
    const takeBusy = S.pending.has('take');
    paint('transport', [air, next, hold, taking, block, S.seq, [...S.pending], takeCues, S.data.show], () => [
      el('div', { class: 'nowline air' }, el('span', { class: 'lbl', text: taking ? 'Taking' : 'On air' }),
        el('span', { class: 'val', text: taking ? '… ' + taking.slug : (air ? '● ' + air.slug : 'Nothing on air') })),
      el('div', { class: 'nowline next' }, el('span', { class: 'lbl', text: 'Next' }),
        el('span', { class: 'val', text: next ? '▶ ' + next.slug : 'End of show' })),
      el('span', { class: 'spacer' }),
      el('div', {}, btn(
        [el('span', { class: 'tk' }, icon('take'), takeBusy ? 'TAKING…' : 'TAKE'),
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
    const seconds = {};
    for (const it of items) seconds[it.id] = itemSeconds(mediaOf(it.id));
    const timing = frontTimes(items, seconds, Date.now());
    paint('rundown-sum', [S.data.show && 1, items.length, timing.totalSec, timing.driftSec], () => {
      if (!S.data.show || !items.length) return null;
      const d = timing.driftSec;
      return [el('span', { text: 'Total ' + fmtOffset(timing.totalSec) + ' · ' + items.length + (items.length === 1 ? ' item' : ' items') }),
        d == null ? null : el('span', { class: 'drift' + (d > 0 ? ' behind' : '') }, icon(d > 0 ? 'alert' : 'ready', 's'),
          'Drift ' + fmtSigned(d) + (d > 0 ? ' behind' : d < 0 ? ' ahead' : ' on time'))];
    });
    paint('rundown', [items, S.data.media, S.selected, S.armed, [...S.pending], S.seq, timing], () => {
      if (!S.data.show) return el('p', { class: 'empty', text: 'No show yet. Use New show below.' });
      if (!items.length) return el('p', { class: 'empty', text: 'This show has no items.' });
      return [el('div', { class: 'rhead', 'aria-hidden': 'true' }, el('span', { text: '#' }), el('span', { text: 'State' }), el('span', { text: 'Item' }),
          el('span', { text: 'Length' }), el('span', { text: 'Front' }), el('span')),
        el('ol', { class: 'list' }, items.map((it, idx) => rundownRow(it, idx, items, air, next, timing.rows[idx], seconds[it.id])))];
    });

    const sItem = items.find((i) => i.id === S.selected);
    $('h-cues-text').textContent = sItem ? 'Cues on ' + sItem.slug : 'Cues';
    paint('cues', [S.selected, S.data.cues, S.cueDetail, S.cueLast, sItem && sItem.status, [...S.pending]], () => {
      if (!sItem) return el('p', { class: 'empty', text: 'Select a rundown row.' });
      const cs = cuesOf(sItem.id);
      if (!cs.length) return el('p', { class: 'empty', text: 'No cues on this item. Taking it only changes the rundown.' });
      return [cs.map((c) => el('div', { class: 'cue' },
        el('div', {}, el('span', { class: 'cmd', text: CUE_LABEL[c.command] || c.command }),
          el('small', { text: c.command + (c.payload ? ' · ' + c.payload : '') + (c.status !== 'fired' && (S.cueDetail[c.id] || (c.failure && c.failure.detail)) ? ' · last take: ' + (S.cueDetail[c.id] || c.failure.detail) : '') })),
        cueChip(c.status !== 'fired' && S.cueLast[c.id] && S.cueLast[c.id] !== 'fired' ? S.cueLast[c.id] : (c.status !== 'fired' && c.failure ? c.failure.status : c.status)),
        sItem.status === 'ready' && c.status !== 'fired' ? btn('Remove', { key: 'cue-rm:' + c.id, cls: 'ghost small', aria: 'Remove cue ' + (CUE_LABEL[c.command] || c.command),
          onclick: () => act('cueRemove', c.id, {}, { key: 'cue-rm:' + c.id, okTitle: 'Cue removed.', ok: () => '' }) }) : null)),
        el('p', { class: 'note', text: 'Take resumes at the first cue that did not fire. Fired cues are not sent twice.' })];
    });

    paint('room', S.data.room, () => roomPanel(S.data.room));
    renderBuild(sItem);
  }

  // ---- show building: new show, add / rename / remove an item, attach a clip, add / remove a cue ---------------
  // Every rule is the server's (volt/building.py): an item that is on air or has aired cannot be edited, a reorder
  // that quotes an old list is refused. This section sends the request and shows what came back.
  function loadCmds() {
    if (S.cmds || Date.now() - S.cmdsTried < 10000) return;
    S.cmdsTried = Date.now();
    call('cueCommands').then((r) => { if (r.data && r.data.ok) { S.cmds = r.data.commands; S.sigs['cue-add'] = null; render(); } }).catch(() => {});
  }
  const cmdInfo = (command) => (S.cmds || []).find((c) => c.command === command) || null;

  function formRow(label, id, input) {
    return [el('label', { class: 'f', for: id, text: label }), input];
  }
  function textIn(id, draftKey, o) {
    return el('input', Object.assign({ id, type: 'text', 'data-fk': id, value: S.build[draftKey], autocomplete: 'off',
      oninput: (e) => { S.build[draftKey] = e.target.value; } }, o || {}));
  }
  function needName(what) {
    setFlash({ kind: 'bad', title: 'Name the ' + what + ' first.', code: 'bad_input', reason: 'A ' + what + ' needs a name.' });
    render();
  }

  function createShow() {
    const title = S.build.title.trim();
    if (!title) { needName('show'); return; }
    S.build.title = '';
    act('showCreate', '', { title }, { key: 'show-create', okTitle: 'Show created.', ok: (d) => {
      // The new show becomes the open one, the way choosing it in the header would. Runs before act's own refresh.
      S.show = d.show.id; S.selected = null; S.sigs = {};
      try { sessionStorage.setItem('volt.show', S.show); } catch (x) { /* ignore */ }
      return '"' + d.show.title + '" is open and has no items yet.';
    } });
  }
  function addItem() {
    const slug = S.build.slug.trim();
    if (!slug) { needName('item'); return; }
    S.build.slug = '';
    act('itemAdd', S.show, { slug }, { key: 'item-add', okTitle: 'Item added.', ok: () => '"' + slug + '" is last in the rundown.' });
  }
  function renameItem(it) {
    const slug = S.build.rename.trim();
    if (!slug) { needName('item'); return; }
    act('itemRename', it.id, { slug }, { key: 'item-rename', okTitle: 'Renamed.', ok: () => 'Now "' + slug + '".' });
  }
  function removeItem(it) {
    armedAction('remove:' + it.id, () => act('itemRemove', it.id, {}, { key: 'item-remove', okTitle: 'Removed.',
      ok: () => '"' + it.slug + '" is gone from the rundown. Kill keeps an item in the list instead.' }));
  }
  function attachFromSelection(it) {
    const e = (typeof L !== 'undefined' ? L.sel : []).find((x) => x.id === S.build.pick);
    if (!e) {
      setFlash({ kind: 'bad', title: 'Choose a clip first.', code: 'bad_input', reason: 'Tick clips on the Library screen, then pick one here.' });
      render();
      return;
    }
    act('attach', it.id, { span: e.id }, { key: 'item-attach', okTitle: 'Attached.', ok: () => e.name + ' is on "' + it.slug + '".' });
  }
  function addCue(it) {
    const payload = S.build.payload;
    S.build.payload = '';
    act('cueAdd', it.id, { command: S.build.cmd, payload }, { key: 'cue-add', okTitle: 'Cue added.',
      ok: (d) => (CUE_LABEL[d.cue.command] || d.cue.command) + ' is on "' + it.slug + '".' + (d.warning ? ' ' + d.warning : '') });
  }

  function renderBuild(sItem) {
    loadCmds();
    const b = S.build;
    const editable = !!sItem && sItem.status === 'ready';
    if (sItem && b.renameId !== sItem.id) { b.renameId = sItem.id; b.rename = sItem.slug; }
    const sel = typeof L !== 'undefined' ? L.sel : [];
    const m = sItem ? mediaOf(sItem.id) : {};
    const pending = [...S.pending];
    const rmKey = sItem ? 'remove:' + sItem.id : '';
    paint('build', [S.data.show, sItem, m.span, sel.map((e) => e.id + e.name), pending, S.armed, b.pick], () => [
      el('div', { class: 'body' },
        el('form', { 'aria-label': 'Add an item', onsubmit: (e) => { e.preventDefault(); addItem(); } },
          formRow('New item', 'build-slug', textIn('build-slug', 'slug', { maxlength: '200', disabled: !S.data.show })),
          el('div', { class: 'bar flush' }, btn('Add item', { key: 'item-add', cls: 'ghost', disabled: !S.data.show, onclick: addItem }))),
        sItem ? el('fieldset', { class: 'grp' }, el('legend', { text: 'Edit ' + sItem.slug }),
          editable ? [
            el('form', { 'aria-label': 'Rename the selected item', onsubmit: (e) => { e.preventDefault(); renameItem(sItem); } },
              formRow('Name', 'build-rename', textIn('build-rename', 'rename', { maxlength: '200' })),
              el('div', { class: 'bar flush' }, btn('Rename', { key: 'item-rename', cls: 'ghost', onclick: () => renameItem(sItem) }))),
            formRow('Clip from the Library selection', 'build-pick', el('select', { id: 'build-pick', 'data-fk': 'build-pick', onchange: (e) => { b.pick = e.target.value; } },
              el('option', { value: '' }, sel.length ? 'Choose a clip' : 'Nothing selected in the Library'),
              sel.map((e) => el('option', { value: e.id, selected: b.pick === e.id }, e.name + ' · ' + e.tcIn)))),
            el('div', { class: 'bar flush' },
              btn('Attach clip', { key: 'item-attach', cls: 'ghost', disabled: !sel.length, onclick: () => attachFromSelection(sItem) }),
              m.span ? btn('Detach clip', { key: 'item-detach', cls: 'ghost', aria: 'Detach the clip from ' + sItem.slug,
                onclick: () => act('itemDetach', sItem.id, {}, { key: 'item-detach', okTitle: 'Detached.', ok: () => 'The item has no media now.' }) }) : null,
              btn(S.armed === rmKey ? 'Confirm remove' : 'Remove item', { key: 'item-remove', cls: 'danger' + (S.armed === rmKey ? ' armed' : ''),
                aria: (S.armed === rmKey ? 'Confirm remove ' : 'Remove ') + sItem.slug, onclick: () => removeItem(sItem) })),
            el('small', { text: m.span ? 'Clip ' + (m.tcIn || '') + ' to ' + (m.tcOut || '') + '.' : 'No clip yet: this item cannot be taken until one is attached.' }),
          ] : el('small', { text: 'This item is ' + sItem.status + ' and cannot be edited.' })) : null,
        el('details', { class: 'newshow' }, el('summary', { text: 'New show' }),
          el('form', { 'aria-label': 'Create a show', onsubmit: (e) => { e.preventDefault(); createShow(); } },
            formRow('Show name', 'build-title', textIn('build-title', 'title', { maxlength: '200' })),
            el('div', { class: 'bar flush' }, btn('Create show', { key: 'show-create', cls: 'ghost', onclick: createShow }))))),
    ]);

    paint('cue-add', [sItem && sItem.id, editable, S.cmds, b.cmd, pending], () => {
      if (!editable) return [];
      const cmds = S.cmds || [];
      const info = cmdInfo(b.cmd);
      return el('div', { class: 'body' }, el('form', { 'aria-label': 'Add a cue to ' + sItem.slug, onsubmit: (e) => { e.preventDefault(); addCue(sItem); } },
        formRow('Add a cue', 'cue-cmd', el('select', { id: 'cue-cmd', 'data-fk': 'cue-cmd', disabled: !cmds.length,
          onchange: (e) => { b.cmd = e.target.value; S.sigs['cue-add'] = null; render(); } },
          cmds.length ? cmds.map((c) => el('option', { value: c.command, selected: c.command === b.cmd },
            (CUE_LABEL[c.command] || c.command) + (c.verified ? '' : (c.adapter ? ' (not live-proven)' : ' (no verified adapter)')))) : el('option', { value: '' }, 'Loading commands'))),
        formRow('Payload (optional)', 'cue-payload', textIn('cue-payload', 'payload', { maxlength: '2000' })),
        info && !info.verified ? el('small', { class: 'no', text: info.adapter
          ? '? ' + info.command + ' has an adapter, but no live Program take is recorded yet. The cue is stored and a take will run it.'
          : '? No verified adapter for ' + info.command + ' yet. The cue is stored, but a take stops at it.' }) : null,
        el('div', { class: 'bar flush' }, btn('Add cue', { key: 'cue-add', cls: 'ghost', disabled: !cmds.length, onclick: () => addCue(sItem) }))));
    });
  }

  function rundownRow(it, idx, items, air, next, tm, secs) {
    const m = mediaOf(it.id);
    const isNext = next && next.id === it.id;
    const meta = [];
    if (m.tcIn) meta.push('clip ' + m.tcIn + ' to ' + m.tcOut);
    if (it.asRunIn) meta.push('as-run ' + fmtTime(it.asRunIn) + ' to ' + (it.asRunOut ? fmtTime(it.asRunOut) : 'now'));
    const blocked = (it.status === 'ready' || it.status === 'on air') && m.blockedBy && m.blockedBy !== 'ok' ? m.blockedBy : null;
    const ready = it.status === 'ready';
    const cls = 'rrow' + (it.status === 'on air' || it.status === 'taking' ? ' is-air' : isNext ? ' is-next' : '') + (S.selected === it.id ? ' sel' : '');
    const prev = items[idx - 1];
    const canUp = ready && prev && prev.status !== 'on air' && prev.status !== 'aired';
    const canDown = ready && idx < items.length - 1;
    const killKey = 'kill:' + it.id;
    const front = tm && tm.startMs != null ? new Date(tm.startMs).toLocaleTimeString([], { hour12: false }) : tm && tm.offset != null ? 'T+' + fmtOffset(tm.offset) : '--:--';
    return el('li', { class: cls },
      el('span', { class: 'n', 'aria-hidden': 'true', text: String(idx + 1) }),
      stateChip(itemStateKind(it.status, isNext)),
      el('button', { type: 'button', class: 'pick', 'data-fk': 'pick:' + it.id, 'aria-current': S.selected === it.id ? 'true' : null,
          onclick: () => { S.selected = it.id; render(); } },
        el('span', { class: 'title' + (it.status === 'skipped' || it.status === 'killed' ? ' struck' : ''), text: it.slug }),
        el('span', { class: 'meta' }, meta.join(' · ') || (it.status === 'ready' ? 'not yet aired' : ''),
          blocked ? el('span', { class: 'bad' }, (meta.length ? ' · ' : '') + '⚠ media ' + blocked) : null)),
      el('span', { class: 'dur mono', text: tm && tm.offset == null ? '--:--' : fmtOffset(secs) }),
      el('span', { class: 'front mono', text: front }),
      el('span', { class: 'tools' },
        ready ? [
          btn('↑', { key: 'up:' + it.id, cls: 'ghost small', aria: 'Move ' + it.slug + ' up', title: 'Move up', disabled: !canUp || S.seq, onclick: () => move(it.id, -1) }),
          btn('↓', { key: 'down:' + it.id, cls: 'ghost small', aria: 'Move ' + it.slug + ' down', title: 'Move down', disabled: !canDown || S.seq, onclick: () => move(it.id, 1) }),
          btn('Skip', { key: 'skip-b:' + it.id, cls: 'ghost small', aria: 'Skip ' + it.slug, disabled: S.seq, onclick: () => skipItem(it.id) }),
          el('span', { class: 'sep', 'aria-hidden': 'true' }),
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
      if (q) return ((s.label || '') + ' ' + (s.text || '') + ' ' + s.id + ' ' + (s.camera || '')).toLowerCase().includes(q);
      return used.has(s.id) || s.versions.length > 0;
    });
    return { spans: spans.slice(0, 60), used, more: Math.max(0, spans.length - 60) };
  }

  function renderPublish() {
    const info = publishList();
    paint('publish', [info.spans, info.more, [...S.pending], S.pubFilter, [...info.used]], () => {
      const head = el('div', { class: 'panel' }, el('div', { class: 'form-row' },
        el('div', {}, el('label', { for: 'pub-filter', text: 'Find a clip or shot by name or words' }),
          el('input', { id: 'pub-filter', type: 'text', value: S.pubFilter, 'data-fk': 'pub-filter', autocomplete: 'off',
            oninput: (e) => { S.pubFilter = e.target.value; renderPublish(); } })),
        el('span', { class: 'hint', text: S.pubFilter ? 'Showing matching clips and shots.' : 'Showing clips and shots in this rundown and those already published. To publish a passage of speech, mark it as a clip in the Library.' })));
      const rows = info.spans.map((s) => {
        const key = 'publish:' + s.id;
        const running = S.pending.has(key);
        const done = s.versions.filter((v) => v.status === 'done').length;
        const anyFail = s.versions.some((v) => v.status === 'failed');
        const label = anyFail ? 'PUBLISH AGAIN' : done === 3 ? 'Publish again' : 'PUBLISH';
        return el('tr', {},
          el('td', {}, el('strong', { text: s.label || s.text || short(s.id) }), el('div', { class: 'muted mono', text: (s.kind === 'shot' ? 'shot · ' : '') + s.tcIn + ' to ' + s.tcOut + (s.camera ? ' · ' + s.camera : '') })),
          el('td', {}, el('div', { class: 'vers' }, KINDS.map((k) => {
            const v = s.versions.find((x) => x.kind === k);
            const url = v && v.url;
            return el('div', { class: 'ver' }, el('span', { text: k }),
              v ? stateChip(v.status) : stateChip('pending', 'Not made'),
              url ? (/^https?:\/\//i.test(url) ? el('a', { class: 'url', href: url, rel: 'noopener', text: url }) : el('span', { class: 'url', text: url })) : el('span', { class: 'url muted', text: '' }));
          })), el('div', { class: 'muted', text: running ? 'Rendering…' : s.versions.length ? done + ' of 3 done' : '' })),
          el('td', {},
            s.blockedBy ? el('div', { class: 'no' }, '✕ Blocked: ' + s.blockedBy, ' ', el('a', { href: '#rights', text: 'Fix on Rights' })) : null,
            btn(running ? [el('span', { class: 'spin', 'aria-hidden': 'true' }), ' RENDERING…'] : label,
              { key, cls: s.blockedBy ? 'ghost' : '', disabled: !!s.blockedBy,
                aria: (running ? 'Rendering' : label) + ' ' + (s.label || s.text || short(s.id)),
                onclick: () => act('publish', s.id, {}, { key, span: s.id, okTitle: 'Published.', ok: (d) => d.versions.length + ' versions done.' }) })));
      });
      return [head, el('section', { class: 'panel', 'aria-labelledby': 'h-clips' }, el('h2', { id: 'h-clips', text: 'Clips' }),
        rows.length ? el('div', { class: 'tblwrap' }, el('table', {},
          el('caption', { class: 'vh', text: 'Clips and their published versions' }),
          el('thead', {}, el('tr', {}, ['Clip', 'Versions', 'Action'].map((h) => el('th', { scope: 'col', text: h })))),
          el('tbody', {}, rows))) : el('p', { class: 'empty', text: S.pubFilter ? 'No clip or shot matches.' : 'No clips or shots in this rundown yet. Mark one in the Library, or attach one to a rundown item.' }),
        info.more ? el('p', { class: 'note', text: info.more + ' more. Type to narrow the list.' }) : null)];
    });
  }

  // ---- Library screen: shots, clips, player, marks, selection -------------------------------------------
  // docs/specs/shot-spans.md (slice 4) and docs/mockups/shot-spans.html. The page shows what the server last
  // returned. Every write goes to the server and the list is read back; nothing is shown optimistically.
  //
  // Media and the token: <img> and <video> cannot send an Authorization header. With a token the page asks
  // /api/media-ticket and puts the returned URL on <video src> / <img src> so Range still works. Loopback with
  // no token keeps a native proxy src. The blob-download path remains only if the ticket route is 404 (old server).
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
      const path = '/api/spans/' + encodeURIComponent(job.id) + '/thumb';
      const show = (url) => {
        if (job.asset !== L.assetId) return;
        T.urls.set(job.key, url);
        for (const n of document.querySelectorAll('img[data-tk="' + CSS.escape(job.key) + '"]')) { n.src = url; n.classList.remove('wait'); }
      };
      const blobThumb = () => fetchBlob(path, { cache: 'reload' }).then((blob) => show(URL.createObjectURL(blob)));
      const start = getToken()
        ? fetchMediaTicket(job.asset, 'thumb').then((t) => {
            if (t.missing) return blobThumb();
            show(path + '?ticket=' + encodeURIComponent(t.ticket));
          })
        : blobThumb();
      start
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
    P.for = null; P.state = 'none'; P.msg = ''; P.pct = 0; P.fallbackBlob = false;
    L.pos = null;
    cancelAnimationFrame(L.raf);
  }

  // Native: let <video> stream and seek by range. Token: a media ticket on the same URL, unless the
  // ticket route is 404 (old server) — then fetch with the header and play from memory.
  async function proxyLoad() {
    const id = L.assetId, P = L.proxy, v = $('lib-video');
    if (!id) return;
    const wantBlob = P.fallbackBlob;
    proxyRelease();
    P.for = id; P.msg = ''; P.fallbackBlob = wantBlob;
    const path = '/api/assets/' + encodeURIComponent(id) + '/proxy';
    if (!getToken()) {
      P.mode = 'native'; P.state = 'loading';
      v.src = path;
      v.load();
      renderVmsg();
      return;
    }
    if (!wantBlob) {
      P.mode = 'native'; P.state = 'loading';
      renderVmsg();
      try {
        const t = await fetchMediaTicket(id, 'proxy');
        if (L.assetId !== id) return;
        if (t.missing) { P.fallbackBlob = true; P.mode = 'blob'; P.state = 'idle'; renderVmsg(); return; }
        v.src = t.url;
        v.load();
      } catch (err) {
        if (L.assetId !== id) return;
        P.state = 'error';
        P.msg = (err.code ? err.code + ': ' : '') + err.message;
        renderVmsg();
      }
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
    if (!L.data || W.tab === 'words') return;   // the transcript tab has its own keys (trKey)
    if (W.tab === 'export') return;             // the export tab has no keys: the player is hidden, so I, O and K have nothing to do
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
  function stateGlyph(kind) {
    const tpl = document.createElement('template');
    const stroke = kind === 'warn' ? '#ff4d4d' : '#a0a0a0';
    const shape = kind === 'warn' ? '<path d="M20 6l15 27H5z"/><path d="M20 16v8M20 28v2"/>'
      : '<rect x="6" y="8" width="28" height="24" rx="3"/><path d="M14 8v24M26 8v24M6 16h8M26 16h8M6 24h8M26 24h8"/>';
    tpl.innerHTML = '<svg width="40" height="40" viewBox="0 0 40 40" fill="none" stroke="' + stroke + '" stroke-width="2" aria-hidden="true" focusable="false">' + shape + '</svg>';
    return tpl.content.firstChild;
  }

  function stateCard(title, kind, text, ...actions) {
    return el('section', { class: 'panel state-card', 'aria-labelledby': 'h-state' }, el('h2', { id: 'h-state', text: title }),
      el('div', { class: 'empty' }, el('div', { 'aria-hidden': 'true' }, stateGlyph(kind)), el('p', { text }), actions.length ? el('div', { class: 'acts' }, actions) : null));
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

  // ---- ingest: one file from a folder the server allows ----------------------------------------------------
  // The server reads the file in the request (probe and checksum), so the button shows busy until it answers.
  // The follow-on jobs are then read from the existing state poll (assets[].jobs lists the unfinished ones).
  const I = { path: '', busy: false, result: null, err: null };
  const JOB_NAME = { ingest: 'Copy and check', proxy: 'Proxy', asr: 'Transcript', shots: 'Shot detection' };

  async function ingestNow() {
    const path = I.path.trim();
    if (I.busy) return;
    I.result = null;
    if (!path) { I.err = { code: 'bad_input', reason: 'Enter the path of a file first.', local: true }; render(); return; }
    I.busy = true; I.err = null; S.epoch++; render();
    try {
      const { status, data } = await call('ingest', '', { path });
      if (status === 401) { setConn('token'); openToken(); }
      if (data && data.ok) { I.result = data; I.path = ''; announce('Ingested. Asset ' + short(data.asset) + '.'); }
      else {
        const e = codeOf(data, status);
        I.err = { code: e.code, reason: e.reason };
        announce((ERR_LABEL[e.code] || 'Refused') + '. ' + (INGEST_WHY[e.code] || e.reason));
      }
    } catch (err) {
      I.err = { code: 'network', reason: 'No answer from the server.' };
      setConn('lost');
    } finally {
      I.busy = false; S.epoch++; render(); refresh();
    }
  }

  // The job rows for the asset just ingested: the types the server queued, each with its state from the last poll.
  function ingestJobs() {
    const r = I.result;
    const a = ((S.data && S.data.assets) || []).find((x) => x.id === r.asset);
    const live = {};
    ((a && a.jobs) || []).forEach((j) => { live[j.type] = j; });
    const types = r.jobs.map((j) => j.type);
    Object.keys(live).forEach((t) => { if (!types.includes(t)) types.push(t); });
    return types.map((t) => {
      const first = r.jobs.find((j) => j.type === t);
      const j = live[t] || (first && first.status !== 'done' && !a ? first : null);
      const status = j ? j.status : 'done';
      let note = j && j.error ? j.error : '';
      if (t === 'asr' && status === 'queued' && a && !a.cleared) note = 'Waits until the asset is cleared (Rights screen).';
      if (t === 'proxy' && status === 'queued') note = 'Waits for a runner; the Library cannot play it until it is done.';
      return { type: t, status, note };
    });
  }

  function paintIngest() {
    const r = I.result;
    const a = r && ((S.data && S.data.assets) || []).find((x) => x.id === r.asset);
    paint('lib-ingest', [I.busy, I.err, r, a && [a.jobs, a.cleared, a.hasProxy]], () => {
      const out = [
        el('form', { 'aria-label': 'Ingest a file', autocomplete: 'off', onsubmit: (e) => { e.preventDefault(); ingestNow(); } },
          el('div', { class: 'form-row' },
            el('div', { class: 'grow' }, el('label', { for: 'ingest-path', text: 'Path of the file, as the server sees it' }),
              el('input', { id: 'ingest-path', type: 'text', 'data-fk': 'ingest-path', value: I.path, spellcheck: 'false', maxlength: '4096',
                'aria-describedby': 'ingest-hint', placeholder: '/media/inbox/clip.mov', oninput: (e) => { I.path = e.target.value; } })),
            el('button', { type: 'submit', class: 'btn', 'data-fk': 'ingest-go', 'aria-disabled': I.busy ? 'true' : null }, I.busy ? 'Ingesting…' : 'Ingest')),
          el('p', { id: 'ingest-hint', class: 'note', text: 'Only files inside a folder the server allows can be taken in. The server copies the file, checks it and queues its proxy and transcript.' })),
      ];
      if (I.err) {
        const w = I.err.local ? null : INGEST_WHY[I.err.code];
        out.push(el('div', { class: 'result', role: 'alert' },
          el('p', {}, el('strong', { text: '✕ ' + (ERR_LABEL[I.err.code] || 'Refused') + '.' }), ' ', w || I.err.reason),
          w && I.err.reason && I.err.reason !== w ? el('p', { class: 'hint', text: 'Server says: ' + I.err.reason }) : null,
          el('p', { class: 'hint', text: 'code: ' + I.err.code })));
      } else if (r) {
        const jobs = ingestJobs();
        out.push(el('div', { class: 'result', role: 'status' },
          el('p', {}, el('strong', { text: r.quarantined ? '! Taken in, but the file did not parse.' : '✓ Ingested.' }), ' Asset ',
            el('span', { class: 'mono', text: r.asset }), r.minted ? ' is new.' : ' was already ingested; nothing was copied again.',
            r.quarantined ? ' It is held in quarantine and nothing will air from it.' : ''),
          el('ul', { class: 'jobs', 'aria-label': 'Follow-on work' }, jobs.map((j) => el('li', {},
            el('span', { class: 'jn', text: JOB_NAME[j.type] || j.type }), stateChip(j.status), j.note ? el('span', { class: 'hint', text: j.note }) : null))),
          el('div', { class: 'bar flush' }, el('button', { type: 'button', class: 'btn ghost small', 'data-fk': 'ingest-open',
            onclick: () => { setAsset(r.asset); } }, 'Open this asset below'))));
      }
      return out;
    });
  }

  function renderLibrary() {
    renderLibPicker();
    paintIngest();
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
      assetChips(d),
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
          el('button', { type: 'button', class: 'btn', 'data-fk': 'tray-export', 'aria-describedby': 'lib-export-why', onclick: xOpen }, 'EXPORT TO NLE'),
          el('button', { type: 'button', class: 'btn ghost', 'data-fk': 'tray-clear', onclick: () => { L.sel = []; store('volt.lib.sel', L.sel); render(); } }, 'Clear')),
        el('p', { class: 'note', id: 'lib-export-why', text: 'Opens the Export tab with this selection in this order. Nothing is written until you press Export there. The selection is kept for this tab.' }),
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

  // ---- Library: mark from transcript (slice 5) -----------------------------------------------------------
  // docs/specs/shot-spans.md "From transcript words" and the "Mark from transcript" tab of the mockup.
  // The page picks two WORDS and sends their span IDs (`from_words`) with the handles and the snap choice. It never
  // sends a time: the server reads the word times from the store and applies the rules (one asset, transcript
  // words only, in order, inside the source). The range shown here is a preview computed from the same arithmetic;
  // what the server returns is what the page reports.
  //
  // Words come from GET /api/assets/{id}/transcript, which lists only kind 'transcript' spans, so a word a speech
  // re-run re-kinded to a clip is not on this screen at all. Selection is a cursor index plus an anchor index into
  // that list. Own state, own functions: nothing here touches the shot list's code, and the shot list's keys
  // (libKey) stand down while this tab is showing.
  const W = {
    tab: 'shots', for: null, tok: 0, load: 'idle', err: null, words: [], next: null, pendingFrames: 0,
    nodes: [], shown: null, a: null, c: null, hin: 0, hout: 0, snap: true, name: '', drag: false, wired: false,
  };
  const WORD_PAGE = 1000, WORD_PAGES = 3, HANDLE_MAX = 10000, SENTENCE_END = /[.!?]["')\]]*$/;
  Object.assign(ERR_LABEL, { not_transcript: 'Not transcript words', frames_pending: 'Words not placed yet' });

  function wordsReset() {
    W.for = L.assetId; W.tok++; W.load = 'idle'; W.err = null; W.words = []; W.next = null; W.pendingFrames = 0;
    W.nodes = []; W.shown = null; W.a = W.c = null; W.hin = W.hout = 0; W.name = '';
  }

  async function wordsLoad(more) {
    const id = L.assetId;
    if (!id) return;
    const tok = ++W.tok;
    if (!more) { W.load = 'loading'; W.err = null; W.words = []; W.next = null; W.a = W.c = null; W.shown = null; W.nodes = []; render(); }
    try {
      let after = more ? W.next : null;
      const got = [];
      for (let i = 0; i < (more ? 1 : WORD_PAGES); i++) {
        const q = { limit: String(WORD_PAGE) };
        if (after) q.after = after;
        const { status, data } = await call('transcript', id, null, q);
        if (tok !== W.tok || id !== L.assetId) return;
        if (status === 401) { setConn('token'); openToken(); throw failure('unauthorized', 'A token is needed to read this transcript.'); }
        if (status !== 200 || !data || !data.ok) { const e = codeOf(data, status); throw failure(e.code, e.reason); }
        got.push(...data.words);
        W.pendingFrames = data.pendingFrames || 0;
        after = data.next;
        if (!after) break;
      }
      W.words = W.words.concat(got); W.next = after; W.load = 'ready'; W.err = null;
    } catch (e) {
      if (tok !== W.tok) return;
      if (!more) { W.load = 'error'; W.err = { code: e.code || 'internal', reason: e.message || 'The transcript could not be read.' }; }
      else setFlash({ kind: 'bad', title: 'More words could not be read.', code: e.code || 'internal', reason: e.message || '' });
    }
    render();
  }

  const wordNode = (e) => (e.target && e.target.closest ? e.target.closest('.w') : null);

  function wordsWire() {
    if (W.wired) return;
    W.wired = true;
    $('lib-body').setAttribute('role', 'tabpanel');
    $('lib-body').setAttribute('aria-labelledby', 'lib-tab-shots');
    const tabs = ['shots', 'words', 'export'];
    for (const name of tabs) {
      const b = $('lib-tab-' + name);
      b.addEventListener('click', () => setTab(name, false));
      b.addEventListener('keydown', (e) => {
        const at = tabs.indexOf(name);
        const to = e.key === 'ArrowRight' ? tabs[(at + 1) % tabs.length] : e.key === 'ArrowLeft' ? tabs[(at + tabs.length - 1) % tabs.length] : e.key === 'Home' ? tabs[0] : e.key === 'End' ? tabs[tabs.length - 1] : null;
        if (!to) return;
        e.preventDefault();
        setTab(to, true);
      });
    }
    const tr = $('lib-tr');
    tr.addEventListener('keydown', trKey);
    tr.addEventListener('focus', () => {
      if (W.c == null && W.words.length) { W.c = nearestWord(L.pos); paintSel(); render(); }
    });
    tr.addEventListener('mousedown', (e) => {
      const w = wordNode(e);
      if (!w || e.button) return;
      e.preventDefault();                                   // no native text selection: the selection is ours
      tr.focus({ preventScroll: true });
      const i = +w.dataset.i;
      if (e.shiftKey && (W.a != null || W.c != null)) { if (W.a == null) W.a = W.c; W.c = i; } else { W.a = i; W.c = i; }
      W.drag = true;
      paintSel();
    });
    tr.addEventListener('mouseover', (e) => {
      if (!W.drag) return;
      const w = wordNode(e);
      if (w) { W.c = +w.dataset.i; paintSel(); }
    });
    document.addEventListener('mouseup', () => { if (W.drag) { W.drag = false; announceWord(); render(); } });
    $('lib-w-reload').addEventListener('click', () => wordsLoad(false));
    $('lib-w-more').addEventListener('click', () => wordsLoad(true));
  }

  function setTab(name, focus) {
    if (W.tab === name) return;
    W.tab = name;
    if (name === 'export') X.key = null;                    // the export tab asks the server again each time it opens
    const v = $('lib-video');
    if (v && !v.paused) v.pause();
    S.sigs = {};
    render();
    if (focus) $('lib-tab-' + name).focus();
    announce(name === 'words' ? 'Mark from transcript.' : name === 'export' ? 'Export to an NLE.' : 'Shots and clips.');
  }

  function nearestWord(frame) {
    const words = W.words;
    if (!words.length) return null;
    if (frame == null) return 0;
    let lo = 0, hi = words.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (words[mid].frameIn < frame) lo = mid + 1; else hi = mid; }
    return Math.min(lo, words.length - 1);
  }

  const selRange = () => (W.a == null || W.c == null ? null : { i: Math.min(W.a, W.c), j: Math.max(W.a, W.c) });
  const isSentenceStart = (i) => i === 0 || SENTENCE_END.test(W.words[i - 1].text || '');

  function moveCursor(to, extend) {
    if (extend) { if (W.a == null) W.a = W.c == null ? to : W.c; } else W.a = null;
    W.c = to;
    paintSel();
    if (W.nodes[to]) W.nodes[to].scrollIntoView({ block: 'nearest' });
    announceWord();
    render();
  }

  function announceWord() {
    const view = libView(), w = W.words[W.c];
    if (!w || !view.ok) return;
    const r = selRange();
    announce(w.text + ', ' + w.tcIn + '. ' + (r ? (r.j - r.i + 1) + ' word' + (r.j > r.i ? 's' : '') + ' selected.' : 'Nothing selected.'));
  }

  // Arrows move the cursor, Shift extends the range, Up and Down jump by sentence, Space picks the one word under
  // the cursor, Enter makes the clip, Escape clears (and, with nothing selected, falls through so focus can leave).
  function trKey(e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const n = W.words.length, k = e.key;
    if (!n) return;
    const c = W.c == null ? (nearestWord(L.pos) || 0) : W.c;
    let to = null;
    if (k === 'ArrowRight') to = Math.min(n - 1, c + 1);
    else if (k === 'ArrowLeft') to = Math.max(0, c - 1);
    else if (k === 'ArrowDown') { to = c; while (to < n - 1) { to++; if (isSentenceStart(to)) break; } }
    else if (k === 'ArrowUp') { to = c; if (to > 0) { to--; while (to > 0 && !isSentenceStart(to)) to--; } }
    else if (k === 'Home') to = 0;
    else if (k === 'End') to = n - 1;
    else if (k === ' ') {
      e.preventDefault(); e.stopPropagation();
      W.c = c; W.a = W.a === c ? null : c;
      paintSel(); announceWord(); render();
      return;
    } else if (k === 'Enter') { e.preventDefault(); e.stopPropagation(); if (!e.repeat) addFromWords(); return; }
    else if (k === 'Escape') { if (W.a != null) { e.stopPropagation(); W.a = null; paintSel(); announce('Selection cleared.'); render(); } return; }
    else return;
    e.preventDefault(); e.stopPropagation();
    W.c = c;                                 // the cursor a first key press starts from
    moveCursor(to, e.shiftKey);
  }

  function clearWordSel() { W.a = null; paintSel(); render(); }

  // ---- the range the server will build, computed the same way (the server's answer is the one that counts) ------
  function previewRange(view, r) {
    const first = W.words[r.i], last = W.words[r.j];
    let fi = first.frameIn - W.hin, fo = Math.max(last.frameOut, first.frameOut) + W.hout;
    const raw = { fi, fo };
    if (W.snap) {
      const shots = view.list.filter((x) => x.s.kind === 'shot').map((x) => x.s);
      const a = shots.filter((s) => s.frameIn <= fi && s.frameOut > fi).map((s) => s.frameIn);
      const b = shots.filter((s) => s.frameIn < fo && s.frameOut >= fo).map((s) => s.frameOut);
      if (a.length) fi = Math.min(fi, ...a);
      if (b.length) fo = Math.max(fo, ...b);
    }
    const d = view.d;
    const warn = [];
    if (fi < view.startF) warn.push('The in is before the start of the source. The server will refuse it: lower the handle before.');
    if (d.durationFrames && fo > view.startF + d.durationFrames) warn.push('The out is past the end of the source. The server will refuse it: lower the handle after or turn snap off.');
    return { fi, fo, raw, warn, first, last };
  }

  const handleText = (n) => (n ? '+' + n : '0') + ' fr';

  function snapText(view, p) {
    const tc = (f) => framesToTc(f, view.rate, view.drop);
    const parts = [];
    if (p.fi !== p.raw.fi) parts.push('In moves back to the cut at ' + tc(p.fi) + '.');
    if (p.fo !== p.raw.fo) parts.push('Out moves forward to the cut at ' + tc(p.fo) + '.');
    if (!parts.length) parts.push(view.list.some((x) => x.s.kind === 'shot') ? 'Both ends are already on a cut or in no shot.' : 'There are no shots to snap to.');
    return parts.join(' ');
  }

  // ---- painting ----------------------------------------------------------------------------------------------
  // Called by render() right after renderLibrary(), which has just shown or hidden the shot list's body.
  function renderWordsTab() {
    wordsWire();
    const body = $('lib-body'), panel = $('lib-words'), tabs = $('lib-tabs');
    const live = !body.hidden;                   // shown means an asset is loaded and markable
    if (W.for !== L.assetId) wordsReset();
    tabs.hidden = !live;
    const xpanel = $('lib-export');
    if (!live) { panel.hidden = true; xpanel.hidden = true; return; }
    const words = W.tab === 'words', exporting = W.tab === 'export';
    body.hidden = words || exporting;
    panel.hidden = !words;
    xpanel.hidden = !exporting;
    for (const name of ['shots', 'words', 'export']) {
      const b = $('lib-tab-' + name), on = W.tab === name;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    }
    if (exporting) { renderExport(); return; }
    if (!words) return;
    if (W.load === 'idle') { W.load = 'loading'; wordsLoad(false); }
    const view = libView();
    paintWordsHead(view);
    paintTranscript(view);
    paintNewClip(view);
  }

  function paintWordsHead(view) {
    const d = view.d;
    const a = ((S.data && S.data.assets) || []).find((x) => x.id === d.assetId) || {};
    paint('lib-w-head', [d.assetId, a.camera, d.rate, d.start, d.durationFrames, d.open, d.cleared, d.legalHold, d.hasProxy], () => el('div', { class: 'asset' },
      kv('Asset', short(d.assetId), true), kv('Camera', a.camera || a.show || '—'), kv('Rate', fmtRate(view.rate), true), kv('Start', d.start, true),
      kv('Length', d.durationFrames ? lengthTc(d.durationFrames, view.rate) : (d.open ? 'growing' : 'unknown'), true),
      assetChips(d),
      kv('Proxy', d.hasProxy ? 'ready' : 'none')));
  }

  function paintTranscript(view) {
    const tr = $('lib-tr');
    const n = W.words.length;
    let card = null;
    if (W.load === 'idle' || W.load === 'loading') card = el('div', { class: 'empty' }, el('p', { role: 'status', text: 'Reading the transcript of ' + short(L.assetId) + '…' }));
    else if (W.load === 'error') {
      card = el('div', { class: 'empty' }, el('p', { text: (W.err.code ? W.err.code + ': ' : '') + W.err.reason }),
        el('div', { class: 'acts' }, el('button', { type: 'button', class: 'btn', 'data-fk': 'w-retry', onclick: () => wordsLoad(false) }, 'Try again')));
    } else if (!n) {
      card = el('div', { class: 'empty' }, el('p', { text: W.pendingFrames
        ? W.pendingFrames + ' transcript word(s) exist but are not placed on frames yet, so none can be selected. Placing them runs in the background; check again shortly.'
        : 'This asset has no transcript yet. Words appear here once speech recognition has run on it. You can still mark by hand on the Shots and clips tab.' }),
        el('div', { class: 'acts' }, el('button', { type: 'button', class: 'btn ghost', 'data-fk': 'w-recheck', onclick: () => wordsLoad(false) }, 'Check again'),
          el('button', { type: 'button', class: 'btn ghost', 'data-fk': 'w-toshots', onclick: () => setTab('shots', true) }, 'Back to shots and clips')));
    }
    paint('lib-tr-state', [W.load, W.err, n, W.pendingFrames], () => card || []);
    tr.hidden = !!card;
    $('lib-w-more').hidden = !W.next || !!card;
    $('lib-w-reload').hidden = !!card;
    const note = (W.pendingFrames && n ? W.pendingFrames + ' word(s) are not placed on frames yet and cannot be selected. ' : '') + (W.next ? 'Showing the first ' + n + ' words.' : '');
    $('lib-tr-note').hidden = !note || !!card;
    if ($('lib-tr-note').textContent !== note) $('lib-tr-note').textContent = note;
    if (card) return;
    const shots = view.list.filter((x) => x.s.kind === 'shot');
    const before = S.sigs['lib-tr'];
    paint('lib-tr', [n, W.words[0].id, W.words[n - 1].id, shots.map((x) => x.s.id + x.s.frameIn)], () => buildTranscript(view, shots));
    if (S.sigs['lib-tr'] !== before) paintSel();
  }

  // Words in order, with the shot starts between them: a line of its own when the shot starts a sentence, an inline
  // marker when it cuts through one (the mockup's two forms).
  function buildTranscript(view, shots) {
    const out = [];
    W.nodes = [];
    W.shown = null;
    let k = 0;
    const tc = (f) => framesToTc(f, view.rate, view.drop);
    W.words.forEach((w, i) => {
      while (k < shots.length && shots[k].s.frameIn <= w.frameIn) {
        const x = shots[k++];
        if (isSentenceStart(i)) out.push(el('span', { class: 'ts', text: tc(x.s.frameIn) + ' · ' + x.tag }));
        else out.push(el('span', { class: 'cut' }, el('span', { 'aria-hidden': 'true', text: '▌ ' }), x.tag + ' · ' + tc(x.s.frameIn)), ' ');
      }
      const node = el('span', { class: 'w', 'data-i': String(i), text: w.text });
      W.nodes.push(node);
      out.push(node, ' ');
    });
    return out;
  }

  function paintSel() {
    const r = selRange(), nodes = W.nodes, prev = W.shown || { lo: -1, hi: -2, c: null };
    for (let i = prev.lo; i <= prev.hi; i++) if (nodes[i] && (!r || i < r.i || i > r.j)) nodes[i].classList.remove('sel');
    if (r) for (let i = r.i; i <= r.j; i++) if (nodes[i]) nodes[i].classList.add('sel');
    if (prev.c != null && prev.c !== W.c && nodes[prev.c]) nodes[prev.c].classList.remove('cur');
    if (W.c != null && nodes[W.c]) nodes[W.c].classList.add('cur');
    W.shown = { lo: r ? r.i : -1, hi: r ? r.j : -2, c: W.c };
  }

  function stepper(label, id, key) {
    const get = () => (key === 'hin' ? W.hin : W.hout);
    const set = (v) => { W[key] = Math.max(0, Math.min(HANDLE_MAX, v)); render(); announce(label + ' ' + handleText(W[key])); };
    const side = key === 'hin' ? 'before' : 'after';
    const mini = (text, aria, delta) => el('button', { type: 'button', class: 'mini', 'data-fk': id + delta, 'aria-label': aria,
      'aria-disabled': delta < 0 && get() === 0 ? 'true' : null, onclick: () => set(get() + delta) }, text);
    return el('div', {}, el('label', { class: 'f', for: id, text: label }),
      el('div', { class: 'stepper' }, mini('−', '− 1 frame ' + side, -1), el('output', { id, text: handleText(get()) }),
        mini('+', '+ 1 frame ' + side, 1), mini('+5', '+5 frames ' + side, 5)));
  }

  function paintNewClip(view) {
    const r = selRange();
    const p = r ? previewRange(view, r) : null;
    paint('lib-newclip', [r, p && [p.fi, p.fo], W.hin, W.hout, W.snap, S.pending.has('lib:words'), W.words.length], () => {
      if (!r) return el('p', { class: 'empty', text: 'No words selected. Click a word, or move with the arrow keys and press Space; extend the range with Shift and the arrows or by dragging.' });
      const tc = (f) => framesToTc(f, view.rate, view.drop);
      const q = (w) => '"' + w.text.replace(/[.,;:!?]+$/, '') + '" ' + w.tcIn;
      return [
        el('div', { class: 'body' },
          el('dl', { class: 'kv' },
            el('dt', { text: 'First word' }), el('dd', { text: q(p.first) }),
            el('dt', { text: 'Last word' }), el('dd', { text: q(p.last) }),
            el('dt', { text: 'Words' }), el('dd', { text: String(r.j - r.i + 1) })),
          stepper('Handle before', 'w-hin', 'hin'), stepper('Handle after', 'w-hout', 'hout'),
          el('div', { class: 'opt' },
            el('input', { type: 'checkbox', id: 'w-snap', 'data-fk': 'w-snap', checked: W.snap, onchange: (e) => { W.snap = e.target.checked; render(); } }),
            el('label', { for: 'w-snap' }, el('span', {}, 'Snap to shot boundaries', el('small', { text: W.snap ? snapText(view, p) : 'Off: the range is the words plus the handles.' })))),
          el('label', { class: 'f', for: 'w-name', text: 'Name (optional)' }),
          el('input', { id: 'w-name', type: 'text', maxlength: '200', value: W.name, 'data-fk': 'w-name', oninput: (e) => { W.name = e.target.value; },
            onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); addFromWords(); } } }),
          el('dl', { class: 'kv' },
            el('dt', { text: 'In' }), el('dd', { text: tc(p.fi) }),
            el('dt', { text: 'Out' }), el('dd', { text: tc(p.fo) }),
            el('dt', { text: 'Length' }), el('dd', { text: lengthTc(Math.max(0, p.fo - p.fi), view.rate) })),
          p.warn.map((t) => el('p', { class: 'no', role: 'alert', text: '✕ ' + t }))),
        el('div', { class: 'bar' },
          btn('Add clip', { key: 'lib:words', onclick: addFromWords }),
          btn('Clear selection', { key: 'w-clear', cls: 'ghost', onclick: clearWordSel })),
      ];
    });
  }

  // ---- writing: the one request. The page sends word IDs, handles and snap, never a time ----------------------
  function addFromWords() {
    const r = selRange();
    if (!r || !L.assetId) {
      announce('Select words first.');
      setFlash({ kind: 'bad', title: 'Select words first.', reason: 'Click a word, or press Space on it, then extend with Shift and the arrow keys.', code: 'bad_input' });
      render();
      return;
    }
    const body = { kind: 'clip', from_words: [W.words[r.i].id, W.words[r.j].id] };
    if (W.hin) body.handle_in = W.hin;
    if (W.hout) body.handle_out = W.hout;
    if (W.snap) body.snap = 'shot';
    const name = W.name.trim();
    if (name) body.label = name;
    libAct('spanCreate', L.assetId, body, {
      key: 'lib:words', okTitle: 'Clip added.',
      ok: (d) => d.span.tcIn + ' to ' + d.span.tcOut + (d.span.text ? ' · "' + (d.span.text.length > 60 ? d.span.text.slice(0, 59).trimEnd() + '…' : d.span.text) + '"' : '') + '.',
      after: (d) => { W.a = null; W.name = ''; L.cur = d.span.id; L.draft = null; paintSel(); },
      decorate: (f, e) => {
        if (['not_transcript', 'not_found', 'frames_pending'].includes(e.code)) {
          f.sub = 'The transcript on this screen may be out of date.';
          f.action = { label: 'Reload transcript', fn: () => wordsLoad(false) };
        }
      },
    });
  }

  // ---- Library: export to an NLE (slice 6; docs/specs/shot-spans.md "The NLE panel", mockup tab "Selection to export") ----
  // The tray's selection goes to POST /api/exports/nle. Own state (X) and own functions; nothing here is called by
  // the shot list or the transcript tab, and there are no keys: Space and the rest keep their native behaviour.
  //
  // Every change of selection or option asks the server for a dry run (a stale answer is thrown away by token), and
  // the page shows only what the server said: the verdict of each span, every refusal with its stable code, and the
  // first lines of the file. Nothing is written until Export is pressed. The real run is the same request with
  // dry_run false; the page then saves `content` with a Blob (fetch first, so a bearer token goes with it).
  //
  // Which paths the file carries is the SERVER's setting (VOLT_EXPORT_PATH_MAP). The mockup's "path as the NLE sees
  // it" drop-down cannot be a per-request choice (the API has no such field), so the page reports what the server
  // did to each path instead of offering a choice that would not be honoured.
  const XPORT = { MAX: 500, DEBOUNCE: 250, PREVIEW: 20, DEFAULT_TC: '01:00:00:00' };
  // Flip to true only when the manual import check in docs/specs/shot-spans.md has a recorded result.
  const FORMAT_VERIFIED = false;
  const X = { format: 'xmeml', media: 'hi-res', name: '', nameSet: false, start: XPORT.DEFAULT_TC,
    dry: null, dryKey: null, err: null, load: 'idle', tok: 0, timer: 0, key: null, run: false, done: null };
  Object.assign(ERR_LABEL, { mixed_rates: 'Mixed frame rates', not_local: 'Media not reachable',
    proxy_mismatch: 'Proxy does not match', unsupported_rate: 'Frame rate not supported', bad_path_map: 'Server path map is broken' });
  const TC_SHAPE = /^\d\d:\d\d:\d\d[:;]\d\d$/;

  // Plain words for each refusal: [headline, what to do]. The server's own reason is always shown under it, with the code.
  const XWHY = {
    essence_open: ['A file is still being written.', 'The hi-res (or proxy) of one asset has not been wrapped. Wait for wrap, or export the proxy.'],
    not_local: ['The media is not a file an editor can open.', 'It is stored as an object-store key or a relative path. Export the proxy, or restore the file to disk.'],
    no_proxy: ['One asset has no proxy.', 'Pick Hi-res, or wait for the proxy job.'],
    proxy_mismatch: ['A proxy does not line up with its hi-res.', 'It records a different frame rate or start timecode, so frames would land in the wrong place. Export the hi-res.'],
    outside_source: ['A span lies outside its file.', 'Trim it on the shot list so it sits inside the recording, or take it out of the selection.'],
    mixed_rates: ['The selection mixes frame rates.', 'One sequence has one rate. Export them in separate files.'],
    unsupported_rate: ['A frame rate cannot be written.', 'Only whole-number and /1001 broadcast rates can go into xmeml or an EDL.'],
    bad_input: ['The request was not accepted.', 'Check the options on the right.'],
    not_found: ['A selected span no longer exists.', 'It was deleted or merged since you selected it. Remove it from the selection.'],
    bad_path_map: ['The server\'s path map is broken.', 'This is a server setting (VOLT_EXPORT_PATH_MAP), not something to fix here. Tell whoever runs the server.'],
    unauthorized: ['A token is needed.', 'Use the Token button at the top, then try again.'],
    offline: ['The server did not answer.', 'Nothing was written. Check the connection and try again.'],
    internal: ['The server could not build the file.', 'Nothing was written. Try again; if it repeats, the server log has the reason.'],
  };

  function xBody(dry) {
    const b = { format: X.format, spans: L.sel.map((e) => e.id), media: X.media, name: X.name.trim() || xDefaultName(), dry_run: !!dry };
    if (X.start.trim()) b.start_tc = X.start.trim();
    return b;
  }
  function xDefaultName() {
    const a = ((S.data && S.data.assets) || []).find((x) => L.sel.length && x.id === L.sel[0].assetId);
    return ((a && a.show) || 'Volt') + ' selects';
  }
  const xKey = () => JSON.stringify(xBody(true));
  const xStart = () => X.start.trim() === '' || TC_SHAPE.test(X.start.trim());
  const xCurrent = () => X.dryKey === xKey() && L.sel.length > 0;

  function xOpen() {
    if (!L.sel.length) return;
    setTab('export', false);
    const p = $('lib-export');
    if (p) p.focus();
  }

  // A dry run is asked for when the request differs from the last one asked, after a short wait so typing a name
  // is one request, not one per letter. The token makes an answer to an older request harmless.
  function xSchedule() {
    const key = xKey();
    if (key === X.key) return;
    X.key = key; X.done = null; X.err = null;
    clearTimeout(X.timer);
    const tok = ++X.tok;
    if (!L.sel.length || L.sel.length > XPORT.MAX || !xStart()) { X.load = 'idle'; X.dry = null; X.dryKey = null; return; }
    X.load = 'loading';
    X.timer = setTimeout(() => xDry(key, tok), XPORT.DEBOUNCE);
  }

  async function xDry(key, tok) {
    let out;
    try { out = await call('exportNle', null, JSON.parse(key)); } catch (e) { out = { status: 0, data: null }; }
    if (tok !== X.tok) return;
    xTake(out, key, true);
    render();
  }

  function xTake(out, key, dry) {
    const { status, data } = out;
    if (status === 200 && data && data.ok) { X.dry = data; X.dryKey = key; X.err = null; X.load = 'ready'; return true; }
    if (status === 401) { setConn('token'); openToken(); }
    const e = status === 0 ? { code: 'offline', reason: 'The request did not reach the server.' } : codeOf(data, status);
    if (status === 401) e.code = 'unauthorized';
    X.err = { code: e.code, reason: e.reason, status, spans: (data && data.spans) || null, rates: (data && data.rates) || null, run: !dry };
    if (dry) { X.dry = null; X.dryKey = null; }
    X.load = 'ready';
    return false;
  }

  function xFixes() {
    const e = X.err, fixes = [];
    if (!e) return fixes;
    if ((e.code === 'not_local' || e.code === 'essence_open') && X.media === 'hi-res') fixes.push(['Use the proxy', () => xSet({ media: 'proxy' })]);
    if ((e.code === 'no_proxy' || e.code === 'proxy_mismatch') && X.media === 'proxy') fixes.push(['Use the hi-res', () => xSet({ media: 'hi-res' })]);
    if (e.code === 'mixed_rates') {
      for (const r of [...new Set(L.sel.map((s) => s.rate))]) {
        const p = parseRate(r);
        fixes.push(['Keep only the ' + (p ? fmtRate(p) : String(r)) + ' spans', () => { L.sel = L.sel.filter((s) => s.rate === r); store('volt.lib.sel', L.sel); render(); }]);
      }
    }
    if (e.code === 'not_found') {
      const gone = L.sel.find((s) => (e.reason || '').includes(s.id));
      if (gone) fixes.push(['Remove ' + gone.name + ' from the selection', () => { L.sel = L.sel.filter((s) => s.id !== gone.id); store('volt.lib.sel', L.sel); render(); }]);
    }
    return fixes;
  }

  function xSet(patch) {
    Object.assign(X, patch);
    render();
  }

  async function xRun() {
    if (X.run || !xCurrent() || !X.dry) return;
    const key = xKey();
    X.run = true; X.err = null; render();
    let out;
    try { out = await call('exportNle', null, Object.assign(JSON.parse(key), { dry_run: false })); } catch (e) { out = { status: 0, data: null }; }
    X.run = false;
    if (xKey() !== key) { render(); return; }               // the options moved on while the file was being built
    const d = out.data;
    if (out.status === 200 && d && d.ok && typeof d.content === 'string') {
      X.done = { filename: d.filename || 'volt-selects.' + (d.format === 'edl' ? 'edl' : 'xml'), mime: d.mime || 'text/plain', content: d.content, sha: d.sha256,
        clips: (d.spans || []).filter((s) => s.status === 'ready').length, frames: d.durationFrames, rate: d.rate, format: d.format, saved: false };
      X.done.saved = xSave(X.done);
      announce(X.done.saved ? 'Saved ' + X.done.filename + '.' : 'Built ' + X.done.filename + ' but the browser did not save it.');
    } else xTake(out, key, false);
    render();
  }

  // fetch() has already carried the token; the file is saved from a Blob, so no token is ever put in a URL.
  function xSave(done) {
    try {
      const url = URL.createObjectURL(new Blob([done.content], { type: done.mime }));
      const a = el('a', { href: url, download: done.filename, class: 'vh', tabindex: '-1' });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 15000);
      return true;
    } catch (e) { return false; }
  }

  // ---- painting -----------------------------------------------------------------------------------------
  function renderExport() {
    if (!X.nameSet && !X.name) X.name = xDefaultName();
    xSchedule();
    const cur = xCurrent() && X.dry;
    paint('lib-x-msg', [FORMAT_VERIFIED, X.err && [X.err.code, X.err.reason, X.err.run, X.media, L.sel.map((e) => e.id)], cur && [cur.warnings, cur.spans], L.sel.length > XPORT.MAX, L.sel.map((e) => e.name)], xMessages);
    paint('lib-x-table', [L.sel, cur ? cur.spans : X.load, X.err && X.err.code], xTable);
    paint('lib-x-prev', [X.format, cur && cur.preview, X.load, !!X.err, L.sel.length], xPreview);
    paint('lib-x-opts', [X.format, X.media, cur && [cur.pathMapSet, cur.paths]], xOptions);
    paint('lib-x-act', [X.format, X.media, X.load, X.run, !!X.err, cur && [cur.filename, cur.spans, cur.durationFrames, cur.startTc, cur.dropFrame], xStart(), L.sel.length, X.name, X.start], xAct);
    paint('lib-x-done', [X.done && [X.done.filename, X.done.saved]], xDone);
  }

  function xMessages() {
    const out = [];
    if (!FORMAT_VERIFIED) {
      out.push(el('div', { class: 'info', id: 'lib-x-unverified' }, el('strong', { text: 'Not checked in an NLE yet. ' }),
        'The xmeml and EDL files have been read back by a second program, but nobody has imported them into Premiere or Resolve and recorded the result. Import one and check the clips before you rely on it. The steps are under "Manual import check" in docs/specs/shot-spans.md.'));
    }
    if (L.sel.length > XPORT.MAX) {
      out.push(el('div', { class: 'banner bad', role: 'alert' }, el('div', { class: 'msg' }, el('strong', { text: 'Too many spans.' }),
        el('span', { class: 'why', text: 'An export holds up to ' + XPORT.MAX + ' spans; ' + L.sel.length + ' are selected. Remove some from the selection.' }), el('span', { class: 'sub', text: 'code: bad_input' }))));
    }
    const e = X.err;
    if (e) {
      const w = XWHY[e.code] || ['The export was refused.', ''];
      out.push(el('div', { class: 'banner bad', role: 'alert', id: 'lib-x-err' }, el('div', { class: 'msg' },
        el('strong', { text: (e.run ? 'Nothing was saved. ' : '') + w[0] }),
        el('span', { class: 'why', text: w[1] }),
        el('span', { class: 'sub', text: e.reason }),
        el('span', { class: 'sub', text: 'code: ' + e.code })),
        xFixes().map((f, i) => el('button', { type: 'button', class: 'btn ghost small', 'data-fk': 'x-fix' + i, onclick: f[1] }, f[0]))));
    }
    const d = X.dry && xCurrent() ? X.dry : null;
    if (d) {
      const unc = d.spans.filter((s) => s.status === 'ready' && s.notCleared);
      if (unc.length) {
        out.push(el('div', { class: 'info', id: 'lib-x-uncleared' }, el('strong', { text: unc.length + ' of ' + d.spans.length + ' spans are not cleared, and can still be exported.' }),
          el('span', { class: 'why', text: ' An export leaves the original source untouched, so it is allowed. Publish and air stay blocked for these spans until they are cleared on the Rights screen.' })));
      }
      const held = d.spans.filter((s) => s.status === 'ready' && s.legalHold);
      if (held.length) {
        out.push(el('div', { class: 'info', id: 'lib-x-hold' }, el('strong', { text: held.length + ' of ' + d.spans.length + ' spans are on legal hold, and can still be exported.' }),
          el('span', { class: 'why', text: ' An export leaves the original source untouched, so it is allowed. Publish and air stay blocked for these spans until the hold is lifted on the Rights screen.' })));
      }
      const warns = (d.warnings || []).filter((t) => !/on legal hold:|are not cleared:/.test(t));
      if (warns.length) {
        out.push(el('div', { class: 'info', id: 'lib-x-warn' }, el('strong', { text: 'Warnings from the server' }), el('ul', { class: 'plain' }, warns.map((t) => el('li', { text: t })))));
      }
    }
    return out;
  }

  function xTable() {
    if (!L.sel.length) {
      return el('div', { class: 'state-card' }, el('div', { class: 'empty' }, el('p', { text: 'Nothing is selected. Tick rows on the shot list to build the list; its order is the order of the sequence.' }),
        el('div', { class: 'acts' }, el('button', { type: 'button', class: 'btn ghost', 'data-fk': 'x-back', onclick: () => setTab('shots', true) }, 'Back to the shot list'))));
    }
    const verdicts = xCurrent() && X.dry ? X.dry.spans : null;
    const head = el('thead', {}, el('tr', {}, ['#', 'Span', 'In', 'Out', 'Length', 'Check'].map((h) => el('th', { scope: 'col', text: h }))));
    const rows = L.sel.map((e, i) => {
      const v = verdicts && verdicts[i];
      const p = parseRate(e.rate);
      let cell;
      if (v) {
        const flags = [v.notCleared ? 'not cleared' : null, v.legalHold ? 'legal hold' : null].filter(Boolean);
        cell = el('td', { class: 'verd ok' }, el('span', { 'aria-hidden': 'true', text: '✓ ' }), flags.length ? 'Ready, ' + flags.join(' and ') : 'Ready',
          flags.length ? el('small', { class: 'muted', text: ' export allowed; publish and air blocked' }) : null);
      } else if (X.err && !X.err.run) cell = el('td', { class: 'verd', text: 'Not checked' });
      else cell = el('td', { class: 'verd', text: X.load === 'loading' ? 'Checking…' : '—' });
      return el('tr', {}, el('td', { class: 'mono', text: String(i + 1) }),
        el('td', {}, el('div', { class: 'nm', text: e.name }), el('small', { class: 'mono', text: e.cam + ' · ' + short(e.assetId) })),
        el('td', { class: 'mono', text: e.tcIn }), el('td', { class: 'mono', text: e.tcOut }),
        el('td', { class: 'mono', text: p ? lengthTc(e.frames, p) : e.frames + ' fr' }), cell);
    });
    return [el('div', { class: 'tablewrap', role: 'region', 'aria-label': 'Selection in export order', tabindex: '0' }, el('table', {}, head, el('tbody', {}, rows))),
      el('div', { class: 'bar' }, el('small', { text: 'Order is the order in the sequence. Reorder it in the Selection panel on the shot list tab.' }))];
  }

  function xPreview() {
    const d = xCurrent() && X.dry ? X.dry : null;
    let body;
    if (!L.sel.length) body = el('p', { class: 'empty', text: 'Nothing to preview.' });
    else if (d && d.preview && d.preview.length) {
      const more = d.preview.length >= XPORT.PREVIEW ? '\n…' : '';
      body = el('pre', { class: 'code', tabindex: '0', role: 'group', 'aria-label': 'Preview of the export file, first ' + d.preview.length + ' lines' }, d.preview.join('\n') + more);
    } else if (X.err) body = el('p', { class: 'empty', text: 'No preview: the server refused this selection (see the message above).' });
    else body = el('p', { class: 'empty', text: X.load === 'loading' ? 'Checking with the server…' : 'No preview yet.' });
    return el('div', { class: 'body' }, body);
  }

  function xRadio(group, value, label, note, checked) {
    const id = 'x-' + group + '-' + value;
    return el('div', { class: 'opt' },
      el('input', { type: 'radio', name: 'x-' + group, id, value, checked: checked ? true : null, 'data-fk': id,
        onchange: () => xSet(group === 'fmt' ? { format: value } : { media: value }) }),
      el('label', { for: id }, el('span', {}, label, el('small', { text: note }))));
  }

  function xPaths(d) {
    if (X.format === 'edl') return el('p', { class: 'note', id: 'lib-x-paths', text: 'An EDL carries no media paths. The editor relinks by reel name and timecode.' });
    if (!d) return el('p', { class: 'note', id: 'lib-x-paths', text: 'Paths are shown once the server has checked the selection.' });
    const shown = d.paths.slice(0, 3);
    return el('div', { id: 'lib-x-paths' },
      el('p', { class: 'note', text: d.pathMapSet
        ? 'The server rewrites media paths with its path map (VOLT_EXPORT_PATH_MAP). Check that the NLE machine can open what is written below.'
        : 'No path map is set on the server (VOLT_EXPORT_PATH_MAP), so paths are written as stored. An editing machine often sees the media somewhere else.' }),
      el('ul', { class: 'plain paths' }, shown.map((p) => el('li', {}, el('span', { class: 'mono', text: p.mapped ? p.location + ' → ' + p.path : p.path }), el('small', { text: p.mapped ? ' (rewritten)' : ' (as stored)' }))),
        d.paths.length > shown.length ? el('li', { text: 'and ' + (d.paths.length - shown.length) + ' more' }) : null));
  }

  function xOptions() {
    const d = xCurrent() && X.dry ? X.dry : null;
    return el('div', { class: 'body' },
      el('fieldset', { class: 'plainset' }, el('legend', { class: 'f', text: 'Format' }),
        xRadio('fmt', 'xmeml', 'Final Cut XML (xmeml)', 'Imports in Premiere. Keeps names, paths, in and out, comments.', X.format === 'xmeml'),
        xRadio('fmt', 'edl', 'CMX3600 EDL', 'Cuts only. Reel names are cut to 8 characters. No paths.', X.format === 'edl')),
      el('fieldset', { class: 'plainset' }, el('legend', { class: 'f', text: 'Media' }),
        xRadio('med', 'hi-res', 'Hi-res', 'Opens without relinking if the NLE sees the same path.', X.media === 'hi-res'),
        xRadio('med', 'proxy', 'Proxy', 'Smaller. Relink to hi-res later.', X.media === 'proxy')),
      el('label', { class: 'f', for: 'x-name', text: 'Sequence name' }),
      el('input', { id: 'x-name', type: 'text', maxlength: '200', value: X.name, 'data-fk': 'x-name', autocomplete: 'off',
        oninput: (e) => { X.name = e.target.value; X.nameSet = true; render(); } }),
      el('label', { class: 'f', for: 'x-start', text: 'Start timecode of the sequence' }),
      el('input', { id: 'x-start', type: 'text', class: 'mono', value: X.start, 'data-fk': 'x-start', spellcheck: 'false', autocomplete: 'off', placeholder: XPORT.DEFAULT_TC, 'aria-describedby': 'x-start-hint',
        oninput: (e) => { X.start = e.target.value; render(); } }),
      el('small', { id: 'x-start-hint', text: 'hh:mm:ss:ff, or hh:mm:ss;ff for drop-frame. Empty means ' + XPORT.DEFAULT_TC + ' (drop-frame when the first file is).' }),
      xPaths(d));
  }

  function xAct() {
    const d = xCurrent() && X.dry ? X.dry : null;
    const total = L.sel.length;
    const ready = d ? d.spans.filter((s) => s.status === 'ready').length : 0;
    let why = '';
    if (!total) why = 'Nothing is selected.';
    else if (total > XPORT.MAX) why = 'Too many spans: the limit is ' + XPORT.MAX + '.';
    else if (!xStart()) why = 'The start timecode must look like 01:00:00:00.';
    else if (X.err && !X.err.run) why = 'The server refused this selection. Fix the message above first.';
    else if (!d) why = 'Waiting for the server to check the selection.';
    const can = !why;
    const n = total;
    const rate = d ? parseRate(d.rate) : null;
    const facts = d && d.filename ? [el('dt', { text: 'File' }), el('dd', { class: 'mono', text: d.filename }),
      el('dt', { text: 'Clips' }), el('dd', { text: String(ready) }),
      el('dt', { text: 'Runs' }), el('dd', { class: 'mono', text: (rate ? lengthTc(d.durationFrames, rate) + ' at ' + fmtRate(rate) : d.durationFrames + ' frames') }),
      el('dt', { text: 'Starts at' }), el('dd', { class: 'mono', text: d.startTc + (d.dropFrame ? ' drop-frame' : '') })] : null;
    return [facts ? el('dl', { class: 'kv x-facts' }, facts) : null,
      el('div', { class: 'bar' },
        el('button', { type: 'button', class: 'btn', id: 'lib-x-go', 'data-fk': 'x-go', 'aria-disabled': can ? null : 'true', 'aria-describedby': 'lib-x-why', 'aria-busy': X.run ? 'true' : null,
          onclick: () => { if (can) xRun(); } }, X.run ? 'BUILDING…' : 'EXPORT ' + (n || '') + (n === 1 ? ' SPAN' : ' SPANS'))),
      el('p', { class: 'note', id: 'lib-x-why', text: can ? 'Builds the file and saves it with your browser. Volt keeps no copy.' : why })];
  }

  function xDone() {
    const d = X.done;
    if (!d) return [];
    const p = parseRate(d.rate);
    return el('div', { class: 'good', role: 'status', id: 'lib-x-saved' },
      el('strong', { text: d.saved ? 'Saved. ' : 'Built, but the browser did not save it. ' }),
      el('span', { class: 'mono', text: d.filename }), ' · ' + d.clips + ' clip' + (d.clips === 1 ? '' : 's') + ' · ' + (p ? lengthTc(d.frames, p) : d.frames + ' frames') +
      ' · sha256 ' + (d.sha || '').slice(0, 8) + '…' + (d.sha || '').slice(-4) + '. Volt keeps no copy.',
      el('div', { class: 'bar flush' }, el('button', { type: 'button', class: 'btn ghost small', 'data-fk': 'x-again', onclick: () => { d.saved = xSave(d); render(); } }, 'Save again')));
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
    for (const b of document.querySelectorAll('#workspaces button')) b.addEventListener('click', () => setWorkspace(b.dataset.ws, true));
    document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); schedule(); } });
    const tick = () => { $('clock').textContent = new Date().toLocaleTimeString([], { hour12: false }); };
    tick(); setInterval(tick, 1000);
    setConn('connecting');
    setWorkspace(storedWorkspace(), false);
    setScreen(location.hash.slice(1) || defaultScreen(S.ws));
    refresh().then(schedule);
  }

  init();
})();
