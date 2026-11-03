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
    const next = taking ? null : (items.find((i, idx) => idx > floor && i.status === 'ready' && !i.floated) || null);
    return { items, air, next, hold: rundown ? rundown.hold : null, taking };
  }

  // ---- rundown timing: front time (when an item starts) and drift (actual start minus planned front) ----------
  // Planned length of an item in seconds from the server's frame count and exact rate; null when either is unknown.
  function itemSeconds(media) {
    const r = media && media.frames != null ? parseRate(media.rate) : null;
    return r ? media.frames * r.d / r.n : null;
  }
  // Planned length of an item and where it came from. An item with media keeps its media length (null when that is unknown);
  // one without media (an anchor read) uses its read time from the server's script count; one with neither has no length.
  // An older server that sends no lengthSource is read as media, which is what it always was.
  function itemPlan(item, media) {
    const src = (item && item.lengthSource) || 'media';
    if (src === 'read') return { seconds: Number.isFinite(item.readSeconds) ? item.readSeconds : null, source: 'read' };
    if (src === 'none') return { seconds: null, source: 'none' };
    return { seconds: itemSeconds(media), source: 'media' };
  }
  // A killed or skipped item takes no time, so it is left out of the running sum, and neither does a floated one (a backup
  // that is not in the running order). An item of unknown length adds zero.
  // The clock is anchored on the first counted item that has started; before that only offsets are known.
  function frontTimes(items, seconds, nowMs) {
    let run = 0;
    const rows = items.map((it) => {
      if (it.status === 'killed' || it.status === 'skipped' || it.floated) return { id: it.id, offset: null, startMs: null };
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
  // ---- script markup: the twin of volt/story.py script_stats (one golden fixture drives both) ------------------
  // A line "[...]" is an instruction and "// ..." a private note; neither is read aloud or counted. An unclosed "[" is read text
  // and warned. Read time is round(words / wpm * 60) with halves up, in integers, so it equals the server's answer.
  function scriptStats(text, wpm) {
    const rate = wpm === undefined ? 160 : wpm;
    if (typeof rate !== 'number' || !Number.isInteger(rate) || rate < 60 || rate > 400) throw new Error('wpm must be a whole number from 60 to 400');
    let words = 0, instructions = 0, notes = 0, unclosed = false;
    for (const raw of String(text == null ? '' : text).split(/\r\n|\r|\n/)) {
      const line = raw.trim();
      if (!line) continue;
      if (line.startsWith('//')) notes++;
      else if (line.startsWith('[') && line.endsWith(']')) instructions++;
      else {
        if (line.startsWith('[') && line.indexOf(']') < 0) unclosed = true;
        words += line.split(/\s+/).length;
      }
    }
    return { words, readSeconds: Math.floor((words * 120 + rate) / (2 * rate)), instructions, notes, warnings: unclosed ? ['unclosed_bracket'] : [] };
  }
  // The script as blocks, one per non-blank line, with the paragraph each belongs to: the twin of story.parse_script (same fixture).
  function scriptBlocks(text) {
    const out = [];
    let para = 0, pendingBreak = false;
    for (const raw of String(text == null ? '' : text).split(/\r\n|\r|\n/)) {
      const line = raw.trim();
      if (!line) { pendingBreak = out.length > 0; continue; }
      if (pendingBreak) { para++; pendingBreak = false; }
      const kind = line.startsWith('//') ? 'note' : line.startsWith('[') && line.endsWith(']') ? 'instruction' : 'read';
      out.push({ kind, text: line, para });
    }
    return out;
  }
  // The story status moves, mirroring story.TRANSITIONS (a test pins the two together). Listed in the order the buttons appear.
  const STORY_MOVES = { draft: ['ready', 'killed'], ready: ['approved', 'draft', 'killed'], approved: ['ready', 'killed'], killed: ['draft'] };
  function legalMoves(storyStatus) {
    return Object.prototype.hasOwnProperty.call(STORY_MOVES, storyStatus) ? STORY_MOVES[storyStatus].slice() : [];
  }
  // A save that lost the race: `mine` is the editor's text, `theirs` what the server now holds ({rev, slug, script}). Keep mine saves
  // my text on top of their revision; take theirs drops mine. Neither ever discards text without the person choosing it.
  function resolveConflict(mine, theirs, choice) {
    if (choice === 'mine') return { slug: mine.slug, script: mine.script, baseRev: theirs.rev, save: true };
    if (choice === 'theirs') return { slug: theirs.slug, script: theirs.script, baseRev: theirs.rev, save: false };
    throw new Error('choice must be mine or theirs');
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

  // J/K/L shuttle: L plays forward, J in reverse, K stops. Repeated presses of the same key double the speed up to 8x;
  // the opposite key starts again at 1x the other way.
  function shuttleNext(state, key) {
    if (key === 'k') return { dir: 0, speed: 0 };
    const dir = key === 'l' ? 1 : -1;
    return state.dir === dir ? { dir, speed: Math.min(8, state.speed * 2) } : { dir, speed: 1 };
  }

  // ---- scheduling: when the clock takes an item --------------------------------------------------------------
  // The chip for an item's schedule: [state-language kind, word], or null when there is nothing to say. An outcome the
  // page does not know is shown by its own word.
  function scheduleChip(state, hasTime, mode) {
    if (!state) return hasTime ? ['pending', 'Scheduled'] : mode === 'after_previous' ? ['pending', 'After previous'] : null;
    const known = { claimed: ['pending', 'Claimed'], fired: ['ready', 'Fired'], failed: ['failed', 'Failed'], missed: ['failed', 'Missed'],
      held: ['locked', 'Held'], blocked: ['failed', 'Blocked'] };
    return Object.prototype.hasOwnProperty.call(known, state) ? known[state] : ['pending', String(state)];
  }
  // "Repeats daily at 20:00 America/New_York" for a recurrence rule.
  const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  function ruleText(rule) {
    if (!rule) return '';
    const when = rule.at + ' ' + rule.tz;
    return rule.freq === 'weekly' ? 'Repeats weekly on ' + (rule.days || []).map((d) => DAY_NAMES[d]).join(', ') + ' at ' + when : 'Repeats daily at ' + when;
  }
  // A datetime-local value ("2026-11-01T20:00", the viewer's own clock) as UTC text for the server; '' is no schedule.
  function localToUtc(value) {
    const t = String(value == null ? '' : value).trim();
    if (!t) return null;
    const d = new Date(t);
    return isNaN(d) ? 'bad' : d.toISOString().replace(/\.\d{3}Z$/, 'Z');
  }
  function utcToLocal(iso) {
    const d = iso ? new Date(iso) : null;
    if (!d || isNaN(d)) return '';
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  // ---- slot length: back time (when an item must start for the show to end on schedule) and over/under -------
  // "15:00" or "1:30:00". A bare number is refused (minutes or seconds?). Empty clears the slot. Returns seconds, null or 'bad'.
  function parseSlot(text) {
    const t = String(text == null ? '' : text).trim();
    if (!t) return null;
    const m = /^(\d{1,3}):(\d{2})(?::(\d{2}))?$/.exec(t);
    if (!m) return 'bad';
    const h3 = m[3] !== undefined;
    const sec = h3 ? Number(m[3]) : Number(m[2]);
    const min = h3 ? Number(m[2]) : Number(m[1]);
    if (sec >= 60 || (h3 && min >= 60)) return 'bad';
    const total = h3 ? Number(m[1]) * 3600 + min * 60 + sec : min * 60 + sec;
    return total >= 1 && total <= 86399 ? total : 'bad';
  }
  // Back time counts back from the slot end: the first item's start plus the slot length, less the planned lengths of this
  // item and every counted item after it. Killed and skipped items take no time. An unknown length at or after an item
  // makes its back time unknown. Aired items count by their actual length, so the totals follow what really happened.
  function backTimes(items, seconds, slotSec) {
    const ft = frontTimes(items, seconds, 0);
    const at = (it, f) => (it[f] ? Date.parse(it[f]) : NaN);
    const eff = {};
    for (const it of items) {
      const a = at(it, 'asRunIn'), b = at(it, 'asRunOut');
      eff[it.id] = it.status === 'aired' && !isNaN(a) && !isNaN(b) ? (b - a) / 1000 : seconds[it.id];
    }
    const counted = items.map((it, i) => ft.rows[i].offset !== null);
    const first = counted.indexOf(true);
    let anchor = first >= 0 ? ft.rows[first].startMs : null;
    if (anchor == null && first >= 0 && !isNaN(at(items[first], 'scheduledAt'))) anchor = at(items[first], 'scheduledAt');
    let total = 0;
    items.forEach((it, i) => { if (counted[i]) total += eff[it.id] || 0; });
    const slotEndMs = slotSec != null && anchor != null ? anchor + slotSec * 1000 : null;
    const rows = items.map((it) => ({ id: it.id, backMs: null }));
    let suffix = 0, unknown = false;
    for (let i = items.length - 1; i >= 0; i--) {
      if (!counted[i]) continue;
      if (eff[items[i].id] == null) unknown = true; else suffix += eff[items[i].id];
      if (slotEndMs != null && !unknown && items[i].status === 'ready') rows[i].backMs = slotEndMs - suffix * 1000;
    }
    return { rows, slotEndMs, totalSec: total, overSec: slotSec != null ? Math.round(total - slotSec) : null };
  }

  // Theme: Auto follows the operating system; Dark and Light are a choice kept in this browser.
  const themeName = (stored) => (stored === 'dark' || stored === 'light' ? stored : 'auto');
  const nextTheme = (name) => ({ auto: 'dark', dark: 'light', light: 'auto' }[themeName(name)]);

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
    withdrawn: { icon: 'close',   word: 'Withdrawn',   cls: 'st done' },
    archived:  { icon: 'library', word: 'Archived',    cls: 'st done' },
    restoring: { icon: 'pending', word: 'Restoring',   cls: 'st pend' },
    archiving: { icon: 'pending', word: 'Archiving',   cls: 'st pend' },
    recording: { icon: 'record',  word: 'Recording',   cls: 'st air' },
    wrapped:   { icon: 'ready',   word: 'Wrapped',     cls: 'st ok' },
    fresh:     { icon: 'ready',   word: 'Fresh',       cls: 'st ok' },
    stale:     { icon: 'alert',   word: 'Stale',       cls: 'st pend' },
    canair:    { icon: 'ready',   word: 'Yes',         cls: 'st ok' },
    blocked:   { icon: 'alert',   word: 'No',          cls: 'st fail' },
    incomplete: { icon: 'alert',  word: 'Incomplete',  cls: 'st pend' },
    shared:    { icon: 'user',    word: 'Shared',      cls: 'st done' },
    required:  { icon: 'alert',   word: 'Required',    cls: 'st pend' },
    // The editorial side of a story (the Scripts screen). Its words never collide with the playout ones: "Story killed" is not "Killed".
    draft:       { icon: 'script', word: 'Draft',        cls: 'st done' },
    storyReady:  { icon: 'ready',  word: 'Ready',        cls: 'st inform' },
    approved:    { icon: 'rights', word: 'Approved',     cls: 'st ok' },
    storyKilled: { icon: 'close',  word: 'Story killed', cls: 'st fail' },
    floated:     { icon: 'float',  word: 'Floated',      cls: 'st done' },
    unsaved:     { icon: 'pending', word: 'Unsaved',     cls: 'st pend' },
    // Edit leases: who has an item open for editing. Never red: red is only for a live path.
    editing:     { icon: 'lock',    word: 'Editing',     cls: 'st hold' },
    yours:       { icon: 'ready',   word: 'You',         cls: 'st ok' },

  };
  // The state-language kind of an item's story status (draft, ready, approved, killed). Not the playout status.
  function storyKind(storyStatus) {
    return storyStatus === 'ready' ? 'storyReady' : storyStatus === 'killed' ? 'storyKilled' : storyStatus === 'approved' ? 'approved' : 'draft';
  }
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
    producer: ['show', 'scripts', 'rights', 'publish', 'library'],
    editor:   ['library', 'publish', 'rights'],
    operator: ['show', 'scripts', 'record'],
  };
  const DEFAULT_WORKSPACE = 'operator';
  const ALL_SCREENS = ['show', 'record', 'rights', 'publish', 'library', 'activity', 'access', 'scripts'];
  // The number keys reach the first six screens only; Access (an admin's screen, opened from the pill) takes no key.
  const KEY_SCREENS = ['show', 'record', 'rights', 'publish', 'library', 'activity'];
  const screenForKey = (key) => (/^[1-9]$/.test(String(key)) ? KEY_SCREENS[+key - 1] || null : null);
  const workspaceName = (stored) => (typeof stored === 'string' && Object.prototype.hasOwnProperty.call(WORKSPACES, stored) ? stored : DEFAULT_WORKSPACE);
  // A hash naming a screen outside the rail keeps the workspace; that screen rides along as the one temporary extra.
  function railFor(workspace, screen) {
    const screens = WORKSPACES[workspaceName(workspace)].slice();
    return { screens, extra: ALL_SCREENS.includes(screen) && !screens.includes(screen) ? screen : null };
  }
  const defaultScreen = (workspace) => WORKSPACES[workspaceName(workspace)][0];

  // ---- sessions: a token is traded for an HttpOnly cookie, so the page stops holding it. A cookie write also sends the
  // per-session X-Volt-Csrf value (kept in memory only); a bearer write sends neither. Any failure leaves the token path as it was.
  function authHeaders(method, token, csrf) {
    const h = {};
    if (token) h.Authorization = 'Bearer ' + token;
    else if (csrf && method === 'POST') h['X-Volt-Csrf'] = csrf;
    return h;
  }
  async function beginSession(fetchFn, token) {
    try {
      const res = await fetchFn('/api/session/start', { method: 'POST', cache: 'no-store', body: '{}',
        headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' } });
      if (!res.ok) return '';
      const data = await res.json();
      return data && data.ok === true && typeof data.csrf === 'string' ? data.csrf : '';
    } catch (e) { return ''; }
  }
  async function endSession(fetchFn, csrf) {
    try {
      const res = await fetchFn('/api/session/end', { method: 'POST', cache: 'no-store', body: '{}',
        headers: { 'Content-Type': 'application/json', 'X-Volt-Csrf': csrf } });
      return res.ok;
    } catch (e) { return false; }
  }
  // After a reload the cookie is still there but the page has lost the CSRF value: ask whoami for it.
  async function adoptSession(fetchFn) {
    try {
      const res = await fetchFn('/api/whoami', { cache: 'no-store' });
      if (!res.ok) return '';
      const who = await res.json();
      return who && who.via === 'session' && typeof who.csrf === 'string' ? who.csrf : '';
    } catch (e) { return ''; }
  }

  // The sign-in anchors for the Token dialog: [{id, label, href}] from the server's provider list. Ids are plain words, labels are cut to
  // 40 characters and only ever set as text, and the return path is this page's own (one leading slash, printable ASCII), else the root.
  function providerLinks(providers, next) {
    if (!Array.isArray(providers)) return [];
    const raw = typeof next === 'string' ? next.split('#')[0] : '';
    const back = /^\/(?!\/)[\x20-\x7e]{0,511}$/.test(raw) && raw.indexOf('\\') < 0 ? raw : '/';
    const out = [];
    for (const p of providers) {
      if (!p || typeof p !== 'object' || typeof p.id !== 'string' || !/^[a-z0-9_-]{1,40}$/.test(p.id)) continue;
      const label = typeof p.label === 'string' && p.label.trim() ? p.label.trim().slice(0, 40) : p.id;
      out.push({ id: p.id, label, href: '/auth/login?provider=' + p.id + '&next=' + encodeURIComponent(back) });
    }
    return out;
  }

  // ---- Find: the search request, the response, and the pieces of a result row. Pure, so a test can drive them under node. ----
  // The scopes the server takes that this page offers. A Labels scope exists on the server and appears here once something writes labels.
  const SEARCH_SCOPES = [['all', 'All'], ['transcript', 'Speech'], ['metadata', 'Details']];
  const FACET_BUILTIN = [['show', 'Show'], ['camera', 'Camera'], ['owner', 'Owner'], ['retention', 'Retention'], ['cleared', 'Rights'], ['created', 'Month']];
  const HIT_KIND = { transcript: 'Speech', meta: 'Details', face: 'Face', text: 'On screen', object: 'Object', scene: 'Scene' };
  const monthRange = (ym) => {
    const m = /^(\d{4})-(\d{2})$/.exec(String(ym));
    if (!m) return null;
    const y = +m[1], mo = +m[2], p = (n) => String(n).padStart(2, '0');
    return { from: y + '-' + p(mo) + '-01', to: y + '-' + p(mo) + '-' + p(new Date(Date.UTC(y, mo, 0)).getUTCDate()) };
  };
  // One filter clause from the values ticked in one facet. A month is a range; true and false together constrain nothing.
  function facetClause(field, values) {
    if (!Array.isArray(values) || !values.length) return null;
    if (field === 'created') { const r = monthRange(values[0]); return r ? { field, op: 'range', from: r.from, to: r.to } : null; }
    if (values.length === 1) return { field, op: 'eq', value: values[0] };
    if (typeof values[0] === 'boolean') return null;
    return { field, op: 'in', value: values.slice(0, 50) };
  }
  // ui: { q, scope, sort, sel: {facet: [values]}, incomplete, extra: [clauses kept as they came], includeProposed, limit, after }.
  // Unreviewed faces are never asked for unless includeProposed is set, and nothing on this page sets it.
  function buildSearchRequest(ui) {
    const u = ui || {}, req = {};
    const q = typeof u.q === 'string' ? u.q.trim().slice(0, 500) : '';
    if (q) req.q = q;
    if (u.scope && u.scope !== 'all') req.scope = u.scope;
    const all = [];
    for (const field of Object.keys(u.sel || {}).sort()) { const c = facetClause(field, u.sel[field]); if (c) all.push(c); }
    if (u.incomplete) all.push({ field: 'incomplete', op: 'eq', value: true });
    for (const c of Array.isArray(u.extra) ? u.extra : []) all.push(c);
    if (all.length) req.filter = { all };
    const sort = u.sort === 'recent' || u.sort === 'show' ? u.sort : (u.sort === 'relevance' && q ? 'relevance' : null);
    if (sort) req.sort = sort;
    if (u.includeProposed === true) req.include = 'proposed';
    if (Number.isInteger(u.limit) && u.limit >= 1 && u.limit <= 100) req.limit = u.limit;
    if (typeof u.after === 'string' && u.after) req.after = u.after;
    return req;
  }
  // What a saved search stores: the question, not the page.
  const savedQueryOf = (req) => { const o = Object.assign({}, req); delete o.after; delete o.limit; delete o.facets; return o; };
  // The request as GET parameters: every value a string, the filter as JSON.
  function searchParams(req, facets) {
    const p = {};
    for (const k of ['q', 'scope', 'sort', 'include', 'after']) if (typeof req[k] === 'string') p[k] = req[k];
    if (req.filter) p.filter = JSON.stringify(req.filter);
    if (req.limit) p.limit = String(req.limit);
    if (Array.isArray(facets) && facets.length) p.facets = facets.join(',');
    return p;
  }
  // The other way: a saved request back into the page's own controls. A clause the controls cannot show is kept in `extra` untouched.
  function uiFromRequest(req) {
    const r = req || {}, ui = { q: typeof r.q === 'string' ? r.q : '', scope: typeof r.scope === 'string' ? r.scope : 'all', sort: typeof r.sort === 'string' ? r.sort : 'auto',
      sel: {}, incomplete: false, extra: [] };
    const all = r.filter && Array.isArray(r.filter.all) ? r.filter.all : [];
    for (const c of all) {
      if (!c || typeof c !== 'object') continue;
      if (c.field === 'incomplete' && c.op === 'eq' && c.value === true) { ui.incomplete = true; continue; }
      const text = FACET_BUILTIN.some((f) => f[0] === c.field && f[0] !== 'created' && f[0] !== 'cleared') || /^meta\./.test(c.field || '');
      if (text && c.op === 'eq' && typeof c.value !== 'object' && !ui.sel[c.field]) { ui.sel[c.field] = [c.value]; continue; }
      if (text && c.op === 'in' && Array.isArray(c.value) && !ui.sel[c.field]) { ui.sel[c.field] = c.value.slice(); continue; }
      ui.extra.push(c);
    }
    return ui;
  }
  // The response, defensively: a missing list is an empty one, and nothing here is trusted to be the right type.
  function parseSearchResponse(json) {
    const out = { ok: false, assets: [], total: 0, facets: {}, next: null, degraded: false };
    if (!json || typeof json !== 'object' || json.ok !== true) return out;
    out.ok = true;
    out.total = Number.isInteger(json.total) ? json.total : 0;
    out.next = typeof json.next === 'string' ? json.next : null;
    out.degraded = json.degraded === true;
    for (const a of Array.isArray(json.assets) ? json.assets : []) {
      if (!a || typeof a.assetId !== 'string') continue;
      out.assets.push(Object.assign({}, a, {
        hits: (Array.isArray(a.hits) ? a.hits : []).filter((h) => h && typeof h === 'object').map((h) => Object.assign({}, h, { snippet: typeof h.snippet === 'string' ? h.snippet : '' })),
        metadata: a.metadata && typeof a.metadata === 'object' ? a.metadata : {}, missing: Array.isArray(a.missing) ? a.missing : [],
      }));
    }
    const f = json.facets && typeof json.facets === 'object' ? json.facets : {};
    for (const name of Object.keys(f)) {
      const v = f[name];
      out.facets[name] = { values: v && Array.isArray(v.values) ? v.values.filter((x) => x && Number.isInteger(x.count)) : [], other: v && Number.isInteger(v.other) ? v.other : 0 };
    }
    return out;
  }
  // The words of the query that should be marked in a snippet: bare words and quoted phrases, not -excluded ones.
  function queryTerms(q) {
    const out = [];
    for (const m of String(q || '').matchAll(/(-?)(?:"([^"]+)"|(\S+))/g)) {
      if (m[1]) continue;
      const t = (m[2] || m[3] || '').replace(/^["*]+|["*]+$/g, '').trim().toLowerCase();
      if (t && !out.includes(t)) out.push(t);
    }
    return out.slice(0, 20);
  }
  // A snippet cut into pieces, each marked hit or not. The page sets every piece as a text node: nothing here is ever markup.
  function splitHighlight(text, q) {
    const s = String(text == null ? '' : text), terms = queryTerms(q);
    if (!terms.length || !s) return [{ text: s, hit: false }];
    const re = new RegExp(terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'gi');
    const out = [];
    let at = 0;
    for (const m of s.matchAll(re)) {
      if (!m[0]) continue;
      if (m.index > at) out.push({ text: s.slice(at, m.index), hit: false });
      out.push({ text: m[0], hit: true });
      at = m.index + m[0].length;
    }
    if (at < s.length) out.push({ text: s.slice(at), hit: false });
    return out.length ? out : [{ text: s, hit: false }];
  }
  // The value of a facet as a person reads it.
  function facetValueText(name, value) {
    if (typeof value === 'boolean') return name === 'cleared' ? (value ? 'Cleared' : 'Not cleared') : (value ? 'Yes' : 'No');
    if (name === 'created') {
      const m = /^(\d{4})-(\d{2})$/.exec(String(value));
      return m ? ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'][+m[2] - 1] + ' ' + m[1] : String(value);
    }
    return String(value);
  }
  // Keys of the Find tab. `/` is the only one handled outside the results list; never inside a field, never off the Library screen.
  function findKeyAction(key, state) {
    const s = state || {};
    if (s.inField || s.screen !== 'library') return null;
    return key === '/' ? 'focus' : null;
  }
  // A field's draft (what is typed) as the list of values the server takes, or the refusal it would give. The server checks it again.
  function metaValues(field, draft) {
    const bad = (reason) => ({ ok: false, code: 'type_mismatch', reason });
    const d = draft == null ? '' : draft;
    const items = Array.isArray(d) ? d : (field.multi && field.type === 'text' ? String(d).split('\n') : [d]);
    const vals = [];
    for (const raw of items) {
      if (field.type === 'bool') { if (raw === true || raw === 'true') vals.push(true); else if (raw === false || raw === 'false') vals.push(false); continue; }
      const t = typeof raw === 'string' ? (field.type === 'longtext' ? raw : raw.trim()) : raw;
      if (t === '' || t == null) continue;
      if (field.type === 'number') {
        const n = Number(t);
        if (typeof t === 'string' && !/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(t)) return bad(field.label + ' must be a number.');
        if (!Number.isFinite(n)) return bad(field.label + ' must be a number.');
        vals.push(n);
      } else if (field.type === 'date') {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(t)) return bad(field.label + ' must be a date written YYYY-MM-DD.');
        vals.push(t);
      } else vals.push(String(t));
    }
    return { ok: true, values: vals };
  }

  // ---- per-show access: who may view, edit or run a show. The server decides; these only name things. ----
  // view reads and comments, edit builds the rundown, run is edit plus go, take, hold, release, skip and kill.
  const ACCESS_SPEC = {
    view: { icon: 'eye',     word: 'View', cls: 'st done', does: 'read the rundown and comment' },
    edit: { icon: 'sliders', word: 'Edit', cls: 'st next', does: 'build the rundown' },
    run:  { icon: 'take',    word: 'Run',  cls: 'st ok',   does: 'edit, and go, take, hold, release, skip and kill' },
  };
  function accessLevelSpec(level) {
    return Object.prototype.hasOwnProperty.call(ACCESS_SPEC, level) ? ACCESS_SPEC[level] : { icon: 'pending', word: String(level), cls: 'st', does: '' };
  }
  function accessSummary(rows) {
    if (Array.isArray(rows) && rows.length) {
      return { kind: 'restricted', icon: 'lock', word: 'Restricted', cls: 'st hold',
        text: 'Only the people and groups listed here, and every admin, can use this show. Removing the last row opens it again.' };
    }
    return { kind: 'open', icon: 'ready', word: 'Open', cls: 'st ok',
      text: 'Everyone who can sign in can use this show at their own role. Add a person or a group to restrict it.' };
  }
  // The names an admin can still add: enabled users, or groups, that are not already listed for this show.
  function accessCandidates(kind, users, groups, rows) {
    const listed = new Set((Array.isArray(rows) ? rows : []).filter((r) => r && r.subject_kind === kind).map((r) => r.subject));
    const pool = kind === 'group' ? groups : users;
    return (Array.isArray(pool) ? pool : []).filter((x) => x && typeof x.name === 'string' && !x.disabled_at && !listed.has(x.name)).map((x) => x.name);
  }
  // A refusal for a show the signed-in person may not use at that level. The server's reason names the level and nobody else.
  function forbiddenShow(e) {
    return { title: 'You do not have the access this needs on this show.', reason: (e && e.reason) || '',
      sub: 'Only an admin can change who has access to a show.' };
  }

  if (typeof document === 'undefined') {   // loaded by a test under node: export the pure functions and stop
    if (typeof module !== 'undefined') module.exports = { parseRate, framesToTc, tcToFrames, isDropLabel, lengthTc, fmtRate, deriveShow, STATE, stateChipSpec, WORKSPACES, DEFAULT_WORKSPACE, workspaceName, railFor, defaultScreen, ruleText, scheduleChip, localToUtc, utcToLocal, parseSlot, backTimes, themeName, nextTheme, shuttleNext, itemStateKind, cueStateKind, itemSeconds, itemPlan, frontTimes, fmtOffset, fmtSigned, scriptStats, scriptBlocks, legalMoves, resolveConflict, storyKind, authHeaders, beginSession, endSession, adoptSession, providerLinks, screenForKey, accessLevelSpec, accessSummary, accessCandidates, forbiddenShow, leaseView, leaseOutcome, leaseAllowsSave, presenceGroups, presenceLines ,
      SEARCH_SCOPES, FACET_BUILTIN, buildSearchRequest, savedQueryOf, searchParams, uiFromRequest, parseSearchResponse, queryTerms, splitHighlight, facetValueText, findKeyAction, metaValues, monthRange };
    return;
  }
  // ---- endpoints: the only paths this page calls. A test checks each exists on the server.
  const EP = {
    state:       ['GET',  '/api/state'],
    health:      ['GET',  '/api/health'],
    go:          ['POST', '/api/shows/{id}/go'],
    take:        ['POST', '/api/shows/{id}/take'],
    hold:        ['POST', '/api/shows/{id}/hold'],
    showSlot:    ['POST', '/api/shows/{id}/slot'],
    mosResolve:  ['POST', '/api/mos/{id}/resolve'],
    assetArchive: ['POST', '/api/assets/{id}/archive'],
    assetRehydrate: ['POST', '/api/assets/{id}/rehydrate'],
    itemSchedule: ['POST', '/api/items/{id}/schedule'],
    showRecurrence: ['POST', '/api/shows/{id}/recurrence'],
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
    audit:       ['GET',  '/api/audit'],
    showAccess:  ['GET',  '/api/shows/{id}/access'],
    showAccessSet: ['POST', '/api/shows/{id}/access'],
    users:       ['GET',  '/api/users'],
    groups:      ['GET',  '/api/groups'],
    ingest:      ['POST', '/api/ingest'],
    search:      ['GET',  '/api/search'],
    searches:    ['GET',  '/api/searches'],
    searchSave:  ['POST', '/api/searches'],
    searchDelete: ['POST', '/api/searches/{id}/delete'],
    bins:        ['GET',  '/api/bins'],
    bin:         ['GET',  '/api/bins/{id}'],
    binCreate:   ['POST', '/api/bins'],
    binAdd:      ['POST', '/api/bins/{id}/add'],
    binRemove:   ['POST', '/api/bins/{id}/remove'],
    binDelete:   ['POST', '/api/bins/{id}/delete'],
    schema:      ['GET',  '/api/schema'],
    fieldDefine: ['POST', '/api/schema/fields'],
    fieldRetire: ['POST', '/api/schema/fields/{id}/retire'],
    fieldTerms:  ['POST', '/api/schema/fields/{id}/terms'],
    metaGet:     ['GET',  '/api/assets/{id}/metadata'],
    metaSet:     ['POST', '/api/assets/{id}/metadata'],
    story:       ['GET',  '/api/items/{id}/story'],
    scriptSave:  ['POST', '/api/items/{id}/script'],
    storyStatus: ['POST', '/api/items/{id}/story-status'],
    itemFloat:   ['POST', '/api/items/{id}/float'],
    showPace:    ['POST', '/api/shows/{id}/reading-pace'],
    prompterTicket: ['POST', '/api/shows/{id}/prompter-ticket'],
    wireList:    ['GET',  '/api/wire'],
    wireTake:    ['POST', '/api/wire/{id}/take'],
    wireDismiss: ['POST', '/api/wire/{id}/dismiss'],
    templateList: ['GET',  '/api/templates'],
    templateCreate: ['POST', '/api/templates'],
    templateUse: ['POST', '/api/templates/{id}/instantiate'],
    templateDelete: ['POST', '/api/templates/{id}/delete'],
    stories:     ['GET',  '/api/stories'],
    itemCopy:    ['POST', '/api/items/{id}/copy'],
    leaseAcquire: ['POST', '/api/items/{id}/lease'],
    leaseRelease: ['POST', '/api/items/{id}/lease/release'],
    leaseTakeover: ['POST', '/api/items/{id}/lease/takeover'],
    presence:    ['POST', '/api/presence'],
  };
  // Actions that run one at a time on the server (the sequencing lock). Hold, release, rights
  // and publish do not wait on it, so HOLD stays pressable while a take waits on the room.
  const SEQUENCED = new Set(['go', 'take', 'skip', 'kill', 'reorder', 'recordStart', 'recordStop', 'attach']);

  const POLL_MS = 2000, POLL_HIDDEN_MS = 6000, TAKE_COOLDOWN_MS = 1500, ARM_MS = 4000;
  const SCREENS = ['show', 'record', 'rights', 'publish', 'library', 'activity', 'access', 'scripts'];

  const ERR_LABEL = {
    held: 'Held', not_cleared: 'Not cleared', no_media: 'No media', cue_failed: 'Cue failed',
    cue_unsupported: 'Cue unsupported', no_room: 'No room', room_refused: 'Room refused',
    not_wrapped: 'Not wrapped', publish_failed: 'Publish failed', not_found: 'Not found',
    not_skippable: 'Cannot skip', not_killable: 'Cannot kill', not_attachable: 'Cannot attach',
    bad_input: 'Bad input', unauthorized: 'Token needed', bad_host: 'Wrong host',
    forbidden_role: 'Admin only', forbidden_show: 'No access to this show',
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
    type_mismatch: 'Wrong kind of value', not_in_vocabulary: 'Not in the list', required_field: 'Required field', unknown_field: 'Field does not apply',
    field_exists: 'Field exists', type_locked: 'Type is locked', bin_full: 'Bin is full',
    stale_script: 'Story changed', story_killed: 'Story killed', bad_transition: 'Move not allowed', not_approver: 'Not an approver',
    not_floatable: 'Cannot float', story_not_approved: 'Not approved', already_taken: 'Already taken', wrong_kind: 'Wrong template kind',
    leased: 'Being edited', lease_lost: 'Edit lease lost', stale_item: 'Item changed', not_leased: 'Not being edited', collab_unavailable: 'Collaboration unavailable',
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
    'deck.play': 'Deck: play', 'deck.stop': 'Deck: stop', 'deck.goto': 'Deck: go to clip',
    'deck.record.start': 'Deck: start record', 'deck.record.stop': 'Deck: stop record',
  };
  const REC_STEPS = ['requested', 'preparing', 'producing', 'stopping', 'finalizing', 'completed'];
  const REC_ALIAS = { starting: 'preparing', live: 'producing' };

  const SESS = { csrf: '' };   // set while a cookie session is in use; memory only, never stored
  const S = {
    data: null, show: null, screen: 'show', epoch: 0, seq: false, busy: false,
    pending: new Set(), lastOk: 0, conn: 'connecting', selected: null, armed: null,
    flash: null, cueDetail: {}, cueLast: {}, lastTake: 0, pubFilter: '', sigs: {},
    cmds: null, cmdsTried: 0, ws: DEFAULT_WORKSPACE,
    // Show building drafts: what is typed but not yet sent. The server's answer replaces the screen, never the draft.
    build: { recOpen: false, rec: { freq: 'daily', at: '20:00', tz: (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch (e) { return 'UTC'; } })(), days: [0] }, after: '', sched: '', slot: '', title: '', slug: '', renameId: null, rename: '', pick: '', cmd: 'overlay.take', payload: '' },
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

  // The story summary every /rundown item carries (never the script text). An older server sends none: draft, rev 1, media length.
  function storyFields(i) {
    return { storyStatus: i.storyStatus || 'draft', scriptRev: Number.isInteger(i.scriptRev) ? i.scriptRev : 1,
      words: Number.isInteger(i.words) ? i.words : 0, readSeconds: Number.isFinite(i.readSeconds) ? i.readSeconds : 0,
      floated: i.floated === true, lengthSource: i.lengthSource || 'media' };
  }
  // ---- the one place that knows the /rundown shape: {hold, items} ----------
  function parseRundown(raw) {
    const r = raw || {};
    const hold = r.hold ? { reason: r.hold.reason || '', heldAt: r.hold.heldAt || '' } : null;
    const items = (r.items || []).map((i) => ({
      id: i.id, slug: i.slug, status: i.status, urls: i.urls || [],
      scheduledAt: i.scheduledAt || null, scheduleState: i.scheduleState || null, scheduleNote: i.scheduleNote || null,
      scheduleMode: i.scheduleMode || null, scheduleOffsetSeconds: Number.isInteger(i.scheduleOffsetSeconds) ? i.scheduleOffsetSeconds : null,
      asRunIn: i.asRunInPrecise || i.asRunIn || null, asRunOut: i.asRunOutPrecise || i.asRunOut || null,
      ...storyFields(i),
    }));
    return { hold, items, slotSeconds: Number.isInteger(r.slotSeconds) ? r.slotSeconds : null, readWpm: Number.isInteger(r.readWpm) ? r.readWpm : 160,
      recurrence: r.recurrence && typeof r.recurrence === 'object' ? r.recurrence : null, recurrenceOf: r.recurrenceOf || null };
  }

  // ---- network -------------------------------------------------------------
  function getToken() { try { return sessionStorage.getItem('volt.token') || ''; } catch (e) { return ''; } }
  function setToken(v) {
    try { v ? sessionStorage.setItem('volt.token', v) : sessionStorage.removeItem('volt.token'); } catch (e) { /* private mode */ }
  }

  // `query` is an object of GET parameters (library reads); `body` is the JSON of a POST.
  async function call(kind, id, body, query) {
    const [method, tpl] = EP[kind];
    let path = tpl.replace('{id}', encodeURIComponent(id || '').replace(/%3A/gi, ':'));   // a colon is legal in a path, and `archive:<id>` needs it
    if (kind === 'state' && S.show) path += '?show=' + encodeURIComponent(S.show);
    if (query) path += '?' + new URLSearchParams(query).toString();
    const headers = authHeaders(method, getToken(), SESS.csrf);
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
  const WHO = { name: '', role: '' };   // who the server says this is; a hint for what to show, never what is allowed
  async function showWhoami() {
    const token = getToken();
    if (!token && !SESS.csrf) { whoamiFor = null; WHO.name = ''; WHO.role = '';$('token-btn').textContent = 'Token'; $('who').hidden = true; return; }
    const key = token || 'session:' + SESS.csrf;
    if (whoamiFor === key) return;
    whoamiFor = key;
    try {
      const res = await fetch('/api/whoami', { headers: authHeaders('GET', token, ''), cache: 'no-store' });
      const who = res.ok ? await res.json() : null;
      WHO.name = who && typeof who.name === 'string' ? who.name : ''; WHO.role = who && typeof who.role === 'string' ? who.role : '';
      $('token-btn').textContent = who && who.name ? who.name + ' (' + who.role + ')' : 'Token';
      // The pill names the person and their access level; it stays hidden when the server does not say.
      const pill = $('who');
      pill.hidden = !(who && who.name);
      if (!pill.hidden) pill.replaceChildren(icon('user', 's'), el('span', { text: who.name + ' · ' + who.role }),
        who.role === 'admin' ? el('a', { href: '#activity', class: 'pill-link', text: 'Activity' }) : null,
        who.role === 'admin' ? el('a', { href: '#access', class: 'pill-link', text: 'Access' }) : null);
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
      if (status === 401) { SESS.csrf = ''; setConn('token'); if (!$('token-dlg').open) openToken(); return; }   // a session that expired is gone
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
    if (e.code === 'forbidden_show') Object.assign(f, forbiddenShow(e));
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

  // The archive state of an asset's hi-res, and the one control that fits it: Archive (two presses) when it is local and free to
  // move, Restore when it is cold. A job in flight shows as Archiving or Restoring instead of a button.
  function archiveBits(a) {
    if (!a || !a.id) return [];
    const busy = (a.jobs || []).find((j) => (j.type === 'archive' || j.type === 'restore') && j.status !== 'failed');
    const key = 'archive:' + a.id;
    if (a.tier === 'archived' || a.tier === 'restoring') {
      return [stateChip(a.tier), a.tier === 'archived' && !busy ? btn('Restore', { key: 'rehydrate:' + a.id, cls: 'ghost small', aria: 'Restore the archived hi-res of ' + short(a.id),
        onclick: () => act('assetRehydrate', a.id, {}, { key: 'rehydrate:' + a.id, okTitle: 'Restore queued.', ok: () => 'The hi-res of ' + short(a.id) + ' is coming back. It can air and publish again when it is online.' }) }) : null];
    }
    if (busy) return [stateChip('archiving', busy.type === 'restore' ? 'Restoring' : 'Archiving')];
    if (a.open || a.legalHold || !a.file) return [];
    return [btn(S.armed === key ? 'Confirm archive' : 'Archive', { key: 'archive-b:' + a.id, cls: 'ghost small' + (S.armed === key ? ' armed' : ''),
      aria: (S.armed === key ? 'Confirm archive of ' : 'Archive ') + short(a.id),
      onclick: () => armedAction(key, () => act('assetArchive', a.id, {}, { key: 'archive:' + a.id, okTitle: 'Archive queued.',
        ok: () => 'The hi-res of ' + short(a.id) + ' is uploaded and checked, then the local file is removed. Playback of the proxy is unaffected.' })) })];
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
    if (S.screen === 'activity') renderActivity();
    if (S.screen === 'scripts') renderScripts();
    if (S.screen === 'access') renderAccess();
    leaseSync();
  }

  // ---- Library: Find and Details --------------------------------------------------------------------------------------------
  // Find asks the server one question (GET /api/search) and lists assets, each with up to three hits. Details is one asset's typed
  // metadata as a form built from GET /api/schema. Nothing is shown before the server has said it, every write is read back, and
  // a refusal is shown beside what was refused with its code. Saved searches and bins belong to whoever made them: the server
  // decides who may see or change one, and this page only hides a button the server would refuse.
  const FIND_PAGE = 25, FACET_SHOW = 6, META_FACETS_MAX = 4;
  const SORT_CHOICES = [['auto', 'Best match, or newest without a search'], ['recent', 'Newest first'], ['show', 'By show']];
  const SINGLE_FACETS = new Set(['created', 'cleared']);   // a month is one range; Cleared and Not cleared together would constrain nothing
  const F = {
    q: '', scope: 'all', sort: 'auto', sel: {}, incomplete: false, extra: [], formKey: 0,
    res: null, load: 'idle', err: null, tok: 0, expanded: {}, ran: false, seek: null, fields: null,
    saved: { list: [], load: 'idle', err: null, name: '', shared: false, armDel: null },
    bins: { list: [], load: 'idle', err: null, cur: null, members: [], mload: 'idle', name: '', shared: false, armDel: null },
  };
  const metaFacetFields = () => (F.fields || []).filter((f) => !f.retired && (f.type === 'enum' || f.type === 'bool')).slice(0, META_FACETS_MAX);
  const findFacetNames = () => FACET_BUILTIN.map((f) => f[0]).concat(metaFacetFields().map((f) => 'meta.' + f.key));
  function facetLabel(name) {
    const b = FACET_BUILTIN.find((f) => f[0] === name);
    if (b) return b[1];
    const f = (F.fields || []).find((x) => 'meta.' + x.key === name);
    return f ? f.label : name;
  }
  const findUi = () => ({ q: F.q, scope: F.scope, sort: F.sort, sel: F.sel, incomplete: F.incomplete, extra: F.extra });
  const filterCount = () => Object.values(F.sel).filter((v) => v.length).length + (F.incomplete ? 1 : 0) + F.extra.length;
  const netFail = () => ({ code: 'network', reason: 'The server did not answer.' });

  function findFocus() {
    if (W.tab !== 'find') setTab('find', false);
    setTimeout(() => { const n = $('find-q'); if (n) { n.focus(); n.select(); } }, 0);
  }

  async function findFields() {
    try {
      const { status, data } = await call('schema');
      if (status === 200 && data && data.ok && Array.isArray(data.fields)) F.fields = data.fields;
      else if (F.fields == null) F.fields = [];
    } catch (e) { if (F.fields == null) F.fields = []; }
  }

  async function findRun(more) {
    const tok = ++F.tok;
    if (!more) { F.load = 'loading'; F.err = null; }
    render();
    try {
      const req = buildSearchRequest(Object.assign(findUi(), { limit: FIND_PAGE, after: more && F.res ? F.res.next : null }));
      const { status, data } = await call('search', null, null, searchParams(req, findFacetNames()));
      if (tok !== F.tok) return;
      if (status === 401) { setConn('token'); openToken(); }
      const r = parseSearchResponse(data);
      if (!r.ok) throw Object.assign(new Error(codeOf(data, status).reason), { code: codeOf(data, status).code });
      r.q = req.q || '';
      F.res = more && F.res ? Object.assign(r, { assets: F.res.assets.concat(r.assets) }) : r;
      F.load = 'ready'; F.err = null;
    } catch (e) {
      if (tok !== F.tok) return;
      F.load = 'error'; F.err = e.code ? { code: e.code, reason: e.message } : netFail();
    }
    render();
  }

  async function findSaved() {
    const s = F.saved;
    s.load = 'loading';
    try {
      const { status, data } = await call('searches');
      if (status === 200 && data && data.ok) { s.list = Array.isArray(data.searches) ? data.searches : []; s.err = null; s.load = 'ready'; }
      else { s.err = codeOf(data, status).code + ': ' + codeOf(data, status).reason; s.load = 'error'; }
    } catch (e) { s.err = netFail().reason; s.load = 'error'; }
    render();
  }

  async function findBins() {
    const b = F.bins;
    b.load = 'loading';
    try {
      const { status, data } = await call('bins');
      if (status === 200 && data && data.ok) { b.list = Array.isArray(data.bins) ? data.bins : []; b.err = null; b.load = 'ready'; if (b.cur && !b.list.some((x) => x.id === b.cur)) { b.cur = null; b.members = []; } }
      else { b.err = codeOf(data, status).code + ': ' + codeOf(data, status).reason; b.load = 'error'; }
    } catch (e) { b.err = netFail().reason; b.load = 'error'; }
    render();
  }

  async function binOpen(id) {
    const b = F.bins;
    b.cur = id; b.members = []; b.mload = 'loading';
    render();
    try {
      const { status, data } = await call('bin', id);
      if (b.cur !== id) return;
      if (status === 200 && data && data.ok) { b.members = Array.isArray(data.members) ? data.members : []; b.mload = 'ready'; }
      else { b.mload = 'error'; b.members = []; setFlash({ kind: 'bad', title: 'The bin could not be read.', code: codeOf(data, status).code, reason: codeOf(data, status).reason }); }
    } catch (e) { b.mload = 'error'; }
    render();
  }

  // One write: the page shows what the server returned or refused, then reads the lists again. `key` is the busy key of the button.
  async function findAct(kind, id, body, o) {
    if (S.pending.has(o.key)) return null;
    S.pending.add(o.key);
    render();
    let out = null;
    try {
      const { status, data } = await call(kind, id, body);
      if (status === 401) { setConn('token'); openToken(); }
      if (data && data.ok) {
        out = data;
        if (o.after) await o.after(data);
        const reason = o.ok ? o.ok(data) : '';
        setFlash({ kind: 'good', title: o.okTitle || 'Done', reason });
        announce((o.okTitle || 'Done') + ' ' + reason);
      } else {
        const e = codeOf(data, status);
        if (o.fail) o.fail(e);
        setFlash({ kind: 'bad', title: (ERR_LABEL[e.code] || 'Refused') + '.', code: e.code, reason: e.reason });
        announce((ERR_LABEL[e.code] || 'Refused') + '. ' + e.reason);
      }
    } catch (err) {
      setFlash({ kind: 'bad', title: 'No answer from the server.', code: 'network', reason: 'The request may or may not have landed. Read the list again before trying twice.' });
      setConn('lost');
    } finally {
      S.pending.delete(o.key);
      render();
    }
    return out;
  }

  // Open an asset from a result. A speech hit also seeks the player to it once the proxy is ready.
  function findOpen(assetId, hit) {
    const frame = hit && hit.kind === 'transcript' && Number.isInteger(hit.frameIn) ? hit.frameIn : null;
    if (assetId !== L.assetId) setAsset(assetId);
    F.seek = frame == null ? null : { asset: assetId, frame };
    setTab('shots', false);
  }
  function applySeek() {
    const k = F.seek;
    if (!k || k.asset !== L.assetId || !L.live || W.tab !== 'shots' || !videoReady()) return;
    F.seek = null;
    if (seekTo(k.frame)) { L.cur = null; announce('Moved to the hit.'); }
  }

  function toggleFacet(name, value) {
    const have = F.sel[name] || [];
    const on = have.some((v) => v === value);
    F.sel[name] = SINGLE_FACETS.has(name) || /^meta\./.test(name) && typeof value === 'boolean' ? (on ? [] : [value]) : (on ? have.filter((v) => v !== value) : have.concat([value]));
    if (!F.sel[name].length) delete F.sel[name];
    findRun(false);
  }
  function clearFilters() { F.sel = {}; F.incomplete = false; F.extra = []; findRun(false); }

  function findApply(query) {
    const ui = uiFromRequest(query);
    F.q = ui.q; F.scope = SEARCH_SCOPES.some((s) => s[0] === ui.scope) ? ui.scope : 'all';
    F.sort = SORT_CHOICES.some((s) => s[0] === ui.sort) ? ui.sort : 'auto';
    F.sel = ui.sel; F.incomplete = ui.incomplete; F.extra = ui.extra; F.formKey++;
    findRun(false);
  }

  function findSave() {
    const s = F.saved, name = s.name.trim();
    if (!name) { setFlash({ kind: 'bad', title: 'Name the search first.', code: 'bad_input', reason: 'A saved search needs a name of 1 to 100 characters.' }); render(); return; }
    findAct('searchSave', null, { name, query: savedQueryOf(buildSearchRequest(findUi())), shared: s.shared }, {
      key: 'find:save', okTitle: 'Search saved.', ok: (d) => '“' + d.search.name + '” is in your list' + (d.search.shared ? ' and shared with every operator.' : '.'),
      after: async () => { s.name = ''; await findSaved(); } });
  }
  function findDeleteSearch(item) {
    const s = F.saved;
    if (s.armDel !== item.id) { s.armDel = item.id; render(); return; }
    s.armDel = null;
    findAct('searchDelete', item.id, {}, { key: 'find:delsearch:' + item.id, okTitle: 'Saved search deleted.', ok: () => '', after: findSaved });
  }
  function binCreate() {
    const b = F.bins, name = b.name.trim();
    if (!name) { setFlash({ kind: 'bad', title: 'Name the bin first.', code: 'bad_input', reason: 'A bin needs a name of 1 to 100 characters.' }); render(); return; }
    findAct('binCreate', null, { name, shared: b.shared }, { key: 'find:bincreate', okTitle: 'Bin made.', ok: (d) => '“' + d.bin.name + '” is empty. Choose it, then add assets from the results.',
      after: async (d) => { b.name = ''; await findBins(); await binOpen(d.bin.id); } });
  }
  function binDelete(item) {
    const b = F.bins;
    if (b.armDel !== item.id) { b.armDel = item.id; render(); return; }
    b.armDel = null;
    findAct('binDelete', item.id, {}, { key: 'find:bindel:' + item.id, okTitle: 'Bin deleted.', ok: () => 'The assets in it are untouched.', after: async () => { if (b.cur === item.id) { b.cur = null; b.members = []; } await findBins(); } });
  }
  function binAddAsset(assetId) {
    const b = F.bins, id = b.cur;
    if (!id) return;
    findAct('binAdd', id, { assetId }, { key: 'find:add:' + assetId, okTitle: 'Added to the bin.', ok: (d) => (d.added ? '' : 'It was already in the bin.'),
      after: async () => { await findBins(); await binOpen(id); } });
  }
  function binRemoveMember(m) {
    const b = F.bins, id = b.cur, key = 'find:rm:' + m.assetId + ':' + (m.spanId || '');
    findAct('binRemove', id, m.spanId ? { assetId: m.assetId, spanId: m.spanId } : { assetId: m.assetId }, { key, okTitle: 'Removed from the bin.', ok: () => 'The asset itself is untouched.',
      after: async () => { await findBins(); await binOpen(id); } });
  }

  let findWired = false;
  function findWire() {
    if (findWired) return;
    findWired = true;
    // Up and Down walk the results; Enter and Space are the buttons' own.
    $('lib-find-results').addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      const items = [...$('lib-find-results').querySelectorAll('[data-nav]')];
      const at = items.indexOf(document.activeElement);
      if (at < 0) return;
      e.preventDefault();
      items[e.key === 'ArrowDown' ? Math.min(at + 1, items.length - 1) : Math.max(at - 1, 0)].focus();
    });
  }

  function renderFind() {
    findWire();
    if (!F.ran) {
      F.ran = true;
      findSaved(); findBins();
      findFields().then(() => findRun(false));
    }
    paintFindForm();
    paintFindState();
    paintFindResults();
    paintFacets();
    paintSaved();
    paintBins();
  }

  function paintFindForm() {
    const busy = F.load === 'loading';
    paint('lib-find-form', [F.formKey, F.scope, F.sort, F.incomplete, busy], () => el('form', { class: 'find-form', role: 'search', 'aria-label': 'Search the library', autocomplete: 'off',
      onsubmit: (e) => { e.preventDefault(); findRun(false); } },
      el('div', { class: 'form-row' },
        el('div', { class: 'grow' }, el('label', { for: 'find-q', text: 'Search speech and details' }),
          el('input', { id: 'find-q', type: 'text', 'data-fk': 'find-q', value: F.q, maxlength: 500, autocomplete: 'off', spellcheck: 'false', 'aria-keyshortcuts': '/',
            placeholder: 'a name, “a phrase”, -leave out',
            oninput: (e) => { F.q = e.target.value; },
            onkeydown: (e) => {
              if (e.key === 'ArrowDown') { const n = $('lib-find-results').querySelector('[data-nav]'); if (n) { e.preventDefault(); n.focus(); } }
              if (e.key === 'Escape' && e.target.value) { e.preventDefault(); e.stopPropagation(); e.target.value = ''; F.q = ''; findRun(false); }
            } })),
        el('button', { type: 'submit', class: 'btn', 'data-fk': 'find-go', disabled: busy }, icon('search', 's'), ' Search')),
      el('div', { class: 'bar' },
        el('div', { class: 'scopes', role: 'group', 'aria-label': 'Where to look' }, SEARCH_SCOPES.map(([v, label]) => el('button', { type: 'button', class: 'pill', 'data-fk': 'scope:' + v,
          'aria-pressed': String(F.scope === v), onclick: () => { F.scope = v; findRun(false); } }, label))),
        el('div', {}, el('label', { for: 'find-sort', class: 'vh', text: 'Order' }),
          el('select', { id: 'find-sort', 'data-fk': 'find-sort', onchange: (e) => { F.sort = e.target.value; findRun(false); } },
            SORT_CHOICES.map(([v, label]) => el('option', { value: v, selected: F.sort === v }, label)))),
        el('label', { class: 'chk' }, el('input', { type: 'checkbox', 'data-fk': 'find-incomplete', checked: F.incomplete, onchange: (e) => { F.incomplete = e.target.checked; findRun(false); } }),
          el('span', { text: 'Only assets missing a required detail' })))));
  }

  function paintFindState() {
    const r = F.res;
    paint('lib-find-state', [F.load, F.err, r && [r.total, r.assets.length, r.degraded], F.extra.length], () => {
      if (F.load === 'error') {
        return el('div', { class: 'empty' }, el('p', { role: 'alert' }, stateChip('failed'), ' ', (F.err.code ? F.err.code + ': ' : '') + F.err.reason),
          el('button', { type: 'button', class: 'btn ghost small', 'data-fk': 'find-retry', onclick: () => findRun(false) }, 'Try again'));
      }
      if (F.load === 'loading' && !r) return el('p', { class: 'note', role: 'status', text: 'Searching…' });
      if (!r) return [];
      const words = r.total === 1 ? '1 asset matches.' : r.total + ' assets match.';
      return el('div', { class: 'note', role: 'status' }, words + (r.assets.length < r.total ? ' Showing ' + r.assets.length + '.' : ''),
        r.degraded ? [' ', stateChip('stale', 'Slow text search'), ' The full-text index is not available, so a word search reads the text directly and lists at most 25.'] : null,
        F.extra.length ? ' ' + F.extra.length + ' saved condition' + (F.extra.length > 1 ? 's' : '') + ' this screen cannot show still apply.' : null);
    });
  }

  function metaLine(a) {
    const byKey = new Map((F.fields || []).map((f) => [f.key, f]));
    const parts = [];
    for (const key of Object.keys(a.metadata || {})) {
      const f = byKey.get(key), vals = Array.isArray(a.metadata[key]) ? a.metadata[key] : [];
      if (!vals.length) continue;
      parts.push((f ? f.label : key) + ': ' + vals.map((v) => (typeof v === 'boolean' ? (v ? 'Yes' : 'No') : String(v))).join(', '));
    }
    return parts.slice(0, 5).join(' · ');
  }

  function hitNode(a, h, q) {
    const kind = HIT_KIND[h.kind] || String(h.kind);
    const conf = typeof h.confidence === 'number' ? ' · ' + Math.round(h.confidence * 100) + '%' : '';
    return el('li', {}, el('button', { type: 'button', class: 'fhit', 'data-nav': '1', 'data-fk': 'hit:' + a.assetId + ':' + (h.spanId || h.labelId || h.kind),
      'aria-label': kind + (h.tcIn ? ' at ' + h.tcIn : '') + ' in ' + (a.camera || short(a.assetId)) + ': ' + h.snippet.slice(0, 80),
      onclick: () => findOpen(a.assetId, h) },
      el('span', { class: 'chip kind', text: kind }), h.tcIn ? el('span', { class: 'tc mono', text: h.tcIn + conf }) : (conf ? el('span', { class: 'tc mono', text: conf.slice(3) }) : null),
      el('span', { class: 'tx' }, splitHighlight(h.snippet, q).map((p) => (p.hit ? el('mark', { text: p.text }) : p.text)))));
  }

  function paintFindResults() {
    const r = F.res, bin = F.bins.list.find((b) => b.id === F.bins.cur);
    paint('lib-find-results', [r, bin && bin.name, [...S.pending].filter((k) => k.startsWith('find:add:')), F.load], () => {
      if (!r) return F.load === 'error' ? [] : el('p', { class: 'empty', text: 'Reading the library…' });
      if (!r.assets.length) return el('p', { class: 'empty', text: filterCount() || F.q ? 'Nothing matches. Take a word or a filter away.' : 'There is nothing in the library yet. Record or ingest something first.' });
      return [
        el('ul', { class: 'finds' }, r.assets.map((a) => {
          const line = metaLine(a);
          return el('li', { class: 'find-row' },
            el('div', { class: 'find-head' },
              el('button', { type: 'button', class: 'rowname', 'data-nav': '1', 'data-fk': 'asset:' + a.assetId, onclick: () => findOpen(a.assetId, null) }, (a.camera || 'Asset') + (a.show ? ' · ' + a.show : '')),
              el('small', { class: 'mono muted', text: short(a.assetId) }),
              stateChip(a.cleared ? 'cleared' : 'uncleared'), a.legalHold ? stateChip('hold') : null, a.archived ? stateChip('archived') : null, a.incomplete ? stateChip('incomplete') : null,
              el('span', { class: 'spacer' }),
              bin ? btn('Add to ' + bin.name, { key: 'find:add:' + a.assetId, cls: 'ghost small', aria: 'Add ' + (a.camera || short(a.assetId)) + ' to the bin ' + bin.name, onclick: () => binAddAsset(a.assetId) }) : null),
            line ? el('small', { class: 'find-meta', text: line }) : null,
            a.hits.length ? el('ul', { class: 'find-hits' }, a.hits.map((h) => hitNode(a, h, r.q))) : null);
        })),
        r.next ? el('div', { class: 'bar' }, btn('Show more', { key: 'find:more', cls: 'ghost', disabled: F.load === 'loading', onclick: () => findRun(true) })) : null,
      ];
    });
  }

  function paintFacets() {
    const r = F.res;
    paint('lib-find-facets', [r && r.facets, F.sel, F.incomplete, F.expanded, F.fields, F.extra.length], () => {
      if (!r) return el('p', { class: 'empty', text: 'Filters appear with the first results.' });
      const out = [];
      if (filterCount()) out.push(el('div', { class: 'bar' }, btn('Clear ' + filterCount() + ' filter' + (filterCount() > 1 ? 's' : ''), { key: 'find:clear', cls: 'ghost small', onclick: clearFilters })));
      for (const name of findFacetNames()) {
        const f = r.facets[name];
        const chosen = F.sel[name] || [];
        if ((!f || !f.values.length) && !chosen.length) continue;
        const values = f ? f.values : [];
        const open = !!F.expanded[name];
        const shown = values.filter((v, i) => open || i < FACET_SHOW || chosen.some((c) => c === v.value));
        out.push(el('fieldset', { class: 'facet' }, el('legend', { text: facetLabel(name) }),
          shown.map((v) => el('label', { class: 'chk' },
            el('input', { type: 'checkbox', 'data-fk': 'fv:' + name + ':' + String(v.value), checked: chosen.some((c) => c === v.value), onchange: () => toggleFacet(name, v.value) }),
            el('span', { text: facetValueText(name, v.value) }), el('span', { class: 'n', text: String(v.count) }))),
          values.length > shown.length || open && values.length > FACET_SHOW
            ? el('button', { type: 'button', class: 'btn ghost small', 'data-fk': 'more:' + name, 'aria-expanded': String(open),
              onclick: () => { F.expanded[name] = !open; render(); } }, open ? 'Show fewer' : 'Show ' + (values.length - shown.length) + ' more') : null,
          f && f.other ? el('small', { class: 'hint', text: '+' + f.other + ' more not listed' }) : null));
      }
      if (!out.length) out.push(el('p', { class: 'empty', text: 'Nothing to narrow by yet.' }));
      return out;
    });
  }

  const mineOrUnknown = (owner) => !WHO.name || owner === WHO.name || WHO.role === 'admin';

  function paintSaved() {
    const s = F.saved;
    paint('lib-find-saved', [s.list, s.load, s.err, s.armDel, WHO.name, WHO.role, [...S.pending].filter((k) => k.startsWith('find:'))], () => [
      s.err ? el('p', { class: 'empty', role: 'alert', text: s.err }) : null,
      s.list.length ? el('ul', { class: 'plain side' }, s.list.map((x) => el('li', {},
        el('div', {}, el('button', { type: 'button', class: 'rowname', 'data-fk': 'run:' + x.id, 'aria-label': 'Run the saved search ' + x.name, onclick: () => findApply(x.query) }, x.name),
          el('small', { class: 'block muted' }, 'by ' + x.owner + ' ', x.shared ? stateChip('shared') : null)),
        mineOrUnknown(x.owner) ? btn(s.armDel === x.id ? 'Press again to delete' : 'Delete', { key: 'find:delsearch:' + x.id, cls: 'ghost small' + (s.armDel === x.id ? ' danger' : ''),
          aria: 'Delete the saved search ' + x.name, onclick: () => findDeleteSearch(x) }) : null)))
        : (s.err ? null : el('p', { class: 'empty', text: s.load === 'loading' ? 'Reading…' : 'None yet. Search, then save the question under a name.' })),
      el('form', { class: 'form-row', 'aria-label': 'Save this search', onsubmit: (e) => { e.preventDefault(); findSave(); } },
        el('div', { class: 'grow' }, el('label', { for: 'find-save-name', text: 'Save this search as' }),
          el('input', { id: 'find-save-name', type: 'text', 'data-fk': 'find-save-name', maxlength: 100, autocomplete: 'off', value: s.name, oninput: (e) => { s.name = e.target.value; } })),
        el('label', { class: 'chk' }, el('input', { type: 'checkbox', 'data-fk': 'find-save-shared', checked: s.shared, onchange: (e) => { s.shared = e.target.checked; } }), el('span', { text: 'Share with every operator' })),
        el('button', { type: 'submit', class: 'btn', 'data-fk': 'find-save', disabled: S.pending.has('find:save') }, 'Save')),
    ]);
  }

  function memberName(m) {
    const a = ((S.data && S.data.assets) || []).find((x) => x.id === m.assetId);
    const base = a ? (a.camera || a.show || 'Asset') : 'Asset ' + short(m.assetId);
    return m.spanId ? base + ' · ' + (m.label || (m.kind === 'shot' ? 'Shot' : 'Clip')) + (m.tcIn ? ' ' + m.tcIn + ' to ' + m.tcOut : '') : base;
  }

  function paintBins() {
    const b = F.bins;
    paint('lib-find-bins', [b.list, b.load, b.err, b.cur, b.members, b.mload, b.armDel, WHO.name, WHO.role, (S.data && S.data.assets || []).length, [...S.pending].filter((k) => k.startsWith('find:'))], () => [
      b.err ? el('p', { class: 'empty', role: 'alert', text: b.err }) : null,
      b.list.length ? el('ul', { class: 'plain side' }, b.list.map((x) => el('li', {},
        el('div', {}, el('button', { type: 'button', class: 'rowname', 'data-fk': 'bin:' + x.id, 'aria-current': b.cur === x.id ? 'true' : null,
          'aria-label': 'Open the bin ' + x.name + ', ' + x.count + ' item' + (x.count === 1 ? '' : 's'), onclick: () => binOpen(x.id) }, x.name),
          el('small', { class: 'block muted' }, x.count + ' item' + (x.count === 1 ? '' : 's') + ' · by ' + x.owner + ' ', x.shared ? stateChip('shared') : null)),
        mineOrUnknown(x.owner) ? btn(b.armDel === x.id ? 'Press again to delete' : 'Delete', { key: 'find:bindel:' + x.id, cls: 'ghost small' + (b.armDel === x.id ? ' danger' : ''),
          aria: 'Delete the bin ' + x.name, onclick: () => binDelete(x) }) : null)))
        : (b.err ? null : el('p', { class: 'empty', text: b.load === 'loading' ? 'Reading…' : 'No bins yet. A bin is a short list of assets to come back to.' })),
      b.cur ? el('div', { class: 'members' }, el('h3', { class: 'sub', text: 'In ' + ((b.list.find((x) => x.id === b.cur) || {}).name || 'this bin') }),
        b.mload === 'loading' ? el('p', { class: 'empty', role: 'status', text: 'Reading…' })
          : b.members.length ? el('ul', { class: 'plain side' }, b.members.map((m) => el('li', {},
            el('div', {}, el('button', { type: 'button', class: 'rowname', 'data-fk': 'open:' + m.assetId + ':' + (m.spanId || ''), onclick: () => findOpen(m.assetId, null) }, memberName(m)),
              m.gone ? el('small', { class: 'block' }, stateChip('stale', 'Clip gone')) : null, m.assetDeleted ? el('small', { class: 'block' }, stateChip('withdrawn', 'Asset deleted')) : null),
            btn('Remove', { key: 'find:rm:' + m.assetId + ':' + (m.spanId || ''), cls: 'ghost small', aria: 'Remove ' + memberName(m) + ' from the bin', onclick: () => binRemoveMember(m) }))))
            : el('p', { class: 'empty', text: 'Empty. Choose Add to this bin on a result.' })) : null,
      el('form', { class: 'form-row', 'aria-label': 'Make a bin', onsubmit: (e) => { e.preventDefault(); binCreate(); } },
        el('div', { class: 'grow' }, el('label', { for: 'bin-name', text: 'New bin' }),
          el('input', { id: 'bin-name', type: 'text', 'data-fk': 'bin-name', maxlength: 100, autocomplete: 'off', value: b.name, oninput: (e) => { b.name = e.target.value; } })),
        el('label', { class: 'chk' }, el('input', { type: 'checkbox', 'data-fk': 'bin-shared', checked: b.shared, onchange: (e) => { b.shared = e.target.checked; } }), el('span', { text: 'Share with every operator' })),
        el('button', { type: 'submit', class: 'btn', 'data-fk': 'bin-make', disabled: S.pending.has('find:bincreate') }, 'Make bin')),
    ]);
  }

  // ---- Details: one asset's metadata, as a form made from the schema ------------------------------------------------------
  const DT = { for: null, tok: 0, load: 'idle', err: null, fields: [], values: {}, missing: [], draft: {}, errors: {}, arm: null, def: { err: null, key: '', label: '', type: 'text', scope: 'all', required: false, multi: false }, term: {} };
  const FIELD_TYPES = [['text', 'Text'], ['longtext', 'Long text'], ['number', 'Number'], ['date', 'Date'], ['bool', 'Yes or no'], ['enum', 'Choice from a list']];

  function draftOf(f, vals) {
    const v = Array.isArray(vals) ? vals : [];
    if (f.type === 'bool') return v.length ? String(v[0]) : '';
    if (f.type === 'enum') return f.multi ? v.slice() : (v.length ? String(v[0]) : '');
    if (f.multi) return v.join('\n');
    return v.length ? String(v[0]) : '';
  }

  async function detailsLoad(quiet) {
    const id = L.assetId, tok = ++DT.tok;
    DT.for = id;
    if (!quiet) { DT.load = 'loading'; DT.err = null; render(); }
    try {
      const asset = ((S.data && S.data.assets) || []).find((a) => a.id === id) || {};
      const [sch, meta] = await Promise.all([call('schema', null, null, asset.show ? { show: asset.show } : null), call('metaGet', id)]);
      if (tok !== DT.tok || id !== L.assetId) return;
      if (sch.status === 401 || meta.status === 401) { setConn('token'); openToken(); }
      if (!(sch.data && sch.data.ok)) throw Object.assign(new Error(codeOf(sch.data, sch.status).reason), { code: codeOf(sch.data, sch.status).code });
      if (!(meta.data && meta.data.ok)) throw Object.assign(new Error(codeOf(meta.data, meta.status).reason), { code: codeOf(meta.data, meta.status).code });
      DT.fields = (sch.data.fields || []).filter((f) => !f.retired && (f.showName == null || f.showName === asset.show));
      DT.values = meta.data.values || {}; DT.missing = meta.data.missing || [];
      DT.draft = {}; DT.errors = {};
      for (const f of DT.fields) DT.draft[f.key] = draftOf(f, DT.values[f.key]);
      DT.load = 'ready'; DT.err = null;
    } catch (e) {
      if (tok !== DT.tok) return;
      DT.load = 'error'; DT.err = e.code ? { code: e.code, reason: e.message } : netFail();
    }
    render();
  }

  function detailsSave(f) {
    const r = metaValues(f, DT.draft[f.key]), id = L.assetId;
    if (!r.ok) { DT.errors[f.key] = { code: r.code, reason: r.reason }; render(); return; }
    delete DT.errors[f.key];
    findAct('metaSet', id, { key: f.key, values: r.values }, {
      key: 'd:save:' + f.key, okTitle: f.label + ' saved.', ok: (d) => (d.missing && d.missing.length ? 'Still missing: ' + d.missing.join(', ') + '.' : ''),
      fail: (e) => { DT.errors[f.key] = e; },
      after: (d) => { if (DT.for !== id) return; if (d.values && d.values.length) DT.values[f.key] = d.values; else delete DT.values[f.key]; DT.missing = d.missing || []; DT.draft[f.key] = draftOf(f, d.values); } });
  }

  function detailsDefine() {
    const d = DT.def, asset = ((S.data && S.data.assets) || []).find((a) => a.id === L.assetId) || {};
    const body = { key: d.key.trim(), label: d.label.trim(), type: d.type, required: d.required, multi: d.multi };
    if (d.scope === 'show' && asset.show) body.show = asset.show;
    d.err = null;
    findAct('fieldDefine', null, body, { key: 'd:define', okTitle: 'Field defined.', ok: (r) => r.field.label + ' is on the form' + (body.show ? ' for ' + body.show + '.' : ' for every show.'),
      fail: (e) => { d.err = e; },
      after: async () => { d.key = ''; d.label = ''; d.required = false; d.multi = false; await findFields(); await detailsLoad(true); } });
  }
  function detailsRetire(f) {
    if (DT.arm !== f.key) { DT.arm = f.key; render(); return; }
    DT.arm = null;
    findAct('fieldRetire', f.key, {}, { key: 'd:retire:' + f.key, okTitle: f.label + ' retired.', ok: () => 'Its stored values are kept; it leaves the form and the search.', after: async () => { await findFields(); await detailsLoad(true); } });
  }
  function detailsTerm(f) {
    const text = (DT.term[f.key] || '').trim();
    if (!text) { DT.errors[f.key] = { code: 'bad_input', reason: 'Type the term to add.' }; render(); return; }
    findAct('fieldTerms', f.key, { terms: [text] }, { key: 'd:term:' + f.key, okTitle: 'Term added.', ok: () => '“' + text + '” is on the list for ' + f.label + '.',
      fail: (e) => { DT.errors[f.key] = e; }, after: async () => { DT.term[f.key] = ''; delete DT.errors[f.key]; await detailsLoad(true); await findFields(); } });
  }

  const canDefine = () => WHO.role === 'admin' || WHO.role === '';

  function fieldControl(f) {
    const id = 'd-' + f.key, key = f.key, bad = DT.errors[key] ? 'd-err-' + key : null;
    const set = (v) => { DT.draft[key] = v; };
    const base = { id, 'data-fk': id, 'aria-describedby': bad, 'aria-invalid': bad ? 'true' : null };
    if (f.type === 'longtext') return el('textarea', Object.assign({}, base, { rows: 4, maxlength: 10000, oninput: (e) => set(e.target.value) }), DT.draft[key]);
    if (f.type === 'text' && f.multi) return el('textarea', Object.assign({}, base, { rows: 3, maxlength: 20000, 'aria-label': f.label + ', one value per line', oninput: (e) => set(e.target.value) }), DT.draft[key]);
    if (f.type === 'bool') return el('select', Object.assign({}, base, { onchange: (e) => set(e.target.value) }),
      [['', 'Not set'], ['true', 'Yes'], ['false', 'No']].map(([v, label]) => el('option', { value: v, selected: DT.draft[key] === v }, label)));
    if (f.type === 'enum') {
      const terms = (f.terms || []).filter((t) => !t.retired).map((t) => t.term);
      const held = f.multi ? DT.draft[key] : (DT.draft[key] ? [DT.draft[key]] : []);
      for (const h of held) if (!terms.includes(h)) terms.push(h);           // a value whose term was retired stays visible until it is changed
      if (f.multi) {
        return el('div', { class: 'terms', role: 'group', 'aria-labelledby': 'd-lbl-' + key, 'aria-describedby': bad },
          terms.length ? terms.map((t) => el('label', { class: 'chk' }, el('input', { type: 'checkbox', 'data-fk': id + ':' + t, checked: DT.draft[key].includes(t),
            onchange: (e) => { set(e.target.checked ? DT.draft[key].concat([t]) : DT.draft[key].filter((x) => x !== t)); } }), el('span', { text: t }))) : el('small', { class: 'hint', text: 'No terms yet. Add one below.' }));
      }
      return el('select', Object.assign({}, base, { onchange: (e) => set(e.target.value) }),
        el('option', { value: '', selected: !DT.draft[key] }, 'Not set'), terms.map((t) => el('option', { value: t, selected: DT.draft[key] === t }, t)));
    }
    return el('input', Object.assign({}, base, { type: 'text', value: DT.draft[key], maxlength: f.type === 'date' ? 10 : 1000, inputmode: f.type === 'number' ? 'decimal' : null,
      placeholder: f.type === 'date' ? 'YYYY-MM-DD' : null, oninput: (e) => set(e.target.value) }));
  }

  function fieldBlock(f) {
    const key = f.key, err = DT.errors[key], grouped = f.type === 'enum' && f.multi;
    const name = el('span', { id: 'd-lbl-' + key, text: f.label });
    const head = el('div', { class: 'dlabel' }, grouped ? name : el('label', { for: 'd-' + key }, name),
      f.required ? stateChip('required') : el('small', { class: 'hint', text: 'Optional' }), f.showName ? el('small', { class: 'hint', text: 'Only for ' + f.showName }) : null);
    return el('div', { class: 'dfield' }, head, fieldControl(f),
      f.type === 'text' && f.multi ? el('small', { class: 'hint', text: 'One value per line.' }) : null,
      el('div', { class: 'dact' },
        btn('Save', { key: 'd:save:' + key, cls: 'small', aria: 'Save ' + f.label, onclick: () => detailsSave(f) }),
        err ? el('span', { class: 'dno', id: 'd-err-' + key, role: 'alert' }, icon('alert', 's'), ' ' + err.code + ': ' + err.reason) : null,
        canDefine() ? btn(DT.arm === key ? 'Press again to retire' : 'Retire field', { key: 'd:retire:' + key, cls: 'ghost small' + (DT.arm === key ? ' danger' : ''), aria: 'Retire the field ' + f.label, onclick: () => detailsRetire(f) }) : null),
      f.type === 'enum' ? el('div', { class: 'dterm' }, el('label', { for: 'd-term-' + key, class: 'vh', text: 'New term for ' + f.label }),
        el('input', { id: 'd-term-' + key, type: 'text', 'data-fk': 'd-term-' + key, maxlength: 200, placeholder: 'Add a term', autocomplete: 'off', value: DT.term[key] || '', oninput: (e) => { DT.term[key] = e.target.value; } }),
        btn('Add term', { key: 'd:term:' + key, cls: 'ghost small', aria: 'Add a term to ' + f.label, onclick: () => detailsTerm(f) })) : null);
  }

  function renderDetails() {
    if (DT.for !== L.assetId) { detailsLoad(false); }
    const d = L.data || {}, missing = DT.missing, byKey = new Map(DT.fields.map((f) => [f.key, f.label]));
    paint('lib-d-head', [DT.for, DT.load, missing, d.cleared, d.legalHold], () => el('div', { class: 'asset' },
      kv('Asset', short(L.assetId), true), assetChips(d),
      DT.load === 'ready' && missing.length ? stateChip('incomplete') : null,
      DT.load === 'ready' && missing.length ? el('span', { class: 'hint', text: 'Missing: ' + missing.map((k) => byKey.get(k) || k).join(', ') }) : null));
    paint('lib-d-form', [DT.for, DT.load, DT.err, DT.fields, DT.values, DT.errors, DT.arm, WHO.role, [...S.pending].filter((k) => k.startsWith('d:'))], () => {
      if (DT.load === 'loading' || DT.load === 'idle') return el('p', { class: 'empty', role: 'status', text: 'Reading the details of ' + short(L.assetId) + '…' });
      if (DT.load === 'error') return el('div', { class: 'empty' }, el('p', { role: 'alert' }, stateChip('failed'), ' ', (DT.err.code ? DT.err.code + ': ' : '') + DT.err.reason),
        el('button', { type: 'button', class: 'btn ghost small', 'data-fk': 'd-retry', onclick: () => detailsLoad(false) }, 'Try again'));
      if (!DT.fields.length) return el('p', { class: 'empty', text: canDefine() ? 'No fields apply to this asset yet. Define one on the right.' : 'No fields apply to this asset yet. An admin defines them.' });
      return el('div', { class: 'dform' }, DT.fields.map(fieldBlock));
    });
    $('lib-d-define-wrap').hidden = !canDefine();
    if (!canDefine()) return;
    const asset = ((S.data && S.data.assets) || []).find((a) => a.id === L.assetId) || {}, df = DT.def;
    paint('lib-d-define', [df.err, asset.show, [...S.pending].filter((k) => k.startsWith('d:define'))], () => el('form', { class: 'dform padded', 'aria-label': 'Define a field', autocomplete: 'off',
      onsubmit: (e) => { e.preventDefault(); detailsDefine(); } },
      el('div', {}, el('label', { for: 'df-key', text: 'Key (lowercase, digits, _)' }), el('input', { id: 'df-key', type: 'text', 'data-fk': 'df-key', maxlength: 40, spellcheck: 'false', value: df.key, oninput: (e) => { df.key = e.target.value; } })),
      el('div', {}, el('label', { for: 'df-label', text: 'Label' }), el('input', { id: 'df-label', type: 'text', 'data-fk': 'df-label', maxlength: 100, value: df.label, oninput: (e) => { df.label = e.target.value; } })),
      el('div', {}, el('label', { for: 'df-type', text: 'Kind of value' }), el('select', { id: 'df-type', 'data-fk': 'df-type', onchange: (e) => { df.type = e.target.value; } },
        FIELD_TYPES.map(([v, label]) => el('option', { value: v, selected: df.type === v }, label)))),
      asset.show ? el('div', {}, el('label', { for: 'df-scope', text: 'Applies to' }), el('select', { id: 'df-scope', 'data-fk': 'df-scope', onchange: (e) => { df.scope = e.target.value; } },
        [['all', 'Every show'], ['show', 'Only ' + asset.show]].map(([v, label]) => el('option', { value: v, selected: df.scope === v }, label)))) : null,
      el('label', { class: 'chk' }, el('input', { type: 'checkbox', 'data-fk': 'df-required', checked: df.required, onchange: (e) => { df.required = e.target.checked; } }), el('span', { text: 'Required: an asset without it is Incomplete' })),
      el('label', { class: 'chk' }, el('input', { type: 'checkbox', 'data-fk': 'df-multi', checked: df.multi, onchange: (e) => { df.multi = e.target.checked; } }), el('span', { text: 'Can hold several values (text and choice only)' })),
      df.err ? el('p', { class: 'dno', role: 'alert' }, icon('alert', 's'), ' ' + df.err.code + ': ' + df.err.reason) : null,
      el('div', {}, el('button', { type: 'submit', class: 'btn', 'data-fk': 'df-go', disabled: S.pending.has('d:define') }, 'Define field'))));
  }

  // ---- Activity: who did what (admin). Filters, newest first, 'Load more' by cursor, and a CSV that is fetched with the token. ----
  const ACT = { rows: [], next: null, loaded: false, busy: false, err: null, f: { actor: '', action: '', kind: '' } };
  async function actLoad(more) {
    if (ACT.busy) return;
    ACT.busy = true; ACT.err = null; render();
    const q = { limit: '100' };
    for (const k of ['actor', 'action', 'kind']) if (ACT.f[k].trim()) q[k] = ACT.f[k].trim();
    if (more && ACT.next) q.before = ACT.next;
    try {
      const { status, data } = await call('audit', null, null, q);
      if (status === 200 && data && data.ok) { ACT.rows = more ? ACT.rows.concat(data.rows) : data.rows; ACT.next = data.next || null; ACT.loaded = true; }
      else ACT.err = status === 403 ? 'The activity log needs the admin role.' : (data && data.error ? data.error.reason : 'The server did not answer.');
    } catch (e) { ACT.err = 'The server did not answer.'; }
    ACT.busy = false; S.sigs['act-rows'] = null; S.sigs['act-head'] = null; render();
  }
  async function actCsv() {
    const q = new URLSearchParams({ format: 'csv', limit: '500' });
    for (const k of ['actor', 'action', 'kind']) if (ACT.f[k].trim()) q.set(k, ACT.f[k].trim());
    const token = getToken();
    const res = await fetch('/api/audit?' + q.toString(), { headers: token ? { Authorization: 'Bearer ' + token } : {}, cache: 'no-store' });
    if (!res.ok) { setFlash({ kind: 'bad', title: 'Could not export.', code: 'export_failed', reason: 'The server refused the CSV (' + res.status + ').' }); render(); return; }
    const url = URL.createObjectURL(await res.blob());
    const a = el('a', { href: url, download: 'volt-activity.csv' });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function renderActivity() {
    if (!ACT.loaded && !ACT.busy && !ACT.err) { actLoad(false); return; }
    paint('act-head', [ACT.busy, ACT.f.actor, ACT.f.action, ACT.f.kind], () => el('div', { class: 'panel' },
      el('h2', { id: 'h-act' }, icon('wave', 's'), 'Activity'),
      el('form', { class: 'form-row', 'aria-labelledby': 'h-act', onsubmit: (e) => { e.preventDefault(); actLoad(false); } },
        [['actor', 'Who'], ['action', 'Action'], ['kind', 'Kind of thing']].map(([k, label]) => el('div', {}, el('label', { for: 'act-' + k, text: label }),
          el('input', { id: 'act-' + k, type: 'text', 'data-fk': 'act-' + k, value: ACT.f[k], autocomplete: 'off', oninput: (e) => { ACT.f[k] = e.target.value; } }))),
        btn('Show', { key: 'act-go', cls: 'ghost', onclick: () => actLoad(false) }),
        btn('Download CSV', { key: 'act-csv', cls: 'ghost', onclick: actCsv }))));
    paint('act-rows', [ACT.rows, ACT.next, ACT.busy, ACT.err], () => {
      if (ACT.err) return el('div', { class: 'panel' }, el('p', { class: 'empty', text: ACT.err }));
      if (!ACT.rows.length) return el('div', { class: 'panel' }, el('p', { class: 'empty', text: ACT.busy ? 'Reading the log…' : 'Nothing matches.' }));
      return el('div', { class: 'panel' }, el('div', { class: 'tblwrap' }, el('table', {},
        el('caption', { class: 'vh', text: 'Who did what, newest first' }),
        el('thead', {}, el('tr', {}, ['When', 'Who', 'Action', 'Thing', 'Detail'].map((h) => el('th', { scope: 'col', text: h })))),
        el('tbody', {}, ACT.rows.map((r) => el('tr', {}, el('td', { class: 'mono', text: r.at }), el('td', { text: r.actor }),
          el('td', {}, icon('wave', 's'), ' ' + r.action), el('td', { class: 'mono', text: r.target_kind + ' ' + short(r.target_id) }),
          el('td', { text: r.detail || '' })))))),
        ACT.next ? el('div', { class: 'bar' }, btn(ACT.busy ? 'Loading…' : 'Load more', { key: 'act-more', cls: 'ghost', disabled: ACT.busy, onclick: () => actLoad(true) })) : null);
    });
  }

  // ---- Access: who may view, edit or run a show (admin). The list and the two pickers come from the server; a change is one POST. ----
  const ACC = { show: null, rows: [], users: null, groups: null, loaded: null, busy: false, err: null, kind: 'user', subject: '', level: 'view' };
  async function accLoad(showId) {
    if (ACC.busy) return;
    ACC.busy = true; ACC.err = null; ACC.show = showId;
    try {
      const [acc, us, gs] = await Promise.all([call('showAccess', showId), call('users'), call('groups')]);
      if (acc.status === 200 && acc.data && acc.data.ok) {
        ACC.rows = acc.data.access || [];
        ACC.users = us.status === 200 && us.data ? us.data.users || [] : null;
        ACC.groups = gs.status === 200 && gs.data ? gs.data.groups || [] : null;
        ACC.loaded = showId;
        if (us.status === 403) ACC.err = 'Changing show access needs the admin role.';
      } else ACC.err = acc.status === 403 ? 'Changing show access needs the admin role.' : (acc.data && acc.data.error ? acc.data.error.reason : 'The server did not answer.');
    } catch (e) { ACC.err = 'The server did not answer.'; }
    ACC.busy = false; S.sigs['acc-head'] = null; S.sigs['acc-rows'] = null; render();
  }
  function accSet(kind, subject, level) {
    act('showAccessSet', ACC.show, { subject_kind: kind, subject, level }, { key: 'acc:' + kind + ':' + subject,
      okTitle: level ? 'Access saved.' : 'Access removed.',
      ok: (d) => { ACC.rows = d.access || []; S.sigs['acc-head'] = null; S.sigs['acc-rows'] = null;
        const now = accessSummary(ACC.rows); return 'This show is now ' + now.word.toLowerCase() + '. ' + now.text; },
      hint: 'Nothing changed.' });
  }
  function renderAccess() {
    const shows = (S.data && S.data.shows) || [];
    const want = ACC.show && shows.some((x) => x.id === ACC.show) ? ACC.show : (S.show || (shows[0] && shows[0].id) || null);
    if (want && ACC.loaded !== want && !ACC.busy && !ACC.err) { accLoad(want); return; }
    if (want) ACC.show = want;
    const sum = accessSummary(ACC.rows);
    paint('acc-head', [ACC.show, shows.map((x) => x.id + x.title), ACC.busy, ACC.err, sum.kind], () => el('div', { class: 'panel' },
      el('h2', { id: 'h-acc' }, icon('lock', 's'), 'Who can use this show'),
      el('div', { class: 'form-row' }, el('div', {}, el('label', { for: 'acc-show', text: 'Show' }),
        el('select', { id: 'acc-show', 'data-fk': 'acc-show', onchange: (e) => { ACC.show = e.target.value; ACC.loaded = null; ACC.err = null; S.sigs = {}; render(); } },
          shows.map((x) => el('option', { value: x.id, selected: x.id === ACC.show }, x.title))))),
      ACC.err ? el('p', { class: 'empty', role: 'alert', text: ACC.err })
        : el('p', { class: 'hint' }, el('span', { class: sum.cls }, icon(sum.icon, 's'), el('span', { text: sum.word })), ' ', sum.text)));
    if (ACC.err) { paint('acc-rows', [ACC.err], () => []); return; }
    const names = accessCandidates(ACC.kind, ACC.users, ACC.groups, ACC.rows);
    if (!names.includes(ACC.subject)) ACC.subject = names[0] || '';
    paint('acc-rows', [ACC.rows, ACC.busy, ACC.kind, ACC.subject, ACC.level, names, [...S.pending].filter((k) => k.startsWith('acc:'))], () => el('div', { class: 'panel' },
      !ACC.rows.length ? el('p', { class: 'empty', text: 'No one is listed, so the show is open.' })
        : el('div', { class: 'tblwrap' }, el('table', {},
          el('caption', { class: 'vh', text: 'People and groups with access to this show' }),
          el('thead', {}, el('tr', {}, ['Who', 'Level', 'Can'].map((h) => el('th', { scope: 'col', text: h })), el('th', { scope: 'col' }, el('span', { class: 'vh', text: 'Remove' })))),
          el('tbody', {}, ACC.rows.map((r) => {
            const spec = accessLevelSpec(r.level);
            return el('tr', {}, el('td', {}, icon('user', 's'), ' ' + (r.subject_kind === 'group' ? 'Group ' : 'User ') + r.subject),
              el('td', {}, el('span', { class: spec.cls }, icon(spec.icon, 's'), el('span', { text: spec.word }))),
              el('td', { text: spec.does }),
              el('td', {}, btn('Remove', { key: 'acc:' + r.subject_kind + ':' + r.subject, cls: 'ghost small', aria: 'Remove ' + r.subject + ' from this show',
                onclick: () => accSet(r.subject_kind, r.subject, null) })));
          })))),
      el('form', { class: 'form-row', 'aria-label': 'Add or change access', onsubmit: (e) => { e.preventDefault(); if (ACC.subject) accSet(ACC.kind, ACC.subject, ACC.level); } },
        el('div', {}, el('label', { for: 'acc-kind', text: 'Add' }),
          el('select', { id: 'acc-kind', 'data-fk': 'acc-kind', onchange: (e) => { ACC.kind = e.target.value; S.sigs['acc-rows'] = null; render(); } },
            [['user', 'A user'], ['group', 'A group']].map(([v, t]) => el('option', { value: v, selected: v === ACC.kind }, t)))),
        el('div', {}, el('label', { for: 'acc-subject', text: ACC.kind === 'group' ? 'Group' : 'User' }),
          el('select', { id: 'acc-subject', 'data-fk': 'acc-subject', disabled: !names.length, onchange: (e) => { ACC.subject = e.target.value; } },
            names.length ? names.map((n) => el('option', { value: n, selected: n === ACC.subject }, n)) : [el('option', { value: '' }, 'No one left to add')])),
        el('div', {}, el('label', { for: 'acc-level', text: 'Level' }),
          el('select', { id: 'acc-level', 'data-fk': 'acc-level', onchange: (e) => { ACC.level = e.target.value; } },
            ['view', 'edit', 'run'].map((v) => el('option', { value: v, selected: v === ACC.level }, accessLevelSpec(v).word)))),
        el('button', { type: 'submit', class: 'btn', disabled: !names.length || S.pending.has('acc:' + ACC.kind + ':' + ACC.subject) }, 'Save'),
        el('span', { class: 'hint', text: 'A person’s role still caps this: a viewer given Run can only view.' }))));
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
  function setTheme(name) {
    const t = themeName(name);
    if (t === 'auto') document.documentElement.removeAttribute('data-theme'); else document.documentElement.setAttribute('data-theme', t);
    try { if (t === 'auto') localStorage.removeItem('volt.theme'); else localStorage.setItem('volt.theme', t); } catch (e) { /* works without it */ }
    const b = $('theme-btn');
    b.textContent = 'Theme: ' + t.charAt(0).toUpperCase() + t.slice(1);
    b.dataset.theme = t;
  }
  function currentTheme() { return themeName(document.documentElement.getAttribute('data-theme')); }

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
    for (const it of items) seconds[it.id] = itemPlan(it, mediaOf(it.id)).seconds;
    const timing = frontTimes(items, seconds, Date.now());
    const slot = S.data.rundown ? S.data.rundown.slotSeconds : null;
    const back = backTimes(items, seconds, slot);
    const hasSlot = slot != null;
    const rule = S.data.rundown ? S.data.rundown.recurrence : null;
    const repeatOf = S.data.rundown ? S.data.rundown.recurrenceOf : null;
    paint('rundown-sum', [S.data.show && 1, items.length, timing.totalSec, timing.driftSec, slot, back.overSec, rule, repeatOf], () => {
      if (!S.data.show || !items.length) return null;
      const d = timing.driftSec, o = back.overSec;
      return [el('span', { text: 'Total ' + fmtOffset(hasSlot ? back.totalSec : timing.totalSec) + (hasSlot ? ' of ' + fmtOffset(slot) : '') + ' · ' + items.length + (items.length === 1 ? ' item' : ' items') }),
        rule ? el('span', { class: 'repeat' }, icon('cue', 's'), ruleText(rule)) : repeatOf ? el('span', { class: 'repeat' }, icon('cue', 's'), 'A repeat of another show') : null,
        o == null ? null : el('span', { class: 'over' + (o > 0 ? ' behind' : '') }, icon(o > 0 ? 'alert' : 'ready', 's'),
          o > 0 ? 'Over ' + fmtOffset(o) : o < 0 ? 'Under ' + fmtOffset(-o) : 'On slot'),
        d == null ? null : el('span', { class: 'drift' + (d > 0 ? ' behind' : '') }, icon(d > 0 ? 'alert' : 'ready', 's'),
          'Drift ' + fmtSigned(d) + (d > 0 ? ' behind' : d < 0 ? ' ahead' : ' on time'))];
    });
    paint('rundown', [items, S.data.media, S.selected, S.armed, [...S.pending], S.seq, timing, slot, back, (S.data.leases || []).map((l) => [l.item, l.holder, l.clientId])], () => {
      if (!S.data.show) return el('p', { class: 'empty', text: 'No show yet. Use New show below.' });
      if (!items.length) return el('p', { class: 'empty', text: 'This show has no items.' });
      return [el('div', { class: 'rhead' + (hasSlot ? ' hb' : ''), 'aria-hidden': 'true' }, el('span', { text: '#' }), el('span', { text: 'State' }), el('span', { text: 'Item' }),
          el('span', { text: 'Length' }), el('span', { text: 'Front' }), hasSlot ? el('span', { text: 'Back' }) : null, el('span')),
        el('ol', { class: 'list' }, items.map((it, idx) => rundownRow(it, idx, items, air, next, timing.rows[idx], seconds[it.id], hasSlot ? back.rows[idx] : null)))];
    });

    paint('presence', [S.data.presence, S.data.collab, WHO.name, LS.client, S.show], presenceStrip);
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

    paint('room', [S.data.room, S.data.mos, [...S.pending]], () => [roomPanel(S.data.room), mosPanel(S.data.mos)]);
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
  function scheduleItem(it) {
    const at = localToUtc(S.build.sched);
    if (at === 'bad') { setFlash({ kind: 'bad', title: 'Time not understood.', code: 'bad_input', reason: 'Pick a date and time.' }); render(); return; }
    if (at === null) { setFlash({ kind: 'bad', title: 'Pick a time first.', code: 'bad_input', reason: 'Use Clear schedule to remove it.' }); render(); return; }
    act('itemSchedule', it.id, { at }, { key: 'item-schedule', okTitle: 'Scheduled.', ok: () => '"' + it.slug + '" is taken by the clock at the time shown.' });
  }
  function clearSchedule(it) {
    act('itemSchedule', it.id, { at: null }, { key: 'item-schedule-clear', okTitle: 'Schedule cleared.', ok: () => '"' + it.slug + '" is taken by hand.' });
  }
  function textIn2(id, obj, key, o) {
    return el('input', Object.assign({ id, type: 'text', 'data-fk': id, value: obj[key], autocomplete: 'off', spellcheck: 'false', oninput: (e) => { obj[key] = e.target.value; } }, o || {}));
  }
  function afterPrevious(it) {
    const n = Number(S.build.after);
    if (S.build.after === '' || !Number.isInteger(n) || n < 0 || n > 86399) { setFlash({ kind: 'bad', title: 'Seconds not understood.', code: 'bad_input', reason: 'Write a whole number of seconds from 0 to 86399.' }); render(); return; }
    act('itemSchedule', it.id, { mode: 'after_previous', offsetSeconds: n }, { key: 'item-after', okTitle: 'Scheduled.', ok: () => '"' + it.slug + '" follows the previous item by ' + n + ' s.' });
  }
  function setRepeat() {
    const r = S.build.rec;
    const rule = { freq: r.freq, at: r.at, tz: r.tz.trim() };
    if (r.freq === 'weekly') rule.days = [...r.days].sort();
    act('showRecurrence', S.show, { rule }, { key: 'show-repeat', okTitle: 'Repeat set.', ok: () => ruleText(rule) + '. The next copy appears 24 hours ahead.' });
  }
  function stopRepeat() {
    act('showRecurrence', S.show, { rule: null }, { key: 'show-repeat-clear', okTitle: 'Repeat stopped.', ok: () => 'No more copies will be made.' });
  }
  function setSlot() {
    const v = parseSlot(S.build.slot);
    if (v === 'bad') { setFlash({ kind: 'bad', title: 'Slot length not understood.', code: 'bad_input', reason: 'Write it as minutes and seconds, 15:00, or hours, 1:30:00. It must be between 0:01 and 23:59:59.' }); render(); return; }
    if (v === null) { setFlash({ kind: 'bad', title: 'Write a slot length first.', code: 'bad_input', reason: 'Use Clear slot to remove it.' }); render(); return; }
    S.build.slot = '';
    act('showSlot', S.show, { seconds: v }, { key: 'show-slot', okTitle: 'Slot set.', ok: () => 'The show is planned for ' + fmtOffset(v) + '.' });
  }
  function clearSlot() {
    act('showSlot', S.show, { seconds: null }, { key: 'show-slot-clear', okTitle: 'Slot cleared.', ok: () => 'The show has no planned length.' });
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
    act('itemRename', it.id, { slug, clientId: LS.client }, { key: 'item-rename', okTitle: 'Renamed.', ok: () => 'Now "' + slug + '".' });
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
    const info = cmdInfo(S.build.cmd);
    act('cueAdd', it.id, Object.assign({ command: S.build.cmd, payload }, info && info.room ? { room: info.room } : {}), { key: 'cue-add', okTitle: 'Cue added.',
      ok: (d) => (CUE_LABEL[d.cue.command] || d.cue.command) + ' is on "' + it.slug + '".' + (d.warning ? ' ' + d.warning : '') });
  }

  function renderBuild(sItem) {
    loadCmds();
    const b = S.build;
    const editable = !!sItem && sItem.status === 'ready';
    if (sItem && b.renameId !== sItem.id) { b.renameId = sItem.id; b.rename = sItem.slug; b.sched = utcToLocal(sItem.scheduledAt); }
    const sel = typeof L !== 'undefined' ? L.sel : [];
    const m = sItem ? mediaOf(sItem.id) : {};
    const pending = [...S.pending];
    const rmKey = sItem ? 'remove:' + sItem.id : '';
    const slotNow = S.data.rundown ? S.data.rundown.slotSeconds : null;
    paint('build', [S.data.show, S.data.rundown && S.data.rundown.recurrence, b.rec.freq, sItem, sItem && [sItem.scheduledAt, sItem.scheduleState, sItem.scheduleMode], m.span, sel.map((e) => e.id + e.name), pending, S.armed, b.pick, slotNow], () => [
      el('div', { class: 'body' },
        el('form', { 'aria-label': 'Set the slot length', onsubmit: (e) => { e.preventDefault(); setSlot(); } },
          formRow('Slot length (mm:ss or h:mm:ss)', 'build-slot', textIn('build-slot', 'slot', { maxlength: '9', disabled: !S.data.show, placeholder: slotNow != null ? fmtOffset(slotNow) : '15:00' })),
          el('div', { class: 'bar flush' }, btn('Set slot', { key: 'show-slot', cls: 'ghost', disabled: !S.data.show, onclick: setSlot }),
            slotNow != null ? btn('Clear slot', { key: 'show-slot-clear', cls: 'ghost', onclick: clearSlot }) : null)),
        el('form', { 'aria-label': 'Add an item', onsubmit: (e) => { e.preventDefault(); addItem(); } },
          formRow('New item', 'build-slug', textIn('build-slug', 'slug', { maxlength: '200', disabled: !S.data.show })),
          el('div', { class: 'bar flush' }, btn('Add item', { key: 'item-add', cls: 'ghost', disabled: !S.data.show, onclick: addItem }))),
        sItem ? el('fieldset', { class: 'grp' }, el('legend', { text: 'Edit ' + sItem.slug }),
          editable ? [
            el('form', { 'aria-label': 'Rename the selected item', onsubmit: (e) => { e.preventDefault(); renameItem(sItem); } },
              formRow('Name', 'build-rename', textIn('build-rename', 'rename', { maxlength: '200' })),
              el('div', { class: 'bar flush' }, btn('Rename', { key: 'item-rename', cls: 'ghost', onclick: () => renameItem(sItem) }))),
            el('form', { 'aria-label': 'Schedule the selected item', onsubmit: (e) => { e.preventDefault(); scheduleItem(sItem); } },
              formRow('Take at (your local time)', 'build-sched', el('input', { id: 'build-sched', type: 'datetime-local', 'data-fk': 'build-sched', step: '1', value: b.sched || '',
                oninput: (e) => { b.sched = e.target.value; } })),
              el('div', { class: 'bar flush' }, btn('Set schedule', { key: 'item-schedule', cls: 'ghost', onclick: () => scheduleItem(sItem) }),
                sItem.scheduledAt ? btn('Clear schedule', { key: 'item-schedule-clear', cls: 'ghost', onclick: () => clearSchedule(sItem) }) : null)),
            el('form', { 'aria-label': 'Take after the previous item', onsubmit: (e) => { e.preventDefault(); afterPrevious(sItem); } },
              formRow('Or: seconds after the previous item has run', 'build-after', el('input', { id: 'build-after', type: 'number', min: '0', max: '86399', step: '1', 'data-fk': 'build-after',
                value: b.after, oninput: (e) => { b.after = e.target.value; } })),
              el('div', { class: 'bar flush' }, btn('Set after previous', { key: 'item-after', cls: 'ghost', onclick: () => afterPrevious(sItem) }))),
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
        el('details', { class: 'newshow', open: b.recOpen, ontoggle: (e) => { b.recOpen = e.target.open; } }, el('summary', { text: 'Repeat this show' }),
          el('form', { 'aria-label': 'Repeat this show', onsubmit: (e) => { e.preventDefault(); setRepeat(); } },
            formRow('How often', 'build-rec-freq', el('select', { id: 'build-rec-freq', 'data-fk': 'build-rec-freq', onchange: (e) => { b.rec.freq = e.target.value; render(); } },
              el('option', { value: 'daily', selected: b.rec.freq === 'daily' }, 'Every day'), el('option', { value: 'weekly', selected: b.rec.freq === 'weekly' }, 'Every week on'))),
            b.rec.freq === 'weekly' ? el('div', { class: 'days', role: 'group', 'aria-label': 'Days of the week' }, DAY_NAMES.map((d, n) => el('label', { class: 'day' },
              el('input', { type: 'checkbox', checked: b.rec.days.includes(n), 'data-fk': 'build-day-' + n,
                onchange: (e) => { b.rec.days = e.target.checked ? [...b.rec.days, n] : b.rec.days.filter((x) => x !== n); } }), ' ' + d))) : null,
            formRow('At (24-hour time)', 'build-rec-at', el('input', { id: 'build-rec-at', type: 'time', 'data-fk': 'build-rec-at', value: b.rec.at, oninput: (e) => { b.rec.at = e.target.value; } })),
            formRow('Time zone', 'build-rec-tz', textIn2('build-rec-tz', b.rec, 'tz', { maxlength: '60' })),
            el('div', { class: 'bar flush' }, btn('Set repeat', { key: 'show-repeat', cls: 'ghost', disabled: !S.data.show, onclick: setRepeat }),
              S.data.rundown && S.data.rundown.recurrence ? btn('Stop repeating', { key: 'show-repeat-clear', cls: 'ghost', onclick: stopRepeat }) : null))),
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
        formRow(info && info.room === 'hyperdeck' ? 'Deck and clip (A, A/3 or A/intro.mov)' : 'Payload (optional)', 'cue-payload', textIn('cue-payload', 'payload', { maxlength: '2000' })),
        info && !info.verified ? el('small', { class: 'no', text: info.adapter
          ? '? ' + info.command + ' has an adapter, but no live Program take is recorded yet. The cue is stored and a take will run it.'
          : '? No verified adapter for ' + info.command + ' yet. The cue is stored, but a take stops at it.' }) : null,
        el('div', { class: 'bar flush' }, btn('Add cue', { key: 'cue-add', cls: 'ghost', disabled: !cmds.length, onclick: () => addCue(sItem) }))));
    });
  }

  function rundownRow(it, idx, items, air, next, tm, secs, bk) {
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
    const schedChip = scheduleChip(it.scheduleState, !!it.scheduledAt, it.scheduleMode);
    const backText = !bk ? null : bk.backMs != null ? new Date(bk.backMs).toLocaleTimeString([], { hour12: false }) : (it.status === 'ready' ? '--:--' : '');
    return el('li', { class: cls + (bk ? ' hb' : '') },
      el('span', { class: 'n', 'aria-hidden': 'true', text: String(idx + 1) }),
      stateChip(itemStateKind(it.status, isNext)),
      el('button', { type: 'button', class: 'pick', 'data-fk': 'pick:' + it.id, 'aria-current': S.selected === it.id ? 'true' : null,
          onclick: () => { S.selected = it.id; render(); } },
        el('span', { class: 'title' + (it.status === 'skipped' || it.status === 'killed' ? ' struck' : ''), text: it.slug }),
        el('span', { class: 'meta' }, meta.join(' · ') || (it.status === 'ready' ? 'not yet aired' : ''),
          blocked ? el('span', { class: 'bad' }, (meta.length ? ' · ' : '') + '⚠ media ' + blocked) : null,
          schedChip ? el('span', { class: 'sched' }, ' ', stateChip(schedChip[0], schedChip[1]),
            it.scheduledAt ? ' ' + new Date(it.scheduledAt).toLocaleString([], { hour12: false, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' })
              : it.scheduleMode === 'after_previous' ? ' +' + fmtOffset(it.scheduleOffsetSeconds || 0) : '',
            it.scheduleNote ? ' · ' + it.scheduleNote : '') : null,
          // The story: its editorial chip once there is text or a status to show, and a marker when it is a backup.
          it.words > 0 || it.storyStatus !== 'draft' ? el('span', { class: 'sched' }, ' ', stateChip(storyKind(it.storyStatus))) : null,
          it.floated ? el('span', { class: 'sched' }, ' ', stateChip('floated')) : null,
          // Who has the item open for editing (an edit lease): icon, word and name, from the server's state.
          (() => { const lv = it.status === 'ready' ? leaseView(S.data && S.data.leases, it.id, leaseMe()) : null; return lv ? el('span', { class: 'sched' }, ' ', stateChip(lv.kind, lv.word)) : null; })())),
      it.lengthSource === 'read' && !(tm && tm.offset == null)
        ? el('span', { class: 'dur mono', title: 'No media is attached, so the planned length is the read time of the script' }, '~' + fmtOffset(secs), el('small', { class: 'rd', text: 'read' }))
        : el('span', { class: 'dur mono', text: tm && tm.offset == null ? '--:--' : fmtOffset(secs) }),
      el('span', { class: 'front mono', text: front }),
      backText == null ? null : el('span', { class: 'back mono', text: backText }),
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

  // MOS devices Volt sends running orders to (VOLT_MOS_DEVICES): connection state, the last refusal, and any show whose roCreate
  // got no answer, which waits for the desk instead of being sent again blind.
  const MOS_STATE = { up: ['ready', 'Connected'], connecting: ['pending', 'Connecting'], down: ['failed', 'Down'], refused: ['failed', 'Refused'] };
  function mosPanel(devices) {
    if (!devices || !devices.length) return null;
    return devices.map((d) => {
      const [kind, word] = MOS_STATE[d.state] || ['pending', String(d.state)];
      return el('div', { class: 'mos' }, el('dl', { class: 'kv' }, el('dt', { text: 'MOS ' + d.name }), el('dd', {}, stateChip(kind, word), d.detail ? ' ' + d.detail : '')),
        d.unconfirmed.map((show) => el('div', { class: 'note mos-wait' },
          el('p', { text: 'The running order ' + short(show) + ' was sent to ' + d.name + ' and not acknowledged. It will not be sent again until you say whether the device has it.' }),
          el('div', { class: 'acts' },
            btn('It was created', { key: 'mos-ok:' + d.name + show, cls: 'ghost small', onclick: () => act('mosResolve', d.name + ':' + show, { action: 'created' }, { key: 'mos-ok:' + d.name + show, okTitle: 'Noted.', ok: () => 'Later changes go to ' + d.name + ' as replacements.' }) }),
            btn('Send it again', { key: 'mos-retry:' + d.name + show, cls: 'ghost small', onclick: () => act('mosResolve', d.name + ':' + show, { action: 'retry' }, { key: 'mos-retry:' + d.name + show, okTitle: 'Will resend.', ok: () => 'The roCreate goes to ' + d.name + ' again.' }) }))))); 
    });
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
        let cls = 'step', glyph = 'pending';
        if (idx >= 0 && (j < idx || (j === idx && name === 'completed' && room.finalized))) { cls += ' done'; glyph = 'ready'; }
        else if (idx >= 0 && j === idx) { cls += ' now'; glyph = 'record'; }
        return el('li', { class: cls, 'aria-current': cls.includes('now') ? 'step' : null },
          el('span', { class: 'g', 'aria-hidden': 'true' }, icon(glyph)), name);
      });
      if (dead) steps.push(el('li', { class: 'step fail', 'aria-current': 'step' }, el('span', { class: 'g', 'aria-hidden': 'true' }, icon('alert')), room.state));
      const starting = S.pending.has('recordStart');
      const stopping = cur && S.pending.has('recordStop:' + cur.id);
      return [
        el('section', { class: 'panel', 'aria-labelledby': 'h-recstart' }, el('h2', { id: 'h-recstart' }, icon('record', 's'), 'Start a recording'),
          el('div', { class: 'form-row' },
            el('div', {}, el('label', { for: 'rec-camera', text: 'Camera or label (optional)' }),
              el('input', { id: 'rec-camera', type: 'text', maxlength: '100', autocomplete: 'off', value: S.recCamera || '', 'data-fk': 'rec-camera',
                oninput: (e) => { S.recCamera = e.target.value; } })),
            btn(starting ? 'STARTING…' : 'START RECORDING', { key: 'recordStart', disabled: !room.configured || S.seq,
              onclick: () => act('recordStart', null, S.recCamera ? { camera: S.recCamera } : {}, { key: 'recordStart', okTitle: 'Recording.', ok: (d) => 'Asset ' + short(d.asset.id) + ' minted once the room showed output.' })}),
            el('span', { class: 'hint', text: room.configured ? 'Mints the asset only after the room shows a file being written.' : 'No room configured. Set VOLT_ROOM_URL on the server.' }))),
        el('section', { class: 'panel', 'aria-labelledby': 'h-lifecycle' },
          el('h2', { id: 'h-lifecycle' }, icon('wave', 's'), 'Recording lifecycle' + (cur ? ' · ' + (cur.camera || short(cur.id)) : '')),
          el('ol', { class: 'steps', 'aria-label': 'Lifecycle steps' }, steps),
          el('dl', { class: 'kv' },
            el('dt', { text: 'State' }), el('dd', { text: room.reachable ? room.state + ' · ' + room.health : room.configured ? 'room unreachable' : 'no room' }),
            el('dt', { text: 'Asset' }), el('dd', { text: cur ? cur.id : 'none yet. It is minted once output is observed.' }),
            el('dt', { text: 'Session' }), el('dd', { text: room.sessionId || 'none' }),
            el('dt', { text: 'File' }), el('dd', { text: cur && cur.file ? cur.file + (cur.open ? ' · open' : ' · closed') : 'none' }),
            el('dt', { text: 'Snapshot' }), el('dd', {}, room.reachable ? stateChip(room.stale ? 'stale' : 'fresh') : 'none'),
            el('dt', { text: 'Finalized' }), el('dd', { text: room.reachable ? (room.finalized ? 'yes' : 'no') : 'unknown' }),
            el('dt', { text: 'Checksum' }), el('dd', { text: cur && cur.checksum ? cur.checksum : 'not wrapped' }),
            room.error ? [el('dt', { text: 'Room error' }), el('dd', { text: room.error })] : null),
          el('div', { class: 'bar top-rule' },
            btn(stopping ? [el('span', { class: 'spin', 'aria-hidden': 'true' }), ' WAITING FOR THE ROOM…'] : 'STOP AND WRAP',
              { key: 'recordStop:' + (cur ? cur.id : ''), disabled: !cur || !cur.open || S.seq,
                onclick: () => stopRecording(cur.id) }),
            el('span', { class: 'hint', text: cur && cur.open ? 'Stops, then waits for completed and finalized from the room. Never wraps on a stop ack.' : 'Nothing open to stop.' }))),
        el('section', { class: 'panel', 'aria-labelledby': 'h-assets' }, el('h2', { id: 'h-assets' }, icon('library', 's'), 'Recordings'),
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
        el('td', {}, stateChip(a.open ? 'recording' : 'wrapped')),
        el('td', { class: 'mono', text: a.checksum ? short(a.checksum) + '…' : '—' }),
        withStop ? el('td', {}, a.open ? btn('Stop and wrap', { key: 'recordStop:' + a.id, cls: 'ghost small', aria: 'Stop and wrap ' + (a.camera || short(a.id)), disabled: S.seq, onclick: () => stopRecording(a.id) }) : null) : null)))));
  }

  // ---- Rights screen -------------------------------------------------------
  function renderRights() {
    paint('rights', [S.data.assets, S.armed, [...S.pending]], () => {
      const list = S.data.assets;
      return el('section', { class: 'panel', 'aria-labelledby': 'h-assets2' }, el('h2', { id: 'h-assets2' }, icon('rights', 's'), 'Assets'),
        el('p', { class: 'note', text: 'Take, go and publish refuse anything not cleared or on legal hold. The reason shows where the operator is blocked.' }),
        !list.length ? el('p', { class: 'empty', text: 'No assets yet.' }) :
        el('div', { class: 'tblwrap' }, el('table', {},
          el('caption', { class: 'vh', text: 'Assets and their rights' }),
          el('thead', {}, el('tr', {}, ['Asset', 'Camera', 'Cleared', 'Legal hold', 'Can air or publish', 'Actions'].map((h) => el('th', { scope: 'col', text: h })))),
          el('tbody', {}, list.map((a) => {
            const lk = 'lift:' + a.id;
            return el('tr', {},
              el('td', { class: 'mono', text: short(a.id) }), el('td', { text: a.camera || a.show || '—' }),
              el('td', {}, stateChip(a.cleared ? 'cleared' : 'uncleared')),
              el('td', {}, a.legalHold ? stateChip('hold', 'On hold') : 'No'),
              el('td', {}, a.blockedBy ? [stateChip('blocked'), ' ' + a.blockedBy] : stateChip('canair')),
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
            s.blockedBy ? el('div', { class: 'blocked' }, stateChip('blocked', 'Blocked'), ' ' + s.blockedBy + ' ', el('a', { href: '#rights', text: 'Fix on Rights' })) : null,
            btn(running ? [el('span', { class: 'spin', 'aria-hidden': 'true' }), ' RENDERING…'] : label,
              { key, cls: s.blockedBy ? 'ghost' : '', disabled: !!s.blockedBy,
                aria: (running ? 'Rendering' : label) + ' ' + (s.label || s.text || short(s.id)),
                onclick: () => act('publish', s.id, {}, { key, span: s.id, okTitle: 'Published.', ok: (d) => d.versions.length + ' versions done.' }) })));
      });
      return [head, el('section', { class: 'panel', 'aria-labelledby': 'h-clips' }, el('h2', { id: 'h-clips' }, icon('publish', 's'), 'Clips'),
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
    $('lib-asset').addEventListener('change', (e) => { if (e.target.value && W.tab === 'find') W.tab = 'shots'; setAsset(e.target.value); });
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
    v.addEventListener('play', () => { $('lib-play-text').textContent = 'Pause'; tickPos(); });
    // The clip played out (or stopped by itself): forget the shuttle so the next K or L starts clean.
    v.addEventListener('ended', () => { if (SH.dir === 1) { cancelAnimationFrame(SH.raf); SH.dir = 0; SH.speed = 0; v.playbackRate = 1; } });
    v.addEventListener('pause', () => { $('lib-play-text').textContent = 'Play'; updatePos(); });
    v.addEventListener('error', onVideoError);
  }

  function libEnter() { if (L.assetId) libLoad(L.load === 'ready'); }
  function libLeave() { shuttleStop(); const v = $('lib-video'); if (v && !v.paused) v.pause(); }

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
    shuttleStop();
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
    applySeek();
  }

  async function onVideoError() {
    const P = L.proxy, v = $('lib-video');
    if (!v.getAttribute('src')) return;   // we just cleared it
    shuttleStop();
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

  // The shuttle. Forward is the browser's own playbackRate; reverse is a timed seek loop, since a video cannot play backwards.
  const SH = { dir: 0, speed: 0, raf: 0, last: 0 };
  function shuttleRun(next) {
    const v = $('lib-video');
    cancelAnimationFrame(SH.raf);
    SH.dir = next.dir; SH.speed = next.speed;
    if (!v || !videoReady()) { SH.dir = 0; SH.speed = 0; return; }
    if (SH.dir === 1) { v.playbackRate = SH.speed; v.play().catch(() => {}); return; }
    v.playbackRate = 1;
    if (!v.paused) v.pause();
    if (SH.dir === 0) return;
    SH.last = 0;
    const back = (t) => {
      // A hidden tab pauses animation frames; cap the step so coming back does not jump the playhead.
      const dt = SH.last ? Math.min((t - SH.last) / 1000, 0.1) : 0;
      SH.last = t;
      v.currentTime = Math.max(0, v.currentTime - dt * SH.speed);
      updatePos();
      if (v.currentTime <= 0) { shuttleStop(); return; }
      SH.raf = requestAnimationFrame(back);
    };
    SH.raf = requestAnimationFrame(back);
  }
  function shuttleStop() {
    if (SH.dir === 0 && SH.speed === 0) return;
    shuttleRun({ dir: 0, speed: 0 });
  }
  function shuttleKey(key) { shuttleRun(shuttleNext(SH, key)); }
  function togglePlay() {
    const v = $('lib-video');
    if (!videoReady()) return;
    if (SH.dir !== 0) { shuttleStop(); return; }   // K while shuttling stops, as on an edit controller
    if (v.paused) v.play().catch(() => {}); else { shuttleStop(); v.pause(); }
  }
  function stepFrames(n) {
    shuttleStop();
    const view = libView(), v = $('lib-video');
    if (!view.ok || !videoReady()) return;
    if (!v.paused) v.pause();
    seekTo(Math.max(view.startF, (L.pos == null ? frameNow(view) : L.pos) + n));
  }

  function jumpShot(dir) {
    shuttleStop();
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
    if (W.tab === 'find' || W.tab === 'details') return;   // Find and Details have no player keys either; the / key is handled before this
    const t = e.target;
    const k = e.key;
    const onControl = t && t !== document.body && t.closest && t.closest('button,a,summary,select,[role="button"],input,textarea');
    if (k === 'Enter') { if (!onControl && !e.repeat) { e.preventDefault(); addSpan('clip'); } return; }
    const low = k.toLowerCase();
    if (low === 'i' && !e.repeat) { markAt('lib-in', 'In'); return; }
    if (low === 'o' && !e.repeat) { markAt('lib-out', 'Out'); return; }
    if (low === 'k' && !e.repeat) { togglePlay(); return; }
    if (low === 'j' && !e.repeat) { shuttleKey('j'); return; }
    if (low === 'l' && !e.repeat) { shuttleKey('l'); return; }
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
    if (W.tab == null) W.tab = L.assetId ? 'shots' : 'find';   // Find is the landing view until an asset is open
    if (!L.assetId && W.tab !== 'find') W.tab = 'find';
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
    L.live = !card;                               // an asset is open and markable; the tabs that need one show only then
    const finding = W.tab === 'find';
    paint('lib-empty', [card && !finding ? [L.assetId, L.load, L.err, assets.length] : null], () => (finding ? null : card) || []);
    $('lib-body').hidden = finding || !!card;
    if (finding || card) return;
    if (W.tab === 'details') return;              // Details paints itself and needs no player
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
    applySeek();                                  // a hit chosen in Find moves the player once it can seek
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
    paint('lib-head', [d.assetId, a.camera, d.rate, d.start, d.durationFrames, d.open, d.cleared, d.legalHold, d.hasProxy, a.tier, a.jobs, S.armed, [...S.pending]], () => el('div', { class: 'asset' },
      kv('Asset', short(d.assetId), true), kv('Camera', a.camera || a.show || '—'), kv('Rate', fmtRate(view.rate), true), kv('Start', d.start, true),
      kv('Length', d.durationFrames ? lengthTc(d.durationFrames, view.rate) : (d.open ? 'growing' : 'unknown'), true),
      assetChips(d), archiveBits(a),
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
    tab: null, for: null, tok: 0, load: 'idle', err: null, words: [], next: null, pendingFrames: 0,
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
    for (const name of LIB_TABS) {
      const b = $('lib-tab-' + name);
      b.addEventListener('click', () => setTab(name, false));
      b.addEventListener('keydown', (e) => {
        const tabs = LIB_TABS.filter((n) => !$('lib-tab-' + n).hidden);   // arrows skip the tabs that need an asset when none is open
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
    shuttleStop();
    W.tab = name;
    if (name === 'export') X.key = null;                    // the export tab asks the server again each time it opens
    const v = $('lib-video');
    if (v && !v.paused) v.pause();
    S.sigs = {};
    render();
    if (focus) $('lib-tab-' + name).focus();
    announce(name === 'words' ? 'Mark from transcript.' : name === 'export' ? 'Export to an NLE.' : name === 'find' ? 'Find.' : name === 'details' ? 'Details.' : 'Shots and clips.');
  }
  const LIB_TABS = ['find', 'shots', 'words', 'export', 'details'];

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
    const live = !!L.live;                       // an asset is loaded and markable
    if (W.for !== L.assetId) wordsReset();
    tabs.hidden = false;
    const xpanel = $('lib-export'), finding = W.tab === 'find', details = W.tab === 'details';
    for (const name of LIB_TABS) {
      const b = $('lib-tab-' + name), on = W.tab === name;
      b.hidden = name !== 'find' && !live;       // Find needs no asset; the rest do
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    }
    $('lib-find').hidden = !finding;
    $('lib-details').hidden = !(details && live);
    if (finding) { body.hidden = true; panel.hidden = true; xpanel.hidden = true; renderFind(); return; }
    if (!live) { panel.hidden = true; xpanel.hidden = true; return; }
    const words = W.tab === 'words', exporting = W.tab === 'export';
    body.hidden = words || exporting || details;
    panel.hidden = !words;
    xpanel.hidden = !exporting;
    if (details) { renderDetails(); return; }
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
    paint('lib-w-head', [d.assetId, a.camera, d.rate, d.start, d.durationFrames, d.open, d.cleared, d.legalHold, d.hasProxy, a.tier, a.jobs, S.armed, [...S.pending]], () => el('div', { class: 'asset' },
      kv('Asset', short(d.assetId), true), kv('Camera', a.camera || a.show || '—'), kv('Rate', fmtRate(view.rate), true), kv('Start', d.start, true),
      kv('Length', d.durationFrames ? lengthTc(d.durationFrames, view.rate) : (d.open ? 'growing' : 'unknown'), true),
      assetChips(d), archiveBits(a),
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
  Object.assign(ERR_LABEL, { archived: 'Hi-res archived', mixed_rates: 'Mixed frame rates', not_local: 'Media not reachable',
    proxy_mismatch: 'Proxy does not match', unsupported_rate: 'Frame rate not supported', bad_path_map: 'Server path map is broken' });
  const TC_SHAPE = /^\d\d:\d\d:\d\d[:;]\d\d$/;

  // Plain words for each refusal: [headline, what to do]. The server's own reason is always shown under it, with the code.
  const XWHY = {
    essence_open: ['A file is still being written.', 'The hi-res (or proxy) of one asset has not been wrapped. Wait for wrap, or export the proxy.'],
    not_local: ['The media is not a file an editor can open.', 'It is stored as an object-store key or a relative path. Export the proxy, or restore the file to disk.'],
    no_proxy: ['One asset has no proxy.', 'Pick Hi-res, or wait for the proxy job.'],
    archived: ['A hi-res is in the archive.', 'Restore it on the Library header, or export the proxy instead.'],
    proxy_mismatch: ['A proxy does not line up with its hi-res.', 'It records a different frame rate or start timecode, so frames would land in the wrong place. Export the hi-res.'],
    outside_source: ['A span lies outside its file.', 'Trim it on the shot list so it sits inside the recording, or take it out of the selection.'],
    mixed_rates: ['The selection mixes frame rates.', 'One sequence has one rate. Export them in separate files.'],
    unsupported_rate: ['A frame rate cannot be written.', 'Only whole-number and /1001 broadcast rates can go into xmeml, an EDL or FCPXML.'],
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
      X.done = { filename: d.filename || 'volt-selects.' + (d.format === 'edl' ? 'edl' : d.format === 'fcpxml' ? 'fcpxml' : 'xml'), mime: d.mime || 'text/plain', content: d.content, sha: d.sha256,
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
        'The xmeml, EDL and FCPXML files have been read back by a second program, but nobody has imported them into Premiere, Final Cut Pro or Resolve and recorded the result. Import one and check the clips before you rely on it. The steps are under "Manual import check" in docs/specs/shot-spans.md.'));
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
        xRadio('fmt', 'edl', 'CMX3600 EDL', 'Cuts only. Reel names are cut to 8 characters. No paths.', X.format === 'edl'),
        xRadio('fmt', 'fcpxml', 'Final Cut Pro X / Resolve (FCPXML 1.10)', 'Imports in Final Cut Pro and DaVinci Resolve. Keeps names, paths, in and out. Not yet opened in either.', X.format === 'fcpxml')),
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

  // ---- Scripts: write, revise and approve the story on each rundown item (docs/superpowers/specs/2026-11-01-newsroom-scripts-design.md) ----
  // The list comes from /rundown (which never carries script text); a story is read with GET /api/items/{id}/story when its row is chosen.
  // Text typed but not saved lives in SC.drafts, one per item, with the revision it started from: the server's answer replaces the screen,
  // never the draft, and a save quotes that revision so a lost race is a conflict dialog and not a silent overwrite.
  const SC_COUNT_MS = 1000, SC_PREVIEW_MS = 250, SC_MAX = 20000;
  const SC = { sel: null, story: null, loading: false, loadErr: null, loadedAt: 0, drafts: {}, saving: false, conflict: null, filter: 'all',
    pace: null, link: null, tab: 'history', countAt: 0, countTimer: null, prevTimer: null, returnTo: null };
  const SC_TABS = [{ id: 'history', label: 'History', icon: 'script' }, { id: 'wire', label: 'Wire', icon: 'wire' }, { id: 'templates', label: 'Templates', icon: 'transcript' }, { id: 'archive', label: 'Archive', icon: 'library' }];
  const SC_FILTERS = [['all', 'All stories'], ['draft', 'Draft'], ['ready', 'Ready'], ['approved', 'Approved'], ['killed', 'Story killed']];
  const SC_PLAYOUT_WORD = { aired: 'has aired', skipped: 'was skipped', killed: 'was killed from air' };

  const scItems = () => (S.data && S.data.rundown ? S.data.rundown.items : []);
  const scItem = () => scItems().find((i) => i.id === SC.sel) || null;
  const scLoaded = () => (SC.story && SC.story.item === SC.sel ? SC.story : null);
  const scWpm = () => (S.data && S.data.rundown ? S.data.rundown.readWpm : 160);
  // Why this story cannot be edited right now, in words; null when it can.
  function scReason(it, st) {
    if (!it || !st) return null;
    if (SC_PLAYOUT_WORD[it.status]) return 'This item ' + SC_PLAYOUT_WORD[it.status] + ', so its script is part of the record and cannot be edited.';
    if (st.storyStatus === 'killed') return 'This story is killed. Revive it to edit.';
    return null;
  }
  const scDraftOf = (id) => SC.drafts[id] || null;
  const scText = () => { const d = scDraftOf(SC.sel), st = scLoaded(); return d ? d.script : st ? st.script : ''; };
  const scSlug = () => { const d = scDraftOf(SC.sel), st = scLoaded(); return d ? d.slug : st ? st.slug : ''; };

  function scSelect(id, quiet) {
    if (SC.sel === id && scLoaded()) return;
    SC.sel = id; SC.story = null; SC.loadErr = null;
    scLoad(id);
    if (!quiet) render();
  }
  async function scLoad(id) {
    SC.loading = true;
    try {
      const { status, data } = await call('story', id);
      if (SC.sel !== id) return;
      if (status === 200 && data && data.ok) { SC.story = data; SC.loadErr = null; }
      else SC.loadErr = status === 401 ? 'A token is needed to read this story.' : codeOf(data, status).reason || 'The server refused to show this story.';
    } catch (e) { if (SC.sel === id) SC.loadErr = 'The server did not answer.'; }
    SC.loading = false; SC.loadedAt = Date.now(); render();
  }

  // Typing: the draft starts from the loaded story and remembers its revision. Back to the saved text means no draft at all.
  function scEdit(field, value) {
    const st = scLoaded();
    if (!st) return;
    const was = !!SC.drafts[SC.sel];
    const d = SC.drafts[SC.sel] || (SC.drafts[SC.sel] = { slug: st.slug, script: st.script, base: st.rev });
    d[field] = value;
    if (d.slug === st.slug && d.script === st.script) delete SC.drafts[SC.sel];
    scCountSoon(); scPreviewSoon();
    if (was !== !!SC.drafts[SC.sel]) render();
  }

  // The live count is a polite status region that changes at most once a second, however fast the typing is.
  function scCountText() {
    const st = scLoaded();
    if (!st) return '';
    const n = scriptStats(scText(), scWpm());
    return n.words + (n.words === 1 ? ' word' : ' words') + ' · read time ' + fmtOffset(n.readSeconds) + ' at ' + scWpm() + ' words a minute' +
      (n.instructions ? ' · ' + n.instructions + (n.instructions === 1 ? ' instruction' : ' instructions') : '') +
      (n.notes ? ' · ' + n.notes + (n.notes === 1 ? ' private note' : ' private notes') : '') +
      (n.warnings.length ? ' · a line starts with [ and never closes, so it is read aloud' : '') +
      (scText().length > SC_MAX * 0.9 ? ' · ' + scText().length + ' of ' + SC_MAX + ' characters' : '');
  }
  function scCountNow() {
    const n = $('sc-count'), text = scCountText();
    if (n.textContent !== text) n.textContent = text;
    SC.countAt = Date.now();
  }
  function scCountSoon() {
    const wait = SC_COUNT_MS - (Date.now() - SC.countAt);
    if (wait <= 0) scCountNow();
    else if (!SC.countTimer) SC.countTimer = setTimeout(() => { SC.countTimer = null; scCountNow(); }, wait);
  }
  function scPreviewSoon() {
    clearTimeout(SC.prevTimer);
    SC.prevTimer = setTimeout(() => { SC.prevTimer = null; renderScPreview(); }, SC_PREVIEW_MS);
  }

  // Save: one request quoting the revision the draft started from. 409 stale_script opens the conflict dialog with both texts.
  async function scSave() {
    const id = SC.sel, d = scDraftOf(id), it = scItem();
    if (!d || SC.saving || !it || scReason(it, scLoaded())) return;
    if (!leaseAllowsSave(leaseState())) { announce('Saving is off: this tab does not hold the edit lease. Your text is still here.'); return; }
    if (!d.slug.trim()) { setFlash({ kind: 'bad', title: 'Name the story first.', code: 'bad_input', reason: 'A story needs a slug.' }); render(); return; }
    SC.saving = true; render();
    const sent = { slug: d.slug, script: d.script };
    try {
      const { status, data } = await call('scriptSave', id, { slug: d.slug, script: d.script, baseRev: d.base, clientId: LS.client });
      if (status === 401) { setConn('token'); openToken(); }
      if (data && data.ok) {
        const cur = SC.drafts[id];
        if (cur && cur.slug === sent.slug && cur.script === sent.script) delete SC.drafts[id];
        else if (cur) cur.base = data.rev;      // typed more while the request ran: that text starts from the revision just saved
        setFlash({ kind: 'good', title: data.changed ? 'Saved.' : 'Nothing to save.', reason: 'Revision ' + data.rev + (data.stats ? ', ' + data.stats.words + ' words.' : '.') });
        announce((data.changed ? 'Saved. ' : 'Nothing changed. ') + 'Revision ' + data.rev + '.');
        await scLoad(id);
      } else {
        const e = codeOf(data, status);
        if (e.code === 'stale_script' && data && data.current) { SC.conflict = { id, mine: sent, theirs: data.current }; scOpenConflict(); }
        else if ((e.code === 'leased' || e.code === 'lease_lost') && LS.item === id) {   // someone holds the item: the editor goes read-only, the text stays
          leaseApply({ state: e.code === 'leased' ? 'blocked' : 'lost', fails: 0, holder: (data && data.holder) || '', secondsLeft: (data && data.secondsLeft) || 0 }, false);
          announce((ERR_LABEL[e.code] || 'Refused') + '. ' + e.reason + ' Nothing was saved.');
        }
        else { setFlash({ kind: 'bad', title: (ERR_LABEL[e.code] || 'Refused') + '.', code: e.code, reason: e.reason }); announce((ERR_LABEL[e.code] || 'Refused') + '. ' + e.reason); }
      }
    } catch (err) {
      setFlash({ kind: 'bad', title: 'No answer from the server.', code: 'network', reason: 'Your text is still here and unsaved. Try again when the connection is back.' });
      setConn('lost');
    }
    SC.saving = false; render(); refresh();
  }

  function scOpenConflict() {
    const c = SC.conflict, dlg = $('sc-conflict');
    if (!c) return;
    SC.returnTo = document.activeElement && document.activeElement.id ? document.activeElement.id : 'sc-text';
    const pane = (id, label, who, v) => el('div', { class: 'sc-pane' }, el('label', { for: id, text: label }),
      el('textarea', { id, rows: '10', readonly: true, spellcheck: 'false' }, v.slug + '\n\n' + v.script),
      el('small', { class: 'muted', text: who }));
    $('sc-conflict-body').replaceChildren(
      el('p', { id: 'sc-conflict-why' }, el('strong', { text: 'Changed by ' + (c.theirs.actor || 'someone') + (c.theirs.at ? ' at ' + fmtTime(c.theirs.at) : '') + '. ' }),
        'This story is now at revision ' + c.theirs.rev + '. Nothing was saved, and your text is still in the editor.'),
      el('div', { class: 'sc-two' }, pane('sc-cf-mine', 'Your version', 'What is in your editor now', c.mine),
        pane('sc-cf-theirs', 'Their version', 'Revision ' + c.theirs.rev, c.theirs)),
      el('div', { class: 'bar' },
        el('button', { type: 'button', class: 'btn', 'data-fk': 'sc-keep-mine', onclick: () => scResolve('mine') }, 'Keep mine'),
        el('button', { type: 'button', class: 'btn ghost', 'data-fk': 'sc-take-theirs', onclick: () => scResolve('theirs') }, 'Take theirs'),
        el('button', { type: 'button', class: 'btn ghost', 'data-fk': 'sc-decide-later', onclick: () => $('sc-conflict').close() }, 'Decide later')));
    if (!dlg.open) dlg.showModal();
    document.querySelector('[data-fk="sc-keep-mine"]').focus();
  }
  function scResolve(choice) {
    const c = SC.conflict;
    if (!c) return;
    const r = resolveConflict(c.mine, c.theirs, choice);
    SC.conflict = null;
    $('sc-conflict').close();
    if (choice === 'mine') { SC.drafts[c.id] = { slug: r.slug, script: r.script, base: r.baseRev }; scSave(); }
    else { delete SC.drafts[c.id]; scLoad(c.id); setFlash({ kind: 'good', title: 'Took their version.', reason: 'Your text was dropped because you chose theirs.' }); render(); }
  }

  // Status moves, float and pace go through act(), like every other write on the page.
  function scMoveLabel(from, to) {
    if (from === 'killed') return 'Revive';
    if (from === 'approved' && to === 'ready') return 'Take back to ready';
    return { ready: 'Mark ready', draft: 'Back to draft', approved: 'Approve', killed: 'Kill story' }[to];
  }
  function scMove(to) {
    const st = scLoaded(), it = scItem();
    if (!st || !it) return;
    const key = 'sc-status:' + to, go = () => act('storyStatus', SC.sel, { status: to, baseRev: st.rev }, { key, okTitle: scMoveLabel(st.storyStatus, to) + '.',
      ok: () => (to === 'killed' && it.status === 'on air' ? 'The item is still on air: killing the story does not take it off.' : 'Revision ' + st.rev + ' is now ' + to + '.'),
      hint: 'The story changed since this screen loaded it. It is reloaded now: read it again before you decide.' });
    if (to === 'approved') armedAction('scappr:' + SC.sel, go); else go();
  }
  function scFloat() {
    const it = scItem();
    if (!it) return;
    act('itemFloat', it.id, { floated: !it.floated }, { key: 'sc-float', okTitle: it.floated ? 'Back in the rundown.' : 'Floated.',
      ok: () => (it.floated ? '"' + it.slug + '" counts in the timing and can go to air again.' : '"' + it.slug + '" stays in the list but is passed over by Go and left out of the timing.') });
  }
  function scSetPace(wpm) {
    if (wpm !== null && (!Number.isInteger(wpm) || wpm < 60 || wpm > 400)) {
      setFlash({ kind: 'bad', title: 'Pace not understood.', code: 'bad_input', reason: 'Write a whole number of words a minute from 60 to 400.' }); render(); return;
    }
    SC.pace = null;
    act('showPace', S.show, { wpm }, { key: 'sc-pace', okTitle: wpm === null ? 'Default pace.' : 'Pace set.', ok: () => 'Read times use ' + (wpm === null ? 160 : wpm) + ' words a minute.' });
  }

  // A link a prompter computer opens without an operator token: one show, read only, 4 hours. Shown to copy; never stored by the page.
  async function scMakeLink() {
    if (!S.show || S.pending.has('sc-link')) return;
    S.pending.add('sc-link'); render();
    try {
      const { status, data } = await call('prompterTicket', S.show, {});
      if (data && data.ok) { SC.link = { url: location.origin + data.url, expires: data.expiresAt }; announce('Prompter link made. It works for four hours.'); }
      else { const e = codeOf(data, status); setFlash({ kind: 'bad', title: (ERR_LABEL[e.code] || 'Refused') + '.', code: e.code, reason: e.reason }); }
    } catch (e) { setFlash({ kind: 'bad', title: 'No answer from the server.', code: 'network', reason: 'No link was made.' }); }
    S.pending.delete('sc-link'); render();
  }
  function scPrompter() {
    const L = SC.link;
    return el('div', { class: 'form-row' },
      btn('Make a prompter link', { key: 'sc-link', cls: 'ghost small', disabled: !S.show, title: 'A read-only link for the prompter computer, good for four hours', onclick: scMakeLink }),
      L ? el('div', { class: 'grow' }, el('label', { for: 'sc-link-url', text: 'Open this on the prompter. It expires at ' + new Date(L.expires * 1000).toLocaleTimeString([], { hour12: false }) + '.' }),
        el('input', { id: 'sc-link-url', type: 'text', readonly: true, 'data-fk': 'sc-link-url', value: L.url, onfocus: (e) => e.target.select() })) : null);
  }

  function scChips(it) {
    const draft = !!SC.drafts[it.id];
    return [stateChip(storyKind(it.storyStatus)), it.floated ? stateChip('floated') : null,
      it.status !== 'ready' ? stateChip(itemStateKind(it.status, false), it.status === 'killed' ? 'Killed from air' : null) : null,
      draft ? stateChip('unsaved') : null];
  }
  function scRow(it) {
    return el('li', { class: 'scrow' + (SC.sel === it.id ? ' sel' : '') },
      el('button', { type: 'button', class: 'pick', 'data-fk': 'scpick:' + it.id, 'aria-current': SC.sel === it.id ? 'true' : null, onclick: () => scSelect(it.id) },
        el('span', { class: 'title' + (it.status === 'skipped' || it.status === 'killed' ? ' struck' : ''), text: it.slug }),
        el('span', { class: 'chips' }, scChips(it)),
        el('span', { class: 'meta mono', text: it.words + (it.words === 1 ? ' word' : ' words') + ' · ' + fmtOffset(it.readSeconds) })));
  }
  function scList() {
    const all = scItems().filter((i) => SC.filter === 'all' || i.storyStatus === SC.filter);
    const main = all.filter((i) => !i.floated), floated = all.filter((i) => i.floated);
    if (!all.length) return el('p', { class: 'empty', text: scItems().length ? 'No story is ' + (SC_FILTERS.find((f) => f[0] === SC.filter) || ['', ''])[1].toLowerCase() + '.' : 'This show has no items.' });
    return [main.length ? el('ol', { class: 'list sc-ul', 'aria-label': 'Stories in rundown order' }, main.map(scRow)) : null,
      floated.length ? [el('h3', { class: 'sc-floated', id: 'sc-floated-h' }, icon('float', 's'), 'Floated'),
        el('ul', { class: 'list sc-ul', 'aria-labelledby': 'sc-floated-h' }, floated.map(scRow))] : null];
  }

  function scEditor() {
    const it = scItem(), st = scLoaded();
    if (!it) return el('p', { class: 'empty', text: scItems().length ? 'Choose a story.' : 'This show has no items.' });
    if (!st) return el('p', { class: 'empty', text: SC.loadErr || 'Reading the story…' });
    const reason = scReason(it, st), d = scDraftOf(SC.sel);
    const old = $('sc-text'), top = old ? old.scrollTop : 0;
    const newer = d && st.rev > d.base;
    const moves = legalMoves(st.storyStatus);
    const ta = el('textarea', { id: 'sc-text', 'data-fk': 'sc-text', rows: '16', spellcheck: 'true', readonly: !!reason || !leaseAllowsEdit(), maxlength: String(SC_MAX), 'aria-keyshortcuts': 'Control+S',
      'aria-describedby': reason ? 'sc-reason' : null,
      oninput: (e) => scEdit('script', e.target.value),
      // Ctrl+S saves from inside the editor only; the page-wide key handler never sees it.
      onkeydown: (e) => { if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 's') { e.preventDefault(); scSave(); } } }, scText());
    requestAnimationFrame(() => { const n = $('sc-text'); if (n) n.scrollTop = top; });
    return el('div', { class: 'sc-form' },
      reason ? el('p', { class: 'info', id: 'sc-reason' }, icon('lock', 's'), ' ', el('strong', { text: 'Editing is off. ' }), reason) : null,
      newer ? el('p', { class: 'info', role: 'status' }, icon('alert', 's'), ' ', el('strong', { text: 'Newer revision. ' }),
        'Revision ' + st.rev + ' was saved while you were typing. Saving will show you both versions; nothing is overwritten.') : null,
      el('label', { for: 'sc-slug', text: 'Slug' }),
      el('input', { id: 'sc-slug', type: 'text', 'data-fk': 'sc-slug', value: scSlug(), maxlength: '200', autocomplete: 'off', readonly: !!reason || !leaseAllowsEdit(),
        oninput: (e) => scEdit('slug', e.target.value) }),
      el('label', { for: 'sc-text', text: 'Script' }), ta,
      el('p', { class: 'hint' }, 'A line in [brackets] is an instruction for the crew: it is not read and does nothing on air. A line starting // is a private note and never leaves the editing screens.'),
      el('div', { class: 'bar flush' },
        btn(SC.saving ? 'Saving…' : 'Save', { key: 'sc-save', cls: 'signal', disabled: !d || !!reason || SC.saving || !leaseAllowsEdit(), title: 'Ctrl+S inside the script', onclick: scSave }),
        el('span', { class: 'sep', 'aria-hidden': 'true' }),
        moves.map((to) => {
          const arm = to === 'approved' && S.armed === 'scappr:' + SC.sel;
          return btn(arm ? 'Confirm approve' : scMoveLabel(st.storyStatus, to), { key: 'sc-status:' + to, cls: (to === 'approved' ? 'signal' : 'ghost') + (arm ? ' armed' : ''),
            disabled: !!d, title: d ? 'Save first: a status applies to the saved revision.' : null, onclick: () => scMove(to) });
        }),
        el('span', { class: 'sep', 'aria-hidden': 'true' }),
        btn(it.floated ? 'Put back in the rundown' : 'Float', { key: 'sc-float', cls: 'ghost', disabled: !it.floated && it.status !== 'ready',
          title: !it.floated && it.status !== 'ready' ? 'Only a ready item can be floated.' : null, onclick: scFloat })),
      it.status === 'on air' ? el('p', { class: 'hint', text: 'This item is on air. Saving changes the prompter text on its next refresh and takes the approval back; it does not touch playout.' }) : null);
  }

  function scPreviewNodes(text) {
    const blocks = scriptBlocks(text);
    if (!blocks.length) return el('p', { class: 'empty', text: 'Nothing to read yet.' });
    const paras = [];
    for (const b of blocks) (paras[b.para] || (paras[b.para] = [])).push(b);
    return paras.map((lines) => el('div', { class: 'sc-para' }, lines.map((b) => b.kind === 'read' ? el('p', { class: 'sc-read', text: b.text })
      : el('p', { class: b.kind === 'note' ? 'sc-note' : 'sc-ins' }, el('span', { class: 'tag', text: b.kind === 'note' ? 'Private note' : 'Instruction' }), ' ', b.text))));
  }
  function renderScPreview() {
    const st = scLoaded();
    paint('sc-preview', [SC.sel, !!st, st ? scText() : ''], () => (st ? scPreviewNodes(scText()) : el('p', { class: 'empty', text: 'Choose a story.' })));
  }

  function scFoot() {
    const st = scLoaded(), d = scDraftOf(SC.sel);
    if (!st) return null;
    const cur = (st.revisions || []).find((r) => r.rev === st.rev) || {};
    return [el('span', { class: 'mono', text: 'Revision ' + st.rev }),
      el('span', { text: cur.actor ? 'saved by ' + cur.actor + (cur.at ? ' at ' + fmtTime(cur.at) : '') : 'as first written' }),
      st.storyStatus === 'approved' && st.approvedBy ? el('span', { class: 'ok' }, icon('rights', 's'), 'Approved by ' + st.approvedBy + (st.approvedAt ? ' at ' + fmtTime(st.approvedAt) : '') + ', revision ' + st.approvedRev) : null,
      d ? stateChip('unsaved') : null];
  }

  function scHistory() {
    const st = scLoaded();
    if (!st) return el('p', { class: 'empty', text: 'Choose a story.' });
    const eventText = (e) => (e.kind === 'status' ? e.actor + ' moved it from ' + e.from + ' to ' + e.to : e.kind === 'float' ? e.actor + ': ' + e.to : e.actor + ': ' + e.kind) +
      (e.rev ? ' (revision ' + e.rev + ')' : '') + (e.note ? ' · ' + e.note : '');
    return [el('h3', { text: 'Revisions' }),
      el('ul', { class: 'list sc-hist' }, (st.revisions || []).map((r) => el('li', {}, el('span', { class: 'mono', text: 'r' + r.rev }), ' ',
        r.actor ? r.actor + (r.at ? ' · ' + fmtTime(r.at) : '') : 'as first written', r.rev === st.rev ? el('span', { class: 'muted', text: ' · current' }) : null))),
      el('h3', { text: 'Status and float' }),
      (st.events || []).length ? el('ul', { class: 'list sc-hist' }, st.events.map((e) => el('li', {}, el('span', { class: 'muted', text: fmtTime(e.at) + ' ' }), eventText(e))))
        : el('p', { class: 'empty', text: 'No status changes yet.' })];
  }
  function scTabs() {
    const tabs = $('sc-tabs');
    paint('sc-tabs', [SC.tab, SC_TABS.map((t) => t.id)], () => SC_TABS.map((t) => el('button', { type: 'button', role: 'tab', id: 'sc-tab-' + t.id, 'aria-controls': 'sc-side',
      'aria-selected': String(SC.tab === t.id), tabindex: SC.tab === t.id ? null : '-1', 'data-fk': 'sc-tab-' + t.id, onclick: () => { SC.tab = t.id; render(); } }, icon(t.icon, 's'), t.label)));
    $('sc-side').setAttribute('aria-labelledby', 'sc-tab-' + SC.tab);
    if (!tabs.dataset.keys) {
      tabs.dataset.keys = '1';
      tabs.addEventListener('keydown', (e) => {   // arrow keys move between tabs, as in the Library
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        const i = SC_TABS.findIndex((t) => t.id === SC.tab), j = (i + (e.key === 'ArrowRight' ? 1 : SC_TABS.length - 1)) % SC_TABS.length;
        e.preventDefault(); SC.tab = SC_TABS[j].id; render();
        const n = $('sc-tab-' + SC.tab); if (n) n.focus();
      });
    }
  }

  // ---- Wire: the inbox. A person takes an entry into the open show as a draft story, or dismisses it. Nothing arrives on air by itself.
  const WIRE = { rows: [], feeds: [], next: null, busy: false, err: null, at: 0, loaded: false };
  const WIRE_EVERY_MS = 15000;
  async function wireLoad(more) {
    if (WIRE.busy) return;
    WIRE.busy = true; WIRE.err = null; render();
    try {
      const q = { state: 'new', limit: '50' };
      if (more && WIRE.next) q.cursor = WIRE.next;
      const { status, data } = await call('wireList', null, null, q);
      if (status === 200 && data && data.ok) { WIRE.rows = more ? WIRE.rows.concat(data.stories) : data.stories; WIRE.feeds = data.feeds || []; WIRE.next = data.next || null; WIRE.loaded = true; }
      else WIRE.err = status === 401 ? 'A token is needed to read the inbox.' : codeOf(data, status).reason || 'The server refused to list the inbox.';
    } catch (e) { WIRE.err = 'The server did not answer. The list may be out of date.'; }
    WIRE.busy = false; WIRE.at = Date.now(); render();
  }
  function wireMaybeLoad() {
    if (!WIRE.busy && (!WIRE.loaded || Date.now() - WIRE.at > WIRE_EVERY_MS) && !(WIRE.err && Date.now() - WIRE.at < WIRE_EVERY_MS)) wireLoad(false);
  }
  function wireRow(w) {
    const take = 'wire-take:' + w.id, dis = 'wire-dismiss:' + w.id;
    return el('li', { class: 'wirerow' },
      el('div', { class: 'wh', text: w.headline }),
      el('div', { class: 'meta' }, w.feed + (w.byline ? ' · ' + w.byline : '') + (w.publishedAt ? ' · ' + fmtTime(w.publishedAt) : '')),
      w.snippet ? el('p', { class: 'snip', text: w.snippet }) : null,
      el('div', { class: 'bar flush' },
        btn('Take into this show', { key: take, cls: 'small', disabled: !S.show, aria: 'Take "' + w.headline + '" into this show as a draft',
          onclick: () => act('wireTake', w.id, { show: S.show }, { key: take, okTitle: 'Taken as a draft.', ok: () => 'It is at the end of the rundown, not approved and not on air. Open it in the editor to work on it.',
            hint: 'Someone else may have taken or dismissed it. The inbox is reloaded.' }).then(() => wireLoad(false)) }),
        btn('Dismiss', { key: dis, cls: 'ghost small', aria: 'Dismiss "' + w.headline + '"',
          onclick: () => act('wireDismiss', w.id, {}, { key: dis, okTitle: 'Dismissed.', ok: () => 'It leaves the inbox.' }).then(() => wireLoad(false)) })));
  }
  function wirePanel() {
    return [el('div', { class: 'bar' }, btn(WIRE.busy ? 'Loading…' : 'Refresh', { key: 'wire-refresh', cls: 'ghost small', disabled: WIRE.busy, onclick: () => wireLoad(false) }),
        el('span', { class: 'hint', text: 'Copy from feeds the server is set to read. Nothing here airs until you take it and approve it.' })),
      WIRE.err ? el('p', { class: 'info', role: 'alert' }, icon('alert', 's'), ' ', WIRE.err) : null,
      WIRE.feeds.length ? el('ul', { class: 'list wirefeeds', 'aria-label': 'Feeds' }, WIRE.feeds.map((f) => el('li', {},
        f.lastError ? stateChip('failed', 'Feed error') : stateChip('fresh', 'Feed ok'), ' ', el('strong', { text: f.feed }),
        f.lastError ? el('span', { class: 'hint', text: ' · ' + f.lastError }) : f.lastOkAt ? el('span', { class: 'hint', text: ' · last read ' + fmtTime(f.lastOkAt) }) : null))) : null,
      WIRE.rows.length ? el('ul', { class: 'list wirelist', 'aria-label': 'Wire stories not yet taken' }, WIRE.rows.map(wireRow))
        : el('p', { class: 'empty', text: WIRE.loaded ? (WIRE.feeds.length ? 'Nothing waiting in the inbox.' : 'No feed has been read yet. Whoever runs the server sets the feeds (VOLT_WIRE_FEEDS); with none set, nothing is fetched.') : 'Reading the inbox…' }),
      WIRE.next ? el('div', { class: 'bar' }, btn('Load more', { key: 'wire-more', cls: 'ghost small', disabled: WIRE.busy, onclick: () => wireLoad(true) })) : null,
      el('p', { class: 'note', text: 'Wire formats are checked against sample files only, not against a real agency feed.' })];
  }

  // ---- Templates: start a story or a whole show from a saved shape. Stories made from one are drafts, never approved. ----
  const TPL = { rows: [], busy: false, err: null, loaded: false, name: '', title: '' };
  async function tplLoad() {
    if (TPL.busy) return;
    TPL.busy = true; TPL.err = null; render();
    try {
      const { status, data } = await call('templateList');
      if (status === 200 && data && data.ok) { TPL.rows = data.templates; TPL.loaded = true; }
      else TPL.err = status === 401 ? 'A token is needed to read the templates.' : codeOf(data, status).reason || 'The server refused to list the templates.';
    } catch (e) { TPL.err = 'The server did not answer.'; }
    TPL.busy = false; render();
  }
  function tplSave(kind) {
    if (!TPL.name.trim()) { setFlash({ kind: 'bad', title: 'Name the template first.', code: 'bad_input', reason: 'A template needs a name.' }); render(); return; }
    const body = kind === 'story' ? { kind, name: TPL.name.trim(), fromItem: SC.sel } : { kind, name: TPL.name.trim(), fromShow: S.show };
    TPL.name = '';
    act('templateCreate', '', body, { key: 'tpl-save-' + kind, okTitle: 'Template saved.', ok: () => (kind === 'story' ? 'The slug and script of this story, private notes included.' : 'The stories and their cues, the slot length and the pace. No media is kept.') }).then(tplLoad);
  }
  function tplUse(t) {
    if (t.kind === 'story') {
      act('templateUse', t.id, { show: S.show }, { key: 'tpl-use:' + t.id, okTitle: 'Draft added.', ok: () => 'A new draft story is at the end of the rundown. It is not approved.' });
      return;
    }
    const title = TPL.title.trim();
    if (!title) { setFlash({ kind: 'bad', title: 'Name the new show first.', code: 'bad_input', reason: 'A show made from a template needs a title.' }); render(); return; }
    TPL.title = '';
    act('templateUse', t.id, { title }, { key: 'tpl-use:' + t.id, okTitle: 'Show created.', ok: (d) => {
      S.show = d.show.id; S.selected = null; SC.sel = null; S.sigs = {};
      try { sessionStorage.setItem('volt.show', S.show); } catch (x) { /* ignore */ }
      return '"' + d.show.title + '" is open with ' + d.items.length + (d.items.length === 1 ? ' draft story' : ' draft stories') + (d.skippedCues ? '; ' + d.skippedCues + ' cue(s) the server no longer accepts were left out.' : '.');
    } });
  }
  function tplPanel() {
    const hasStory = !!scLoaded();
    return [el('div', { class: 'sc-tplform' },
        el('label', { for: 'tpl-name', text: 'Save as a template' }),
        el('input', { id: 'tpl-name', type: 'text', 'data-fk': 'tpl-name', value: TPL.name, maxlength: '120', autocomplete: 'off', placeholder: 'Template name', oninput: (e) => { TPL.name = e.target.value; } }),
        el('div', { class: 'bar flush' },
          btn('New from this story', { key: 'tpl-save-story', cls: 'ghost small', disabled: !hasStory, title: hasStory ? null : 'Choose a story first', onclick: () => tplSave('story') }),
          btn('New from this show', { key: 'tpl-save-show', cls: 'ghost small', disabled: !S.show, onclick: () => tplSave('show') }))),
      el('div', { class: 'sc-tplform' }, el('label', { for: 'tpl-title', text: 'Title for a new show' }),
        el('input', { id: 'tpl-title', type: 'text', 'data-fk': 'tpl-title', value: TPL.title, maxlength: '200', autocomplete: 'off', placeholder: 'Used by show templates', oninput: (e) => { TPL.title = e.target.value; } })),
      TPL.err ? el('p', { class: 'info', role: 'alert' }, icon('alert', 's'), ' ', TPL.err) : null,
      TPL.rows.length ? el('ul', { class: 'list wirelist', 'aria-label': 'Templates' }, TPL.rows.map((t) => {
        const del = 'tpl-del:' + t.id;
        return el('li', { class: 'wirerow' }, el('div', { class: 'wh', text: t.name }),
          el('div', { class: 'meta', text: t.kind === 'story' ? 'Story template' : 'Show template · ' + t.stories + (t.stories === 1 ? ' story' : ' stories') + (t.slotSeconds ? ' · slot ' + fmtOffset(t.slotSeconds) : '') + ' · ' + (t.createdBy || '') }),
          el('div', { class: 'bar flush' },
            btn(t.kind === 'story' ? 'Add to this show' : 'Make a show', { key: 'tpl-use:' + t.id, cls: 'small', disabled: t.kind === 'story' && !S.show,
              aria: (t.kind === 'story' ? 'Add a draft from ' : 'Make a show from ') + t.name, onclick: () => tplUse(t) }),
            btn(S.armed === del ? 'Confirm delete' : 'Delete', { key: 'tpl-delb:' + t.id, cls: 'danger small' + (S.armed === del ? ' armed' : ''), aria: (S.armed === del ? 'Confirm delete of ' : 'Delete ') + t.name,
              onclick: () => armedAction(del, () => act('templateDelete', t.id, {}, { key: 'tpl-del-act:' + t.id, okTitle: 'Template deleted.', ok: () => 'Stories made from it are not affected.' }).then(tplLoad)) })));
      })) : el('p', { class: 'empty', text: TPL.loaded ? 'No templates yet.' : 'Reading the templates…' }),
      el('p', { class: 'note', text: 'A template keeps text and cues, never media. Anything made from one starts as a draft with no approval.' })];
  }

  // ---- Archive: find a story, live or from a deleted show, and copy it into this show as a draft. Search is LIKE, not full text. ----
  const ARC = { q: '', status: '', from: '', to: '', rows: [], next: null, busy: false, err: null, loaded: false };
  async function arcLoad(more) {
    if (ARC.busy) return;
    ARC.busy = true; ARC.err = null; render();
    const q = { limit: '25' };
    for (const [k, v] of [['q', ARC.q.trim()], ['status', ARC.status], ['from', ARC.from.trim()], ['to', ARC.to.trim()]]) if (v) q[k] = v;
    if (more && ARC.next) q.cursor = ARC.next;
    try {
      const { status, data } = await call('stories', null, null, q);
      if (status === 200 && data && data.ok) { ARC.rows = more ? ARC.rows.concat(data.rows) : data.rows; ARC.next = data.next || null; ARC.loaded = true; }
      else ARC.err = status === 401 ? 'A token is needed to search.' : codeOf(data, status).reason || 'The server refused the search.';
    } catch (e) { ARC.err = 'The server did not answer.'; }
    ARC.busy = false; render();
  }
  function arcPanel() {
    const field = (id, label, key, ph) => el('div', {}, el('label', { for: id, text: label }),
      el('input', { id, type: 'text', 'data-fk': id, value: ARC[key], placeholder: ph, autocomplete: 'off', oninput: (e) => { ARC[key] = e.target.value; } }));
    return [el('form', { class: 'sc-tplform', 'aria-label': 'Search stories', onsubmit: (e) => { e.preventDefault(); arcLoad(false); } },
        field('arc-q', 'Words in the slug or script', 'q', 'bridge'),
        el('div', { class: 'form-row flush' }, el('div', {}, el('label', { for: 'arc-status', text: 'Story status' }),
          el('select', { id: 'arc-status', 'data-fk': 'arc-status', onchange: (e) => { ARC.status = e.target.value; } },
            SC_FILTERS.map(([v, label]) => el('option', { value: v === 'all' ? '' : v, selected: (v === 'all' ? '' : v) === ARC.status }, label))))),
        el('div', { class: 'form-row flush' }, field('arc-from', 'From', 'from', 'YYYY-MM-DD'), field('arc-to', 'To', 'to', 'YYYY-MM-DD')),
        el('div', { class: 'bar flush' }, el('button', { type: 'submit', class: 'btn small', 'data-fk': 'arc-go', disabled: ARC.busy }, ARC.busy ? 'Searching…' : 'Search'))),
      ARC.err ? el('p', { class: 'info', role: 'alert' }, icon('alert', 's'), ' ', ARC.err) : null,
      ARC.rows.length ? el('ul', { class: 'list wirelist', 'aria-label': 'Stories found' }, ARC.rows.map((r) => {
        const key = 'arc-copy:' + r.id;
        return el('li', { class: 'wirerow' }, el('div', { class: 'wh', text: r.slug }),
          el('div', { class: 'chips scchips' }, stateChip(storyKind(r.storyStatus)), r.archived ? stateChip('archived') : null),
          el('div', { class: 'meta', text: (r.show || '') + (r.at ? ' · ' + fmtTime(r.at) : '') }),
          r.snippet ? el('p', { class: 'snip', text: r.snippet }) : null,
          el('div', { class: 'bar flush' }, btn('Copy to this show', { key, cls: 'small', disabled: !S.show, aria: 'Copy "' + r.slug + '" to this show as a draft',
            onclick: () => act('itemCopy', r.id, { show: S.show }, { key, okTitle: 'Copied as a draft.', ok: () => 'It is at the end of the rundown, revision 1, not approved. The original is unchanged.' }) })));
      })) : el('p', { class: 'empty', text: ARC.loaded ? 'No story matches.' : 'Reading the archive…' }),
      ARC.next ? el('div', { class: 'bar' }, btn('Load more', { key: 'arc-more', cls: 'ghost small', disabled: ARC.busy, onclick: () => arcLoad(true) })) : null,
      el('p', { class: 'note', text: 'Searches the slug and the words the talent reads, never a private note. It is a plain text match, fine for thousands of stories and not a full-text index. Stories of deleted shows are kept here.' })];
  }

  // What each tab of the story panel loads (once, or on a timer) and paints. A tab's `sig` is what repaints it.
  const SC_PANELS = {
    history: { sig: () => { const st = scLoaded(); return [SC.sel, st && [st.rev, st.storyStatus, (st.events || []).length, (st.revisions || []).length]]; }, build: () => scHistory() },
    wire: { load: wireMaybeLoad, sig: () => [WIRE.rows, WIRE.feeds, WIRE.err, WIRE.busy, S.show, [...S.pending].filter((k) => k.startsWith('wire-'))], build: () => wirePanel() },
    templates: { load: () => { if (!TPL.loaded && !TPL.busy && !TPL.err) tplLoad(); },
      sig: () => [TPL.rows, TPL.err, TPL.busy, TPL.name, TPL.title, S.show, SC.sel, !!scLoaded(), S.armed, [...S.pending].filter((k) => k.startsWith('tpl-'))], build: () => tplPanel() },
    archive: { load: () => { if (!ARC.loaded && !ARC.busy && !ARC.err) arcLoad(false); },
      sig: () => [ARC.rows, ARC.next, ARC.err, ARC.busy, ARC.q, ARC.status, ARC.from, ARC.to, S.show, [...S.pending].filter((k) => k.startsWith('arc-'))], build: () => arcPanel() },
  };

  function renderScripts() {
    const items = scItems();
    if (S.data && S.data.rundown && !items.find((i) => i.id === SC.sel)) { SC.sel = null; SC.story = null; }
    if (!SC.sel && items.length) scSelect((items.find((i) => i.status === 'on air' || i.status === 'taking') || items.find((i) => i.status === 'ready') || items[0]).id, true);
    const it = scItem(), st = scLoaded();
    // Someone else (or an action here) changed the story: the rundown summary no longer matches what was read, so read it again.
    if (it && st && !SC.loading && Date.now() - SC.loadedAt > 1500 &&
        (it.scriptRev !== st.rev || it.storyStatus !== st.storyStatus || it.floated !== st.floated || it.status !== st.itemStatus)) scLoad(SC.sel);
    const wpm = scWpm();
    const paceText = SC.pace === null ? String(wpm) : SC.pace;
    const pacePress = () => { const v = (SC.pace === null ? String(scWpm()) : SC.pace).trim(); scSetPace(/^\d+$/.test(v) ? Number(v) : NaN); };
    paint('sc-pace', [wpm, SC.pace, S.show, [...S.pending].filter((k) => k.startsWith('sc-pace'))], () => el('form', { class: 'form-row', 'aria-labelledby': 'h-sc-list',
      onsubmit: (e) => { e.preventDefault(); pacePress(); } },
      el('div', {}, el('label', { for: 'sc-wpm', text: 'Reading pace, words a minute' }),
        el('input', { id: 'sc-wpm', type: 'text', inputmode: 'numeric', 'data-fk': 'sc-wpm', value: paceText, autocomplete: 'off', size: '5',
          oninput: (e) => { SC.pace = e.target.value; } })),
      btn('Set pace', { key: 'sc-pace', cls: 'ghost small', onclick: pacePress }),
      btn('Default', { key: 'sc-pace-default', cls: 'ghost small', title: 'Use 160 words a minute', onclick: () => scSetPace(null) })));
    paint('sc-prompter', [SC.link, S.show, [...S.pending].filter((k) => k === 'sc-link')], scPrompter);
    paint('sc-filter', [SC.filter], () => el('div', { class: 'form-row' }, el('div', {}, el('label', { for: 'sc-filter-sel', text: 'Show' }),
      el('select', { id: 'sc-filter-sel', 'data-fk': 'sc-filter-sel', onchange: (e) => { SC.filter = e.target.value; render(); } },
        SC_FILTERS.map(([v, label]) => el('option', { value: v, selected: v === SC.filter }, label))))));
    paint('sc-list', [items, SC.sel, SC.filter, Object.keys(SC.drafts)], scList);
    paint('sc-presence', [S.data.presence, S.data.collab, WHO.name, LS.client, S.show], presenceStrip);
    paint('sc-lease', [SC.sel, leaseState(), LS.holder, leaseState() === 'blocked' ? Math.floor(((leaseView(S.data.leases, SC.sel, leaseMe()) || {}).secondsLeft || 0) / 15) : 0, LS.copied, S.armed === 'takeover:' + SC.sel], leaseBanner);
    paint('sc-edit', [SC.sel, !!st, leaseAllowsEdit(), st && [st.rev, st.storyStatus, st.approvedBy], it && [it.status, it.floated], SC.loadErr, SC.saving, !!scDraftOf(SC.sel),
      scDraftOf(SC.sel) && scDraftOf(SC.sel).base < (st ? st.rev : 0), S.armed, [...S.pending].filter((k) => k.startsWith('sc-'))], scEditor);
    paint('sc-foot', [SC.sel, st && [st.rev, st.storyStatus, st.approvedBy, st.approvedAt, (st.revisions || []).length], !!scDraftOf(SC.sel)], scFoot);
    scCountNow();
    renderScPreview();
    scTabs();
    const panel = SC_PANELS[SC.tab] || SC_PANELS.history;
    if (panel.load) panel.load();
    paint('sc-side', [SC.tab, ...panel.sig()], panel.build);
  }
  // Closing the conflict dialog (Esc included) gives focus back to the editor and keeps the draft exactly as it was.
  function scConflictInit() {
    $('sc-conflict').addEventListener('close', () => {
      if (SC.conflict) SC.conflict = null;
      const n = $(SC.returnTo || 'sc-text') || $('sc-text');
      if (n) n.focus();
    });
  }

  // ---- Edit leases and presence (docs/superpowers/specs/2026-11-01-collaboration-identity-design.md) ------------------------------
  // A lease says "this person has this item open for editing". The server decides and enforces it; this section asks for one when the
  // Scripts editor opens an item, renews it every 15 seconds, gives it back when the editor closes, and shows what the server said.
  // The text typed in the editor lives in SC.drafts and is never touched here: losing a lease changes what the page lets you do, not
  // what you typed. Nothing on the on-air path (take, go, hold, release, skip, kill) reads or waits for any of this.
  const LEASE_RENEW_MS = 15000, PRESENCE_MS = 10000;

  // What a lease means for the row chip. `me` is {user, client}: a lease is yours only when both match, so a second tab of the same
  // person sees the first tab's lease as someone else's ("in another tab").
  function leaseView(leases, item, me) {
    const l = (leases || []).find((x) => x.item === item);
    if (!l) return null;
    const mine = l.holder === me.user && l.clientId === me.client;
    if (mine) return { kind: 'yours', word: 'You editing', holder: l.holder, secondsLeft: l.secondsLeft, mine: true };
    return { kind: 'editing', word: l.holder + ' editing' + (l.holder === me.user ? ' in another tab' : ''), holder: l.holder, secondsLeft: l.secondsLeft, mine: false };
  }

  // The server's answer to an acquire or renew, as the state the editor is in. A failure to get an answer is not yet a loss of the lease
  // (the 45 s lease outlives one missed renew); two in a row means the page can no longer tell, so it stops saving and says so.
  function leaseOutcome(status, data, fails) {
    const code = data && data.error && data.error.code;
    if (status === 200 && data && data.ok) return { state: 'held', fails: 0, secondsLeft: data.lease ? data.lease.secondsLeft : 0 };
    if (status === 409 && code === 'leased') return { state: 'blocked', fails: 0, holder: data.holder || '', secondsLeft: data.secondsLeft || 0 };
    if (status === 409 && code === 'lease_lost') return { state: 'lost', fails: 0, holder: data.holder || '' };
    if (status === 409 && code === 'not_editable') return { state: 'ended', fails: 0 };
    if (code === 'collab_unavailable') return { state: 'unavailable', fails: 0 };
    if (status === 403) return { state: 'none', fails: 0 };   // a viewer, or no edit access to this show: the server refuses the save in its own words
    const n = (fails || 0) + 1;
    return { state: n >= 2 ? 'unsure' : 'held', fails: n };
  }
  // A save goes out only while the lease is held, or when there is no lease system to ask. The server checks again either way.
  function leaseAllowsSave(state) { return state === 'held' || state === 'unavailable' || state === 'none'; }

  // Who is here: one entry per person, however many tabs. `initials` are for the circles; the words below carry the same information.
  function presenceGroups(presence, me) {
    const out = [];
    for (const p of presence || []) {
      let g = out.find((x) => x.user === p.user);
      if (!g) {
        const words = String(p.name || p.user).trim().split(/\s+/).filter(Boolean);
        g = { user: p.user, name: p.name || p.user, initials: words.slice(0, 2).map((w) => w[0].toUpperCase()).join(''), tabs: 0, views: [], you: p.user === me.user };
        out.push(g);
      }
      g.tabs += 1;
      g.views.push(p.view);
    }
    return out;
  }
  function presenceViewWords(view, slugOf) {
    if (view === 'library') return 'in the library';
    if (view === 'show') return 'viewing the rundown';
    if (typeof view === 'string' && view.startsWith('item:')) return 'working on ' + ((slugOf && slugOf[view.slice(5)]) || 'an item');
    return 'here';
  }
  function presenceLines(presence, me, slugOf) {
    return presenceGroups(presence, me).map((g) => {
      const words = [];
      for (const v of g.views) { const w = presenceViewWords(v, slugOf); if (!words.includes(w)) words.push(w); }
      return (g.you ? 'You' : g.name) + ': ' + words.join(', ');
    });
  }

  // This tab's id: random, kept for the life of the tab (sessionStorage) so a reload does not leave the tab fighting its own lease.
  function leaseClientId() {
    try { const v = sessionStorage.getItem('volt.client'); if (v && v.length <= 64 && /^[\w.:-]+$/.test(v)) return v; } catch (e) { /* works without it */ }
    let id = 'c';
    try { id += Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, '0')).join(''); }
    catch (e) { id += Math.random().toString(16).slice(2, 18); }
    try { sessionStorage.setItem('volt.client', id); } catch (e) { /* works without it */ }
    return id;
  }

  // item: the item this tab holds or wants; state: none | held | blocked | lost | unsure | ended | unavailable.
  const LS = { client: leaseClientId(), item: null, state: 'none', holder: '', secondsLeft: 0, fails: 0, timer: null, busy: false, copied: false };
  const leaseMe = () => ({ user: WHO.name || 'desk', client: LS.client });
  const leaseState = () => (LS.item && LS.item === SC.sel ? LS.state : 'none');
  const slugMap = () => { const m = {}; for (const i of (S.data && S.data.rundown ? S.data.rundown.items : [])) m[i.id] = i.slug; return m; };

  // Which item should this tab hold right now: the Scripts editor's open item, when it is ready and editable.
  function leaseWant() {
    if (S.screen !== 'scripts' || !SC.sel || !S.data || !S.data.rundown) return null;
    const it = scItem(), st = scLoaded();
    return it && it.status === 'ready' && !(st && scReason(it, st)) ? SC.sel : null;
  }
  function leaseSync() {
    const want = leaseWant();
    if (want === LS.item) return;
    leaseGiveBack();
    LS.item = want; LS.state = 'none'; LS.holder = ''; LS.secondsLeft = 0; LS.fails = 0; LS.copied = false;
    if (want) leaseAsk(false);
  }
  function leaseApply(res, wasRenew) {
    const was = LS.state;
    if (res.state === 'held' && res.fails === 0) { LS.state = 'held'; LS.fails = 0; }
    else if (res.state === 'held') { LS.fails = res.fails; if (was !== 'held') LS.state = 'unsure'; }   // never held: a missing answer is not a lease
    else { LS.state = res.state; LS.fails = res.fails; }
    if (res.holder != null) LS.holder = res.holder;
    if (res.secondsLeft != null) LS.secondsLeft = res.secondsLeft;
    clearTimeout(LS.timer); LS.timer = null;
    if (LS.state === 'held' || LS.state === 'blocked' || LS.state === 'unsure') LS.timer = setTimeout(() => leaseAsk(LS.state === 'held' || wasRenew), LEASE_RENEW_MS);
    if (LS.state !== was) announce(LS.state === 'lost' ? (LS.holder || 'Someone') + ' took this item over. Your text is still in the editor and was not saved.'
      : LS.state === 'blocked' ? (LS.holder || 'Someone') + ' is editing this item.' : LS.state === 'unsure' ? 'Cannot confirm you still hold this item. Saving is off until it can.' : '');
    S.sigs['sc-edit'] = null; S.sigs['sc-lease'] = null;
    render();
  }
  async function leaseAsk(renew) {
    const id = LS.item;
    if (!id || LS.busy) return;
    LS.busy = true;
    let res;
    try {
      const { status, data } = await call('leaseAcquire', id, renew ? { clientId: LS.client, renew: true } : { clientId: LS.client });
      res = leaseOutcome(status, data, LS.fails);
    } catch (e) { res = leaseOutcome(0, null, LS.fails); }
    LS.busy = false;
    if (LS.item === id) leaseApply(res, renew);
  }
  async function leaseTakeover() {
    const id = LS.item;
    if (!id || LS.busy) return;
    LS.busy = true;
    try {
      const { status, data } = await call('leaseTakeover', id, { clientId: LS.client });
      LS.busy = false;
      if (LS.item === id) {
        if (status === 409 && data && data.error && data.error.code === 'not_leased') { leaseApply({ state: 'none', fails: 0 }, false); leaseAsk(false); }
        else leaseApply(leaseOutcome(status, data, 0), false);
        if (status === 200) setFlash({ kind: 'good', title: 'You are editing now.', reason: 'The previous editor is told. Their next save is refused and their text stays in their editor.' });
        else if (data && data.error) setFlash({ kind: 'bad', title: (ERR_LABEL[data.error.code] || 'Refused') + '.', code: data.error.code, reason: data.error.reason || '' });
      }
    } catch (e) { LS.busy = false; setFlash({ kind: 'bad', title: 'No answer from the server.', code: 'network', reason: 'Nothing changed. Try again.' }); render(); }
  }
  // Give the lease back (the editor closed). Fire and forget: a lease that does not get back lapses on its own in 45 seconds.
  function leaseGiveBack(onUnload) {
    const id = LS.item;
    clearTimeout(LS.timer); LS.timer = null;
    if (!id || (LS.state !== 'held' && LS.state !== 'unsure')) return;
    try {
      if (onUnload) {
        const headers = authHeaders('POST', getToken(), SESS.csrf); headers['Content-Type'] = 'application/json';
        fetch('/api/items/' + encodeURIComponent(id) + '/lease/release', { method: 'POST', headers, body: JSON.stringify({ clientId: LS.client }), keepalive: true, cache: 'no-store' });
      } else call('leaseRelease', id, { clientId: LS.client }).catch(() => {});
    } catch (e) { /* the lease lapses by itself */ }
  }
  function leaseReopen() { LS.state = 'none'; LS.fails = 0; LS.copied = false; S.sigs['sc-edit'] = null; S.sigs['sc-lease'] = null; leaseAsk(false); render(); }
  async function leaseCopy() {
    const d = scDraftOf(SC.sel);
    const text = d ? d.script : scText();
    try { await navigator.clipboard.writeText(text); LS.copied = true; announce('Your text is copied.'); }
    catch (e) { const t = $('sc-text'); if (t) { t.focus(); t.select(); } announce('Your text is selected: press Ctrl+C to copy it.'); }
    S.sigs['sc-lease'] = null; render();
  }

  // The banner above the editor: why it is read-only, in words, with the one action that fits. An icon and words, never colour alone.
  function leaseBanner() {
    const state = leaseState(), who = LS.holder || 'Someone';
    const lv = leaseView(S.data && S.data.leases, SC.sel, leaseMe());
    const left = lv ? lv.secondsLeft : LS.secondsLeft;
    if (state === 'blocked') {
      const armed = S.armed === 'takeover:' + SC.sel;
      return el('div', { class: 'lease-banner', role: 'status' }, icon('lock', 's'),
        el('div', { class: 'msg' }, el('strong', { text: who + ' is editing this item. ' }),
          'The editor is read-only' + (left && left <= 60 ? '; their lease ends in ' + left + ' seconds if they stop' : '') + '. You can take over: they are told, and their next save is refused.'),
        btn(armed ? 'Confirm take over' : 'Take over', { key: 'lease-takeover', cls: 'ghost small' + (armed ? ' armed' : ''), onclick: () => armedAction('takeover:' + SC.sel, leaseTakeover) }));
    }
    if (state === 'lost') {
      return el('div', { class: 'lease-banner', role: 'alert' }, icon('alert', 's'),
        el('div', { class: 'msg' }, el('strong', { text: who + ' took this item over. ' }),
          'Your text is still here and was not saved. Copy it, then open the item again once they are done.'),
        btn(LS.copied ? 'Copied' : 'Copy my text', { key: 'lease-copy', cls: 'ghost small', onclick: leaseCopy }),
        btn('Try to edit again', { key: 'lease-reopen', cls: 'ghost small', onclick: leaseReopen }));
    }
    if (state === 'unsure') {
      return el('div', { class: 'lease-banner', role: 'alert' }, icon('alert', 's'),
        el('div', { class: 'msg' }, el('strong', { text: 'Cannot confirm you still hold this item. ' }),
          'Saving is off until the server answers. Your text is still here.'),
        btn(LS.copied ? 'Copied' : 'Copy my text', { key: 'lease-copy', cls: 'ghost small', onclick: leaseCopy }));
    }
    if (state === 'ended') {
      return el('div', { class: 'lease-banner', role: 'status' }, icon('lock', 's'),
        el('div', { class: 'msg' }, el('strong', { text: 'This item is no longer editable. ' }), 'It went to air or was passed over, so editing ended. Your text is still here.'),
        btn(LS.copied ? 'Copied' : 'Copy my text', { key: 'lease-copy', cls: 'ghost small', onclick: leaseCopy }));
    }
    if (state === 'unavailable') {
      return el('div', { class: 'lease-banner', role: 'status' }, icon('alert', 's'),
        el('div', { class: 'msg' }, el('strong', { text: 'Collaboration is unavailable. ' }),
          'Editing leases are off, so nothing stops two people editing this item. Saving still checks the revision. The on-air controls are not affected.'));
    }
    return null;
  }
  // The editor is read-only unless this tab holds the lease (or there is no lease system to ask).
  const leaseAllowsEdit = () => leaseAllowsSave(leaseState());

  // The strip of who is here, under the Show header and above the editor. Initials in circles are for sighted people (and say the name in a
  // tooltip); the visible list beside them is the same information in words for keyboard and screen-reader users.
  function presenceStrip() {
    const d = S.data || {}, me = leaseMe(), groups = presenceGroups(d.presence, me), lines = presenceLines(d.presence, me, slugMap());
    const off = d.collab && d.collab.available === false;
    return [off ? el('p', { class: 'info presence-off', role: 'status' }, icon('alert', 's'), ' ', el('strong', { text: 'Collaboration is unavailable. ' }),
        'Who is here and who is editing cannot be shown, and edits are not protected by a lease. The on-air controls (take, go, hold, release, skip, kill) are not affected.') : null,
      groups.length ? el('div', { class: 'presence-row' },
        el('ul', { class: 'avatars', 'aria-hidden': 'true' }, groups.map((g) => el('li', { class: 'avatar' + (g.you ? ' you' : ''), title: g.name + (g.tabs > 1 ? ' (' + g.tabs + ' tabs)' : ''), text: g.initials }))),
        el('ul', { class: 'presence-list', 'aria-label': 'Who is here' }, lines.map((t) => el('li', { text: t })))) : null];
  }

  // Presence: one heartbeat every 10 s from a visible tab; the page reads everyone's from the state poll. Best effort, never audited.
  function presenceView() {
    if (S.screen === 'scripts' && SC.sel) return 'item:' + SC.sel;
    return S.screen === 'library' ? 'library' : 'show';
  }
  async function presenceBeat() {
    if (document.hidden || !S.data || !S.show || S.conn !== 'ok') return;
    try { await call('presence', null, { clientId: LS.client, show: S.show, view: presenceView() }); } catch (e) { /* the next beat tries again */ }
  }
  function presenceInit() {
    setInterval(presenceBeat, PRESENCE_MS);
    window.addEventListener('pagehide', () => leaseGiveBack(true));
  }

  // ---- dialogs, keys, screens ----------------------------------------------
  function openToken() { const d = $('token-dlg'); if (!d.open) { d.showModal(); $('token-in').focus(); loadProviders(); } }
  // The Token dialog also offers 'Sign in with ...' when the server has OIDC configured (GET /auth/providers, 404 when off). Anchors, not
  // forms: the CSP has form-action 'none', and the server does the redirect.
  function renderProviders(providers, next) {
    const links = providerLinks(providers, next);
    $('token-providers').hidden = links.length === 0;
    $('token-provider-list').replaceChildren(...links.map((l) => el('li', {},
      el('a', { class: 'btn ghost', href: l.href, 'data-provider': l.id }, icon('signin', 's'), el('span', { text: 'Sign in with ' + l.label })))));
  }
  let providersLoaded = false;
  async function loadProviders() {
    if (providersLoaded) return;
    providersLoaded = true;
    try {
      const res = await fetch('/auth/providers', { cache: 'no-store' });
      const data = res.ok ? await res.json() : null;
      renderProviders(data && data.providers, location.pathname + location.search);
    } catch (e) { providersLoaded = false; }   // offline or an older server: try again next time the dialog opens
  }
  function anyDialog() { return $('token-dlg').open || $('keys-dlg').open || $('sc-conflict').open; }

  function setScreen(name) {
    const was = S.screen;
    S.screen = SCREENS.includes(name) ? name : 'show';
    for (const s of SCREENS) $('screen-' + s).hidden = s !== S.screen;
    S.sigs = {};
    if (was === 'scripts' && S.screen !== 'scripts') leaseSync();   // the editor closed: give the lease back
    if (was === 'library' && S.screen !== 'library') libLeave();
    if (S.screen === 'library' && was !== 'library') libEnter();
    render();
  }

  function onKey(e) {
    if (e.ctrlKey || e.metaKey || e.altKey || anyDialog()) return;
    const t = e.target;
    if (t && t.matches && t.matches('input,select,textarea,[contenteditable]')) return;
    if (e.key === '?') { $('keys-dlg').showModal(); return; }
    if (findKeyAction(e.key, { inField: false, screen: S.screen }) === 'focus') { e.preventDefault(); findFocus(); return; }   // / goes to Find; fields were turned away above
    if (screenForKey(e.key)) { location.hash = '#' + screenForKey(e.key); return; }
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

  // Trade a token the user just entered for a cookie session. If the server cannot (an older one, the legacy VOLT_TOKEN, no
  // public URL off loopback) the token simply stays in use, as before.
  async function useSession(token) {
    const csrf = await beginSession(fetch, token);
    if (!csrf || getToken() !== token) return;
    SESS.csrf = csrf; whoamiFor = null;
    setToken('');   // the cookie carries the session from here; the page no longer holds the token
    refresh();
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
    $('token-clear').addEventListener('click', async () => {
      const csrf = SESS.csrf;
      SESS.csrf = ''; whoamiFor = null; setToken(''); $('token-in').value = ''; $('token-dlg').close();
      if (csrf) await endSession(fetch, csrf);
      refresh();
    });
    $('token-form').addEventListener('submit', () => {
      const token = $('token-in').value.trim();
      setToken(token); $('token-in').value = ''; setTimeout(refresh, 0);
      if (token) useSession(token);
    });
    $('keys-btn').addEventListener('click', () => $('keys-dlg').showModal());
    setTheme(currentTheme());
    $('theme-btn').addEventListener('click', () => setTheme(nextTheme(currentTheme())));
    libInit();
    scConflictInit();
    presenceInit();
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
    // A reload keeps the cookie but loses the CSRF value, so ask for it before the first read when no token is held.
    (getToken() ? Promise.resolve('') : adoptSession(fetch)).then((csrf) => { SESS.csrf = csrf; }).then(refresh).then(schedule);
  }

  init();
})();
