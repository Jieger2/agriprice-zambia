/* AgriPrice Zambia web client - vanilla JS, no framework. Built for 2G:
   ETag/304 revalidation, delta sync of prices, last-known data shown offline, and an
   outbox that retries writes with Idempotency-Key headers so a retry never duplicates.

   Pages: Prices (public) · Trends (public) · Buy (buyers) · Officer (officers) · Admin (admins).
   Sign-in: Cognito hosted UI (code + PKCE) when deployed; a role picker when
   window.AGRI.mockAuth is set by the local emulator. */
(() => {
  'use strict';
  const CFG = window.AGRI || {};
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const store = {
    get(k, d = null) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
    del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
  };
  const toast = (msg) => { const t = $('#toast'); t.textContent = msg; t.hidden = false; clearTimeout(toast.h); toast.h = setTimeout(() => (t.hidden = true), 3800); };
  const kwacha = (ngweePerBag) => (ngweePerBag / 100).toLocaleString('en-ZM', { maximumFractionDigits: 0 });
  const bagK = (ngweePerKg) => Math.round((ngweePerKg * 50) / 100);
  const perKg = (kPerBag) => Math.round((Number(kPerBag) * 100) / 50);
  const dayToDate = (d) => new Date(d * 86400000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  const pct = (t) => (t ? (t > 0 ? '+' : '') + (t / 10).toFixed(1) + '%' : '=');
  const uid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
  let REF = { m: [], c: [], p: [] };
  const market = (code) => REF.m.find((m) => m[0] === code);
  const marketName = (code) => (market(code) || [code, code])[1];
  const cropName = (code) => (REF.c.find((c) => c[0] === code) || [code, code])[1];

  // ---------------------------------------------------------------- auth
  const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const b64urlText = (s) => btoa(unescape(encodeURIComponent(s))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const claims = () => {
    const t = store.get('id_token');
    if (!t || store.get('id_exp', 0) < Date.now()) return null;
    try { return JSON.parse(decodeURIComponent(escape(atob(t.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))))); } catch { return null; }
  };
  const groups = () => { const c = claims(); if (!c) return []; const g = c['cognito:groups'] || []; return Array.isArray(g) ? g : String(g).split(/[ ,]+/).filter(Boolean); };
  const redirectUri = () => location.origin + '/';
  async function login() {
    if (CFG.mockAuth) { $('#mock-login').showModal(); return; }
    if (!CFG.cognitoDomain) return toast('Sign-in is not configured in this environment');
    const verifier = b64url(crypto.getRandomValues(new Uint8Array(48)));
    sessionStorage.setItem('pkce', verifier);
    const challenge = b64url(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
    const q = new URLSearchParams({ response_type: 'code', client_id: CFG.clientId, redirect_uri: redirectUri(), scope: 'openid email phone profile', code_challenge_method: 'S256', code_challenge: challenge });
    location.assign(`https://${CFG.cognitoDomain}/oauth2/authorize?${q}`);
  }
  function mockLogin(e) {
    const form = $('#mock-login-form');
    if (e.submitter && e.submitter.value === 'cancel') return;
    const f = new FormData(form);
    const email = String(f.get('email') || 'demo@agriprice.local');
    const payload = { sub: 'local-' + email.replace(/[^a-z0-9]/gi, ''), email, phone_number: String(f.get('phone') || ''), 'cognito:groups': [f.get('role')], 'cognito:username': email, exp: Math.floor(Date.now() / 1000) + 8 * 3600 };
    // Unsigned token: accepted ONLY by the local emulator (scripts/local_server.py). API Gateway rejects it.
    store.set('id_token', `${b64urlText('{"alg":"none","typ":"JWT"}')}.${b64urlText(JSON.stringify(payload))}.local`);
    store.set('id_exp', Date.now() + 8 * 3600 * 1000);
    renderAuth(); toast('Signed in as ' + f.get('role'));
  }
  async function finishLogin() {
    const code = new URLSearchParams(location.search).get('code');
    if (!code) return;
    history.replaceState(null, '', '/');
    const body = new URLSearchParams({ grant_type: 'authorization_code', client_id: CFG.clientId, code, redirect_uri: redirectUri(), code_verifier: sessionStorage.getItem('pkce') || '' });
    const r = await fetch(`https://${CFG.cognitoDomain}/oauth2/token`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body });
    if (!r.ok) return toast('Sign-in failed');
    const tok = await r.json();
    store.set('id_token', tok.id_token);
    store.set('id_exp', Date.now() + (tok.expires_in - 60) * 1000);
  }
  function logout() {
    store.del('id_token'); store.del('id_exp');
    if (CFG.mockAuth || !CFG.cognitoDomain) { renderAuth(); selectTab('prices'); return; }
    location.assign(`https://${CFG.cognitoDomain}/logout?${new URLSearchParams({ client_id: CFG.clientId, logout_uri: redirectUri() })}`);
  }

  // ---------------------------------------------------------------- network
  const authHeader = () => { const t = store.get('id_token'); return t && claims() ? { Authorization: t } : {}; };
  async function getJSON(path, { cache = true } = {}) {
    const cacheKey = 'GET ' + path;
    const cached = cache ? store.get(cacheKey) : null;
    const headers = { ...authHeader() };
    if (cached && cached.etag) headers['If-None-Match'] = cached.etag;
    try {
      const r = await fetch(path, { headers });
      if (r.status === 304 && cached) return { data: cached.data, at: cached.at, fresh: true };
      const data = r.status === 204 ? null : await r.json().catch(() => null);
      if (!r.ok) throw Object.assign(new Error((data && data.message) || `HTTP ${r.status}`), { status: r.status });
      if (cache) store.set(cacheKey, { etag: r.headers.get('ETag'), data, at: Date.now() });
      return { data, at: Date.now(), fresh: true };
    } catch (e) {
      if (cached && !e.status) return { data: cached.data, at: cached.at, fresh: false };
      throw e;
    }
  }
  async function send(method, path, body, key = uid()) {
    const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key, ...authHeader() }, body: JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(data.message || `HTTP ${r.status}`), { status: r.status });
    return data;
  }

  // Outbox: writes made offline are queued and replayed with the SAME idempotency key.
  const outbox = { all: () => store.get('outbox', []), save(q) { store.set('outbox', q); $('#outbox').textContent = q.length; } };
  async function submit(path, body, okMsg) {
    const item = { path, body, key: uid() };
    try { const res = await send('POST', item.path, item.body, item.key); toast(okMsg); return res || true; } catch (e) {
      if (e.status) { toast(e.message); return false; }
      const q = outbox.all(); q.push(item); outbox.save(q); toast('Offline - saved, will send automatically'); return true;
    }
  }
  async function flush() {
    let q = outbox.all();
    for (const item of [...q]) {
      try { await send('POST', item.path, item.body, item.key); q = q.filter((x) => x.key !== item.key); } catch (e) { if (e.status) q = q.filter((x) => x.key !== item.key); else break; }
    }
    outbox.save(q);
  }

  // ---------------------------------------------------------------- reference data
  async function loadRef() {
    const { data } = await getJSON('/api/markets');
    REF = data;
    fillSelects();
  }
  function fillSelects() {
    const opt = (v, t) => `<option value="${esc(v)}">${esc(t)}</option>`;
    const keep = (s, html) => { const v = s.value; s.innerHTML = html; if (v && [...s.options].some((o) => o.value === v)) s.value = v; };
    $$('select.crops').forEach((s) => keep(s, REF.c.map(([c, n]) => opt(c, n)).join('')));
    $$('select.markets').forEach((s) => keep(s, REF.m.map(([c, n]) => opt(c, n)).join('')));
    $$('select.markets-any').forEach((s) => keep(s, opt('', 'All markets') + REF.m.map(([c, n]) => opt(c, n)).join('')));
    $$('select.provinces').forEach((s) => keep(s, REF.p.map(([c, n]) => opt(c, n)).join('')));
    $$('select.provinces-zm').forEach((s) => keep(s, opt('ZM', 'All Zambia') + REF.p.map(([c, n]) => opt(c, n)).join('')));
  }

  // ---------------------------------------------------------------- Prices (delta sync)
  async function showPrices(e) {
    if (e) e.preventDefault();
    const f = new FormData($('#price-form'));
    const m = market(f.get('near'));
    const q = new URLSearchParams({ c: f.get('c') || 'MZE' });
    if (m) { q.set('lat', m[3]); q.set('lon', m[4]); }
    const key = 'prices ' + q;
    const saved = store.get(key, { rows: [], at: 0 });
    const since = saved.rows.reduce((mx, r) => Math.max(mx, r.d), 0);
    try {
      // Ask only for records newer than what we already hold; an empty array costs ~2 bytes on 2G.
      const { data, fresh } = await getJSON('/api/prices?' + q + (since ? `&since=${since}` : ''), { cache: false });
      const merged = new Map(saved.rows.map((r) => [r.m, r]));
      data.forEach((r) => merged.set(r.m, r));
      const order = new Map(data.concat(saved.rows).map((r, i) => [r.m, i]));
      saved.rows = [...merged.values()].sort((a, b) => (order.get(a.m) ?? 0) - (order.get(b.m) ?? 0));
      saved.at = Date.now();
      store.set(key, saved);
      renderPrices(saved.rows, fresh ? `Updated ${new Date().toLocaleTimeString()} · ${data.length} new record(s)` : '');
    } catch (err) {
      if (saved.rows.length) renderPrices(saved.rows, `Offline - showing prices saved ${new Date(saved.at).toLocaleString()}`);
      else $('#price-meta').textContent = 'Could not load prices: ' + err.message;
    }
  }
  function renderPrices(rows, meta) {
    $('#price-table tbody').innerHTML = rows.length ? rows.map((r) => {
      const cls = r.t > 0 ? 'up' : r.t < 0 ? 'down' : '';
      return `<tr><td>${esc(marketName(r.m))}</td><td>${kwacha(r.p)}</td><td class="${cls}">${pct(r.t)}</td><td>${dayToDate(r.d)}</td></tr>`;
    }).join('') : '<tr><td colspan="4" class="muted">No prices yet</td></tr>';
    $('#price-meta').textContent = meta;
  }

  // ---------------------------------------------------------------- Trends (hand-drawn SVG: no chart library on 2G)
  async function showTrends(e) {
    if (e) e.preventDefault();
    const f = new FormData($('#trend-form'));
    try {
      const { data } = await getJSON(`/analytics/series?c=${encodeURIComponent(f.get('c'))}&r=${encodeURIComponent(f.get('r'))}`);
      drawChart(data); drawSeason(data.si || []); drawWeekly(data);
    } catch (err) {
      $('#chart').innerHTML = `<p class="muted">${err.status === 404 ? 'No history yet for this selection.' : 'Could not load trends.'}</p>`;
      $('#season').innerHTML = ''; $('#weekly').innerHTML = ''; $('#wx').textContent = '';
    }
  }
  function drawChart(d) {
    const s = d.s || [], fc = d.f || [];
    if (!s.length) { $('#chart').innerHTML = '<p class="muted">No data</p>'; return; }
    const W = 640, H = 260, P = 36, all = [...s.map((r) => r[1]), ...s.map((r) => r[3]), ...s.map((r) => r[2]), ...fc.map((r) => r[1])];
    const lo = Math.min(...all) * 0.95, hi = Math.max(...all) * 1.05, n = s.length + fc.length;
    const x = (i) => P + (i * (W - 2 * P)) / Math.max(n - 1, 1), y = (v) => H - P - ((v - lo) * (H - 2 * P)) / (hi - lo || 1);
    const band = s.map((r, i) => `${x(i)},${y(r[3])}`).join(' ') + ' ' + s.map((r, i) => `${x(i)},${y(r[2])}`).reverse().join(' ');
    const line = s.map((r, i) => `${x(i)},${y(r[1])}`).join(' ');
    const fline = fc.length ? [`${x(s.length - 1)},${y(s[s.length - 1][1])}`, ...fc.map((r, i) => `${x(s.length + i)},${y(r[1])}`)].join(' ') : '';
    const ticks = [lo, (lo + hi) / 2, hi].map((v) => `<text x="4" y="${y(v) + 4}" font-size="11" fill="currentColor">K${bagK(v)}</text><line x1="${P}" x2="${W - P}" y1="${y(v)}" y2="${y(v)}" stroke="currentColor" stroke-opacity=".12"/>`).join('');
    const step = Math.ceil(n / 8);
    const labels = [...s.map((r) => r[0]), ...fc.map((r) => r[0])].map((ym, i) => (i % step === 0 ? `<text x="${x(i)}" y="${H - 10}" font-size="10" text-anchor="middle" fill="currentColor">${ym.slice(4)}/${ym.slice(2, 4)}</text>` : '')).join('');
    $('#chart').innerHTML = `<svg viewBox="0 0 ${W} ${H}" style="color:var(--muted)">${ticks}
      <polygon points="${band}" fill="var(--accent2)" fill-opacity=".18"/>
      <polyline points="${line}" fill="none" stroke="var(--accent)" stroke-width="2.5"/>
      ${fline ? `<polyline points="${fline}" fill="none" stroke="var(--accent2)" stroke-width="2.5" stroke-dasharray="6 5"/>` : ''}
      ${labels}<text x="${W - P}" y="16" font-size="11" text-anchor="end" fill="currentColor">K per 50 kg · band = min-max · dashed = forecast</text></svg>`;
  }
  function drawSeason(si) {
    const months = 'JFMAMJJASOND', max = Math.max(...si, 1);
    $('#season').innerHTML = si.map((v, i) => `<div style="height:${(v / max) * 100}%" title="${v}"><span>${months[i]}</span></div>`).join('');
  }
  function drawWeekly(d) {
    const w = d.w || [];
    $('#weekly').innerHTML = w.length ? w.map(([day, v]) => `<div class="card"><small>${new Date(day).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })}</small><b>K${bagK(v)}</b></div>`).join('') : '<p class="muted small">Not enough history for a forecast.</p>';
    const wx = d.wx || {}, adj = parseFloat(wx.adj || '1'), ratio = wx.ratio == null ? null : parseFloat(wx.ratio);
    $('#wx').textContent = ratio == null ? 'Weather adjustment: none (no rainfall signal or outside the growing season).'
      : `Weather: forecast rainfall at ${Math.round(ratio * 100)}% of normal · forecast ${adj > 1 ? 'raised' : adj < 1 ? 'lowered' : 'unchanged'}${adj !== 1 ? ' by ' + Math.abs(Math.round((adj - 1) * 1000) / 10) + '%' : ''} (Open-Meteo, illustrative heuristic).`;
  }

  // ---------------------------------------------------------------- Buy (buyers)
  async function postDemand(e) {
    e.preventDefault();
    const f = new FormData(e.target), m = market(f.get('m'));
    if (await submit('/api/demand', { c: f.get('c'), q: Number(f.get('q')), p: perKg(f.get('pk')), lat: m[3], lon: m[4], r: Number(f.get('r')) }, 'Demand posted - matching sellers are notified by SMS')) {
      e.target.reset(); setTimeout(loadMine, 1500);
    }
  }
  async function findSupply(e) {
    e.preventDefault();
    const f = new FormData(e.target), m = market(f.get('m'));
    try {
      const { data } = await getJSON(`/api/listings?c=${f.get('c')}&lat=${m[3]}&lon=${m[4]}&r=${f.get('r')}`, { cache: false });
      $('#supply-table').hidden = false;
      $('#supply-table tbody').innerHTML = data.length ? data.map((r) => `<tr><td>${r.q.toLocaleString()} kg</td><td>${esc(marketName(r.m))}</td><td>${r.km}</td><td>${esc(r.f || '')}</td><td>${esc(r.ph)}</td></tr>`).join('')
        : '<tr><td colspan="5" class="muted">No listings within that radius. Post a demand and you will be notified.</td></tr>';
    } catch (err) { toast(err.message); }
  }
  async function loadMine() {
    if (!(groups().includes('buyers') || groups().includes('admins'))) return;
    try {
      const { data } = await getJSON('/api/demand/mine', { cache: false });
      $('#mine').innerHTML = data.length ? data.map((d) => `<details class="demand" data-id="${esc(d.id)}"><summary><b>${d.q.toLocaleString()} kg ${esc(cropName(d.c))}</b> · up to K${bagK(d.p)}/50kg · ${d.r} km · <span class="pill dark">${d.n} match${d.n === 1 ? '' : 'es'}</span></summary><div class="matches muted small">Loading...</div></details>`).join('')
        : '<p class="muted">No demand posted yet.</p>';
      $$('#mine details').forEach((el) => el.addEventListener('toggle', () => el.open && loadMatches(el)));
    } catch { /* offline */ }
  }
  async function loadMatches(el) {
    try {
      const { data } = await getJSON(`/api/demand/${encodeURIComponent(el.dataset.id)}/matches`, { cache: false });
      $('.matches', el).innerHTML = data.length ? `<table class="grid small"><thead><tr><th>Qty</th><th>Market</th><th>km</th><th>From</th><th>Call</th></tr></thead><tbody>${data.map((m) => `<tr><td>${m.q.toLocaleString()} kg</td><td>${esc(marketName(m.m))}</td><td>${m.km}</td><td>${esc(m.f)}</td><td><a href="tel:${esc(m.ph)}">${esc(m.ph)}</a></td></tr>`).join('')}</tbody></table>` : 'No sellers matched yet.';
    } catch (err) { $('.matches', el).textContent = err.message; }
  }

  // ---------------------------------------------------------------- Officer
  async function showBoard(e) {
    if (e) e.preventDefault();
    const r = new FormData($('#board-form')).get('r');
    try {
      const { data } = await getJSON(`/api/officer/board?r=${encodeURIComponent(r)}`, { cache: false });
      const head = `<tr><th>Crop</th>${data.m.map((c) => `<th>${esc(marketName(c))}</th>`).join('')}</tr>`;
      const rows = data.rows.map((row) => `<tr><td>${esc(cropName(row.c))}</td>${data.m.map((c) => { const p = row.p[c]; return p ? `<td title="${dayToDate(p.d)}">${kwacha(p.p)} <small class="${p.t > 0 ? 'up' : p.t < 0 ? 'down' : 'muted'}">${pct(p.t)}</small></td>` : '<td class="muted">-</td>'; }).join('')}</tr>`).join('');
      $('#board').innerHTML = `<table class="grid small"><thead>${head}</thead><tbody>${rows}</tbody></table><p class="muted small">K per 50 kg; hover a price for its date.</p>`;
    } catch (err) { $('#board').innerHTML = `<p class="muted">${esc(err.message)}</p>`; }
  }
  const formJSON = (form, extra = {}) => ({ ...Object.fromEntries(new FormData(form)), ...extra });
  async function registerFarmer(e) { e.preventDefault(); if (await submit('/api/farmers', formJSON(e.target), 'Farmer registered')) e.target.reset(); }
  async function listCrop(e) {
    e.preventDefault(); const b = formJSON(e.target); b.q = Number(b.q); if (!b.from) delete b.from;
    if (await submit('/api/listings', b, 'Listed - buyers nearby will be matched')) e.target.reset();
  }
  async function fieldPrice(e) {
    e.preventDefault(); const b = formJSON(e.target); const body = { c: b.c, m: b.m, p: perKg(b.pk) }; if (b.d) body.d = b.d;
    if (await submit('/api/field-prices', body, 'Price report queued for the next ingestion run')) e.target.reset();
  }

  // ---------------------------------------------------------------- Admin (FR10)
  let ADM = { doc: { markets: [], commodities: [], units: {} }, markets: [], commodities: [], provinces: [], sources: null };
  const upsert = (arr, row) => { const i = arr.findIndex((r) => r.code === row.code); if (i >= 0) arr[i] = row; else arr.push(row); };
  async function loadCatalog() {
    const { data } = await getJSON('/api/admin/catalog', { cache: false });
    ADM = { ...ADM, ...data };
    ADM.doc.markets = ADM.doc.markets || []; ADM.doc.commodities = ADM.doc.commodities || []; ADM.doc.units = ADM.doc.units || {};
    renderCatalog();
  }
  function renderCatalog() {
    const prov = (c) => (ADM.provinces.find((p) => p.code === c) || { name: c }).name;
    $('#adm-market-rows').innerHTML = ADM.markets.map((m) => `<tr><td>${esc(m.code)}</td><td>${esc(m.name)}</td><td>${esc(prov(m.province))}</td><td>${m.lat.toFixed(3)}, ${m.lon.toFixed(3)}</td><td><button class="ghost" data-edit-market="${esc(m.code)}">Edit</button> <button class="ghost danger" data-disable-market="${esc(m.code)}">Disable</button></td></tr>`).join('');
    const offM = ADM.doc.markets.filter((m) => m.disabled);
    $('#adm-disabled-markets').innerHTML = offM.length ? 'Disabled: ' + offM.map((m) => `${esc(m.code)} <button class="ghost" data-enable-market="${esc(m.code)}">re-enable</button>`).join(' · ') : '';
    $('#adm-crop-rows').innerHTML = ADM.commodities.map((c) => `<tr><td>${esc(c.code)}</td><td>${esc(c.name)}</td><td>${esc(c.short)}</td><td>${esc((c.match || []).join(', '))}</td><td><button class="ghost danger" data-disable-crop="${esc(c.code)}">Disable</button></td></tr>`).join('');
    const offC = ADM.doc.commodities.filter((c) => c.disabled);
    $('#adm-disabled-crops').innerHTML = offC.length ? 'Disabled: ' + offC.map((c) => `${esc(c.code)} <button class="ghost" data-enable-crop="${esc(c.code)}">re-enable</button>`).join(' · ') : '';
    const units = Object.entries(ADM.doc.units);
    $('#adm-unit-rows').innerHTML = units.length ? units.map(([l, kg]) => `<li>${esc(l)} = ${kg} kg <button class="ghost danger" data-del-unit="${esc(l)}">remove</button></li>`).join('') : '<li class="muted">No extra units yet (kg and "N KG" are built in).</li>';
  }
  async function saveCatalog(msg) {
    try { await send('PUT', '/api/admin/catalog', ADM.doc); toast(msg); await loadCatalog(); store.del('GET /api/markets'); await loadRef(); } catch (err) { toast(err.message); await loadCatalog(); }
  }
  function onCatalogClick(e) {
    const b = e.target.closest('button'); if (!b) return;
    const d = b.dataset;
    if (d.editMarket) {
      const m = ADM.markets.find((x) => x.code === d.editMarket); const f = $('#adm-market-form');
      Object.entries(m).forEach(([k, v]) => { if (f.elements[k]) f.elements[k].value = v; }); f.scrollIntoView({ behavior: 'smooth' });
    } else if (d.disableMarket) {
      ADM.doc.markets = ADM.doc.markets.filter((x) => x.code !== d.disableMarket); ADM.doc.markets.push({ code: d.disableMarket, disabled: true });
      saveCatalog(`Market ${d.disableMarket} disabled`);
    } else if (d.enableMarket) {
      ADM.doc.markets = ADM.doc.markets.filter((x) => x.code !== d.enableMarket); saveCatalog(`Market ${d.enableMarket} re-enabled`);
    } else if (d.disableCrop) {
      ADM.doc.commodities = ADM.doc.commodities.filter((x) => x.code !== d.disableCrop); ADM.doc.commodities.push({ code: d.disableCrop, disabled: true });
      saveCatalog(`Crop ${d.disableCrop} disabled`);
    } else if (d.enableCrop) {
      ADM.doc.commodities = ADM.doc.commodities.filter((x) => x.code !== d.enableCrop); saveCatalog(`Crop ${d.enableCrop} re-enabled`);
    } else if (d.delUnit) {
      delete ADM.doc.units[d.delUnit]; saveCatalog('Unit removed');
    }
  }
  function saveMarket(e) {
    e.preventDefault(); const f = formJSON(e.target);
    upsert(ADM.doc.markets, { code: f.code.toUpperCase(), name: f.name, province: f.province, lat: Number(f.lat), lon: Number(f.lon) });
    saveCatalog(`Market ${f.code.toUpperCase()} saved`); e.target.reset();
  }
  function saveCrop(e) {
    e.preventDefault(); const f = formJSON(e.target);
    const match = String(f.match || f.name).split(',').map((s) => s.trim().toLowerCase()).filter(Boolean);
    upsert(ADM.doc.commodities, { code: f.code.toUpperCase(), name: f.name, short: f.short || f.name.slice(0, 7), match });
    saveCatalog(`Crop ${f.code.toUpperCase()} saved`); e.target.reset();
  }
  function saveUnit(e) { e.preventDefault(); const f = formJSON(e.target); ADM.doc.units[f.label.trim().toLowerCase()] = Number(f.kg); saveCatalog('Unit saved'); e.target.reset(); }

  async function loadSources() {
    const { data } = await getJSON('/api/admin/sources', { cache: false });
    ADM.sources = data; ADM.sources.doc.sources = ADM.sources.doc.sources || [];
    const active = new Set(data.effective.map((s) => s.id));
    const all = new Map(); data.builtin.forEach((s) => all.set(s.id, { ...s, builtin: true })); data.doc.sources.forEach((s) => all.set(s.id, { ...(all.get(s.id) || {}), ...s }));
    $('#adm-source-rows').innerHTML = [...all.values()].map((s) => `<tr><td>${esc(s.id)}${s.builtin ? ' <small class="muted">built-in</small>' : ''}</td><td>${esc(s.type || '')}</td><td class="wrap">${esc(s.url || s.key || s.prefix || '')}</td><td>${active.has(s.id) ? '<span class="pill dark">enabled</span>' : '<span class="muted">disabled</span>'}</td><td><button class="ghost" data-toggle-source="${esc(s.id)}">${active.has(s.id) ? 'Disable' : 'Enable'}</button>${s.builtin ? '' : ` <button class="ghost danger" data-del-source="${esc(s.id)}">Remove</button>`}</td></tr>`).join('');
  }
  async function saveSources(msg) { try { await send('PUT', '/api/admin/sources', ADM.sources.doc); toast(msg); } catch (err) { toast(err.message); } loadSources(); }
  function onSourceClick(e) {
    const b = e.target.closest('button'); if (!b) return;
    const list = ADM.sources.doc.sources, { toggleSource, delSource } = b.dataset;
    if (toggleSource) {
      const active = ADM.sources.effective.some((s) => s.id === toggleSource);
      const row = list.find((s) => s.id === toggleSource);
      const builtin = ADM.sources.builtin.find((s) => s.id === toggleSource) || {};
      if (row) { row.enabled = !active; if (!active && !row.type) Object.assign(row, builtin, { enabled: true }); } else list.push(active ? { id: toggleSource, enabled: false } : { ...builtin, enabled: true });
      saveSources(`Source ${toggleSource} ${active ? 'disabled' : 'enabled'}`);
    } else if (delSource) {
      ADM.sources.doc.sources = list.filter((s) => s.id !== delSource); saveSources(`Source ${delSource} removed`);
    }
  }
  function addSource(e) {
    e.preventDefault(); const f = formJSON(e.target);
    ADM.sources.doc.sources = ADM.sources.doc.sources.filter((s) => s.id !== f.id);
    ADM.sources.doc.sources.push({ id: f.id, type: 'wfp_csv', url: f.url, label: f.label, enabled: true });
    saveSources(`Source ${f.id} added - it runs on the next ingestion`); e.target.reset();
  }
  async function runIngestion(e) {
    e.preventDefault();
    try { const r = await send('POST', '/api/admin/ingest', { lookback_days: Number(new FormData(e.target).get('lookback_days')) }); $('#adm-ingest-status').textContent = `Ingestion ${r.status} at ${new Date().toLocaleTimeString()}. New prices appear on the Prices page within a few minutes; trends refresh nightly.`; } catch (err) { toast(err.message); }
  }

  async function loadUsers() {
    try {
      const { data } = await getJSON('/api/admin/users', { cache: false });
      $('#adm-user-rows').innerHTML = data.map((u) => `<tr><td>${esc(u.email || u.username)}</td><td>${esc(u.phone)}</td><td><small>${esc(u.status)}</small></td>${['buyers', 'officers', 'admins'].map((g) => `<td><input type="checkbox" aria-label="${g}" data-user="${esc(u.username)}" data-group="${g}" ${u.groups.includes(g) ? 'checked' : ''}></td>`).join('')}</tr>`).join('') || '<tr><td colspan="6" class="muted">No users yet.</td></tr>';
    } catch (err) { $('#adm-user-rows').innerHTML = `<tr><td colspan="6" class="muted">${esc(err.message)}</td></tr>`; }
  }
  async function onRoleChange(e) {
    const c = e.target; if (!c.dataset.user) return;
    try { await send('POST', `/api/admin/users/${encodeURIComponent(c.dataset.user)}/roles`, { group: c.dataset.group, action: c.checked ? 'add' : 'remove' }); toast(`Role ${c.dataset.group} ${c.checked ? 'granted' : 'removed'}`); } catch (err) { c.checked = !c.checked; toast(err.message); }
  }
  async function inviteUser(e) {
    e.preventDefault();
    try { await send('POST', '/api/admin/users', formJSON(e.target)); toast('Invitation sent - Cognito emails a temporary password'); e.target.reset(); loadUsers(); } catch (err) { toast(err.message); }
  }
  function selectSub(id) {
    $$('.subtabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.sub === id)));
    $$('#admin .sub').forEach((s) => (s.hidden = s.id !== id));
    const load = { 'adm-markets': loadCatalog, 'adm-crops': loadCatalog, 'adm-sources': loadSources, 'adm-users': loadUsers }[id];
    load().catch((err) => toast(err.message));
  }

  // ---------------------------------------------------------------- shell
  function selectTab(name) {
    $$('.tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
    $$('main > .panel').forEach((p) => (p.hidden = p.id !== name));
    if (name === 'trends' && !$('#chart').innerHTML) showTrends();
    if (name === 'buy') loadMine();
    if (name === 'officer' && !$('#board').innerHTML) showBoard();
    if (name === 'admin') selectSub($('.subtabs button[aria-selected=true]').dataset.sub);
  }
  function renderAuth() {
    const c = claims(), g = groups();
    $('#auth').textContent = c ? 'Sign out' : 'Sign in';
    $('#who').textContent = c ? `${c.email || 'signed in'} (${g.join(', ') || 'no role yet'})` : 'not signed in';
    $$('.tabs button[data-role]').forEach((b) => (b.hidden = !(g.includes(b.dataset.role) || g.includes('admins'))));
    const current = $('.tabs button[aria-selected=true]');
    if (current && current.hidden) selectTab('prices');
  }
  const netStatus = () => ($('#net').textContent = navigator.onLine ? 'online' : 'offline');

  async function init() {
    netStatus();
    $('#sim-link').hidden = !CFG.simulator;
    window.addEventListener('online', () => { netStatus(); flush(); });
    window.addEventListener('offline', netStatus);
    $$('.tabs button').forEach((b) => b.addEventListener('click', () => selectTab(b.dataset.tab)));
    $$('.subtabs button').forEach((b) => b.addEventListener('click', () => selectSub(b.dataset.sub)));
    $('#auth').addEventListener('click', () => (claims() ? logout() : login()));
    $('#mock-login-form').addEventListener('submit', mockLogin);
    const on = (sel, fn) => $(sel).addEventListener('submit', fn);
    on('#price-form', showPrices); on('#trend-form', showTrends); on('#demand-form', postDemand); on('#supply-form', findSupply);
    on('#board-form', showBoard); on('#farmer-form', registerFarmer); on('#listing-form', listCrop); on('#field-form', fieldPrice);
    on('#adm-market-form', saveMarket); on('#adm-crop-form', saveCrop); on('#adm-unit-form', saveUnit);
    on('#adm-source-form', addSource); on('#adm-ingest-form', runIngestion); on('#adm-user-form', inviteUser);
    $('#adm-markets').addEventListener('click', onCatalogClick); $('#adm-crops').addEventListener('click', onCatalogClick);
    $('#adm-sources').addEventListener('click', onSourceClick); $('#adm-users').addEventListener('change', onRoleChange);
    outbox.save(outbox.all());
    await finishLogin().catch(() => toast('Sign-in failed'));
    renderAuth();
    try { await loadRef(); await showPrices(); } catch { $('#price-meta').textContent = 'Offline and no saved data yet.'; }
    flush();
    if ('serviceWorker' in navigator && !CFG.mockAuth) navigator.serviceWorker.register('sw.js').catch(() => {});
  }
  init();
})();
