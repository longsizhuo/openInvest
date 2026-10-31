"""One process/container, one ledger. Only a private gateway credential can reach it."""
import asyncio
from contextlib import asynccontextmanager, closing
import io
import json
import os
import tarfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route


class Initialization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    risk: str = Field(default="Balanced", pattern=r"^(Conservative|Balanced|Aggressive)$")
    symbols: list[str] = Field(min_length=1, max_length=50)


def initialize(data):
    from openinvest.paths import INVEST_ROOT
    from openinvest.core.memory_store import MemoryStore
    from openinvest.core.schemas import validate_portfolio, validate_strategy
    from openinvest.services.symbol_map import normalize_symbol
    import re
    import yaml
    spec = Initialization.model_validate(data)
    store = MemoryStore()
    marker = INVEST_ROOT / ".hosted-initialization.json"
    expected = spec.model_dump()
    resuming = marker.exists() and json.loads(marker.read_text()) == expected
    if not resuming and (marker.exists() or any(store.path_of(n).exists() for n in ("user", "strategy", "portfolio"))):
        raise ValueError("Ledger already initialized; import/reset is not implicit")
    symbols = [normalize_symbol(s) for s in spec.symbols]
    if any(not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^=:_-]{0,39}", s) or re.fullmatch(r"\d{6}", s) for s in symbols):
        raise ValueError("Use explicit market symbols")
    targets = [{"symbol": s, "max_single_invest_cny": 0} for s in symbols]
    strategy = {"target_allocation_stock": .7, "target_allocation_cash": .3, "target_assets": targets}
    portfolio = {"schema_version": 2, "cash": {spec.currency: 0.0}, "holdings": []}
    validate_strategy(strategy)
    validate_portfolio(portfolio)
    # Validate every document before writing; no external model parses onboarding.
    config = INVEST_ROOT / "config.yaml"
    if config.exists() and not resuming:
        raise ValueError("Existing configuration cannot be overwritten")
    if not marker.exists():
        from openinvest.core.memory_store import _atomic_write_text
        _atomic_write_text(marker, json.dumps(expected))
    # Retrying the same onboarding after interruption fills missing documents only.
    if all(store.path_of(n).exists() for n in ("user", "strategy", "portfolio")) and config.exists():
        return {"initialized": True}
    config.write_text(yaml.safe_dump({"portfolio": {"base_currency": spec.currency}}), encoding="utf-8", newline="\n")
    if not store.path_of("user").exists():
        store.write("user", "user", {"display_name": "Investor", "risk_tolerance": spec.risk}, "")
    if not store.path_of("strategy").exists():
        store.write("strategy", "strategy", strategy, "")
    if not store.path_of("portfolio").exists():
        store.write("portfolio", "portfolio", portfolio, "")
    from openinvest.core.config import reset_config
    reset_config()
    return {"initialized": True}


def snapshot():
    """Caller excludes other requests while the SQLite-aware snapshot is assembled."""
    from openinvest.paths import INVEST_ROOT
    import sqlite3
    import tempfile
    output = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path in sorted(INVEST_ROOT.rglob("*")):
            relative = path.relative_to(INVEST_ROOT)
            if not path.is_file() or path.is_symlink() or path.name.endswith(("-wal", "-shm", ".lock")):
                continue
            if any(part in {".cache", "cache_data", "logs"} for part in relative.parts) or path.name == ".env":
                continue
            with path.open("rb") as source:
                magic = source.read(16)
            if magic.startswith(b"SQLite format 3"):
                target = Path(tmp)/str(len(archive.getmembers()))
                with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as source, closing(sqlite3.connect(target)) as dest:
                    source.backup(dest)
                    dest.execute("PRAGMA journal_mode=DELETE")
                archive.add(target, arcname=str(relative), recursive=False)
            else:
                archive.add(path, arcname=str(relative), recursive=False)
    return output.getvalue()


def create_worker():
    if os.getenv("INVEST_HOSTED_WORKER") != "1" or not os.getenv("INVEST_API_TOKEN"):
        raise ValueError("Worker requires explicit isolated configuration")
    from openinvest.connectors.mcp_server import mcp, _configure_http_settings, _BearerAuthMiddleware
    # Middleware protects internal routes too; the public gateway never forwards paths/headers.
    _configure_http_settings("127.0.0.1", 8766)
    inner = mcp.streamable_http_app()
    lock = asyncio.Lock()
    state = {"active": 0}

    async def initialize_route(request):
        async with lock:
            try:
                result = await asyncio.to_thread(initialize, await request.json())
                return JSONResponse(result)
            except Exception:
                return JSONResponse({"error": "Invalid initialization or ledger already exists"}, status_code=409)

    async def export_route(_request):
        async with lock:
            return Response(await asyncio.to_thread(snapshot), media_type="application/gzip")

    async def cancel_route(_request):
        from openinvest.hosted.analysis import cancel
        async with lock:
            try:
                await asyncio.to_thread(cancel)
                return JSONResponse({"cancelled": True})
            except ValueError:
                return JSONResponse({"error": "Submission active"}, status_code=409)

    async def ready(_request):
        return JSONResponse({"active": state["active"]})

    async def evidence(request):
        from .analysis import evidence_for
        async with lock:
            try:
                return JSONResponse(await asyncio.to_thread(evidence_for, request.path_params["analysis_id"]))
            except ValueError:
                return JSONResponse({"error": "Unknown analysis"}, status_code=404)

    async def restore_route(request):
        from openinvest.paths import INVEST_ROOT
        async with lock:
            if any((INVEST_ROOT/"memory").glob("*.md")):
                return JSONResponse({"error": "Restore requires an empty ledger volume"}, status_code=409)
            data = await request.body()
            try:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                    members = archive.getmembers()
                    if sum(m.size for m in members) > 512*1024*1024 or len(members) > 50000:
                        raise ValueError()
                    for member in members:
                        path = Path(member.name)
                        if not member.isfile() or path.is_absolute() or ".." in path.parts or path.name == ".env":
                            raise ValueError()
                        target = INVEST_ROOT/path
                        if any(parent.is_symlink() for parent in [target, *target.parents] if parent == INVEST_ROOT or INVEST_ROOT in parent.parents):
                            raise ValueError()
                    for member in members:
                        target = INVEST_ROOT/member.name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(archive.extractfile(member).read())
            except Exception:
                return JSONResponse({"error": "Invalid backup"}, status_code=422)
            return JSONResponse({"restored": True})

    @asynccontextmanager
    async def lifespan(_app):
        async with inner.router.lifespan_context(inner):
            yield

    app = Starlette(routes=[Route("/internal/initialize", initialize_route, methods=["POST"]),
                            Route("/internal/export", export_route),
                            Route("/internal/cancel", cancel_route, methods=["POST"]),
                            Route("/internal/restore", restore_route, methods=["POST"]),
                            Route("/internal/ready", ready), Route("/internal/evidence/{analysis_id}", evidence), Mount("/", app=inner)], lifespan=lifespan)
    app.add_middleware(_BearerAuthMiddleware)

    class Serialized:
        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or scope["path"] == "/internal/ready":
                return await app(scope, receive, send)
            # Serialize snapshot and tool requests. No snapshot can observe a torn ledger.
            if scope["path"].startswith("/internal/"):
                return await app(scope, receive, send)
            async with lock:
                state["active"] += 1
                try:
                    await app(scope, receive, send)
                finally:
                    state["active"] -= 1
    return Serialized()


def main():
    import uvicorn
    from .safelog import configure
    configure()
    uvicorn.run(create_worker(), host="0.0.0.0", port=8766, access_log=False, log_config=None)


if __name__ == "__main__":
    main()
