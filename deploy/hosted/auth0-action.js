// Deploy as an Auth0 post-login Action. No portfolio information is sent to Auth0.
const crypto = require('crypto');

exports.onExecutePostLogin = async (event, api) => {
  if (event.client.client_id === event.secrets.PORTAL_CLIENT_ID ||
      event.transaction?.protocol === 'oauth2-refresh-token' ||
      event.resource_server?.identifier !== event.secrets.MCP_AUDIENCE) return;
  const query = event.request?.query || {};
  if (query.code_challenge_method !== 'S256' ||
      typeof query.code_challenge !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(query.code_challenge)) {
    api.access.deny('PKCE S256 is required for MCP authorization.');
    return;
  }
  const ticket = api.redirect.encodeToken({
    secret: event.secrets.HOSTED_ACTION_SECRET,
    expiresInSeconds: 300,
    payload: { sub: event.user.user_id, client_id: event.client.client_id,
      aud: event.secrets.PORTAL_ORIGIN, jti: crypto.randomUUID() },
  });
  api.redirect.sendUserTo(`${event.secrets.PORTAL_ORIGIN}/auth/action`, {
    query: { session_token: ticket },
  });
};

exports.onContinuePostLogin = async (event, api) => {
  try {
    const token = api.redirect.validateToken({
      secret: event.secrets.HOSTED_ACTION_SECRET, tokenParameterName: 'session_token',
    });
    if (token.sub !== event.user.user_id || token.client_id !== event.client.client_id ||
        token.aud !== event.secrets.PORTAL_ORIGIN || token.iss !== event.secrets.PORTAL_ORIGIN ||
        token.state !== event.request?.query?.state || token.approved !== true) throw new Error();
  } catch (_) {
    api.access.deny('Account authorization was not completed.');
  }
};
