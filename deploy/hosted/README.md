# Hosted MCP operator runbook

Status: implementation and synthetic acceptance environment. Public registration defaults **closed**. No cloud purchase, public deployment, existing-ledger import, paid inference or broker execution is part of these files.

## Install

Use a dedicated Singapore Linux host with Docker, Caddy, Python/uv, encrypted disk and at least 2 CPU / 4 GB RAM. The gateway must be a single process: its worker leases and backup maintenance gate are intentionally not distributed. Do not use multiple Uvicorn workers or gateway replicas.

```sh
uv sync --frozen --extra hosted
docker build -f deploy/hosted/Dockerfile -t openinvest-hosted-worker:local .
```

Create a dedicated `openinvest-hosted` system user with Docker access. Docker access is administrative: only the gateway account receives it; worker containers never receive the socket. Install this checkout at `/opt/openinvest-hosted`. Copy `hosted.env.example` to `/etc/openinvest-hosted.env`, owned by root with mode 600. Set a dedicated `/srv/openinvest-hosted` directory, owned by the service user, mode 700. Keep it outside any existing INVEST_HOME. Generate independent random session, Action and admin secrets (32 bytes minimum) and a 32-byte base64 encryption key. Keep an offline copy of the encryption key; without it research and backups cannot be recovered. Do not put secrets in Git, command arguments, screenshots or shell history.

The gateway binds only `127.0.0.1:8768`. Install the provided systemd gateway, backup service and timer files after adjusting paths. Enable the gateway after configuration. Backups require a Singapore COS bucket and separate least-privilege COS credentials. Enable the backup timer only after a restore test. The encryption key and environment file require a separate secure backup.

## Auth0

中文逐项配置：[Auth0 配置清单](AUTH0_SETUP_zh.md)。Run `openinvest-hosted check-config` with the protected environment loaded to validate required local fields without printing secrets or creating state. It does not contact Auth0/COS or prove that external credentials work.

Use a dedicated Auth0 tenant. Register:

1. A **Regular Web Application** for the portal, with callback `HOSTED_ORIGIN/auth/callback`. Enable Google (`google-oauth2`) and GitHub (`github`) connections using their provider credentials. Disable email-based linking / automatic account merging. Do not ask for the email scope.
2. An RS256 API whose identifier is exactly `HOSTED_ORIGIN/mcp`, with scopes `invest:read`, `invest:write`, `invest:analyze`. Use authorization-code grants and PKCE S256 for public clients. Access tokens target this API, never the Google/GitHub ID token or `/userinfo` audience. Configure permissions so newly registered clients can request these user-approved scopes; do not silently issue no scopes via an unconfigured RBAC rule.
3. Enable OAuth discovery and dynamic client registration (`/oidc/register`) for MCP clients. Auth0 must advertise authorization/token/registration endpoints and `S256` in discovery. Enable the **Resource Parameter Compatibility Profile** where available and verify `resource=HOSTED_ORIGIN/mcp` produces the correct audience; for tenants lacking that profile, set the dedicated tenant's Default Audience to the API identifier. Test the authorize and token steps rather than assuming `resource` is recognized.
4. Add `auth0-action.js` to the post-login flow. Action secrets: `PORTAL_CLIENT_ID`, `MCP_AUDIENCE`, `PORTAL_ORIGIN`, `HOSTED_ACTION_SECRET`. The last secret must equal the gateway secret. The Action redirects MCP authorization through the same portal and resumes only after explicit web authorization and initialization. Portal login and refresh grants skip the redirect. Enable the two social connections for dynamically registered clients as required by the tenant configuration.

The post-login Action rejects MCP authorization without PKCE S256. Its continuation token includes the original state and is bound to the current user/client.

MCP clients discover the Auth0 issuer from `/.well-known/oauth-protected-resource/mcp`. The gateway validates RS256 signature, exact issuer/audience, expiration, scopes and an existing per-account client grant. It does not provision identities from an arbitrary bearer token. PATs are hashed, displayed once and revocable. Token revocation and OAuth grant revocation are checked on every request.

See [OpenAI authentication requirements](https://developers.openai.com/plugins/build/auth), [Auth0 MCP resource compatibility](https://auth0.com/blog/secure-csharp-mcp-server-with-auth0/) and [Auth0 audience troubleshooting](https://support.auth0.com/center/s/article/mcp-audience-error-with-auth0). Auth0 configuration and actual Google/GitHub/ChatGPT login must be verified in the target tenant before opening registration.

## Web portal and TLS

In the companion `openinvest-site` checkout:

```sh
pnpm install --frozen-lockfile
pnpm build:portal
```

Primary deployment uses the **existing** `https://openinvest.involutionhell.com` site, not a new website. `pnpm build` includes `/connect/` as a separate HTML entry. Configure its Cloudflare Pages runtime binding `HOSTED_GATEWAY_ORIGIN` to the Singapore origin server. Set `HOSTED_ORIGIN=https://openinvest.involutionhell.com` and audience `https://openinvest.involutionhell.com/mcp`; Auth0 callbacks also use this existing domain. Pages Functions stream only auth/account/MCP/discovery routes to the gateway, keeping browser sessions and CSRF on the same origin. Infrastructure `HOSTED_HOSTNAME` belongs to the origin server and may differ from the user-facing origin. The public Cloudflare proxy still needs target-environment streaming acceptance.

Alternatively, copy the complete `dist-portal/` to `HOSTED_PORTAL_DIR` if the gateway itself serves the portal. This is a separate HTML entry with **no Umami, marketing script or global copy handler**. Do not deploy the marketing `index.html` as a fallback for `/connect`, `/auth`, `/api` or account pages. The gateway serves the portal and same-origin APIs; no cross-origin CORS is enabled. OAuth handoff queries and credentials must not enter access logs. Gateway and worker access logs are disabled; the supplied Caddy configuration has no access logger. Apply the same exclusion to any CDN or upstream proxy logging.

Set `HOSTED_HOSTNAME` for Caddy and use `Caddyfile` as the HTTPS proxy. `/operator/*` is blocked at the public proxy. It has streaming flush enabled and a 21-minute response timeout. The gateway proxies SSE immediately and keeps the user's lease until the stream finishes. No proxy retry rule may replay writes.

Optionally set the marketing site's `VITE_HOSTED_ORIGIN` at build time to show the live portal link; otherwise the nav links to the guide. Build the marketing site with `INVEST_DOCS_DIR` pointing to this backend checkout's `docs/wiki` so its Chinese docs match this change.

## Limits and isolation

The default pool runs at most two 1 GB workers, starts accounts on demand and recycles only idle workers. Each worker has its own named volume and internal bearer credential, runs as UID 10001 with a read-only root filesystem, dropped capabilities, a PID limit and no gateway secrets, identity database, research database or Docker socket. Volumes survive eviction and process restarts. The gateway maps verified identities to random UUIDs; clients cannot choose another user's paths, volumes or account IDs.

There is no shared private cache. Current market caches remain inside each worker; sharing public market cache is an optional future optimization. The host keeps an encrypted research queue capped at 128 MiB. Monitor **ledger disk growth** separately: ledger data is intentionally long-lived and does not have an automatic deletion quota. Set infrastructure alerts and do not open unlimited registrations beyond the initial small deployment without capacity review.

Each account has 30 tool calls/minute and one request at a time. A pending analysis must be submitted or cancelled before starting another. TokenHub and `run_committee` are absent from the hosted whitelist. No provider secrets are passed into worker containers. Money writes require account-scoped idempotency keys; changed arguments return 409. An uncertain interrupted write stays pending and is never automatically replayed. Inspect its ledger/operation receipt before taking another action. A worker failure during submission similarly retains an uncertain analysis state for operator investigation.

## Research review and export

Run all commands on the server with its protected environment. Never expose research commands as MCP tools. Inspect outputs go to private files with mode 600.

```sh
openinvest-hosted research list
openinvest-hosted research inspect SAMPLE_ID /private/sample.json
openinvest-hosted research review SAMPLE_ID --label accepted --cleaned /private/review.json
openinvest-hosted research export /private/dataset-v1.jsonl
openinvest-hosted research mark-used DATASET_VERSION
```

Review labels: `accepted`, `rejected`, `failure_example`. The optional cleaned JSON accepts only `task_summary`, `rationale`, `evidence_summary`; review identifying content manually even after automated checks. Unreviewed/quarantined text is never automatically put in a training export. Structured data uses a field whitelist and retains numeric amounts/currencies. Derived features do not guess exchange rates. Inputs the MCP never received remain unknown. JSONL plus a SHA256 manifest identifies sample IDs, review labels and dataset version. `mark-used` must be called when a real training run consumes the dataset; exporting alone does not mean trained.

Stopping research bumps a preference epoch, cancels pending samples and compensates for an upload racing with opt-out. Archived samples remain until separately deleted. Deletion of unused samples invalidates tracked untrained exports; object deletion uses durable tombstones and retries after a COS outage. Do not copy untracked training exports around: deletion cannot revoke an unmanaged copy. Samples already marked used remain available for lineage; trained models have no unlearning implementation.

Research writes fail independently of successful ledger operations. No raw request/exception content is included in the hosted error logs. Quarantined text is encrypted and screened, but de-identification cannot prove no person can be reidentified; review is mandatory.

## Backups and recovery

```sh
openinvest-hosted status
openinvest-hosted backup
openinvest-hosted restore ACCOUNT_UUID BACKUP_TIMESTAMP
```

Backups briefly gate new business requests and refuse to interrupt an active worker. They take SQLite-aware ledger/control/research snapshots, encrypt each artifact with AES-GCM and upload a manifest last. COS cleanup retains 30 days of backup objects. Backup failure and queue/archive errors appear in operator status counters; wire these counters and disk/worker memory to your monitor. The included timer is daily; configure your monitor to alert on failed systemd jobs and absent daily successful backup, not only process liveness. Failed backups should be retried during an idle window.

A single-user restore requires an empty ledger volume. Preserve the existing volume under an operator-controlled name for investigation rather than overwriting it. Restore tools reject traversal, symlinks and archives over the restore size limit. Verify the restored cash, transactions, pending operations and decisions using a synthetic account before publishing. Restore is operator-only and not a client capability.

For full-host loss, stop the gateway and restore into an **empty** new HOSTED_DATA_DIR using the original encryption key:

```sh
openinvest-hosted restore-control BACKUP_TIMESTAMP
```

Then start the gateway with registration closed and restore individual ledger volumes using the same manifest timestamp. Database recovery revokes sessions/PATs/OAuth connections, disables research and requires a fresh web notice. Research recovery drops untrained candidates and untrained export records because the old backup cannot establish whether a user deleted them later. Review archived object retention separately before any training. Do not run dataset review/training-state mutations during a backup/recovery window.

## Acceptance and release gate

```sh
uv run --extra hosted pytest tests/test_hosted.py tests/test_hosted_worker.py -q
HOSTED_DOCKER_TEST=1 uv run --extra hosted pytest tests/test_hosted_worker.py -q
uv run --extra hosted pytest tests/ -q
uv run --extra hosted lint-imports --no-cache
```

Use synthetic data only. Run the local long-stream test with `HOSTED_STREAM_TEST=1 uv run --extra hosted pytest tests/test_hosted_streaming.py -q`. It verifies 130 seconds through local Caddy and a concurrent second-user query. The Caddy test container adds NET_BIND_SERVICE because the stock image binary carries that file capability. This is a local proxy test, not proof of Cloudflare/public behavior.

For the 24-hour staging soak, create ten synthetic accounts through the configured signup flow, put their PATs in a mode-600 JSON list outside the repository, and run `uv run --extra hosted python deploy/hosted/soak.py --url https://STAGING_ORIGIN --tokens /private/synthetic-pats.json --ack-synthetic-only synthetic-test-ledgers --synthetic-write`. Default duration is 86400 seconds. The script retries a single 1.25 USD synthetic deposit per account, never compounding the ledger, and emits counts only. Poll operator memory/OOM/backup metrics alongside the request loop.

The opt-in Docker test creates ten random synthetic accounts, tests eviction, isolated money writes with the same key, a two-worker capacity gate, volume persistence and restore, and removes its own volumes afterward.

Before changing `HOSTED_REGISTRATION_OPEN=1`, record evidence for **both social providers**, repeated login, real ChatGPT web OAuth+read+write, at least one terminal client, a 130-second stream behind the actual public proxy alongside another user's query, ten-account/two-worker **24-hour soak with no OOM**, COS outage/recovery and a daily backup/user restore. Unit mocks and a short local run do not satisfy these gates. Publish updated web/privacy documents first. Do not import the author's ledger or pre-rule history. Keep paid inference disabled.

Cloud purchase remains pending coupon arrival and verification of annual-payment applicability and actual price. A $100 coupon package is not assumed to be $100 cash balance.
