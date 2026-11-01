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
    completed = set()
    last_success = {}
    run_id = uuid.uuid4().hex
    async with httpx.AsyncClient(trust_env=False, timeout=180) as client:
        async def call(i, token):
            nonlocal errors, requests, busy
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
                    if response.status_code != 200 or not result or result.get('error') or result.get('result',{}).get('isError'):
                        errors += 1
                    elif args.synthetic_write and business_result(result).get('new_balance') != 1.25:
                        errors += 1
                    else:
                        completed.add(i)
                        last_success[i] = time.monotonic()
                except httpx.HTTPError:
                    errors += 1
        while time.monotonic()-started < args.seconds:
            await asyncio.gather(*(call(i,t) for i,t in enumerate(tokens)))
            # Emits only counts; no tokens, account IDs, portfolio data or errors.
            print(json.dumps({'elapsed_seconds':int(time.monotonic()-started),'requests':requests,
                              'errors':errors,'busy_responses':busy,'successful_accounts':len(completed)}),flush=True)
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
