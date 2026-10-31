# Implementation and acceptance record

Updated: 2026-10-31. Acceptance uses synthetic data only. The Oracle gateway is running with registration closed. Auth0 tenant/application/API/Action configuration is installed; production social-provider credentials, COS and end-to-end user authorization remain incomplete.

## Delivered

- Backend: optional `openinvest[hosted]` package extra and `openinvest-hosted` command, Auth0 login/authorization integration and post-login Action, resource discovery, JWT scopes and revocable hashed PATs.
- Gateway: explicit tool whitelist, account-scoped idempotency receipts in a separate encrypted business store, rate/capacity limits, streamed HTTP responses, no paid inference.
- Isolation: separate Docker volume/container per account, two-worker host limit, 1 GB worker memory limit, idle recycling, no socket/control/research mount in workers.
- Committee: required hosted `analysis_id`, immutable input evidence, separate rule/tool/prompt versions, stale-input rejection, replay protection and uncertain-outcome handling. Legacy local calls remain compatible.
- Research: web-controlled default after notice, preference epochs and pending cancellation, precise numeric fields/currencies, screened encrypted quarantine, manual review, versioned JSONL export, explicit training-use marking, unused-sample deletion and compensating archive tombstones.
- Account APIs: onboarding, research preferences, PAT and OAuth connection management, own-data export, ledger reset, account deletion and separate research deletion.
- Operations: Singapore COS archive adapter, bounded encrypted queue, protected metrics, consistent encrypted daily backups, single-user/full recovery and a synthetic 24-hour soak runner. Recovery revokes connections, stops research and discards untrained candidates from stale snapshots.
- Existing website: companion worktree `/home/ubuntu/projects-review/openinvest-site-wt-hosted-mcp`. The current `openinvest.involutionhell.com` homepage/docs remain the website; `/connect/` is a separate HTML entry without analytics. Same-origin Pages Functions forward auth/account/MCP/discovery routes to Singapore. English/Chinese privacy and backend README explanations are updated.

## Completed local validation

| Check | Result |
|---|---|
| Full backend suite | **2398 passed, 3 skipped** (opt-in Docker/long-stream tests below) |
| Focused hosted control-plane suite | **24 passed** |
| Real Docker acceptance | **4 passed**: ten isolated accounts, same-key writes, eviction/persistence, two-worker gate, user archive restore, official MCP SDK through actual gateway and worker |
| TCP + local Caddy 130-second SSE, second-user query | **1 passed**, 132 seconds total test runtime |
| Import contracts | **3 kept, 0 broken** |
| Dependency lock | `uv lock --check` passed |
| Site/portal TypeScript and production builds | Passed |
| Portal browser test | Passed: web notice, onboarding, opt-out, token display/revocation, research deletion, no analytics requests or credential browser storage |
| Same-origin proxy test | Passed: route allowlist, configuration failure closed, credential/cookie forwarding, SSE pass-through, no request replay or exception disclosure |
| Site lint | No errors; one existing Fast Refresh warning in `src/i18n.tsx` |

The official MCP SDK test uses synthetic personal access tokens. It is not evidence of a real ChatGPT/Codex/Claude OAuth authorization. No existing author ledger or pre-policy records were imported. All test-created ledger containers and volumes were removed.

## Required external acceptance before release

- Configure the actual Auth0 tenant, Google/GitHub social credentials, API scopes, PKCE/resource compatibility, dynamic client registration and the post-login Action. Verify both first and repeated login and a revoked connection.
- Configure the Singapore host and COS credentials, archive access, backup timer and operational alerts. Verify real COS outage/recovery and a full backup/user restore there.
- Configure the existing site's Pages `HOSTED_GATEWAY_ORIGIN` binding and backend `HOSTED_ORIGIN=https://openinvest.involutionhell.com`. Deploy the web notice/privacy documents before enabling registration.
- Complete actual ChatGPT web OAuth, query and ledger write, and at least one target terminal client. Verify streams through the deployed Cloudflare/public proxy; local Caddy acceptance does not establish this.
- Run the supplied ten-account/two-worker soak for **24 hours**, monitoring OOM, memory, disk, queue and backup status. This test has not been run for 24 hours locally or in production.
- Keep `HOSTED_REGISTRATION_OPEN=0` until these gates pass. No cloud purchase was made; wait for the coupon and verify actual annual-payment applicability and order price.

Market caches currently remain private inside each worker; sharing only public market data is a future optimization. Local mocks establish queue/backup lifecycle behavior but do not prove real Auth0, COS or public network configuration.

Operator instructions and commands: [README.md](README.md).


## Oracle installation status — 2026-10-31

- Program: `/opt/openinvest-hosted`, Python 3.13, hosted dependencies installed. Hosted module imports succeed as the dedicated `openinvest-hosted` user.
- Data directory: `/srv/openinvest-hosted`, mode 700, initially empty. No author ledger or earlier research records imported.
- Environment: `/etc/openinvest-hosted.env`, root-owned mode 600. Independent encryption/session/Action/admin secrets generated locally; no values copied to this repository.
- Gateway/backup units installed and checked with `systemd-analyze verify`. Gateway is enabled and running; the backup timer stays disabled until COS and restore acceptance.
- `check-config` succeeds with the actual tenant and portal client credentials. COS remains unconfigured; it is required before public registration.
- A proxied DNS/TLS origin serves health, account, OAuth, discovery and MCP routes. Operator and other paths return 404 at the public proxy. Access logging is not enabled for this route. The production Pages gateway binding points to this origin; preview bindings remain unchanged.
- The full suite uses a temporary `INVEST_HOME`, disabled dotenv loading and a synthetic model key. This avoids local model overrides, event data and scheduler locks influencing compatibility tests. The three tests that failed under the developer environment pass in isolation and in the isolated full suite.
- Added a non-mutating `check-config` operator command and aligned gateway-served page CSP with the Mantine portal: runtime style attributes allowed, inline scripts/style elements still blocked.

## Auth0 configuration acceptance — 2026-10-31

- Actual portal application: Regular Web Application, canonical-site callback and web origin, authorization-code grant, client-secret Basic authentication. Secrets stored only in the root-only server environment and required Auth0 Action configuration.
- API: RS256, exact canonical `/mcp` audience, three scopes, user access through grants, M2M denied. Default third-party user grant created for exactly those scopes; first-party API `allow_all` alone was insufficient for a strict DCR client.
- Dynamic registration and resource compatibility configured. Discovery advertises `/oidc/register` and S256.
- Post-login Action deployed on Node 22, all four named secrets present, attached to the login trigger.
- Google connection enabled for the portal and at domain level. It still uses Auth0 development keys. GitHub OAuth credentials are not configured.
- A real synthetic public client registered through DCR (HTTP 201). Its `resource` + S256 authorization request now reaches Google's authorization endpoint (HTTP 302). This is not evidence of completed user login, returned token scopes or a real ChatGPT connection.
- The actual gateway login endpoint produces the correct tenant, canonical callback and S256 challenge. Gateway health is 200, anonymous account access is 401, and protected-resource discovery contains the exact audience/issuer.
- Default httpx, curl and ChatGPT-style user agents reach the origin. Cloudflare Browser Integrity Check rejects Python-urllib; no zone-wide security setting was changed. Public streaming/client acceptance remains required.

Next configuration steps: [Auth0 checklist](AUTH0_SETUP_zh.md). Dedicated Google/GitHub OAuth credentials, actual login, public connector acceptance, COS recovery and the 24-hour soak remain release gates.
