"""Soak diagnostics classify failures without exposing requests or error bodies."""
from argparse import Namespace
import asyncio
import json

import httpx
import pytest

from deploy.hosted import soak


@pytest.mark.parametrize('kind', ['http_401', 'invalid_response', 'rpc_error', 'rpc_error_with_result', 'tool_error', 'balance_mismatch', 'timeout', 'transport'])
def test_soak_retains_failure_category_and_rejects_failed_run(tmp_path, monkeypatch, capsys, kind):
    token_file = tmp_path/'tokens.json'
    token_file.write_text(json.dumps([f'oi_SYNTHETIC_{i}' for i in range(10)]))
    token_file.chmod(0o600)
    clock = [0.0]
    monkeypatch.setattr(soak.time, 'monotonic', lambda: clock[0])

    async def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(soak.asyncio, 'sleep', sleep)

    class Client:
        calls = 0
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            self.calls += 1
            result = {'jsonrpc':'2.0', 'id':self.calls, 'result':{'structuredContent':{'new_balance':1.25}}}
            if self.calls != 1:
                return httpx.Response(200, json=result)
            if kind == 'http_401':
                return httpx.Response(401, text='PRIVATE_BODY')
            if kind == 'invalid_response':
                return httpx.Response(200, json=[{'data':'PRIVATE_BODY'}])
            if kind == 'rpc_error':
                return httpx.Response(200, json={'error':{'message':'PRIVATE_BODY'}})
            if kind == 'rpc_error_with_result':
                return httpx.Response(200, json={**result,'error':{'message':'PRIVATE_BODY'}})
            if kind == 'tool_error':
                return httpx.Response(200, json={'result':{'isError':True,'content':[{'text':'PRIVATE_BODY'}]}})
            if kind == 'balance_mismatch':
                result['result']['structuredContent']['new_balance'] = 2.5
                return httpx.Response(200, json=result)
            if kind == 'timeout':
                raise httpx.ReadTimeout('PRIVATE_BODY')
            raise httpx.ConnectError('PRIVATE_BODY')

    monkeypatch.setattr(soak.httpx, 'AsyncClient', Client)
    args = Namespace(tokens=token_file, url='https://synthetic.invalid', seconds=1, synthetic_write=True)
    with pytest.raises(SystemExit, match='Soak failed'):
        asyncio.run(soak.run(args))
    output = capsys.readouterr().out
    assert 'PRIVATE_BODY' not in output and 'oi_SYNTHETIC' not in output
    report = json.loads(output.strip().splitlines()[-1])
    assert report['requests'] == 10
    assert report['errors'] == 1
    expected = 'rpc_error' if kind == 'rpc_error_with_result' else kind
    assert report['error_types'] == {expected:1}
    assert report['first_error_elapsed_seconds'] == 0
