"""Browser-only, explicit account linking with fresh authentication on both sides."""
import time

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from .identities import provider_for
from .pool import Busy
from .store import Conflict


CONNECTIONS = {"google": "google-oauth2", "github": "github"}


def register_identity_routes(app, settings, store, browser, oauth, session_response, pool, research):
    async def authenticate(request, provider):
        if provider not in CONNECTIONS:
            raise Conflict("unsupported_login_provider")
        # Authlib owns PKCE, state, nonce, issuer, audience and ID-token verification.
        return await oauth.auth0.authorize_redirect(request, settings.origin+"/auth/callback",
            connection=CONNECTIONS[provider], prompt="login", max_age=0)

    def active_flow(request, credential):
        flow = store.identity_flow(request.session.get("identity_flow", ""), credential["id"])
        if not flow or flow["owner"] != credential["owner"]:
            raise Conflict("login_verification_expired")
        return flow

    @app.post("/api/me/login-methods")
    async def begin(request: Request):
        account, credential = browser(request, mutation=True)
        data = await request.json()
        flow = store.start_identity_flow(account["id"], credential["id"], credential["session_subject"], data.get("provider"))
        request.session["identity_flow"] = flow
        response = await authenticate(request, provider_for(credential["session_subject"]))
        return {"redirect": response.headers["location"]}

    async def callback(request, userinfo):
        _account, credential = browser(request)
        flow = active_flow(request, credential)
        store.verify_identity_flow(flow["id"], credential["id"], userinfo["sub"], userinfo.get("auth_time"))
        if flow["phase"] == "primary":
            return await authenticate(request, flow["provider"])
        return RedirectResponse("/account/?view=settings", status_code=303)

    @app.delete("/api/me/login-methods/pending")
    async def cancel(request: Request):
        account, credential = browser(request, mutation=True)
        with store.db() as db:
            db.execute("DELETE FROM identity_flows WHERE owner=? AND credential=?", (account["id"], credential["id"]))
        request.session.pop("identity_flow", None)
        return {"cancelled": True}

    @app.post("/api/me/login-methods/confirm")
    async def confirm(request: Request):
        account, credential = browser(request, mutation=True)
        flow = active_flow(request, credential)
        data = await request.json()
        if data.get("keep_current_ledger") is not True:
            raise HTTPException(422, "Confirm the ledger to retain")
        # A short admission barrier lets in-flight writes finish before ownership changes.
        # Never stop/destroy source volumes: they remain exportable archived ledgers.
        app.state.backup_in_progress = True
        try:
            if app.state.admitted_requests > 1 or any(w.active for w in getattr(pool, "entries", {}).values()):
                raise Busy()
            owner, subject = store.finish_identity_flow(flow["id"], credential["id"])
            for archived in store.archived_accounts(owner):
                with store.db() as db:
                    participant = db.execute("SELECT participant FROM accounts WHERE id=?", (archived["id"],)).fetchone()[0]
                research.cancel_pending(participant)
            request.session.clear()
            return session_response(store.account(owner), "/account/?view=settings", subject=subject, json_response=True, authenticated_at=flow["started"])
        finally:
            app.state.backup_in_progress = False

    @app.delete("/api/me/login-methods/{identity_id}")
    async def unlink(identity_id: str, request: Request):
        account, credential = browser(request, mutation=True)
        if time.time()-credential["authenticated_at"] > 600:
            raise Conflict("recent_login_required")
        store.unlink_identity(account["id"], identity_id, credential["session_subject"])
        request.session.pop("identity_flow", None)
        return {"unlinked": True}

    @app.get("/api/me/archives/{archived_id}/export")
    async def export_archive(archived_id: str, request: Request):
        account, _credential = browser(request)
        if archived_id not in {item["id"] for item in store.archived_accounts(account["id"])}:
            raise HTTPException(404, "Archived ledger not found")
        body = await pool.internal(archived_id, "export")
        return Response(body, media_type="application/gzip", headers={"Content-Disposition": 'attachment; filename="openinvest-archived-ledger.tar.gz"'})

    return callback


def identity_summary(store, account, credential, flow_id):
    binding = store.identity_binding(credential["session_subject"])
    methods = store.login_methods(account["id"])
    for method in methods:
        method["current"] = bool(binding and method["id"] == binding["id"])
    flow = store.identity_flow(flow_id or "", credential["id"])
    pending = None
    if flow and flow["phase"] == "confirm":
        source = store.account(flow["source_owner"]) if flow["source_owner"] else None
        pending = {"provider": flow["provider"], "existing_account": source is not None,
                   "source_initialized": bool(source and source["initialized"])}
    return {"login_methods": methods, "pending_login_link": pending,
            "archived_ledgers": store.archived_accounts(account["id"])}
