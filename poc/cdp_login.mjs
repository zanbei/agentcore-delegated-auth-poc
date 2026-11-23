// Drive the local CDP Chrome through a real browser OAuth login + consent.
// Usage: KC_USER=alice KC_PASS=... node cdp_login.mjs <authorizationUrl> <returnUrlPrefix>
// Prints the final URL's query keys and session-related values (no tokens are involved).
const [authUrl, returnPrefix] = process.argv.slice(2);
const CDP = 'http://127.0.0.1:9222';
const tab = await (await fetch(`${CDP}/json/new?about:blank`, { method: 'PUT' })).json();
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));
let id = 0;
const pending = new Map();
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) pending.get(msg.id)(msg);
};
const send = (method, params = {}) =>
  new Promise((resolve) => {
    const i = ++id;
    pending.set(i, resolve);
    ws.send(JSON.stringify({ id: i, method, params }));
  });
// start from a clean browser so an earlier user's SSO cookie cannot sign in as the wrong user
const ev = async (expr) => (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result?.result?.value;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

await send('Network.enable');
await send('Network.clearBrowserCookies');
await send('Page.navigate', { url: authUrl });
const steps = [];
let filled = false;
let consented = false;
let finalUrl = null;
for (let n = 0; n < 60; n++) {
  await sleep(1000);
  const href = await ev('location.href');
  if (!href) continue;
  if (href.startsWith(returnPrefix)) { finalUrl = href; break; }
  const page = await ev(`JSON.stringify({u: !!document.querySelector('#username'), a: !!document.querySelector('button[name=accept], input[name=accept]'), t: document.title})`);
  if (!page) continue;
  const p = JSON.parse(page);
  if (p.u && !filled) {
    await ev(`(() => { const f = document.querySelector('#kc-form-login');
      document.querySelector('#username').value = ${JSON.stringify(process.env.KC_USER)};
      document.querySelector('#password').value = ${JSON.stringify(process.env.KC_PASS)};
      f.submit(); })()`);
    filled = true; steps.push('login form submitted');
  } else if (p.a && !consented) {
    const scopes = await ev(`[...document.querySelectorAll('#kc-oauth li, ul li')].map(e => e.innerText.trim()).filter(Boolean).slice(0,10).join(' | ')`);
    steps.push(`consent page shown: ${scopes}`);
    await ev(`document.querySelector('button[name=accept], input[name=accept]').click()`);
    consented = true; steps.push('consent accepted');
  }
}
await send('Page.close').catch(() => {});
await fetch(`${CDP}/json/close/${tab.id}`).catch(() => {});
ws.close();
if (!finalUrl) { console.log(JSON.stringify({ ok: false, steps })); process.exit(1); }
const u = new URL(finalUrl);
console.log(JSON.stringify({ ok: true, steps, returnedTo: u.origin + u.pathname, query: Object.fromEntries(u.searchParams) }));
