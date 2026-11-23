// Mock ITSM API (stands in for ServiceNow). Validates the bearer JWT itself
// and returns only the caller's incidents, like ServiceNow ACLs keyed on the user.
import crypto from 'node:crypto';

const ISSUER = process.env.ISSUER;
const AUDIENCE = 'itsm-api';
const INCIDENTS = {
  alice: [{ number: 'INC0001', short_description: 'Alice laptop VPN broken' }],
  bob: [{ number: 'INC0002', short_description: 'Bob payroll access request' }],
};

// Prefer keys pinned in config so the function needs no outbound network access.
let jwks = process.env.JWKS_JSON ? JSON.parse(process.env.JWKS_JSON).keys : undefined;
async function key(kid) {
  if (!jwks || !jwks.find((k) => k.kid === kid)) {
    if (process.env.JWKS_JSON) throw new Error('unknown kid (pinned JWKS)');
    const res = await fetch(`${ISSUER}/protocol/openid-connect/certs`);
    jwks = (await res.json()).keys;
  }
  const jwk = jwks.find((k) => k.kid === kid);
  if (!jwk) throw new Error('unknown kid');
  return crypto.createPublicKey({ key: jwk, format: 'jwk' });
}

const b64json = (s) => JSON.parse(Buffer.from(s, 'base64url').toString());

async function verify(token) {
  const [h, p, s] = token.split('.');
  const header = b64json(h);
  if (header.alg !== 'RS256') throw new Error(`alg ${header.alg} not allowed`);
  const ok = crypto.verify('RSA-SHA256', Buffer.from(`${h}.${p}`), await key(header.kid), Buffer.from(s, 'base64url'));
  if (!ok) throw new Error('bad signature');
  const c = b64json(p);
  if (c.iss !== ISSUER) throw new Error('bad issuer');
  if (c.exp * 1000 < Date.now()) throw new Error('expired');
  const aud = Array.isArray(c.aud) ? c.aud : [c.aud];
  if (!aud.includes(AUDIENCE)) throw new Error(`aud ${JSON.stringify(c.aud)} does not include ${AUDIENCE}`);
  return c;
}

const reply = (statusCode, body) => ({ statusCode, headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });

export const handler = async (event) => {
  const auth = event.headers?.authorization || '';
  if (!auth.startsWith('Bearer ')) return reply(401, { error: 'missing bearer token' });
  let c;
  try {
    c = await verify(auth.slice(7));
  } catch (e) {
    console.log(JSON.stringify({ rejected: e.message }));
    return reply(401, { error: e.message });
  }
  // What the downstream actually received: logged so the test can prove identity propagation.
  const seen = {
    sub: c.sub, preferred_username: c.preferred_username, aud: c.aud, azp: c.azp, act: c.act ?? null,
    scope: c.scope, sid: c.sid ?? null, auth_time: c.auth_time ?? null, iat: c.iat, exp: c.exp,
    login_age_s: c.auth_time ? Math.round(Date.now() / 1000 - c.auth_time) : null,
  };
  console.log(JSON.stringify({ accepted: seen }));
  // A write operation, so the background-gateway policy has something to forbid (v1, P2).
  const path = event.rawPath || event.requestContext?.http?.path || '';
  const method = event.requestContext?.http?.method || 'GET';
  const approve = path.match(/^\/incidents\/([^/]+)\/approve$/);
  if (method === 'POST' && approve) {
    console.log(JSON.stringify({ approved: approve[1], by: seen.preferred_username, azp: seen.azp }));
    return reply(200, { approved: approve[1], caller: seen });
  }
  return reply(200, { caller: seen, incidents: INCIDENTS[c.preferred_username] ?? [] });
};
