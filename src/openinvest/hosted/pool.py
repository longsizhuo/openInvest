"""Bounded Docker workers with private loopback ports and per-user named volumes."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import secrets
import time
import uuid

import httpx


class Busy(RuntimeError):
    pass


@dataclass
class Worker:
    container: object
    origin: str
    token: str
    active: int = 0
    last_used: float = 0


class DockerPool:
    def __init__(self, settings, client=None):
        import docker
        self.settings = settings
        self.client = client or docker.from_env()
        self.entries = {}
        self.lock = asyncio.Lock()

    @staticmethod
    def name(owner):
        # Identity originates exclusively in the control database, not in RPC arguments.
        return "oi-ledger-" + uuid.UUID(hex=owner).hex

    async def _attach(self, owner):
        import docker
        name = self.name(owner)
        created = False
        try:
            container = await asyncio.to_thread(self.client.containers.get, name)
            await asyncio.to_thread(container.reload)
            if container.labels.get("openinvest.owner") != owner or container.labels.get("openinvest.hosted") != "ledger":
                raise Busy("Unexpected worker name collision")
            if container.status != "running":
                await asyncio.to_thread(container.remove, force=True)
                raise docker.errors.NotFound("stopped")
            env = dict(item.split("=", 1) for item in container.attrs["Config"]["Env"] if "=" in item)
            token = env["INVEST_API_TOKEN"]
        except docker.errors.NotFound:
            # A failed/unattached container still counts toward the hard host limit.
            existing = await asyncio.to_thread(self.client.containers.list, all=True, filters={"label": "openinvest.hosted=ledger"})
            if len(existing) >= self.settings.workers:
                raise Busy("Worker capacity requires inspection")
            token = secrets.token_urlsafe(32)
            container = await asyncio.to_thread(self.client.containers.run,
                self.settings.image, name=name, detach=True, read_only=True,
                cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
                user="10001:10001", mem_limit="1g", nano_cpus=1_000_000_000, pids_limit=128,
                ports={"8766/tcp": ("127.0.0.1", None)},
                volumes={name: {"bind": "/data", "mode": "rw"}},
                tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
                labels={"openinvest.hosted": "ledger", "openinvest.owner": owner},
                environment={"INVEST_HOME": "/data", "INVEST_HOSTED_WORKER": "1", "INVEST_API_TOKEN": token,
                             "INVEST_REASONING_BY": "agent", "INVEST_EMBEDDED_SCHEDULER": "0",
                             "HOME": "/data", "XDG_CACHE_HOME": "/data/.cache", "PYTHONDONTWRITEBYTECODE": "1"})
            created = True
        for _ in range(150):
            await asyncio.to_thread(container.reload)
            ports = container.attrs["NetworkSettings"]["Ports"].get("8766/tcp")
            if ports:
                origin = "http://127.0.0.1:" + ports[0]["HostPort"]
                try:
                    async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
                        response = await client.get(origin+"/internal/ready", headers={"Authorization": "Bearer "+token})
                    if response.status_code == 200:
                        return Worker(container, origin, token, last_used=time.monotonic())
                except httpx.HTTPError:
                    pass
            await asyncio.sleep(.1)
        if created:
            await asyncio.to_thread(container.remove, force=True)
        raise Busy("Worker did not become ready")

    async def _remove(self, owner):
        item = self.entries[owner]
        if item.active:
            return False
        # An old gateway might have left an in-flight worker request after a restart.
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                status = await client.get(item.origin+"/internal/ready", headers={"Authorization": "Bearer "+item.token})
            if status.json().get("active"):
                return False
        except (httpx.HTTPError, ValueError):
            return False
        await asyncio.to_thread(item.container.stop, timeout=10)
        await asyncio.to_thread(item.container.remove)
        del self.entries[owner]
        return True

    async def recover(self):
        containers = await asyncio.to_thread(self.client.containers.list, all=True, filters={"label": "openinvest.hosted=ledger"})
        if len(containers) > self.settings.workers:
            raise Busy("More workers exist than the configured limit; inspect before startup")
        for container in containers:
            owner = container.labels.get("openinvest.owner", "")
            self.entries[owner] = await self._attach(owner)

    @asynccontextmanager
    async def lease(self, owner):
        async with self.lock:
            if owner in self.entries and self.entries[owner].active:
                raise Busy("This account already has a running request")
            if owner not in self.entries:
                if len(self.entries) >= self.settings.workers:
                    for old in sorted(self.entries, key=lambda k: self.entries[k].last_used):
                        if await self._remove(old):
                            break
                    else:
                        raise Busy("All workers are busy")
                self.entries[owner] = await self._attach(owner)
            item = self.entries[owner]
            item.active += 1
        try:
            yield item
        finally:
            item.active -= 1
            item.last_used = time.monotonic()

    async def reap(self):
        async with self.lock:
            for owner, item in list(self.entries.items()):
                if not item.active and time.monotonic()-item.last_used > self.settings.idle_seconds:
                    await self._remove(owner)

    async def internal(self, owner, path, payload=None, raw=None):
        async with self.lease(owner) as item, httpx.AsyncClient(trust_env=False, timeout=120) as client:
            result = await client.request("POST" if payload is not None or raw is not None else "GET", item.origin+"/internal/"+path,
                json=payload, content=raw, headers={"Authorization": "Bearer "+item.token})
            if result.is_error:
                raise ValueError("Worker operation rejected")
            return result.content

    async def destroy(self, owner):
        async with self.lock:
            if owner in self.entries and not await self._remove(owner):
                raise Busy("Account is busy")
            name = self.name(owner)
            import docker
            try:
                volume = await asyncio.to_thread(self.client.volumes.get, name)
                await asyncio.to_thread(volume.remove)
            except docker.errors.NotFound:
                pass


    async def resources(self):
        result = []
        for item in list(self.entries.values()):
            try:
                stats = await asyncio.to_thread(item.container.stats, stream=False)
                await asyncio.to_thread(item.container.reload)
                result.append({"memory_bytes": stats.get("memory_stats", {}).get("usage", 0),
                               "memory_limit": stats.get("memory_stats", {}).get("limit", 0),
                               "oom_killed": bool(item.container.attrs.get("State", {}).get("OOMKilled")),
                               "active": item.active})
            except Exception:
                result.append({"error": "worker_stats_unavailable"})
        return result
