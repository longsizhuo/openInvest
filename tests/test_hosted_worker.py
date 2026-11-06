"""Process-boundary tests: never reuse the author's INVEST_HOME or model keys."""
import os
import subprocess
import sys

import pytest
pytest.importorskip('authlib')


def isolated(tmp_path, code):
    env = {k: v for k, v in os.environ.items() if not any(s in k for s in ('TOKEN', 'SECRET', 'API_KEY', 'INVEST_'))}
    env.update(INVEST_HOME=str(tmp_path), INVEST_HOSTED_WORKER='1', INVEST_API_TOKEN='synthetic-private-token',
               INVEST_REASONING_BY='agent', INVEST_EMBEDDED_SCHEDULER='0')
    result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr


def test_worker_onboarding_tools_and_money_replay(tmp_path):
    isolated(tmp_path, '''
import json
from starlette.testclient import TestClient
from openinvest.hosted.worker import initialize, create_worker, snapshot
from openinvest.core.portfolio_manager import PortfolioManager
spec = {'currency':'USD','risk':'Balanced','symbols':['AAPL']}
assert initialize(spec)['initialized']
assert initialize(spec)['initialized']  # a lost initialization response is recoverable
assert PortfolioManager().cash == {'USD':0}
from openinvest.utils.base_currency import base_currency
assert base_currency() == 'USD'
import os
os.environ['INVEST_PORTFOLIO_BASE_CURRENCY'] = 'CNY'
from openinvest.hosted.worker import _apply_base_currency
_apply_base_currency()  # a new worker rehydrates its own saved currency
assert base_currency() == 'USD'
try:
    initialize({**spec, 'currency':'AUD'})
    raise AssertionError('overwritten ledger')
except ValueError: pass
from openinvest.hosted.app import rpc_message, business_result
headers = {'Authorization':'Bearer synthetic-private-token','Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-03-26'}
with TestClient(create_worker()) as client:
    assert client.get('/internal/export').status_code == 401
    def call(method, params):
        res = client.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':method,'params':params}, headers=headers)
        assert res.status_code == 200, res.text
        return rpc_message(res.content)
    tools = call('tools/list', {})['result']['tools']
    names = {t['name'] for t in tools}
    assert 'run_committee' not in names and 'set_api_config' not in names
    assert 'idempotency_key' in next(t for t in tools if t['name']=='deposit')['inputSchema']['required']
    def deposit(key, amount=100):
        return call('tools/call', {'name':'deposit','arguments':{'currency':'USD','amount':amount,'idempotency_key':key}})
    first = deposit('one')
    assert not first['result'].get('isError'), first
    deposit('one')
    assert PortfolioManager().cash == {'USD':100.0}
    deposit('one', 200)
    assert PortfolioManager().cash == {'USD':100.0}
    doctor = business_result(call('tools/call', {'name':'doctor','arguments':{}}))
    assert doctor.get('coordinator_ready') and not doctor['direct_ready'], doctor
    assert 'checks' not in doctor
assert snapshot()
''')


def test_agent_setup_and_browser_preferences_share_one_ledger(tmp_path):
    isolated(tmp_path, '''
from starlette.testclient import TestClient
from openinvest.hosted.worker import create_worker, _apply_base_currency
from openinvest.hosted.app import rpc_message, business_result
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.core.memory_store import MemoryStore
from openinvest.utils.base_currency import base_currency
headers={'Authorization':'Bearer synthetic-private-token','Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-03-26'}
with TestClient(create_worker()) as client:
    def call(name, arguments={}):
        r=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':arguments}})
        assert r.status_code == 200
        return rpc_message(r.content)
    assert business_result(call('doctor'))['status']=='needs_setup'
    assert business_result(call('ledger_settings')) == {'initialized':False}
    settings=business_result(call('setup_ledger',{'currency':'USD'}))
    assert settings['initialized'] and settings['risk'] is None and settings['symbols']==[]
    assert business_result(call('doctor'))['status']=='ready'
    assert PortfolioManager().cash=={'USD':0}
    # Browser changes are immediately visible to the Agent.
    r=client.post('/internal/settings',headers=headers,json={'expected_revision':settings['revision'],'risk':'Aggressive','symbols':['AAPL','0700.HK']})
    assert r.status_code==200, r.text
    after=business_result(call('ledger_settings'))
    assert after['risk']=='Aggressive' and after['symbols']==['AAPL','0700.HK']
    stale=call('update_ledger_settings',{'expected_revision':settings['revision'],'risk':'Conservative'})
    assert stale['result']['isError']
    assert business_result(call('ledger_settings'))==after
    # Retained target metadata is preserved when the browser edits the ticker list.
    call('track_asset',{'symbol':'AAPL','display_name':'Synthetic Apple','max_single_invest_cny':200})
    after=business_result(call('ledger_settings'))
    updated=business_result(call('update_ledger_settings',{'expected_revision':after['revision'],'currency':'AUD','clear_risk':True,'symbols':['AAPL']}))
    assert updated['currency']=='AUD' and updated['risk'] is None
    target=MemoryStore().read('strategy').metadata['target_assets'][0]
    assert target['display_name']=='Synthetic Apple' and target['max_single_invest_cny']==200
    _apply_base_currency()
    assert base_currency()=='AUD'
    # Returning cash to zero does not make its financial history disappear.
    call('deposit',{'currency':'AUD','amount':10,'idempotency_key':'owned-deposit'})
    call('withdraw',{'currency':'AUD','amount':10,'idempotency_key':'owned-withdraw'})
    settings=business_result(call('ledger_settings'))
    assert not settings['currency_editable']
    r=client.post('/internal/settings',headers=headers,json={'expected_revision':settings['revision'],'currency':'CNY'})
    assert r.status_code==409 and r.json()['error']=='currency_has_history'
    r=client.post('/internal/settings',headers=headers,json={'expected_revision':settings['revision'],'risk':'Conservative','symbols':[]})
    assert r.status_code==200, r.text
    assert r.json()['symbols']==[] and r.json()['risk']=='Conservative'
    assert PortfolioManager().cash=={'USD':0,'AUD':0}
''')


def test_empty_targets_require_explicit_strategy_support():
    from openinvest.core.schemas import validate_strategy
    data={'target_allocation_stock':0,'target_allocation_cash':1,'target_assets':[]}
    with pytest.raises(ValueError):
        validate_strategy(data)
    assert validate_strategy({**data,'allow_empty_targets':True})['target_assets']==[]


@pytest.mark.skipif(not os.getenv('HOSTED_PREVIEW_WORKER_IMAGE'), reason='Requires a separately built local preview image')
def test_real_gateway_agent_setup_then_browser_preferences(tmp_path):
    isolated(tmp_path, '''
import asyncio, socket, threading, time, uuid, os
from dataclasses import replace
from datetime import timedelta
import httpx, uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from openinvest.hosted.app import create_app
from openinvest.hosted.config import Settings
from openinvest.hosted.pool import DockerPool
from openinvest.hosted.store import Store
from openinvest.paths import INVEST_ROOT
settings=Settings(INVEST_ROOT/'gateway','https://synthetic.invalid','https://identity.invalid/','https://synthetic.invalid/mcp','test','test','s'*40,b'k'*32,image=os.environ['HOSTED_PREVIEW_WORKER_IMAGE'])
from dataclasses import replace
settings=replace(settings,worker_namespace="test-"+uuid.uuid4().hex)
store=Store(settings.root,settings.encryption_key);pool=DockerPool(settings)
account=store.identity('google-oauth2|'+uuid.uuid4().hex)
other=store.identity('github|'+uuid.uuid4().hex)
session,credential=store.credential(account['id'],'session',ttl=3600)
headers={'Cookie':'oi_session='+session,'Origin':settings.origin,'X-CSRF-Token':credential['csrf']}
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
server=uvicorn.Server(uvicorn.Config(create_app(settings,store=store,pool=pool),host='127.0.0.1',port=port,access_log=False,log_level='error'))
thread=threading.Thread(target=server.run,daemon=True);thread.start()
while not server.started:
    assert thread.is_alive();time.sleep(.05)
base=f'http://127.0.0.1:{port}'
async def main():
    async with httpx.AsyncClient(base_url=base,headers=headers,trust_env=False,timeout=120) as web:
        try:
            r=await web.post('/api/me/consent',json={'policy_version':settings.policy_version,'research_enabled':False})
            assert r.status_code==200,r.text
            assert not (await web.get('/api/me')).json()['initialized']
            pat=(await web.post('/api/me/credentials')).json()['token']
            async with httpx.AsyncClient(headers={'Authorization':'Bearer '+pat},trust_env=False,timeout=120) as http:
                async with streamable_http_client(base+'/mcp',http_client=http) as (read,write,_):
                    async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=120)) as mcp:
                        await mcp.initialize()
                        tools=await mcp.list_tools()
                        assert {'setup_ledger','ledger_settings','update_ledger_settings'} <= {t.name for t in tools.tools}
                        def unpack(result):
                            import json
                            if result.structuredContent:
                                value=result.structuredContent
                                return value['result'] if set(value)=={'result'} else value
                            return json.loads(result.content[0].text)
                        assert unpack(await mcp.call_tool('doctor',{}))['status']=='needs_setup'
                        setup=await mcp.call_tool('setup_ledger',{'currency':'USD'})
                        assert not setup.isError,setup
                        assert (await web.get('/api/me')).json()['initialized']
                        current=(await web.get('/api/me/ledger/settings')).json()
                        assert current['risk'] is None and current['symbols']==[]
                        r=await web.patch('/api/me/ledger/settings',json={'expected_revision':current['revision'],'risk':'Conservative','symbols':['AAPL']})
                        assert r.status_code==200,r.text
                        shared=unpack(await mcp.call_tool('ledger_settings',{}))
                        assert shared['risk']=='Conservative' and shared['symbols']==['AAPL']
                        agent=await mcp.call_tool('update_ledger_settings',{'expected_revision':shared['revision'],'risk':'Aggressive','symbols':[]})
                        assert not agent.isError,agent
                        stale=await web.patch('/api/me/ledger/settings',json={'expected_revision':shared['revision'],'risk':'Conservative'})
                        assert stale.status_code==409 and stale.json()['error']=='settings_conflict'
                        visible=(await web.get('/api/me/ledger/settings')).json()
                        assert visible['risk']=='Aggressive' and visible['symbols']==[]
                        assert not store.account(other['id'])['initialized']
                        assert not (await mcp.call_tool('deposit',{'currency':'USD','amount':1.25,'idempotency_key':'synthetic-same-key'})).isError
                        visible=(await web.get('/api/me/ledger/settings')).json()
                        assert not visible['currency_editable']
                        r=await web.patch('/api/me/ledger/settings',json={'expected_revision':visible['revision'],'currency':'AUD'})
                        assert r.status_code==409 and r.json()['error']=='currency_has_history'
        finally:
            deleted=await web.delete('/api/me');assert deleted.status_code==200,deleted.text
try:asyncio.run(main())
finally:
    server.should_exit=True;thread.join(10)
    import docker
    engine=docker.from_env()
    for owner in (account['id'],other['id']):
        for c in engine.containers.list(all=True,filters={'label':'openinvest.owner='+owner}):c.remove(force=True)
        try:engine.volumes.get(pool.name(owner)).remove(force=True)
        except docker.errors.NotFound:pass
''')


def test_analysis_snapshot_binding_idempotency_and_cancel(tmp_path):
    isolated(tmp_path, '''
import json
from unittest.mock import patch
from openinvest.hosted.worker import initialize
initialize({'currency':'USD','symbols':['AAPL']})
from openinvest.hosted import analysis
from openinvest.paths import INVEST_ROOT
facts_file = INVEST_ROOT/'facts.json'
facts_file.write_text(json.dumps({'atr_pct':2}))
def prepare(symbol):
    from openinvest.core.decision_evidence import prepare_context
    context = prepare_context(INVEST_ROOT/'memory', symbol, {'atr_pct':2}, model='test')
    return {'asset':{'symbol':symbol},'prompts':{'cio':'test'},'market_data':'Evidence at T1','context_id':context['context_id']}
calls = []
def submit(*args, **kwargs):
    calls.append(args)
    return {'verdict':{'verdict':'HOLD'},'saved':str(INVEST_ROOT/'memory'/'result.md')}
with patch('openinvest.core.runner.coordinator._brief_facts_path', return_value=facts_file), patch('openinvest.core.runner.coordinator._load_brief_facts', return_value={'atr_pct':2}):
    prepared = analysis.prepare(prepare, 'AAPL')
    aid = prepared['analysis_id']
    assert analysis.evidence_for(aid)['input_snapshot']['cash'] == {'USD':0}
    try:
        analysis.prepare(prepare, 'AAPL')
        raise AssertionError('parallel committee')
    except ValueError: pass
    result = analysis.submit(submit,'AAPL','VERDICT: HOLD','test','test',aid)
    assert result['analysis_id'] == aid
    replayed=analysis.submit(submit,'AAPL','VERDICT: HOLD','test','test',aid)
    assert replayed['analysis_id'] == result['analysis_id'] and replayed['replayed']
    assert len(calls) == 1
    try:
        analysis.submit(submit,'AAPL','VERDICT: BUY','test','test',aid)
        raise AssertionError('changed replay')
    except ValueError: pass
    second = analysis.prepare(prepare,'AAPL')
    (INVEST_ROOT/'config.yaml').write_text('portfolio: {base_currency: AUD}')
    try:
        analysis.submit(submit,'AAPL','VERDICT: HOLD','test','test',second['analysis_id'])
        raise AssertionError('stale inputs accepted')
    except ValueError: pass
    analysis.cancel()
''')


@pytest.mark.skipif(os.getenv('HOSTED_DOCKER_TEST') != '1', reason='Explicit isolated Docker acceptance only')
def test_real_docker_isolation_eviction_and_restart(tmp_path):
    # Unique synthetic IDs and volumes; no existing ledger is opened or changed.
    isolated(tmp_path, '''
import asyncio, uuid
from dataclasses import replace
import httpx
from openinvest.hosted.config import Settings
from openinvest.hosted.pool import DockerPool, Busy
from openinvest.hosted.app import rpc_message
from openinvest.paths import INVEST_ROOT
settings=Settings(INVEST_ROOT,'https://test.invalid','https://identity.invalid/','test','test','test','s'*40,b'k'*32)
from dataclasses import replace
settings=replace(settings,worker_namespace="test-"+uuid.uuid4().hex)
pool=DockerPool(settings)
owners=[uuid.uuid4().hex for _ in range(10)]
async def main():
    try:
        for i, owner in enumerate(owners):
            await pool.internal(owner,'initialize',{'currency':'USD','symbols':['AAPL']})
            async with pool.lease(owner) as worker, httpx.AsyncClient(trust_env=False,timeout=30) as client:
                assert worker.container.attrs['HostConfig']['Memory']==1024**3
                assert worker.container.attrs['HostConfig']['ReadonlyRootfs']
                assert len(worker.container.attrs['Mounts']) == 1
                res=await client.post(worker.origin+'/mcp',headers={'Authorization':'Bearer '+worker.token,'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'deposit','arguments':{'currency':'USD','amount':i+1,'idempotency_key':'same'}}})
                data=rpc_message(res.content)
                assert res.status_code==200 and not data['result'].get('isError'),res.text
            assert len(pool.entries)<=2
        for i, owner in enumerate(owners):
            import io, tarfile
            data=await pool.internal(owner,'export')
            with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as tar:
                text=tar.extractfile('memory/portfolio.md').read().decode()
                assert f'USD: {float(i+1)}' in text,text
        async with pool.lease(owners[0]), pool.lease(owners[1]):
            try:
                async with pool.lease(owners[2]): pass
                raise AssertionError('capacity exceeded')
            except Busy: pass
        # Capture a consistent user archive; restore into a new empty user volume.
        archive=await pool.internal(owners[0],'export')
        restored=uuid.uuid4().hex
        owners.append(restored)
        await pool.internal(restored,'restore',raw=archive)
        assert await pool.internal(restored,'export')
    finally:
        for owner in owners:
            await pool.destroy(owner)
asyncio.run(main())
''')


@pytest.mark.skipif(os.getenv('HOSTED_DOCKER_TEST') != '1', reason='Explicit isolated Docker acceptance only')
def test_official_mcp_client_through_real_gateway_and_worker(tmp_path):
    isolated(tmp_path, '''
import asyncio, socket, threading, time, uuid
import httpx, uvicorn
from datetime import timedelta
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from openinvest.hosted.config import Settings
from openinvest.hosted.pool import DockerPool
from openinvest.hosted.store import Store
from openinvest.hosted.app import create_app
from openinvest.paths import INVEST_ROOT
settings=Settings(INVEST_ROOT/'gateway','https://synthetic.invalid','https://identity.invalid/','https://synthetic.invalid/mcp','test','test','s'*40,b'k'*32)
from dataclasses import replace
settings=replace(settings,worker_namespace="test-"+uuid.uuid4().hex)
store=Store(settings.root,settings.encryption_key)
pool=DockerPool(settings)
accounts=[]
for _ in range(2):
    account=store.identity('synthetic|'+uuid.uuid4().hex)
    store.preferences(account['id'],False,settings.policy_version)
    session, credential = store.credential(account['id'],'session',ttl=3600)
    accounts.append((account['id'],store.credential(account['id'])[0],session,credential['csrf']))
with socket.socket() as socket_:
    socket_.bind(('127.0.0.1',0))
    port=socket_.getsockname()[1]
app=create_app(settings,store=store,pool=pool)
server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,access_log=False,log_level='error'))
thread=threading.Thread(target=server.run,daemon=True)
thread.start()
while not server.started:
    assert thread.is_alive()
    time.sleep(.05)
async def main():
    try:
        for owner,token,browser,csrf in accounts:
            async with httpx.AsyncClient(trust_env=False,timeout=30) as web:
                initialized=await web.post(f'http://127.0.0.1:{port}/api/me/initialize',json={'currency':'USD','symbols':['AAPL'],'research_enabled':False,'policy_version':settings.policy_version},headers={'Cookie':'oi_session='+browser,'Origin':settings.origin,'X-CSRF-Token':csrf})
                assert initialized.status_code==200,initialized.text
            async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token},trust_env=False,timeout=30) as http:
                for _ in range(2):
                    async with streamable_http_client(f'http://127.0.0.1:{port}/mcp',http_client=http) as (read,write,get_id):
                        async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=30)) as client:
                            response=await client.initialize()
                            assert 'research' not in response.instructions.lower()
                            tools=await client.list_tools()
                            assert 'run_committee' not in {t.name for t in tools.tools}
                            assert not (await client.call_tool('doctor',{})).isError
                            result=await client.call_tool('deposit',{'currency':'USD','amount':42.5,'idempotency_key':'same'})
                            assert not result.isError,result
            import io,tarfile
            async with httpx.AsyncClient(trust_env=False,timeout=30) as web:
                backup=await web.get(f'http://127.0.0.1:{port}/api/me/export',headers={'Cookie':'oi_session='+browser})
                assert backup.status_code==200
                data=backup.content
            with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as tar:
                assert 'USD: 42.5' in tar.extractfile('memory/portfolio.md').read().decode()
    finally:
        pass
try: asyncio.run(main())
finally:
    server.should_exit=True
    thread.join(10)
    import docker
    for owner,*_rest in accounts:
        try: pool.client.containers.get(pool.name(owner)).remove(force=True)
        except docker.errors.NotFound: pass
        try: pool.client.volumes.get(pool.name(owner)).remove()
        except docker.errors.NotFound: pass
''')


def test_hosted_invalid_roles_and_model_can_be_corrected_before_running(tmp_path):
    isolated(tmp_path, '''
from openinvest.hosted import analysis
from openinvest.hosted.ledger import initialize
from openinvest.core.decision_evidence import prepare_context
from openinvest.paths import INVEST_ROOT
initialize({'currency':'USD','symbols':['AAPL']})
roles=['MACRO','QUANT_R1','RISK_R1','QUANT_R2','RISK_R2','CIO']
def prepare(symbol):
    c=prepare_context(INVEST_ROOT/'memory',symbol,{'required_roles':roles},model='synthetic-model')
    return {'asset':{'symbol':symbol},'context_id':c['context_id']}
p=analysis.prepare(prepare,'AAPL')
def submit(*a,**kw):return {'verdict':{'verdict':'HOLD'}}
for transcript,model in [('=== CIO ===\\nVERDICT: HOLD','synthetic-model'),('VERDICT: HOLD','wrong-model')]:
    try:analysis.submit(submit,'AAPL',transcript,'synthetic',model,p['analysis_id']);raise AssertionError('invalid accepted')
    except ValueError:pass
    with analysis.db() as db:assert db.execute('SELECT state FROM analyses').fetchone()[0]=='prepared'
raw='\\n'.join('=== '+role+' ===\\n'+('VERDICT: HOLD' if role=='CIO' else 'synthetic report') for role in roles)
assert analysis.submit(submit,'AAPL',raw,'synthetic','synthetic-model',p['analysis_id'])['verdict']['verdict']=='HOLD'
''')


def test_real_hosted_restore_preserves_exclusions_and_blocks_writes(tmp_path):
    isolated(tmp_path, '''
import json,shutil
from starlette.testclient import TestClient
from openinvest.hosted.worker import initialize,create_worker,snapshot
from openinvest.hosted.app import rpc_message
from openinvest.paths import INVEST_ROOT
initialize({'currency':'USD','symbols':['AAPL']})
archive=snapshot()
shutil.rmtree(INVEST_ROOT/'memory')
if (INVEST_ROOT/'db').exists(): shutil.rmtree(INVEST_ROOT/'db')
(INVEST_ROOT/'.hosted-initialization.json').unlink()
integrity=INVEST_ROOT/'integrity';integrity.mkdir(exist_ok=True)
(integrity/'eligibility.json').write_text('{"schema":1,"artifacts":{}}')
(integrity/'exclusions.jsonl').write_text('{"artifact_id":"synthetic-revoked"}\\n')
headers={'Authorization':'Bearer synthetic-private-token','Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-03-26'}
with TestClient(create_worker()) as client:
    r=client.post('/internal/restore',content=archive,headers=headers)
    assert r.status_code==200,r.text
    assert (integrity/'restore_pending.json').exists()
    assert (INVEST_ROOT/'.hosted-initialization.json').exists()
    assert 'synthetic-revoked' in (integrity/'exclusions.jsonl').read_text()
    r=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}})
    assert 'quality_status' in {t['name'] for t in rpc_message(r.content)['result']['tools']}
    r=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'deposit','arguments':{'currency':'USD','amount':1,'idempotency_key':'synthetic'}}})
    result=rpc_message(r.content)['result']
    assert result.get('isError') or 'DATA_UNTRUSTED' in str(result)
    from openinvest.core.portfolio_manager import PortfolioManager
    assert PortfolioManager().cash == {'USD':0}
''')
