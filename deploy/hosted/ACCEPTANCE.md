# Implementation and acceptance record

## Registration repair and browser OAuth acceptance — 2026-11-04 (Asia/Shanghai)

The Oracle gateway and worker run source `5baf1410`. A consistent encrypted
backup preceded the update. The public consent endpoint now accepts the current
portal contract, resolving the missing-route registration failure.

Actual Auth0 browser sign-in, the website callback, notice acceptance and native
Codex DCR/PKCE OAuth completed with an isolated database test identity. The
native client discovered 31 tools, created an empty USD ledger, retried the same
deposit without duplication, rejected changed arguments for the same key and
read the expected balance. The browser changed preferences after Agent setup,
retained the research opt-out, downloaded an export, created and revoked a PAT,
revoked the OAuth grant and deleted the owned test account. The revoked native
client received an authentication challenge. Temporary test identities and
clients were removed after each run.

A separate OAuth-first run started with an unaccepted, uninitialized account
and no ledger form. Website notice acceptance, Auth0 permission consent, the
native callback and Agent initialization completed. The browser read the same
AUD ledger preferences, and revocation/account deletion blocked native access.

This run used a dedicated Auth0 database connection. Google/GitHub social login
and ChatGPT client acceptance still require their own current-session checks.
Mac SSH is unavailable during this run. Earlier provider checks below retain
their original dates and scope.

The current public `/mcp` endpoint also passed a controlled long-request check
through Cloudflare. A file lock on the owned synthetic ledger supplied a
140-second wait. The analysis request completed in **141.988 seconds**, response
headers arrived in **0.111 seconds**, and the longest frame gap was **15.102
seconds**. A second account's concurrent query completed in **0.166 seconds**.
Preparation and submission retained the same `analysis_id`; submission used a
synthetic HOLD transcript and a self-reported synthetic model identifier. Both
test accounts and volumes were removed. A test-runner Docker exec-handle error
was corrected before this successful run.

A replacement ten-account/two-worker 24-hour public soak began on 2026-11-04
and remains in progress. Completed short intervals remain interim evidence.

The earlier 24-hour run ended with 15,820 requests, two errors, ten busy
responses, ten successful accounts and zero OOM events. It failed its acceptance
condition. Its logs contain no error categories, so their causes remain
unverified. The soak runner now records bounded failure categories and the time
of the first error without request bodies, credentials or raw exception text.
Focused hosted/MCP tests pass with **70 passed, 4 skipped**.

## PR submission update — 2026-11-02 (Asia/Shanghai)

The PR now includes R2/S3 archive support, shared browser/Agent ledger settings,
worker discovery namespaces and research schema 2. Research adds server-ordered
attempt IDs, scoped retry correlation, explicit error categories and reported
execution feedback. Exact amounts/currencies remain available; identity,
credentials, raw idempotency keys and JSON-RPC IDs are excluded from exports.

Submission validation: isolated full backend suite **2427 passed, 4 skipped**;
MCP/hosted compatibility suite **56 passed**; hosted/storage/worker suite
**53 passed, 4 skipped**; separate local Caddy 130-second stream plus concurrent
second-user query **1 passed**. Import contracts **3 kept, 0 broken** and
`uv lock --check` passed. The opt-in stream result was obtained separately from
the default full suite; no new Docker worker acceptance is claimed here.

This submission changes source and PR metadata only. It does not deploy, restart
production, modify provider settings or establish ChatGPT web acceptance or a
completed 24-hour production soak. Earlier dated entries below are historical
deployment/review evidence; their uncommitted-source statements describe those
earlier review stages.

Updated: 2026-11-01 (Asia/Shanghai). Ledger acceptance uses synthetic data only. The Oracle gateway has registration open at the operator's explicit request for personal end-to-end testing, before the remaining release gates below have passed. Auth0 tenant/application/API/Action configuration, the operator's existing social-provider credentials and both provider callbacks are installed. Real Google and GitHub portal login was verified while registration was closed; after opening, both login redirects and synthetic gateway admission/onboarding were rechecked. R2 archive/backup recovery and native Codex MCP OAuth/read/write are verified. ChatGPT web acceptance and the full 24-hour stability interval remain pending.

## Delivered

- Backend: optional `openinvest[hosted]` package extra and `openinvest-hosted` command, Auth0 login/authorization integration and post-login Action, resource discovery, JWT scopes and revocable hashed PATs.
- Gateway: explicit tool whitelist, account-scoped idempotency receipts in a separate encrypted business store, rate/capacity limits, streamed HTTP responses, no paid inference.
- Isolation: separate Docker volume/container per account, two-worker host limit, 1 GB worker memory limit, idle recycling, no socket/control/research mount in workers.
- Committee: required hosted `analysis_id`, immutable input evidence, separate rule/tool/prompt versions, stale-input rejection, replay protection and uncertain-outcome handling. Legacy local calls remain compatible.
- Research: web-controlled default after notice, preference epochs and pending cancellation, precise numeric fields/currencies, screened encrypted quarantine, manual review, versioned JSONL export, explicit training-use marking, unused-sample deletion and compensating archive tombstones.
- Account APIs: onboarding, research preferences, PAT and OAuth connection management, own-data export, ledger reset, account deletion and separate research deletion.
- Operations: Cloudflare R2 / COS archive adapters, scoped expiring R2 credentials, bounded encrypted queue, protected metrics, consistent encrypted daily backups, single-user/full recovery and a synthetic 24-hour soak runner. Recovery revokes connections, stops research and discards untrained candidates from stale snapshots.
- Existing website: companion worktree `/home/ubuntu/projects-review/openinvest-site-wt-hosted-mcp`. The current `openinvest.involutionhell.com` homepage/docs remain the website; `/connect/` is a separate HTML entry without analytics. Same-origin Pages Functions forward auth/account/MCP/discovery routes to Singapore. English/Chinese privacy and backend README explanations are updated.

## Completed local validation

| Check | Result |
|---|---|
| Full backend suite | **2413 passed, 3 skipped** (opt-in Docker/long-stream tests below) |
| Focused hosted control-plane suite | **39 passed, 2 skipped** |
| Real Docker acceptance | **4 passed**: ten isolated accounts, same-key writes, eviction/persistence, two-worker gate, user archive restore, official MCP SDK through actual gateway and worker |
| TCP + local Caddy 130-second SSE, second-user query | **1 passed**, 132 seconds total test runtime |
| Import contracts | **3 kept, 0 broken** |
| Dependency lock | `uv lock --check` passed |
| Site/portal TypeScript and production builds | Passed |
| Portal browser test | Passed: web notice, onboarding, opt-out, token display/revocation, research deletion, no analytics requests or credential browser storage |
| Same-origin proxy test | Passed: route allowlist, configuration failure closed, credential/cookie forwarding, SSE pass-through, no request replay or exception disclosure |
| Site lint | No errors; one existing Fast Refresh warning in `src/i18n.tsx` |

The official MCP SDK test uses synthetic personal access tokens. It is not evidence of a real ChatGPT/Codex/Claude OAuth authorization. No existing author ledger or pre-policy records were imported. Short-test ledger containers and volumes were removed. The current 24-hour run retains its own ten synthetic fixtures until automatic cleanup.

## Remaining release gates — 2026-11-04

- Complete actual ChatGPT web OAuth, query and ledger write. OAuth discovery succeeds in the operator's Pro browser, but creation is rejected with `Custom apps aren't allowed in this context. Check your workspace permissions or security settings`. The plugin settings page also reports `Couldn't load plugin settings`. No ChatGPT client OAuth/read/write success is claimed. Account/platform access must be resolved before retrying this gate.
- Complete the running ten-account/two-worker **24-hour** public soak with no OOM and verified final balances. Initial successful requests do not establish a completed full-day result. Worker-stat sampling may overlap container eviction; these explicitly counted unavailable samples are separate from OOM events and monitor transport failures.
- Recheck current-session Google and GitHub login after Mac SSH access is restored. The earlier provider tests retain their dated scope.

Registration is open at the operator's request for personal testing. Current source is submitted in PR #577. R2 archive/recovery, backup/user restore, native terminal OAuth/read/write and public long-request evidence are recorded here. The notice/privacy updates are deployed on the existing website.

No Tencent host was purchased. Coupon applicability and actual annual-payment price still require verification before any later purchase. Market caches remain private inside each worker; sharing public data is a future optimization.

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
- Google and GitHub connections are enabled for the portal and at domain level. With explicit operator authorization, both reuse the existing involutionhell production provider credentials; those values are not copied into this repository. No existing provider application callback was replaced.
- Provider authorization URLs were checked against the existing client IDs. Both provider applications now retain the original site's callback and also register the Auth0 callback. Google no longer reports `redirect_uri_mismatch`; GitHub settings retained both addresses after a fresh page load.
- A real synthetic public client registered through DCR (HTTP 201). Its `resource` + S256 authorization request now reaches Google's authorization endpoint (HTTP 302). This is not evidence of completed user login, returned token scopes or a real ChatGPT connection.
- The actual gateway login endpoint produces the correct tenant, canonical callback and S256 challenge. Gateway health is 200, anonymous account access is 401, and protected-resource discovery contains the exact audience/issuer.
- Default httpx, curl and ChatGPT-style user agents reach the origin. Cloudflare Browser Integrity Check rejects Python-urllib; no zone-wide security setting was changed. Public streaming/client acceptance remains required.

- The production website now proxies to the gateway. Two synthetic accounts passed official MCP SDK initialization, queries, same-key repeated writes and independent exact-balance checks through the public endpoint using PATs. Their accounts, containers, volumes and the temporary DCR client were removed. Research remained off; the queue is empty. This does not replace real user OAuth acceptance.
- Live browser testing detected Cloudflare's automatic analytics injection on the connection page. The companion website fix adds `Cache-Control: no-store, no-transform` to private connection pages (site PR #13); the approved homepage UI is unchanged.

## Portal login acceptance — 2026-11-01 (Asia/Shanghai)

- Real Google and GitHub browser logins returned through Auth0 to the canonical site's callback. Auth0 recorded successful social login and authorization-code exchange for each provider.
- The portal application created through the Management API initially retained legacy OIDC defaults with no explicit ID-token signing algorithm. Explicitly enabling OIDC Conformant and RS256 corrected the gateway's login verification failure. The setup checklist now requires both fields; gateway validation was not weakened.
- Both providers now reach `Registration is not open yet` after token and identity verification. This is the expected 503 for identities that have not been admitted while registration is closed. No real-user ledger, policy acceptance, research preference or MCP client grant was created by these checks.
- These checks establish portal authentication through the deployed public proxy, not account onboarding or MCP OAuth authorization. Actual ChatGPT and terminal-client OAuth still require admission, web initialization and the remaining release gates.

Remaining release gates: actual ChatGPT web OAuth/read/write and completion of the 24-hour soak. Registration was subsequently opened at the operator's explicit request for personal testing; neither gate is claimed complete.


## R2 archive, recovery and terminal-client acceptance — 2026-11-01 (Asia/Shanghai)

- With operator approval, remote archive placement changed from Singapore-only COS to Cloudflare R2 default placement. Business ledgers remain on the Oracle Singapore host. English/Chinese web privacy text and backend documentation now distinguish these locations.
- A new private bucket isolates hosted data from existing assets/backups. Root-only delegation refreshes one-hour credentials every 15 minutes. The gateway receives only read/write access to `research/` and `backups/`; an actual cross-bucket request and another-prefix request were denied. Forced refresh and SDK access succeeded. The broad Cloudflare management credential is not in gateway or worker environments.
- A synthetic account passed public MCP same-key repeated deposits with an exact 123.45 USD balance, encrypted R2 research upload, opt-out with continued ledger access, unused-sample deletion, encrypted backup download and single-user ledger restore. Replaying the deposit after restore preserved the exact balance. Separate empty-directory control/research/receipt recovery revoked connections, disabled research and required web acceptance again.
- Simulated object-store failure retained the bounded encrypted queue. Recovery uploaded successfully; reviewed JSONL preserved an allowed exact amount while excluding synthetic identity/credential canaries. No real-user portfolio or prior history was imported.
- The daily backup service completed successfully; the timer is enabled for 03:00 Asia/Singapore with 30-day retention. A separate private copy of the recovery encryption key is stored on the operator's Mac.
- Actual Codex app-server completed discovery, DCR, S256 OAuth, web onboarding/authorization, provider consent and its native loopback callback through the public endpoint. Direct native MCP calls verified query, repeated ledger write and revocation. No model turn or paid inference was started. Real Google/GitHub portal login is recorded separately above; the terminal transport fixture used a temporary synthetic identity.
- Native client testing found that per-ledger base currency was not loaded by core configuration. Worker initialization, restart and restore now rehydrate it from the validated ledger marker. Focused and real Docker tests passed; the deployed native client reports USD for the USD ledger without changing its cash balance.
- A synthetic streaming worker completed a **130.206-second** request through the actual public Pages/Cloudflare/Caddy/gateway path. First byte arrived in 0.176 seconds; a second account queried its real ledger worker in 0.201 seconds during the stream. This establishes transport behavior, not model-analysis quality. Test accounts and volumes were removed.
- The final isolated backend suite passed **2411 tests**, with 3 opt-in skips; real Docker acceptance passed **4 tests**. Portal browser and Pages proxy tests passed. The developer environment's existing model/event/scheduler state caused three unrelated failures in an initial non-isolated run; isolated INVEST_HOME with dotenv disabled passed the complete suite.
- A ten-account, two-worker, 24-hour synthetic public soak is running under systemd. It records exact repeated-write results, worker memory, OOM events, queue/backup state and automatic fixture cleanup. A started run is not a completed 24-hour result.


## Mac desktop acceptance — 2026-11-01 (Asia/Shanghai)

- The installed Mac desktop app (26.1027.30824, bundle `com.openai.codex`, operating in Codex mode) accepted a Streamable HTTP server through its native MCP settings. This proves the native Codex client flow, not ChatGPT connector compatibility. The separate ChatGPT web plugin-creation route returned an account/context restriction.
- Its actual desktop Authenticate action completed DCR/OAuth, the web notice and synthetic-ledger initialization with research off, web authorization, provider consent and the Mac loopback callback. The desktop test conversation selected this computer, used only `openinvest_desktop_acceptance`, and visibly invoked Doctor and Status from that integration. The initial synthetic ledger reported USD 0.00.
- The same desktop conversation issued two identical deposits and one changed-amount request using a shared key. The repeated write was applied once; changed parameters returned HTTP 409. The matching encrypted receipt was independently verified in the hosted account's business store.
- A fractional-cent input exposed an existing core inconsistency: the deposit response returned 42.125 while the ledger's existing two-decimal policy stored 42.12. Deposit and withdrawal now return the balance actually committed under the ledger lock. Their MCP descriptions explicitly describe this rounding. Original requested numeric amounts remain available; this fix does not introduce a new cash-precision policy.
- Two regression cases failed before this fix and passed afterward; they compare the response with a reopened ledger and verify exact replay. The final isolated full suite passed 2413 tests with 3 opt-in skips. Post-deployment desktop retesting passed: two 1.125 USD deposits with one key returned the same 43.24 balance, a changed 1.126 request was rejected with HTTP 409, and two 1.125 withdrawals with one key returned the same 42.12 balance. Final Status returned 42.12. Server-side encrypted receipts independently matched these amounts. Real Docker acceptance passed 4 tests. The final full-day stability interval is restarted after this deployment.
- The desktop application needed a normal cold restart after its MCP reload action left a hosted-runtime identity error. The app recovered. An auxiliary SSH-launched client did not inherit the desktop login state, so its failed handshake is not counted as a successful desktop read/write test; the evidence above comes from the actual desktop conversation and the server receipt.

- The desktop test used a new local-computer conversation; its visible integration trace identified Openinvest Desktop Acceptance, Doctor and Status. Subsequent real desktop conversational turns exercised deposits, withdrawals, idempotent retries and conflict rejection. The server-side receipt keys were unique to that conversation. This is evidence from the actual desktop UI, not a substitute SSH process.
- Worker releases retain a unique release tag as well as the configured image ID. Rebuilding the local tag had made the earlier untagged image unavailable, causing one synthetic-only backup attempt to fail with HTTP 503. The validated release image is now retained, the gateway is healthy and a fresh encrypted R2 backup completed successfully.

- Temporary desktop MCP configuration, hosted test ledger/volume, synthetic Auth0 user/database connection and its DCR client were removed after verification. Server-side account/grant deletion revokes access even though an auxiliary SSH session could not clear the desktop keychain item. The temporary UI helper and its Accessibility grant were removed. The desktop test conversation remains available in history as review evidence.

## Registration opened for operator testing — 2026-11-01 (Asia/Shanghai)

- At the operator's explicit request, changed only `HOSTED_REGISTRATION_OPEN` from `0` to `1`; kept a root-only environment rollback and validated configuration before restarting the gateway. No code was committed or pushed. Paid server inference remains disabled.
- Paused the synthetic soak for the planned configuration restart. All ten owned fixtures were removed; cancellation is recorded as planned interruption, not a full-day pass. Started a fresh ten-account/two-worker interval afterward.
- Public `/connect/` returns 200; Google and GitHub portal login routes redirect to the configured Auth0 social connections. A newly generated synthetic subject was admitted through a signed Action handoff without direct account provisioning. Credential creation was rejected before initialization; initialization with research off, PAT creation, MCP query, PAT revocation and owned-account deletion passed through the public website proxy.
- This targeted smoke validates gateway registration and onboarding, not a new social-provider login or ChatGPT OAuth completion. The operator will test their own identity and ChatGPT connector; the existing ChatGPT account/context restriction is unresolved.
- Private result: `/srv/openinvest-hosted/acceptance-results/registration-open-2026-11-01.json`. The prior soak checkpoint is retained separately; current progress remains under `soak-2026-11-01/progress.json`.

## Local account-flow revision — review only, not deployed

- The operator requested a local review before deployment. Source now separates browser policy/research confirmation and OAuth authorization from Agent-led ledger setup. Before a ledger exists, accepted accounts can authorize connections, manage PATs and research settings, and call only setup/diagnostic MCP tools plus protocol discovery. Ledger reads/writes remain blocked until setup completes.
- Added hosted-only `setup_ledger`, `ledger_settings`, and `update_ledger_settings`. The browser uses the same preferences service; risk can be unspecified and the tracked list can be empty. Existing local/private tools and the default local nonempty-strategy requirement remain compatible.
- Account settings display the sign-in provider/account ID, support optional empty-ledger creation, and edit currency/risk/tracked assets. Retained asset metadata and financial history are preserved. Reporting currency locks after financial/decision history; revision conflicts require reload rather than overwriting another client's changes.
- Local validation: **2417 passed, 4 opt-in skips** on Python 3.14; separate real Docker/Python 3.13 worker + official MCP SDK test passed the consent → connection → Agent setup → browser/Agent preference edits, stale-write rejection and currency-history lock. No new Google/GitHub or ChatGPT client acceptance is claimed.
- Portal and complete site builds passed; browser acceptance, desktop/mobile EN/ZH geometry, modal keyboard/focus/CSP, proxy streaming and offline interactive-artifact checks passed. Import contracts: 3 kept, 0 broken. Lint has only the existing `src/i18n.tsx` Fast Refresh warning. Fixed an existing VitePress serialized edit-link callback that referenced an unavailable closure variable during SSR.
- Review artifact: `/home/ubuntu/.codex/artifacts/openinvest/account-flow-local-2026-11-01/preview.html`; it embeds the built portal with a clearly marked, synthetic, offline API. It does not authenticate to providers or access production ledgers. Screenshots in the same directory cover consent, authorization, connection, account preferences and mobile/English views.
- An initial Docker test used the legacy production discovery label and affected an idle worker's lifecycle. No production volume was removed or ledger mutation performed by that test. Restarted the unchanged deployed gateway to clear its stale container reference. Subsequent ten-account production-soak sampling reported 250 successful requests, zero request errors and zero busy responses; this is not a completed full-day result. Added a worker namespace and a discovery/capacity regression test; every opt-in Docker fixture now uses its own namespace and image. The namespace feature itself is local-only until review.
- These account-flow/worker-namespace changes have not been committed, pushed or deployed. The website and production worker image retain the prior flow. Existing registration and the current release's synthetic soak remain active.
- Final-build ten-account/two-worker soak started on November 1 at 04:11:47 Asia/Shanghai, with an expected full interval ending November 2 at 04:11:47. It is still running; successful early iterations are not reported as a completed full-day result. Registration remains closed. The source changes have not been committed or pushed.


## Simplified connection review — local only

- Updated the local review at the operator's request: first connection has a concise data-use notice/policy link and one explicit agree/authorize action. No research toggle, read checkbox, or ledger fields are shown. The browser combines notice acceptance and the pending OAuth continuation; policy-dialog dismissal does not record acceptance.
- Research still defaults on for new accounts after the continue action. The opt-out and untrained-sample deletion controls are in Account settings → Privacy and data, available before ledger setup. Reaccepting a notice preserves an existing opt-out. No backend collection defaults or policy enforcement were changed in this revision.
- Removed the optional manual setup form from uninitialized account pages. Agents create ledgers or import existing records after connecting. Once initialized, browser preferences remain editable with the existing currency-history guard and revision checks. The existing backend browser setup API remains for compatibility, without a first-run UI.
- Local portal browser tests cover combined consent/authorization, absence of first-run inputs, settings-only opt-out before setup, persistence after reacceptance, token revocation, sample deletion, Agent-created ledger visibility, stale updates and bilingual/mobile layouts. Complete site/portal builds, header/tab/form geometry and private-page CSP checks passed. The offline synthetic interactive artifact was tested independently.
- Current review: `/home/ubuntu/.codex/artifacts/openinvest/account-flow-simple-local-2026-11-01/preview.html`. No source commit, push or deployment, provider changes, production restart or production ledger access was performed for this simplification. The earlier full backend and real-Docker evidence remains unchanged.


## Personal home — local review, not deployed

- Added a private `/account/` entry with overview/connect/settings navigation, provider/account identity, uninitialized or configured ledger summaries, and counts of unrevoked client grants and tokens. It does not infer online client status or display fabricated financial results. Empty accounts do not fetch ledger preferences or show manual onboarding fields.
- Normal browser login now returns to `/account/`; signed MCP authorization handoffs retain `/connect`. Existing consent/default-research and Agent setup behavior is preserved. The local callback/handoff control-plane suite passed 27 tests.
- Added no-store/no-transform, CSP and noindex protections for the new static account entry. It uses the portal build without analytics. Added a header/menu entry and valid overview/settings URLs with browser-back support.
- The new personal-home browser test passed empty/configured states, grant/token counts, read-only access, URL/back behavior, error recovery, 320/390/768/1024/1440px EN/ZH card geometry, no analytics/external requests and private-page CSP. Existing portal/UI consistency tests and complete site/portal builds passed; lint has only the existing Fast Refresh warning. New checks are included in the site deployment workflow.
- Standalone synthetic review: `/home/ubuntu/.codex/artifacts/openinvest/personal-home-local-2026-11-01/preview.html#overview`. The configured example is `#overview-ready`. These fixtures are synthetic; no production account ledger, Auth0 tenant setting or production service was changed. No commit, push or deployment was performed.
