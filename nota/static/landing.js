/* The landing page's whole behaviour, shared by every language of it.

   One file, included by landing.html and landing.ja.html, so a translation cannot end up drawing a
   different page from the English one - the prose is translated, the machinery is the same object.
*/

// The nav: section links live in a native <details> menu. Wide screens hold it open so the links
// read as a row; narrow ones fold it under its summary. The theme is the one the dashboard stores.
(() => {
  const menu = document.querySelector('body > nav .menu'), wide = matchMedia('(min-width:1101px)');
  const fit = () => { if (menu) menu.open = wide.matches; };
  wide.addEventListener('change', fit); fit();
  menu?.addEventListener('click', e => { if (!wide.matches && e.target.closest('a')) menu.open = false; });
  const theme = document.getElementById('theme'), root = document.documentElement;
  const paint = () => theme?.setAttribute('aria-pressed', String(root.dataset.theme === 'dark'));
  theme?.addEventListener('click', () => {
    root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem('nota.theme', root.dataset.theme); } catch {}
    paint();
  });
  paint();
})();
// The mark is drawn from the receipt itself, not decoration: every tick is a nibble of the
// evidence hash and the arc is the probability the judge stated. Same receipt, same mark.
// Where the receipts come from: this deployment's own API, never a copy pasted into this file.
// A hash typed by hand is fabricated data on a page whose whole argument is that nothing here is
// fabricated - and a daily cycle adds receipts, so any copy would be wrong by morning anyway.
const drawHero = (r) => {
  const HASH = r.hash, P_UP = r.p;
  const svg = document.getElementById('mark'), NS = 'http://www.w3.org/2000/svg';
  const cx = 210, cy = 210, add = (tag, attrs, cls) => {
    const el = document.createElementNS(NS, tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    if (cls) el.setAttribute('class', cls);
    svg.appendChild(el); return el;
  };
  const pt = (r, deg) => [cx + r * Math.cos((deg - 90) * Math.PI / 180), cy + r * Math.sin((deg - 90) * Math.PI / 180)];

  add('circle', {cx, cy, r: 196, 'stroke-width': 1}, 'ring');
  add('circle', {cx, cy, r: 150, 'stroke-width': 1}, 'ring');

  // 64 ticks, one per hex character, length set by its value
  [...HASH].forEach((ch, i) => {
    const v = parseInt(ch, 16), a = i * (360 / HASH.length);
    const [x1, y1] = pt(152, a), [x2, y2] = pt(158 + v * 2.6, a);
    const el = add('line', {x1, y1, x2, y2}, 'tick');
    el.style.animationDelay = `${i * 6}ms`;
  });

  // 16 chords across the field, each pair of bytes choosing where it lands
  for (let i = 0; i < 16; i++) {
    const a = parseInt(HASH.slice(i * 4, i * 4 + 2), 16) / 255 * 360;
    const b = parseInt(HASH.slice(i * 4 + 2, i * 4 + 4), 16) / 255 * 360;
    const [x1, y1] = pt(140, a), [x2, y2] = pt(140, b);
    const el = add('path', {d: `M${x1} ${y1} Q ${cx} ${cy} ${x2} ${y2}`}, 'petal');
    el.style.animationDelay = `${120 + i * 22}ms`;
  }

  // the judge's probability, as the arc it literally is
  const R = 174, sweep = P_UP * 360, [sx, sy] = pt(R, 0), [ex, ey] = pt(R, sweep);
  add('circle', {cx, cy, r: R}, 'arc-track');
  add('path', {d: `M${sx} ${sy} A ${R} ${R} 0 ${sweep > 180 ? 1 : 0} 1 ${ex} ${ey}`}, 'arc');

  add('circle', {cx, cy, r: 96, 'stroke-width': 1}, 'core');
  add('text', {x: cx, y: cy + 6}, 'p').textContent = P_UP.toFixed(2);
  add('text', {x: cx, y: cy + 40}, 'plabel').textContent = svg.dataset.plabel || '7-day rise';

  // The prose around the mark names the receipt the mark is of. Before this it was written into the
  // page, which stopped being true the moment the mark started coming from the API: the caption
  // still said dbd7727f5a25 at 0.68 while the drawing was a different receipt entirely. Each
  // template lives in a data- attribute so a translation supplies its own sentence, never its own
  // facts - %s the id, %p the probability, %a the id as a link to it.
  const fill = (t) => t.replace(/%s/g, r.id).replace(/%p/g, P_UP.toFixed(2));
  const label = svg.getAttribute('data-label');
  if (label) svg.setAttribute('aria-label', fill(label));
  const cap = document.querySelector('.seal figcaption');
  if (cap?.dataset.caption) {
    // Built as nodes, not markup: the id is the API's, and this page is the last place that should
    // paste a value it did not check into the document.
    const link = document.createElement('a');
    link.href = `/r/${encodeURIComponent(r.id)}`;
    link.textContent = r.id;
    const [before, ...after] = fill(cap.dataset.caption).split('%a');
    cap.replaceChildren(document.createTextNode(before), link, document.createTextNode(after.join('%a')));
  }
  const out = document.getElementById('verify-out');
  if (out?.dataset.idle) out.textContent = fill(out.dataset.idle);

  // The slip's rows are the receipt's own fields, written as text: the values are the API's, the
  // row names are the page's, so each language labels the same numbers.
  const field = {id: r.id, symbol: r.symbol, action: r.action, p: `${Math.round(P_UP * 100)}%`, source: r.source, date: r.date,
                 stamp: r.date ? r.date.slice(2).replace(/-/g, '.') : undefined};
  document.querySelectorAll('.slip [data-f]').forEach(dd => {
    dd.textContent = field[dd.dataset.f] ?? '—';
    if (dd.dataset.f === 'action') dd.className = r.action === 'long' ? 'long' : r.action === 'short' ? 'short' : '';
  });
};

// The headline claims a receipt re-runs. The button lets the reader check that claim against the
// deployed API before reading another word, and reports a failure as a failure.
// the same drawing, small, once per receipt in the ledger
const drawLedger = (ledger) => {
  const NS = 'http://www.w3.org/2000/svg', host = document.getElementById('ledger');
  if (!host) return;
  // Every sentence this file would otherwise write in English lives on the element instead, so the
  // Japanese page reads as Japanese without owning a second copy of the drawing code.
  const labelFor = (r) => (host.dataset.markLabel || 'Mark for receipt %s: %y %v, stated probability %p.')
    .replace(/%s/g, r.id).replace(/%y/g, r.symbol).replace(/%v/g, r.action).replace(/%p/g, r.p.toFixed(2));
  for (const r of ledger) {
    const fig = document.createElement('figure');
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 200 200');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', labelFor(r));
    const cx = 100, cy = 100;
    const pt = (rad, deg) => [cx + rad * Math.cos((deg - 90) * Math.PI / 180),
                              cy + rad * Math.sin((deg - 90) * Math.PI / 180)];
    const add = (tag, attrs, cls) => {
      const el = document.createElementNS(NS, tag);
      for (const k in attrs) el.setAttribute(k, attrs[k]);
      if (cls) el.setAttribute('class', cls);
      svg.appendChild(el); return el;
    };
    add('circle', {cx, cy, r: 94, 'stroke-width': 1}, 'ring');
    [...r.hash].forEach((ch, i) => {
      const v = parseInt(ch, 16), a = i * (360 / r.hash.length);
      const [x1, y1] = pt(72, a), [x2, y2] = pt(75 + v * 1.15, a);
      add('line', {x1, y1, x2, y2}, 'tick');
    });
    const R = 84, sweep = r.p * 360, [sx, sy] = pt(R, 0), [ex, ey] = pt(R, sweep);
    add('circle', {cx, cy, r: R, 'stroke-width': 5}, 'arc-track');
    add('path', {d: `M${sx} ${sy} A ${R} ${R} 0 ${sweep > 180 ? 1 : 0} 1 ${ex} ${ey}`,
                 'stroke-width': 5}, 'arc');
    add('circle', {cx, cy, r: 46, 'stroke-width': 1}, 'core');
    const p = add('text', {x: cx, y: cy + 5}, 'p');
    p.setAttribute('style', 'font-size:24px'); p.textContent = r.p.toFixed(2);
    fig.appendChild(svg);
    const cap = document.createElement('figcaption');
    const sym = document.createElement('b');
    sym.textContent = r.symbol;
    const link = document.createElement('a');
    link.href = `/r/${encodeURIComponent(r.id)}`;
    link.textContent = r.id;
    cap.append(sym, document.createTextNode(` ${r.action}`), document.createElement('br'),
               link, document.createElement('br'),
               document.createTextNode((host.dataset.sourceLabel || 'source %r').replace(/%r/g, r.source)));
    fig.appendChild(cap);
    host.appendChild(fig);
  }
};

// The most recent receipt in which something actually failed, rendered as it was stored. Reading
// it from the ledger rather than describing it means this section cannot become a claim about
// behaviour the code no longer has.
const drawBroken = async (summaries) => {
  const panel = document.getElementById('broken-panel'), note = document.getElementById('broken-verdict');
  if (!panel) return;
  // the receipt where the most of RYO itself failed (newest first on a tie), not the newest one where a
  // single cross-check came back partial: that is the day this section is about
  const most = Math.max(0, ...summaries.map(d => d.failed_sections || 0));
  const hit = most ? summaries.find(d => d.failed_sections === most) : summaries.find(d => d.degraded);
  if (!hit) {
    panel.textContent = (panel.dataset.none || 'None of the %n most recent receipts met a failing source.')
      .replace(/%n/g, WINDOW);
    return;
  }
  const res = await fetch(`/api/decisions/${hit.id}`);
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const {receipt} = await res.json();
  // Headers and the sentence below the table are prose, so each page supplies its own; only the
  // receipt's own values travel through. An action or a block reason stays in the words the receipt
  // stored, in every language: those are data, not copy.
  const cells = [[panel.dataset.colSection || 'Evidence section', 'h'], [panel.dataset.colStatus || 'status', 'h']];
  for (const [name, status] of Object.entries(receipt.availability)) {
    cells.push([name, ''], [status, status === 'ok' || status === 'available' ? 'ok' : 'bad']);
  }
  panel.replaceChildren(...cells.map(([text, cls]) => {
    const el = document.createElement('div');
    el.textContent = text;
    if (cls) el.className = cls;
    return el;
  }));
  for (const w of receipt.warnings) {
    const el = document.createElement('div');
    el.className = 'warn';
    el.textContent = w;                       // the source's own words, not a summary of them
    panel.appendChild(el);
  }
  const t = receipt.trade;
  const template = t.kind === 'blocked'
    ? note.dataset.blocked || 'Receipt %s, %d: the judge answered %v at p_up_7d %p, and no position was sized - %r.'
    : note.dataset.sized || 'Receipt %s, %d: the judge answered %v at p_up_7d %p, and sized a practice position on what was left.';
  note.textContent = template
    .replace(/%s/g, receipt.id)
    .replace(/%d/g, receipt.created_at.slice(0, 10))
    .replace(/%v/g, receipt.verdict.action.replace('_', ' '))
    .replace(/%p/g, receipt.verdict.p_up_7d.toFixed(2))
    .replace(/%r/g, t.reason || '');
};

// The audit table: Nota's recomputed ATR, RSI and price beside RYO's, from the newest receipt whose
// evidence holds all three checks. The page carries no figure of its own; the API reads them out of the
// stored evidence pack, with the threshold each skill warns at, and 'Apart' is computed here.
const drawAudit = async (summaries) => {
  const host = document.getElementById('audit'), note = document.getElementById('audit-note');
  if (!host) return;
  const has = d => ['technicals_check', 'price_check', 'deep_analysis']
    .every(k => d.availability && d.availability[k] && !['error', 'unavailable'].includes(d.availability[k]));
  for (const d of summaries.filter(has).slice(0, 5)) {
    const res = await fetch(`/api/decisions/${encodeURIComponent(d.id)}`);
    if (!res.ok) continue;
    const {audit, receipt} = await res.json();
    if (!audit) continue;
    const show = v => String(+v.toFixed(4));
    for (const key of ['atr', 'rsi', 'price']) {
      const {nota, ryo, warn} = audit[key];
      // RSI is already a percentage scale, so its gap is in points; ATR and price are relative to RYO
      const apart = key === 'rsi' ? Math.abs(nota - ryo) : Math.abs(nota - ryo) / Math.abs(ryo) * 100;
      const cell = f => host.querySelector(`[data-a="${key}.${f}"]`);
      cell('nota').textContent = show(nota);
      cell('ryo').textContent = show(ryo);
      cell('apart').textContent = key === 'rsi' ? apart.toFixed(1) + (host.dataset.pts || ' pts') : apart.toFixed(2) + '%';
      cell('apart').className = 'n ' + (Number.isFinite(warn) && apart >= warn ? 'far' : 'agree');
    }
    host.dataset.receipt = receipt.id;
    note.textContent = note.dataset.note.replace(/%s/g, receipt.id).replace(/%d/g, receipt.created_at.slice(0, 10));
    return;
  }
  note.textContent = note.dataset.missing.replace(/%n/g, summaries.length);
};

// The newest receipts, drawn from what the API answers right now. A failure says so rather than
// leaving an empty shape the reader has to interpret.
let verifyId = null;
const WINDOW = 50;   // marks are drawn from the newest few; the failure panel searches the window
fetch(`/api/decisions?limit=${WINDOW}`)
  .then(res => res.ok ? res.json() : Promise.reject(new Error('HTTP ' + res.status)))
  .then(rows => {
    const ledger = rows.slice(0, 5).map(d => ({id: d.id, symbol: d.symbol, p: d.p_up_7d, source: d.source,
                                               action: d.action.replace('_', ' '), hash: d.pack_hash,
                                               date: (d.created_at || '').slice(0, 10)}));
    if (!ledger.length) throw new Error('the ledger is empty');
    verifyId = ledger[0].id;
    drawHero(ledger[0]);
    drawLedger(ledger);
    return Promise.all([drawBroken(rows), drawAudit(rows)]);
  })
  .catch(err => {
    for (const id of ['ledger', 'broken-panel', 'audit-note']) {
      const host = document.getElementById(id);
      if (host) host.textContent = `The ledger could not be read: ${err.message}`;
    }
  });

const out = document.getElementById('verify-out'), btn = document.getElementById('verify');
// `identical: true` is the API's own word for it and stays in English on every page: it is the
// value, not the sentence around it. The sentence comes from the page, as nodes rather than markup.
const verdictLine = (cls, verdict, tail) => {
  const mark = document.createElement('span');
  mark.className = cls;
  mark.textContent = verdict;
  out.replaceChildren(mark, document.createTextNode(' ' + tail));
};

// aria-disabled rather than disabled while it runs: a disabled button drops keyboard focus to the body
btn.addEventListener('click', async () => {
  if (btn.getAttribute('aria-disabled') === 'true') return;
  if (!verifyId) { out.textContent = out.dataset.waiting || 'the ledger has not loaded yet'; return; }
  btn.setAttribute('aria-disabled', 'true'); out.setAttribute('aria-busy', 'true');
  out.textContent = out.dataset.running || 'running…';
  const t0 = performance.now();
  try {
    const r = await fetch(`/api/decisions/${verifyId}/replay`);
    const d = await r.json();
    const ms = Math.round(performance.now() - t0);
    // the second seal is pressed only on the API's own identical: true, and lifted on anything else
    document.querySelector('.slip')?.classList.toggle('stamped', d.identical === true);
    if (d.identical) {
      verdictLine('yes', 'identical: true',
                  (out.dataset.ok || 'rebuilt from the stored evidence in %m ms').replace(/%m/g, ms));
    } else {
      verdictLine('no', 'identical: false',
                  (out.dataset.drift || '%n field(s) differ').replace(/%n/g, d.diff.length));
    }
  } catch (e) {
    document.querySelector('.slip')?.classList.remove('stamped');
    verdictLine('no', out.dataset.unreachable || 'could not reach the API', e.message);
  } finally { btn.removeAttribute('aria-disabled'); out.removeAttribute('aria-busy'); }
});

// The scorecard line: RYO's own plans, locked and settled. Numbers from /api/scorecard, words from the
// page, so both languages say the same thing about the same ledger.
(async () => {
  const el = document.getElementById('scorecard-line');
  if (!el) return;
  try {
    const s = await (await fetch('/api/scorecard')).json();
    const [hit, k] = s.target_first;
    // only a window still ahead is announced as upcoming; a closed one waits for the hourly cycle to record it
    const next = s.open.find(r => !r.overdue && new Date(r.settles_at) > Date.now());
    const when = next && new Date(next.settles_at).toLocaleString(document.documentElement.lang,
      {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC'}) + ' UTC';
    const line = s.lock_days === 1 ? el.dataset.lineOne : el.dataset.lineMany;
    el.textContent = line.replace(/%n/g, s.open.length + s.settled.length).replace(/%d/g, s.lock_days)
      .replace(/%c/g, s.contradictions) + ' ' + (k ? el.dataset.settled.replace(/%t/g, hit).replace(/%k/g, k)
      : next ? el.dataset.none.replace(/%w/g, when) : el.dataset.closed);
  } catch (e) {
    el.textContent = el.dataset.error.replace(/%e/g, e.message);
  }
})();
