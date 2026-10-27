/* AgriPrice Zambia web client - vanilla JS, no framework, ~10 KB. Built for 2G:
   ETag/304 revalidation, last-known data shown offline, and an outbox that
   retries writes with Idempotency-Key headers so a retry never duplicates. */
(() => {
  'use strict';
  const CFG = window.AGRI || {};
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const store = {
    get(k, d = null) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
    del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
  };
  const toast = (msg) => { const t = $('#toast'); t.textContent = msg; t.hidden = false; clearTimeout(toast.h); toast.h = setTimeout(() => (t.hidden = true), 3500); };
  const kwacha = (ngweePerBag) => (ngweePerBag / 100).toLocaleString('en-ZM', { maximumFractionDigits: 0 });
  const dayToDate = (d) => new Date(d * 86400000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  const uid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
  let REF = { m: [], c: [], p: [] };

  // ---------------------------------------------------------------- auth (Cognito hosted UI, code + PKCE)
  const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const claims = () => { const t = store.get('id_token'); if (!t || store.get('id_exp', 0) < Date.now()) return null; try { return JSON.parse(atob(t.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))); } catch { return null; } };
  const redirectUri = () => location.origin + '/';
  async function login() {
    if (!CFG.cognitoDomain) return toast('Sign-in is not configured in this environment');
    const verifier = b64url(crypto.getRandomValues(new Uint8Array(48)));
    sessionStorage.setItem('pkce', verifier);
    const challenge = b64url(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
    const q = new URLSearchParams({ response_type: 'code', client_id: CFG.clientId, redirect_uri: redirectUri(), scope: 'openid email phone profile', code_challenge_method: 'S256', code_challenge: challenge });
    location.assign(`https://${CFG.cognitoDomain}/oauth2/authorize?${q}`);
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
    if (CFG.cognitoDomain) location.assign(`https://${CFG.cognitoDomain}/logout?${new URLSearchParams({ client_id: CFG.clientId, logout_uri: redirectUri() })}`);
  }
  const groups = () => { const c = claims(); return c ? (c['cognito:groups'] || []) : []; };

  // ---------------------------------------------------------------- network
  async function getJSON(path) {
    const cacheKey = 'GET ' + path;
    const cached = store.get(cacheKey);
    const headers = {};
    if (cached && cached.etag) headers['If-None-Match'] = cached.etag;
    const tok = store.get('id_token');
    if (tok && claims()) headers.Authorization = tok;
    try {
      const r = await fetch(path, { headers });
      if (r.status === 304 && cached) return { data: cached.data, at: cached.at, fresh: true };
      if (!r.ok) throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status });
      const data = await r.json();
      store.set(cacheKey, { etag: r.headers.get('ETag'), data, at: Date.now() });
      return { data, at: Date.now(), fresh: true };
    } catch (e) {
      if (cached && !e.status) return { data: cached.data, at: cached.at, fresh: false };
      throw e;
    }
  }
  async function send(path, body, key = uid()) {
    const tok = store.get('id_token');
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key, Authorization: tok || '' }, body: JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(data.message || `HTTP ${r.status}`), { status: r.status });
    return data;
  }

  // Outbox: writes made offline are queued and replayed with the SAME idempotency key.
  const outbox = { all: () => store.get('outbox', []), save(q) { store.set('outbox', q); $('#outbox').textContent = q.length; } };
  async function submit(path, body, okMsg) {
    const item = { path, body, key: uid() };
    try { await send(item.path, item.body, item.key); toast(okMsg); return true; } catch (e) {
      if (e.status) { toast(e.message); return false; }
      const q = outbox.all(); q.push(item); outbox.save(q); toast('Offline - saved, will send automatically'); return true;
    }
  }
  async function flush() {
    let q = outbox.all();
    for (const item of [...q]) {
      try { await send(item.path, item.body, item.key); q = q.filter((x) => x.key !== item.key); } catch (e) { if (e.status) q = q.filter((x) => x.key !== item.key); else break; }
    }
    outbox.save(q);
  }

  // ---------------------------------------------------------------- reference data
  async function loadRef() {
    const { data } = await getJSON('/api/markets');
    REF = data;
    const opt = (v, t) => `<option value="${v}">${t}</option>`;
    $$('select.crops').forEach((s) => (s.innerHTML = REF.c.map(([c, n]) => opt(c, n)).join('')));
    $$('select.markets').forEach((s) => (s.innerHTML = (s.closest('#price-form') ? opt('', 'All markets') : '') + REF.m.map(([c, n]) => opt(c, n)).join('')));
    $$('select.provinces').forEach((s) => (s.innerHTML = opt('ZM', 'All Zambia') + REF.p.map(([c, n]) => opt(c, n)).join('')));
  }
  const market = (code) => REF.m.find((m) => m[0] === code);

  // ---------------------------------------------------------------- prices
  async function showPrices(e) {
    if (e) e.preventDefault();
    const f = new FormData($('#price-form'));
    const m = market(f.get('near'));
    const q = new URLSearchParams({ c: f.get('c') || 'MZE' });
    if (m) { q.set('lat', m[3]); q.set('lon', m[4]); }
    try {
      const { data, at, fresh } = await getJSON('/api/prices?' + q);
      $('#price-table tbody').innerHTML = data.length ? data.map((r) => {
        const mk = market(r.m); const cls = r.t > 0 ? 'up' : r.t < 0 ? 'down' : '';
        return `<tr><td>${mk ? mk[1] : r.m}</td><td>${kwacha(r.p)}</td><td class="${cls}">${r.t ? (r.t > 0 ? '+' : '') + (r.t / 10).toFixed(1) + '%' : '='}</td><td>${dayToDate(r.d)}</td></tr>`;
      }).join('') : '<tr><td colspan="4" class="muted">No prices yet</td></tr>';
      $('#price-meta').textContent = fresh ? `Updated ${new Date(at).toLocaleTimeString()}` : `Offline - showing prices saved ${new Date(at).toLocaleString()}`;
    } catch (err) { $('#price-meta').textContent = 'Could not load prices: ' + err.message; }
  }

  // ---------------------------------------------------------------- trends (hand-drawn SVG: no chart library on 2G)
  async function showTrends(e) {
    if (e) e.preventDefault();
    const f = new FormData($('#trend-form'));
    try {
      const { data } = await getJSON(`/analytics/series?c=${f.get('c')}&r=${f.get('r')}`);
      drawChart(data); drawSeason(data.si || []);
    } catch (err) { $('#chart').innerHTML = `<p class="muted">${err.status === 404 ? 'No history yet for this selection.' : 'Could not load trends.'}</p>`; $('#season').innerHTML = ''; }
  }
  function drawChart(d) {
    const s = d.s || [], fc = d.f || [];
    if (!s.length) { $('#chart').innerHTML = '<p class="muted">No data</p>'; return; }
    const W = 640, H = 260, P = 36, all = [...s.map((r) => r[1]), ...s.map((r) => r[3]), ...s.map((r) => r[2]), ...fc.map((r) => r[1])];
    const lo = Math.min(...all) * 0.95, hi = Math.max(...all) * 1.05, n = s.length + fc.length;
    const x = (i) => P + (i * (W - 2 * P)) / Math.max(n - 1, 1), y = (v) => H - P - ((v - lo) * (H - 2 * P)) / (hi - lo || 1);
    const bag = (v) => Math.round((v * 50) / 100);
    const band = s.map((r, i) => `${x(i)},${y(r[3])}`).join(' ') + ' ' + s.map((r, i) => `${x(i)},${y(r[2])}`).reverse().join(' ');
    const line = s.map((r, i) => `${x(i)},${y(r[1])}`).join(' ');
    const fline = fc.length ? [`${x(s.length - 1)},${y(s[s.length - 1][1])}`, ...fc.map((r, i) => `${x(s.length + i)},${y(r[1])}`)].join(' ') : '';
    const ticks = [lo, (lo + hi) / 2, hi].map((v) => `<text x="4" y="${y(v) + 4}" font-size="11" fill="currentColor">K${bag(v)}</text><line x1="${P}" x2="${W - P}" y1="${y(v)}" y2="${y(v)}" stroke="currentColor" stroke-opacity=".12"/>`).join('');
    const lbl = (i, ym) => `<text x="${x(i)}" y="${H - 10}" font-size="10" text-anchor="middle" fill="currentColor">${ym.slice(4)}/${ym.slice(2, 4)}</text>`;
    const step = Math.ceil(n / 8);
    const labels = [...s.map((r) => r[0]), ...fc.map((r) => r[0])].map((ym, i) => (i % step === 0 ? lbl(i, ym) : '')).join('');
    $('#chart').innerHTML = `<svg viewBox="0 0 ${W} ${H}" style="color:var(--muted)">${ticks}
      <polygon points="${band}" fill="var(--accent2)" fill-opacity=".18"/>
      <polyline points="${line}" fill="none" stroke="var(--accent)" stroke-width="2.5"/>
      ${fline ? `<polyline points="${fline}" fill="none" stroke="var(--accent2)" stroke-width="2.5" stroke-dasharray="6 5"/>` : ''}
      ${labels}<text x="${W - P}" y="16" font-size="11" text-anchor="end" fill="currentColor">K per 50 kg · band = min-max · dashed = forecast</text></svg>`;
  }
  function drawSeason(si) {
    const months = 'JFMAMJJASOND';
    const max = Math.max(...si, 1);
    $('#season').innerHTML = si.map((v, i) => `<div style="height:${(v / max) * 100}%" title="${v}"><span>${months[i]}</span></div>`).join('');
  }

  // ---------------------------------------------------------------- buyer & officer forms
  const perKg = (kPerBag) => Math.round((Number(kPerBag) * 100) / 50);
  async function postDemand(e) {
    e.preventDefault();
    const f = new FormData(e.target), m = market(f.get('m'));
    if (await submit('/api/demand', { c: f.get('c'), q: Number(f.get('q')), p: perKg(f.get('pk')), lat: m[3], lon: m[4], r: Number(f.get('r')) }, 'Demand posted - sellers will be notified by SMS')) { e.target.reset(); loadMine(); }
  }
  async function loadMine() {
    if (!groups().includes('buyers')) return;
    try {
      const { data } = await getJSON('/api/demand/mine');
      $('#mine').innerHTML = data.map((d) => `<li>${d.q} kg ${(REF.c.find((c) => c[0] === d.c) || [, d.c])[1]} · up to K${Math.round((d.p * 50) / 100)}/50kg · ${d.r} km</li>`).join('') || '<li class="muted">None yet</li>';
    } catch { /* offline */ }
  }
  const formJSON = (form, extra = {}) => ({ ...Object.fromEntries(new FormData(form)), ...extra });
  async function registerFarmer(e) { e.preventDefault(); if (await submit('/api/farmers', formJSON(e.target), 'Farmer registered')) e.target.reset(); }
  async function listCrop(e) { e.preventDefault(); const b = formJSON(e.target); b.q = Number(b.q); if (await submit('/api/listings', b, 'Listed - buyers nearby will be matched')) e.target.reset(); }
  async function fieldPrice(e) {
    e.preventDefault(); const b = formJSON(e.target); const body = { c: b.c, m: b.m, p: perKg(b.pk) }; if (b.d) body.d = b.d;
    if (await submit('/api/field-prices', body, 'Price report queued for the next ingestion run')) e.target.reset();
  }

  // ---------------------------------------------------------------- shell
  function selectTab(name) {
    $$('.tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
    $$('.panel').forEach((p) => (p.hidden = p.id !== name));
    if (name === 'trends' && !$('#chart').innerHTML) showTrends();
    if (name === 'buy') loadMine();
  }
  function renderAuth() {
    const c = claims(), g = groups();
    $('#auth').textContent = c ? 'Sign out' : 'Sign in';
    $('#who').textContent = c ? `${c.email || 'signed in'} (${g.join(', ') || 'no role yet'})` : 'not signed in';
    $$('.tabs button[data-role]').forEach((b) => (b.hidden = !(g.includes(b.dataset.role) || g.includes('admins'))));
  }
  const netStatus = () => ($('#net').textContent = navigator.onLine ? 'online' : 'offline');

  async function init() {
    netStatus();
    window.addEventListener('online', () => { netStatus(); flush(); });
    window.addEventListener('offline', netStatus);
    $$('.tabs button').forEach((b) => b.addEventListener('click', () => selectTab(b.dataset.tab)));
    $('#auth').addEventListener('click', () => (claims() ? logout() : login()));
    $('#price-form').addEventListener('submit', showPrices);
    $('#trend-form').addEventListener('submit', showTrends);
    $('#demand-form').addEventListener('submit', postDemand);
    $('#farmer-form').addEventListener('submit', registerFarmer);
    $('#listing-form').addEventListener('submit', listCrop);
    $('#field-form').addEventListener('submit', fieldPrice);
    outbox.save(outbox.all());
    await finishLogin().catch(() => toast('Sign-in failed'));
    renderAuth();
    try { await loadRef(); await showPrices(); } catch { $('#price-meta').textContent = 'Offline and no saved data yet.'; }
    flush();
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
  }
  init();
})();
