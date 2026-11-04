"""24h staging soak. Input is a private JSON list of ten synthetic PATs, never real ledgers."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
import uuid

import httpx


async def run(args):
    from openinvest.hosted.app import rpc_message, business_result
    tokens = json.loads(args.tokens.read_text())
    if len(tokens) != 10 or not all(isinstance(t, str) and t.startswith('oi_') for t in tokens):
        raise ValueError('Provide exactly ten synthetic account PATs')
    if args.tokens.stat().st_mode & 0o077:
        raise ValueError('Token file must have mode 600')
    semaphore = asyncio.Semaphore(2)
    started, errors, requests, busy = time.monotonic(), 0, 0, 0
    error_types = {}
    first_error_elapsed_seconds = None
    completed = set()
    last_success = {}
    run_id = uuid.uuid4().hex

    def record_error(kind):
        nonlocal errors, first_error_elapsed_seconds
        errors += 1
        error_types[kind] = error_types.get(kind, 0) + 1
        if first_error_elapsed_seconds is None:
            first_error_elapsed_seconds = round(time.monotonic() - started, 3)

    async with httpx.AsyncClient(trust_env=False, timeout=180) as client:
        async def call(i, token):
            nonlocal requests, busy
            async with semaphore:
                # One deposit per account per run; every later iteration retries
                # the identical key/amount, so a full-day soak does not compound cash.
                body = {'jsonrpc':'2.0','id':requests+1,'method':'tools/call','params':{
                    'name':'deposit' if args.synthetic_write else 'doctor',
                    'arguments':{'currency':'USD','amount':1.25,'idempotency_key':f'soak-{run_id}-{i}'} if args.synthetic_write else {}}}
                requests += 1
                try:
                    response = await client.post(args.url.rstrip('/')+'/mcp',json=body,headers={'Authorization':'Bearer '+token})
                    if response.status_code in {429, 503}:
                        # Capacity/maintenance responses are explicit and do not replay
                        # writes. A later iteration uses the same idempotency key.
                        busy += 1
                        return
                    result = rpc_message(response.content)
                    if response.status_code != 200:
                        record_error(f'http_{response.status_code}')
                    elif isinstance(result, dict) and result.get('error'):
                        record_error('rpc_error')
                    elif not isinstance(result, dict) or not isinstance(result.get('result'), dict):
                        record_error('invalid_response')
                    elif result['result'].get('isError'):
                        record_error('tool_error')
                    elif args.synthetic_write and business_result(result).get('new_balance') != 1.25:
                        record_error('balance_mismatch')
                    else:
                        completed.add(i)
                        last_success[i] = time.monotonic()
                except httpx.TimeoutException:
                    record_error('timeout')
                except httpx.HTTPError:
                    record_error('transport')
        while time.monotonic()-started < args.seconds:
            await asyncio.gather(*(call(i,t) for i,t in enumerate(tokens)))
            # Emits only counts; no tokens, account IDs, portfolio data or errors.
            print(json.dumps({'elapsed_seconds':int(time.monotonic()-started),'requests':requests,
                              'errors':errors,'error_types':error_types,
                              'first_error_elapsed_seconds':first_error_elapsed_seconds,
                              'busy_responses':busy,'successful_accounts':len(completed)}),flush=True)
            await asyncio.sleep(min(30,max(0,args.seconds-(time.monotonic()-started))))
    if errors or len(completed) != 10 or any(time.monotonic()-t > 300 for t in last_success.values()):
        raise SystemExit('Soak failed; inspect operator metrics without request bodies')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',required=True)
    parser.add_argument('--tokens',type=Path,required=True)
    parser.add_argument('--seconds',type=int,default=86400)
    parser.add_argument('--synthetic-write',action='store_true')
    parser.add_argument('--ack-synthetic-only',required=True,choices=['synthetic-test-ledgers'])
    args=parser.parse_args()
    asyncio.run(run(args))
