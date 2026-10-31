"""Hosted gateway + browser-only account APIs. No financial state in the gateway environment."""
import asyncio
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import secrets
import time

import httpx
import jwt
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from .auth import Authenticator, AuthenticationError, resource_metadata
from .config import Settings
from .policy import ALLOWED, MONEY, scope_for
from .pool import DockerPool, Busy
from .research import COSArchive, Research
from .store import Store, Conflict

log = logging.getLogger(__name__)
MAX_BODY = 2 * 1024 * 1024


def rpc_message(body):
    """Extract final JSON-RPC response from JSON or SSE without buffering the live stream."""
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        last = None
        for event in body.decode(errors="replace").replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
            try:
                value = json.loads(data)
                if isinstance(value, dict) and ("result" in value or "error" in value):
                    last = value
            except ValueError:
                pass
        return last


def business_result(message):
    result = message.get("result", {})
    if not isinstance(result, dict):
        return {}
    if isinstance(result.get("structuredContent"), dict):
        value = result["structuredContent"]
        return value["result"] if set(value) == {"result"} and isinstance(value["result"], dict) else value
    for item in result.get("content", []):
        if item.get("type") == "text":
            try:
                value = json.loads(item["text"])
                if isinstance(value, dict):
                    return value["result"] if set(value) == {"result"} and isinstance(value["result"], dict) else value
            except ValueError:
                pass
    return {}


def create_app(settings=None, *, store=None, pool=None, authenticator=None, archive=None, transport=None):
    settings = settings or Settings.from_env()
    store = store or Store(settings.root, settings.encryption_key)
    research = Research(settings, store)
    pool = pool or DockerPool(settings)
    auth = authenticator or Authenticator(settings, store)
    oauth = OAuth()
    oauth.register("auth0", client_id=settings.client_id, client_secret=settings.client_secret,
                   server_metadata_url=settings.issuer+".well-known/openid-configuration",
                   client_kwargs={"scope": "openid profile", "code_challenge_method": "S256"})
    if archive is None and settings.cos_bucket:
        archive = COSArchive(settings)
    stopping = asyncio.Event()

    async def maintenance():
        while not stopping.is_set():
            try:
                await pool.reap()
                if archive:
                    await asyncio.to_thread(research.flush, archive)
            except Exception:
                store.metric("maintenance_failed")
                log.warning("Hosted maintenance failed; payload omitted")
            try:
                await asyncio.wait_for(stopping.wait(), timeout=30)
            except TimeoutError:
                pass

    @asynccontextmanager
    async def lifespan(_app):
        if hasattr(pool, "recover"):
            await pool.recover()
        task = asyncio.create_task(maintenance())
        yield
        stopping.set()
        await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                       session_cookie="oi_oauth_state", https_only=True, same_site="lax", max_age=900)
    app.state.store, app.state.research, app.state.pool, app.state.oauth = store, research, pool, oauth
    app.state.backup_in_progress = False
    app.state.admitted_requests = 0

    @app.middleware("http")
    async def headers_and_bounds(request, call_next):
        # Read size-bounded requests, including chunked bodies, before JSON decoding.
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    return JSONResponse({"error": "Request too large"}, status_code=413)
            request._body = bytes(body)
        store.metric("http_requests")
        try:
            response = await call_next(request)
        except Exception:
            store.metric("request_failed")
            log.warning("Hosted request failed; request/exception content omitted")
            response = JSONResponse({"error": "Service unavailable"}, status_code=503)
        if response.status_code >= 400:
            store.metric("http_errors")
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.middleware("http")
    async def admission(request, call_next):
        guarded = request.url.path == "/mcp" or request.url.path == "/operator/restore" or (
            request.url.path.startswith("/api/me") and request.method != "GET")
        if not guarded:
            return await call_next(request)
        if app.state.backup_in_progress:
            return JSONResponse({"error": "Brief backup maintenance"}, status_code=503, headers={"Retry-After": "5"})
        # Includes authentication/body-read time before a worker lease exists.
        app.state.admitted_requests += 1
        try:
            return await call_next(request)
        finally:
            app.state.admitted_requests -= 1

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _error):
        return JSONResponse({"error": "Invalid input"}, status_code=422)

    @app.exception_handler(Conflict)
    async def conflict_error(_request, error):
        return JSONResponse({"error": str(error)}, status_code=409)

    @app.exception_handler(Busy)
    async def busy_error(_request, _error):
        return JSONResponse({"error": "Capacity busy; retry later"}, status_code=503, headers={"Retry-After": "5"})

    def browser(request, *, mutation=False):
        credential = store.authenticate(request.cookies.get("oi_session", ""), "session")
        if not credential:
            raise HTTPException(401, "Sign in required")
        if mutation and (request.headers.get("origin") != settings.origin or
                         not secrets.compare_digest(request.headers.get("x-csrf-token", ""), credential["csrf"])):
            raise HTTPException(403, "Invalid browser request")
        return store.account(credential["owner"]), credential

    def identity(subject):
        if not settings.registration_open:
            with store.db() as db:
                exists = db.execute("SELECT 1 FROM accounts WHERE subject=? AND deleted=0", (subject,)).fetchone()
            if not exists:
                raise HTTPException(503, "Registration is not open yet")
        return store.identity(subject)

    def session_response(account, location="/connect"):
        token, _credential = store.credential(account["id"], "session", ttl=12*3600)
        response = RedirectResponse(location, status_code=303)
        response.set_cookie("oi_session", token, secure=True, httponly=True, samesite="lax", max_age=12*3600, path="/")
        return response

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    async def metadata():
        return resource_metadata(settings)

    @app.get("/auth/login")
    async def login(request: Request, provider: str):
        connection = {"google": "google-oauth2", "github": "github"}.get(provider)
        if not connection:
            raise HTTPException(400, "Unsupported login provider")
        return await oauth.auth0.authorize_redirect(request, settings.origin+"/auth/callback", connection=connection)

    @app.get("/auth/callback")
    async def callback(request: Request):
        try:
            token = await oauth.auth0.authorize_access_token(request)
            subject = token["userinfo"]["sub"]
        except Exception:
            raise HTTPException(401, "Login could not be verified") from None
        return session_response(identity(subject))

    @app.get("/auth/action")
    async def action(request: Request, session_token: str, state: str):
        # Auth0 post-login redirect action brings MCP users to the same browser portal.
        try:
            claims = jwt.decode(session_token, settings.action_secret, algorithms=["HS256"],
                                audience=settings.origin, options={"require": ["exp", "sub", "client_id", "jti"]})
            if len(state) > 4096:
                raise ValueError()
            store.ticket_once(session_token)
            account = identity(claims["sub"])
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(401, "Invalid authorization handoff") from None
        request.session["pending"] = {"owner": account["id"], "client": claims["client_id"], "state": state}
        return session_response(account)

    @app.get("/api/me")
    async def me(request: Request):
        account, credential = browser(request)
        return {"initialized": bool(account["initialized"]), "policy_version": settings.policy_version,
                "accepted": account["accepted"] == settings.policy_version, "research_enabled": bool(account["research"]) if account["accepted"] else True,
                "csrf": credential["csrf"], "mcp_url": settings.origin+"/mcp",
                "pending_authorization": request.session.get("pending", {}).get("owner") == account["id"],
                "credentials": store.credentials(account["id"]), "connections": store.connections(account["id"])}

    @app.post("/api/me/initialize")
    async def initialize_account(request: Request):
        account, _credential = browser(request, mutation=True)
        data = await request.json()
        if data.get("policy_version") != settings.policy_version or not isinstance(data.get("research_enabled"), bool):
            raise HTTPException(422, "Review the current account settings")
        if not account["initialized"]:
            from .worker import Initialization
            try:
                spec = Initialization.model_validate({k: data[k] for k in ("currency", "risk", "symbols") if k in data})
            except ValueError:
                raise HTTPException(422, "Invalid ledger settings") from None
            await pool.internal(account["id"], "initialize", spec.model_dump())
            store.initialized(account["id"])
        store.preferences(account["id"], data["research_enabled"], settings.policy_version)
        if not data["research_enabled"]:
            research.cancel_pending(account["participant"])
        return {"initialized": True}

    @app.patch("/api/me/research")
    async def research_preferences(request: Request):
        account, _credential = browser(request, mutation=True)
        data = await request.json()
        if not isinstance(data.get("enabled"), bool) or not account["accepted"]:
            raise HTTPException(422, "Invalid setting")
        store.preferences(account["id"], data["enabled"])
        if not data["enabled"]:
            research.cancel_pending(account["participant"])
        return {"enabled": data["enabled"]}

    @app.post("/api/me/authorize")
    async def authorize(request: Request):
        account, _credential = browser(request, mutation=True)
        pending = request.session.get("pending", {})
        if not account["initialized"] or account["accepted"] != settings.policy_version or pending.get("owner") != account["id"]:
            raise HTTPException(409, "Complete account setup first")
        store.connect_oauth(account["id"], pending["client"], authorize=True)
        resume = jwt.encode({"sub": account["subject"], "client_id": pending["client"], "approved": True,
                             "aud": settings.origin, "iss": settings.origin, "state": pending["state"], "iat": int(time.time()), "exp": int(time.time())+60}, settings.action_secret, algorithm="HS256")
        from urllib.parse import urlencode
        result = settings.issuer+"continue?"+urlencode({"state": pending["state"], "session_token": resume})
        request.session.pop("pending", None)
        return {"redirect": result}

    @app.post("/api/me/credentials")
    async def credentials(request: Request):
        account, _credential = browser(request, mutation=True)
        if not account["initialized"] or account["accepted"] != settings.policy_version:
            raise HTTPException(409, "Complete account setup first")
        token, item = store.credential(account["id"])
        return {"id": item["id"], "token": token}

    @app.delete("/api/me/credentials/{credential_id}")
    async def revoke_credential(credential_id: str, request: Request):
        account, _credential = browser(request, mutation=True)
        store.revoke(account["id"], credential_id)
        return {"revoked": True}

    @app.delete("/api/me/connections/{client_id}")
    async def revoke_connection(client_id: str, request: Request):
        account, _credential = browser(request, mutation=True)
        store.revoke_connection(account["id"], client_id)
        return {"revoked": True}

    @app.delete("/api/me/ledger")
    async def reset_ledger(request: Request):
        account, _credential = browser(request, mutation=True)
        await pool.destroy(account["id"])
        store.reset_ledger(account["id"])
        return {"deleted": True}

    @app.post("/api/me/revoke-connections")
    async def revoke_connections(request: Request):
        account, _credential = browser(request, mutation=True)
        store.revoke_all(account["id"])
        return {"revoked": True}

    @app.get("/api/me/export")
    async def export_account(request: Request):
        account, _credential = browser(request)
        body = await pool.internal(account["id"], "export")
        return Response(body, media_type="application/gzip", headers={"Content-Disposition": 'attachment; filename="openinvest-ledger.tar.gz"'})

    @app.post("/api/me/cancel-analysis")
    async def cancel_analysis(request: Request):
        account, _credential = browser(request, mutation=True)
        await pool.internal(account["id"], "cancel", {})
        return {"cancelled": True}

    @app.delete("/api/me/research/unused")
    async def delete_research(request: Request):
        account, _credential = browser(request, mutation=True)
        count = await asyncio.to_thread(research.delete_unused, account["participant"], archive)
        return {"deleted": count}

    @app.delete("/api/me")
    async def delete_account(request: Request):
        account, _credential = browser(request, mutation=True)
        # A busy worker rejects deletion rather than interrupting an active operation.
        await pool.destroy(account["id"])
        store.delete(account["id"])
        research.cancel_pending(account["participant"])
        response = JSONResponse({"deleted": True})
        response.delete_cookie("oi_session", path="/")
        return response

    @app.post("/auth/logout")
    async def logout(request: Request):
        account, credential = browser(request, mutation=True)
        store.revoke(account["id"], credential["id"])
        response = JSONResponse({"signed_out": True})
        response.delete_cookie("oi_session", path="/")
        return response

    def operator(request):
        if not settings.admin_token or not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer "+settings.admin_token):
            raise HTTPException(401, "Operator authentication required")

    @app.get("/operator/status")
    async def operator_status(request: Request):
        operator(request)
        with store.db() as db:
            metrics = {r[0]: r[1] for r in db.execute("SELECT name,value FROM metrics")}
        with research.db() as db:
            queue = db.execute("SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM samples WHERE status='pending'").fetchone()
        import shutil
        return {"metrics": metrics, "queued_samples": queue[0], "queued_bytes": queue[1],
                "disk_free_bytes": shutil.disk_usage(settings.root).free,
                "workers": len(pool.entries) if hasattr(pool, "entries") else 0,
                "worker_resources": await pool.resources() if hasattr(pool, "resources") else [],
                "http_error_rate": metrics.get("http_errors", 0)/max(1, metrics.get("http_requests", 0)),
                "registration_open": settings.registration_open, "archive_configured": archive is not None}

    @app.post("/operator/backup")
    async def backup(request: Request):
        operator(request)
        if app.state.backup_in_progress:
            raise Busy()
        app.state.backup_in_progress = True
        try:
            if app.state.admitted_requests or any(w.active for w in getattr(pool, "entries", {}).values()):
                raise Busy()
            from .backup import backup_all
            result = await backup_all(settings, store, research, pool, archive)
            store.metric("backup_success")
            store.gauge("last_backup_unix", int(time.time()))
            return result
        except Exception:
            store.metric("backup_failed")
            raise
        finally:
            app.state.backup_in_progress = False

    @app.post("/operator/restore")
    async def restore(request: Request):
        operator(request)
        from .backup import unseal
        import uuid
        data = await request.json()
        owner = uuid.UUID(hex=data["owner"]).hex
        stamp = str(data["backup"])
        if not stamp.isdigit() or not store.account(owner) or archive is None:
            raise HTTPException(422, "Invalid recovery source")
        blob = await asyncio.to_thread(archive.get, f"backups/{owner}/{stamp}.aesgcm")
        content = unseal(settings, blob, kind="ledger", owner=owner)
        await pool.internal(owner, "restore", raw=content)
        return {"restored": True}

    @app.post("/mcp")
    async def mcp_route(request: Request):
        challenge = {"WWW-Authenticate": f'Bearer resource_metadata="{settings.origin}/.well-known/oauth-protected-resource/mcp"'}
        try:
            account, scopes = await asyncio.to_thread(auth.bearer, request.headers.get("authorization", ""))
        except AuthenticationError:
            return JSONResponse({"error": "Authentication required"}, status_code=401, headers=challenge)
        if not account["initialized"] or account["accepted"] != settings.policy_version:
            return JSONResponse({"error": "Account setup required"}, status_code=403, headers=challenge)
        try:
            message = await request.json()
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError()
            method = message["method"]
            if method not in {"initialize", "notifications/initialized", "notifications/cancelled", "ping", "tools/list", "tools/call"}:
                raise ValueError()
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise ValueError()
            if method == "initialize":
                from .research import structured
                info = params.get("clientInfo", {})
                if isinstance(info, dict):
                    store.client_info(account["id"], request.headers["authorization"], structured({"client_name":info.get("name"), "client_version":info.get("version")}))
            client_info = store.client_info(account["id"], request.headers["authorization"])
            name = params.get("name") if method == "tools/call" else None
            arguments = params.get("arguments", {}) if name else {}
            if not isinstance(arguments, dict):
                raise ValueError()
            if name and (name not in ALLOWED or scope_for(name) not in scopes):
                raise HTTPException(403, "Tool is unavailable or permission is missing")
            if name and not store.rate(account["id"]):
                return JSONResponse({"error": "Rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"})
            key = arguments.get("idempotency_key") if name in MONEY else None
            if name in MONEY and (not isinstance(key, str) or not 1 <= len(key) <= 128):
                raise HTTPException(422, "idempotency_key is required")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(400, "Invalid MCP request") from None

        # Lease before reserving: capacity failures have no financial side effect.
        lease = pool.lease(account["id"])
        item = await lease.__aenter__()
        client = httpx.AsyncClient(transport=transport, trust_env=False, timeout=httpx.Timeout(1200, connect=10))
        try:
            current = store.account(account["id"])
            if not current or not current["initialized"] or current["accepted"] != settings.policy_version:
                raise HTTPException(403, "Account setup required")
            if key:
                cached = store.reserve(account["id"], key, name, arguments)
                if cached is not None:
                    await client.aclose()
                    await lease.__aexit__(None, None, None)
                    await asyncio.to_thread(research.capture, account, name, arguments, business_result(cached), 0,
                                            failed=bool(cached.get("error")), retry=True, client_info=client_info)
                    return JSONResponse({"jsonrpc": "2.0", "id": message.get("id"), **cached})
            start = time.perf_counter()
            headers = {"Authorization": "Bearer "+item.token, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            if request.headers.get("mcp-protocol-version"):
                headers["MCP-Protocol-Version"] = request.headers["mcp-protocol-version"]
            upstream = await client.send(client.build_request("POST", item.origin+"/mcp", json=message, headers=headers), stream=True)
        except BaseException:
            await client.aclose()
            await lease.__aexit__(None, None, None)
            raise

        async def stream():
            buffer, oversized, final_chunk = bytearray(), False, None
            try:
                async for chunk in upstream.aiter_bytes():
                    if len(buffer)+len(chunk) <= MAX_BODY:
                        buffer.extend(chunk)
                    else:
                        oversized = True
                    candidate = rpc_message(bytes(buffer)) if not oversized else None
                    if isinstance(candidate, dict) and candidate.get("id") == message.get("id") and ("result" in candidate or "error" in candidate):
                        # Persist the receipt and release the lease before the client
                        # sees completion and immediately sends its next request.
                        final_chunk = chunk
                        break
                    yield chunk
                reply = rpc_message(bytes(buffer)) if not oversized else None
                if reply and isinstance(reply, dict):
                    if key and ("result" in reply or "error" in reply):
                        store.complete(account["id"], key, {k: reply[k] for k in ("result", "error") if k in reply})
                    if name:
                        result = business_result(reply)
                        failed = bool(reply.get("error") or reply.get("result", {}).get("isError"))
                        if name in {"prepare_committee", "submit_committee"} and not failed:
                            analysis_id = result.get("analysis_id") or arguments.get("analysis_id")
                            if isinstance(analysis_id, str) and len(analysis_id) == 32:
                                try:
                                    evidence_response = await client.get(item.origin+"/internal/evidence/"+analysis_id, headers={"Authorization": "Bearer "+item.token})
                                    evidence_response.raise_for_status()
                                    result = {**result, "research_evidence": evidence_response.json()}
                                except Exception:
                                    store.metric("research_evidence_unavailable")
                        await asyncio.to_thread(research.capture, account, name, arguments, result,
                                                int((time.perf_counter()-start)*1000), failed=failed, client_info=client_info)
                elif name:
                    store.metric("response_capture_incomplete")
            except Exception:
                store.metric("stream_failed")
                log.warning("MCP stream interrupted; operation is not replayed")
                raise
            finally:
                await upstream.aclose()
                await client.aclose()
                await lease.__aexit__(None, None, None)
            if final_chunk is not None:
                yield final_chunk
        return StreamingResponse(stream(), status_code=upstream.status_code,
                                 media_type=upstream.headers.get("content-type", "application/json"))

    if settings.portal:
        app.mount("/", StaticFiles(directory=settings.portal, html=True), name="portal")
    return app
