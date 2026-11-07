# Account identity migration and linking

The application owns a stable account UUID. Auth0 verifies login identities;
Google and GitHub may both resolve to the same UUID. This implementation does
not merge Auth0 profiles and needs no Auth0 Management API credential or new
Auth0 connection. Keep email-based auto-linking disabled.

## Upgrade

1. With the previous gateway running, obtain a successful consistent backup via
   `POST /operator/backup`. Retain the encrypted control, receipts and research
   databases and the per-account ledger volumes. Verify the backup manifest.
2. Stop the gateway, install this revision, and restart a single gateway process.
   `Store` performs an additive SQLite transaction before serving requests:
   `identities`, `identity_revocations`, `identity_flows`, and a schema marker are
   created; account archive and session identity/authentication fields are added.
   Each active legacy subject is backfilled once to its existing account UUID.
   Legacy sessions are bound to that subject, with no recent-authentication proof.
3. Check `/api/me`: the existing UUID, ledger readiness, policy/research settings,
   personal tokens and authorized clients are unchanged; one login method is
   listed. Check the existing ledger using a synthetic fixture before rollout.
   An MCP authorization already pending in an old browser tab must be restarted;
   it has no verified login-subject binding and is rejected with a conflict.
4. Deploy the companion website PR. Old websites remain able to sign in and use
   existing account APIs. The new settings panel shows an unavailable message
   against an old gateway, rather than presenting nonfunctional binding controls.

The migration is repeatable and does not open or rewrite a ledger, move a volume,
regenerate a participant ID, or modify monetary/idempotency records. It does not
link two existing accounts without their user's authentication and confirmation.

## User flow

`POST /api/me/login-methods` requires the browser's Origin and CSRF token. Authlib
handles OIDC state, nonce, PKCE, issuer/audience and token validation. First the
current session identity is authenticated again, then the requested provider is
authenticated with `prompt=login,max_age=0`. Both callbacks must return a recent
signed `auth_time`; neither callback may provision the secondary account.

The server-side flow expires after ten minutes, is bound to the initiating
browser credential and expects an exact provider/primary identity. It appears
in `/api/me` only after both verifications. The UI then requests explicit
confirmation (`keep_current_ledger=true`). Cancelling leaves account ownership
unchanged. The confirmation is one-use and rechecks both mappings transactionally.

For an existing secondary account, confirmation:

- preserves the initiating account UUID, ledger, preferences, receipts and PATs;
- moves its login bindings, marks the secondary ledger as an archive, and
  reparents any previous archives without combining financial records;
- revokes all secondary browser sessions, PATs and client grants, clears cached
  client metadata, disables secondary research recording and cancels queued
  samples; previously archived samples remain subject to the deletion controls;
- sets a token issue-time floor on transferred identities so a pre-link JWT
  cannot acquire access through an identically named destination client grant;
- invalidates initiating browser sessions and outstanding linking flows, then
  rotates the completing session. Destination OAuth grants and PATs remain valid.

A short admission barrier rejects confirmation while any previously admitted
MCP/account write or active worker request exists. It does not kill requests or
delete source volumes. Retry only after reviewing the unchanged pending flow.
The gateway currently runs as one process; multiple gateway replicas would need
a shared admission barrier before enabling linking.

Archives have an owner-checked browser export route; MCP and ordinary ledger APIs
never resolve an archive as a live account. Account deletion destroys all its
archives. Untrained-research deletion includes the previous participant IDs.
Unlinking requires recent OIDC authentication, keeps at least one login method
and refuses to remove the method used by the current browser session. Removed
identities retain an issue-time revocation floor even if later registered anew.

## Recovery and rollback

Do not downgrade to a gateway that resolves `accounts.subject` directly after
users have linked accounts. That old resolver cannot represent the new bindings.
Use a previous application revision carrying this identity resolver, or deploy a
forward fix; retain the additive schema. Reverting website UI does not undo links.

An archived source ledger remains independently exportable; exporting it does
not append its trades to the destination. To reverse an account association,
first verify both owners, freeze writes and compare the affected mappings and
archives. Do not restore the entire old control database into a running service
or overwrite intervening real transactions. A full disaster recovery uses the
documented stopped-gateway/new-directory procedure and a consistent backup set.
Control restore clears pending linking proofs and forces fresh OAuth tokens.

## Validation scope

`tests/test_hosted_identities.py` uses synthetic identities and temporary control
databases. It covers migration/restart, proof freshness, provider mismatch,
cancel/confirm/replay, credential invalidation, archive isolation, busy workers,
unlink safeguards and account deletion. The companion website test uses mocked
API responses for UI, i18n, mobile and analytics exclusion. These are not claims
of completed live Google/GitHub account-linking acceptance; verify that flow in
the configured Auth0 tenant before deployment.

Auth0 reference: [Account linking precautions](https://auth0.com/docs/manage-users/user-accounts/user-account-linking).
