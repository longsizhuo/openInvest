"""Opt-in real TCP/Caddy long-stream check, with synthetic workers and identities."""
import os
import pytest
pytest.importorskip('authlib')


@pytest.mark.skipif(os.getenv('HOSTED_STREAM_TEST') != '1', reason='130-second local proxy acceptance')
def test_caddy_130_second_stream_and_second_user(tmp_path):
    import asyncio
    from contextlib import asynccontextmanager
    import json
    import socket
    import threading
    import time
    from types import SimpleNamespace
    import uuid
    import docker
    import httpx
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    from openinvest.hosted.app import create_app
    from openinvest.hosted.config import Settings
    from openinvest.hosted.store import Store

    def port():
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]
    worker_port, gateway_port, proxy_port = port(), port(), port()
    worker = FastAPI()

    @worker.post('/mcp')
    async def reply(request: Request):
        message = await request.json()
        async def chunks():
            if message['params']['name'] == 'prepare_committee':
                for _ in range(130):
                    yield b': keepalive\n\n'
                    await asyncio.sleep(1)
            yield ('event: message\ndata: '+json.dumps({'jsonrpc':'2.0','id':message['id'],
                'result':{'content':[{'type':'text','text':'{"status":"ok"}'}]}})+'\n\n').encode()
        return StreamingResponse(chunks(), media_type='text/event-stream')

    class Pool:
        entries = {}
        @asynccontextmanager
        async def lease(self, _owner):
            yield SimpleNamespace(origin=f'http://127.0.0.1:{worker_port}', token='synthetic')
        async def reap(self): pass

    settings = Settings(tmp_path/'control','https://synthetic.invalid','https://identity.invalid/',
        'https://synthetic.invalid/mcp','test','test','s'*40,b'k'*32)
    store = Store(settings.root, settings.encryption_key)
    tokens = []
    for subject in ('google-oauth2|synthetic', 'github|synthetic'):
        account = store.identity(subject)
        store.initialized(account['id'])
        store.preferences(account['id'], False, settings.policy_version)
        tokens.append(store.credential(account['id'])[0])
    gateway = create_app(settings, store=store, pool=Pool())
    servers, threads = [], []
    container = None
    try:
        for app, number in ((worker,worker_port),(gateway,gateway_port)):
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=number, access_log=False, log_level='error'))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start(); servers.append(server); threads.append(thread)
            deadline = time.monotonic()+10
            while not server.started:
                assert time.monotonic()<deadline
                time.sleep(.05)
        config = tmp_path/'Caddyfile'
        config.write_text(f'{{\n admin off\n auto_https off\n persist_config off\n}}\nhttp://127.0.0.1:{proxy_port} {{\n reverse_proxy 127.0.0.1:{gateway_port} {{\n flush_interval -1\n transport http {{\n read_timeout 21m\n }}\n }}\n}}\n')
        container = docker.from_env().containers.run('caddy:2-alpine',
            name='oi-stream-test-'+uuid.uuid4().hex, detach=True, network_mode='host',
            read_only=True, cap_drop=['ALL'], cap_add=['NET_BIND_SERVICE'], user='10001:10001',
            volumes={str(config):{'bind':'/etc/caddy/Caddyfile','mode':'ro'}},
            tmpfs={'/config':'rw,size=8m','/data':'rw,size=8m'})
        url = f'http://127.0.0.1:{proxy_port}'
        with httpx.Client(trust_env=False) as client:
            for _ in range(100):
                try:
                    if client.get(url+'/health').status_code==200: break
                except httpx.HTTPError: pass
                time.sleep(.1)
            else: raise AssertionError('local Caddy unavailable: '+container.logs().decode())

        async def exercise():
            def body(name): return {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':{}}}
            async with httpx.AsyncClient(trust_env=False,timeout=150) as client:
                start = time.monotonic()
                async with client.stream('POST',url+'/mcp',json=body('prepare_committee'),headers={'Authorization':'Bearer '+tokens[0]}) as response:
                    assert response.status_code==200
                    iterator = response.aiter_bytes()
                    first = await anext(iterator)
                    assert b'keepalive' in first and time.monotonic()-start<5
                    light_start = time.monotonic()
                    light = await client.post(url+'/mcp',json=body('doctor'),headers={'Authorization':'Bearer '+tokens[1]})
                    assert light.status_code==200 and time.monotonic()-light_start<5
                    output = first
                    async for chunk in iterator: output += chunk
                    assert b'"status"' in output or b'status' in output
                    assert 130 <= time.monotonic()-start < 150
        asyncio.run(exercise())
    finally:
        if container: container.remove(force=True)
        for server in servers: server.should_exit=True
        for thread in threads: thread.join(10)
