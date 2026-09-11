/* The landing page's whole behaviour, shared by every language of it.

   One file, included by landing.html and landing.ja.html, so a translation cannot end up drawing a
   different page from the English one - the prose is translated, the machinery is the same object.
*/
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
  add('text', {x: cx, y: cy + 30}, 'plabel').textContent = 'p_up_7d';

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
};

// The headline claims a receipt re-runs. The button lets the reader check that claim against the
// deployed API before reading another word, and reports a failure as a failure.
// the same drawing, small, once per receipt in the ledger
const drawLedger = (ledger) => {
  const NS = 'http://www.w3.org/2000/svg', host = document.getElementById('ledger');
  if (!host) return;
  for (const r of ledger) {
    const fig = document.createElement('figure');
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 200 200');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label',
      `Mark for receipt ${r.id}: ${r.symbol} ${r.action}, stated probability ${r.p.toFixed(2)}.`);
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
    cap.innerHTML = `<b>${r.symbol}</b> ${r.action}<br>` +
      `<a href="/r/${r.id}">${r.id}</a><br>source ${r.source}`;
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
  const hit = summaries.find(d => d.degraded);
  if (!hit) { panel.textContent = `None of the ${WINDOW} most recent receipts met a failing source.`; return; }
  const res = await fetch(`/api/decisions/${hit.id}`);
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const {receipt} = await res.json();
  const cells = [['Evidence section', 'h'], ['status', 'h']];
  for (const [name, status] of Object.entries(receipt.availability)) {
    cells.push([name, ''], [status, status === 'ok' ? 'ok' : 'bad']);
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
  note.textContent = `Receipt ${receipt.id}, ${receipt.created_at.slice(0, 10)}: the judge answered `
    + `${receipt.verdict.action.replace('_', ' ')} at p_up_7d ${receipt.verdict.p_up_7d.toFixed(2)}, and `
    + (t.kind === 'blocked' ? `no position was sized - ${t.reason}.` : 'sized a practice position on what was left.');
};

// The newest receipts, drawn from what the API answers right now. A failure says so rather than
// leaving an empty shape the reader has to interpret.
let verifyId = null;
const WINDOW = 50;   // marks are drawn from the newest few; the failure panel searches the window
fetch(`/api/decisions?limit=${WINDOW}`)
  .then(res => res.ok ? res.json() : Promise.reject(new Error('HTTP ' + res.status)))
  .then(rows => {
    const ledger = rows.slice(0, 5).map(d => ({id: d.id, symbol: d.symbol, p: d.p_up_7d, source: d.source,
                                               action: d.action.replace('_', ' '), hash: d.pack_hash}));
    if (!ledger.length) throw new Error('the ledger is empty');
    verifyId = ledger[0].id;
    drawHero(ledger[0]);
    drawLedger(ledger);
    return drawBroken(rows);
  })
  .catch(err => {
    for (const id of ['ledger', 'broken-panel']) {
      const host = document.getElementById(id);
      if (host) host.textContent = `The ledger could not be read: ${err.message}`;
    }
  });

const out = document.getElementById('verify-out'), btn = document.getElementById('verify');
btn.addEventListener('click', async () => {
  if (!verifyId) { out.textContent = 'the ledger has not loaded yet'; return; }
  btn.disabled = true; out.textContent = 'running…';
  const t0 = performance.now();
  try {
    const r = await fetch(`/api/decisions/${verifyId}/replay`);
    const d = await r.json();
    const ms = Math.round(performance.now() - t0);
    out.innerHTML = d.identical
      ? `<span class="yes">identical: true</span> rebuilt from the stored evidence in ${ms} ms`
      : `<span class="no">identical: false</span> ${d.diff.length} field(s) differ`;
  } catch (e) {
    out.innerHTML = `<span class="no">could not reach the API</span> ${e.message}`;
  } finally { btn.disabled = false; }
});
