(() => {
  'use strict';
  // The prompter page. It reads one show's feed (GET /api/shows/{id}/prompter) with the ticket in its own address, and shows what the
  // talent reads. Script text only ever becomes text nodes (textContent): nothing here is parsed as markup.
  const POLL_MS = 2000;

  // The feed as a flat list of lines to draw: pure, so a test can run it under node. `text` is passed through untouched.
  function lines(feed) {
    const out = [];
    const items = feed && Array.isArray(feed.items) ? feed.items : [];
    for (const it of items) {
      const cur = feed.current === it.id;
      out.push({ kind: 'slug', text: String(it.slug), current: cur, item: it.id });
      let para = null;
      for (const b of Array.isArray(it.blocks) ? it.blocks : []) {
        if (b.kind !== 'read' && b.kind !== 'instruction') continue;   // a note never arrives, and one that did would be dropped here too
        out.push({ kind: b.kind, text: String(b.text), para: b.para, newPara: para !== b.para, item: it.id });
        para = b.para;
      }
    }
    return out;
  }
  // The page's own address: ?show=<id>&ticket=<ticket>. Anything else is ignored.
  function params(search) {
    const q = new URLSearchParams(search || '');
    return { show: q.get('show') || '', ticket: q.get('ticket') || '' };
  }
  function clamp(n, lo, hi, dflt) {
    const v = Number(n);
    return Number.isFinite(v) ? Math.min(hi, Math.max(lo, Math.round(v))) : dflt;
  }

  if (typeof document === 'undefined') {
    if (typeof module !== 'undefined') module.exports = { lines, params, clamp };
    return;
  }

  const $ = (id) => document.getElementById(id);
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  const P = params(location.search);
  const store = {
    get(k, d) { try { const v = localStorage.getItem('volt.prompter.' + k); return v == null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem('volt.prompter.' + k, String(v)); } catch (e) { /* works without it */ } },
  };
  const S = { version: null, running: false, last: 0, feed: null, y: 0 };

  function status(text, bad) {
    const n = $('p-status');
    if (n.textContent !== text) n.textContent = text;
    n.classList.toggle('bad', !!bad);
  }

  function draw(feed) {
    const host = $('p-text');
    const nodes = [];
    let box = null, para = null;
    for (const l of lines(feed)) {
      if (l.kind === 'slug') {
        box = el('section', 'pitem' + (l.current ? ' cur' : ''));
        box.setAttribute('data-item', l.item);
        const h = el('h2', 'pslug', l.text);
        if (l.current) h.append(el('span', 'now', 'On air'));
        box.append(h);
        nodes.push(box); para = null;
      } else if (l.kind === 'instruction') {
        box.append(el('p', 'pins', l.text));
      } else {
        if (l.newPara || !para) { para = el('p', 'ppara'); box.append(para); }
        para.append(el('span', 'pline', l.text), document.createElement('br'));
      }
    }
    if (!nodes.length) nodes.push(el('p', 'pempty', 'Nothing to read yet. Approved stories appear here in rundown order.'));
    host.replaceChildren(...nodes);
  }

  async function poll() {
    try {
      if (!P.show) { status('This page needs a link from the producer (it names the show).', true); return; }
      const q = P.ticket ? '?ticket=' + encodeURIComponent(P.ticket) : '';
      const res = await fetch('/api/shows/' + encodeURIComponent(P.show) + '/prompter' + q, { cache: 'no-store' });
      if (res.status === 401 || res.status === 403) { status('This prompter link has expired or is not valid. Ask the producer for a new one.', true); return; }
      if (res.status === 404) { status('This show no longer exists.', true); return; }
      if (!res.ok) throw new Error('feed ' + res.status);
      const feed = await res.json();
      status('Live. Updated ' + new Date().toLocaleTimeString([], { hour12: false }), false);
      if (feed.version !== S.version) {      // repaint only when the content changed, so scrolling is never disturbed
        S.version = feed.version; S.feed = feed;
        const keep = window.scrollY;
        draw(feed);
        window.scrollTo(0, keep);
        $('p-show').textContent = feed.items.length + (feed.items.length === 1 ? ' story' : ' stories');
      }
    } catch (e) { status('No answer from the server. Showing the last text.', true); }
  }

  function jump() {
    const n = document.querySelector('.pitem.cur');
    if (n) n.scrollIntoView({ block: 'start' });
  }
  function applySize() {
    const v = clamp($('p-size').value, 2, 9, 4);
    $('p-text').style.setProperty('--psize', String(v));
    store.set('size', v);
  }
  function setRunning(on) {
    S.running = on; S.last = 0;
    const b = $('p-go');
    b.textContent = on ? 'Stop scrolling' : 'Start scrolling';
    b.setAttribute('aria-pressed', String(on));
    if (on) requestAnimationFrame(tick);
  }
  // Scrolling holds still until started; speed is pixels per second, 1 to 20 steps of 12.
  function tick(now) {
    if (!S.running) return;
    if (S.last) {
      S.y += (clamp($('p-speed').value, 1, 20, 6) * 12) * (now - S.last) / 1000;
      const whole = Math.floor(S.y);
      if (whole) { window.scrollBy(0, whole); S.y -= whole; }
    }
    S.last = now;
    requestAnimationFrame(tick);
  }

  $('p-size').value = String(clamp(store.get('size', 4), 2, 9, 4));
  $('p-speed').value = String(clamp(store.get('speed', 6), 1, 20, 6));
  applySize();
  const mirror = (on) => { $('p-text').classList.toggle('mirror', on); $('p-mirror').setAttribute('aria-pressed', String(on)); store.set('mirror', on ? '1' : '0'); };
  mirror(store.get('mirror', '0') === '1');
  $('p-size').addEventListener('input', applySize);
  $('p-speed').addEventListener('input', () => store.set('speed', clamp($('p-speed').value, 1, 20, 6)));
  $('p-go').addEventListener('click', () => setRunning(!S.running));
  $('p-mirror').addEventListener('click', () => mirror(!$('p-text').classList.contains('mirror')));
  $('p-jump').addEventListener('click', jump);
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && t.matches && t.matches('input,button')) return;
    if (e.key === ' ') { e.preventDefault(); setRunning(!S.running); }
  });
  poll();
  setInterval(poll, POLL_MS);
})();
