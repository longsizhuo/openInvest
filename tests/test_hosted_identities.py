"""Synthetic-only identity migration, linking and credential boundary checks."""
import json
import sqlite3
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.responses import RedirectResponse

from test_hosted import settings, environment, browser, rpc  # noqa: F401
from openinvest.hosted.auth import Authenticator, AuthenticationError
from openinvest.hosted.store import Store, Conflict


def prepare_link(env, monkeypatch, secondary=None):
    account = env.accounts[0][0]
    headers = browser(env, account)
    calls = []

    async def redirect(_request, callback, **options):
        calls.append((callback, options))
        return RedirectResponse('https://identity.test/authorize?state=synthetic-state')

    async def token(_request):
        subject = account['subject'] if len(calls) == 1 else secondary or env.accounts[1][0]['subject']
        return {'userinfo': {'sub': subject, 'auth_time': int(time.time())}}

    monkeypatch.setattr(env.app.state.oauth.auth0, 'authorize_redirect', redirect)
    monkeypatch.setattr(env.app.state.oauth.auth0, 'authorize_access_token', token)
    response = env.client.post('/api/me/login-methods', headers=headers, json={'provider': 'github'})
    assert response.status_code == 200
    assert env.client.get('/auth/callback', follow_redirects=False).status_code == 307
    assert env.client.get('/auth/callback', follow_redirects=False).status_code == 303
    assert len(calls) == 2
    assert [c[1]['connection'] for c in calls] == ['google-oauth2', 'github']
    assert all(c[1]['prompt'] == 'login' and c[1]['max_age'] == 0 for c in calls)
    return headers


def test_legacy_migration_preserves_ids_preferences_tokens_and_files(settings):
    settings.root.mkdir(parents=True, exist_ok=True)
    path = settings.root/'control.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE accounts(id TEXT PRIMARY KEY,subject TEXT UNIQUE,participant TEXT UNIQUE NOT NULL,
            initialized INTEGER DEFAULT 0,accepted TEXT,research INTEGER DEFAULT 0,epoch INTEGER DEFAULT 0,
            revoked_before REAL DEFAULT 0,deleted INTEGER DEFAULT 0);
            CREATE TABLE credentials(id TEXT PRIMARY KEY,owner TEXT,digest TEXT UNIQUE,kind TEXT,label TEXT,
            created REAL,expires REAL,csrf TEXT,revoked INTEGER DEFAULT 0);''')
        for owner, subject in [('a','google-oauth2|a'), ('b','github|b')]:
            db.execute('INSERT INTO accounts VALUES(?,?,?,1,?,0,7,0,0)', (owner,subject,owner+'-participant','policy-v1'))
        db.execute("INSERT INTO credentials VALUES('session','a',?,'session','',?,?,'csrf',0)",
                   (Store.digest('synthetic-session'), time.time(), time.time()+3600))
    ledger = settings.root/'ledger-sentinel'
    ledger.write_bytes(b'private-synthetic-ledger-must-not-change')
    first = Store(settings.root, settings.encryption_key)
    assert first.identity('google-oauth2|a')['id'] == 'a'
    assert first.identity('github|b')['id'] == 'b'
    assert first.account('a')['epoch'] == 7
    assert first.account('a')['research'] == 0
    assert first.authenticate('synthetic-session','session')['session_subject'] == 'google-oauth2|a'
    methods = first.login_methods('a')
    again = Store(settings.root, settings.encryption_key)
    assert again.login_methods('a') == methods
    assert ledger.read_bytes() == b'private-synthetic-ledger-must-not-change'


def test_existing_accounts_link_only_after_confirmation_and_keep_separate_ledgers(environment, monkeypatch):
    env = environment
    owner, owner_pat = env.accounts[0]
    source, source_pat = env.accounts[1]
    source_session, _ = env.store.credential(source['id'], 'session', ttl=3600)
    env.pool.balances.update({owner['id']: 12.5, source['id']: 98.75})
    env.store.connect_oauth(source['id'], 'same-client', authorize=True)
    headers = prepare_link(env, monkeypatch)
    me = env.client.get('/api/me').json()
    assert me['pending_login_link'] == {'provider':'github','existing_account':True,'source_initialized':True}
    assert len(me['login_methods']) == 1
    assert env.store.identity(source['subject'])['id'] == source['id']
    assert env.client.post('/api/me/login-methods/confirm', headers=headers, json={}).status_code == 422
    response = env.client.post('/api/me/login-methods/confirm', headers=headers, json={'keep_current_ledger':True})
    assert response.status_code == 200
    me = env.client.get('/api/me').json()
    assert me['account_id'] == owner['id'] and len(me['login_methods']) == 2
    assert me['pending_login_link'] is None
    assert me['archived_ledgers'] == [{'id':source['id'],'initialized':1}]
    assert env.store.identity(source['subject'])['id'] == owner['id']
    assert env.store.authenticate(source_pat, 'pat') is None
    assert env.store.authenticate(source_session, 'session') is None
    assert env.store.authenticate(owner_pat, 'pat') is not None
    assert env.pool.balances == {owner['id']:12.5, source['id']:98.75}
    assert env.client.get(f'/api/me/archives/{source["id"]}/export').content == b'synthetic-backup'
    assert env.client.get(f'/api/me/archives/{owner["id"]}/export').status_code == 404
    replay_headers = {'Origin':env.settings.origin,'X-CSRF-Token':me['csrf']}
    assert env.client.post('/api/me/login-methods/confirm', headers=replay_headers, json={'keep_current_ledger':True}).status_code == 409
    # Browser login through either verified provider now returns the same account.
    async def token(_request):
        return {'userinfo': {'sub':source['subject']}}
    monkeypatch.setattr(env.app.state.oauth.auth0, 'authorize_access_token', token)
    env.client.get('/auth/callback', follow_redirects=False)
    assert env.client.get('/api/me').json()['account_id'] == owner['id']
    assert env.client.get('/api/me').json()['provider'] == 'github'


def test_new_identity_cancellation_csrf_and_no_disclosure(environment, monkeypatch):
    env = environment
    headers = prepare_link(env, monkeypatch, 'github|new-synthetic')
    me = env.client.get('/api/me').json()
    assert me['pending_login_link']['existing_account'] is False
    assert 'new-synthetic' not in json.dumps(me)
    assert env.client.post('/api/me/login-methods/confirm', json={'keep_current_ledger':True}).status_code == 403
    assert env.client.delete('/api/me/login-methods/pending', headers=headers).status_code == 200
    assert env.client.post('/api/me/login-methods/confirm', headers=headers, json={'keep_current_ledger':True}).status_code == 409
    assert env.store.identity_binding('github|new-synthetic') is None


def test_missing_or_stale_authentication_cannot_prove_either_identity(environment):
    env = environment
    owner = env.accounts[0][0]
    token, credential = env.store.credential(owner['id'], 'session', ttl=3600)
    flow = env.store.start_identity_flow(owner['id'], credential['id'], owner['subject'], 'github')
    for subject, auth_time in [(owner['subject'], None), (owner['subject'],time.time()-60), ('google-oauth2|attacker',time.time()), (owner['subject'],time.time()+600)]:
        with pytest.raises(Conflict):
            env.store.verify_identity_flow(flow, credential['id'], subject, auth_time)
    env.store.verify_identity_flow(flow, credential['id'], owner['subject'], time.time())
    with pytest.raises(Conflict):
        env.store.verify_identity_flow(flow, credential['id'], 'google-oauth2|wrong-provider', time.time())
    with env.store.db() as db:
        db.execute('UPDATE identity_flows SET expires=0')
    with pytest.raises(Conflict):
        env.store.finish_identity_flow(flow, credential['id'])


def test_link_rejects_busy_worker_and_stale_mapping(environment, monkeypatch):
    env = environment
    headers = prepare_link(env, monkeypatch)
    env.pool.entries['busy'] = SimpleNamespace(active=1)
    assert env.client.post('/api/me/login-methods/confirm', headers=headers, json={'keep_current_ledger':True}).status_code == 503
    assert not env.app.state.backup_in_progress
    env.pool.entries.clear()
    source = env.accounts[1][0]
    with env.store.db() as db:
        db.execute('DELETE FROM identities WHERE owner=?', (source['id'],))
    assert env.client.post('/api/me/login-methods/confirm', headers=headers, json={'keep_current_ledger':True}).status_code == 409


def test_old_source_jwt_cannot_gain_destination_grant(environment, monkeypatch):
    env = environment
    owner, source = (row[0] for row in env.accounts)
    key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    auth = Authenticator(env.settings, env.store, SimpleNamespace(get_signing_key_from_jwt=lambda _:SimpleNamespace(key=key.public_key())))
    env.store.connect_oauth(owner['id'],'shared-client',authorize=True)
    env.store.connect_oauth(source['id'],'shared-client',authorize=True)
    claims = {'sub':source['subject'],'iss':env.settings.issuer,'aud':env.settings.audience,'iat':int(time.time())-30,'exp':int(time.time())+3600,'azp':'shared-client','scope':'invest:read'}
    sign = lambda data:'Bearer '+jwt.encode(data,key,algorithm='RS256')
    old = sign(claims)
    assert auth.bearer(old)[0]['id'] == source['id']
    headers = prepare_link(env,monkeypatch)
    assert env.client.post('/api/me/login-methods/confirm',headers=headers,json={'keep_current_ledger':True}).status_code == 200
    with pytest.raises(AuthenticationError):
        auth.bearer(old)
    assert auth.bearer(sign({**claims,'iat':time.time()}))[0]['id'] == owner['id']


def test_unlink_guards_revocation_and_restart_does_not_restore_identity(environment, monkeypatch):
    env = environment
    headers = prepare_link(env,monkeypatch,'github|new-synthetic')
    assert env.client.post('/api/me/login-methods/confirm',headers=headers,json={'keep_current_ledger':True}).status_code == 200
    me = env.client.get('/api/me').json()
    headers['X-CSRF-Token'] = me['csrf']
    primary = next(m for m in me['login_methods'] if m['current'])
    second = next(m for m in me['login_methods'] if not m['current'])
    assert env.client.delete('/api/me/login-methods/'+primary['id'],headers=headers).status_code == 409
    assert env.client.delete('/api/me/login-methods/'+second['id'],headers=headers).status_code == 200
    assert env.client.delete('/api/me/login-methods/'+primary['id'],headers=headers).status_code == 409
    again = Store(env.settings.root,env.settings.encryption_key)
    assert len(again.login_methods(me['account_id'])) == 1
    assert again.identity_binding('github|new-synthetic') is None
    new_account = again.identity('github|new-synthetic')
    assert new_account['id'] != me['account_id']
    assert again.identity_binding('github|new-synthetic')['tokens_after'] > 0


def test_delete_linked_account_removes_all_identities_and_archive_access(environment,monkeypatch):
    env = environment
    headers = prepare_link(env,monkeypatch)
    source = env.accounts[1][0]
    assert env.client.post('/api/me/login-methods/confirm',headers=headers,json={'keep_current_ledger':True}).status_code == 200
    me = env.client.get('/api/me').json()
    headers['X-CSRF-Token'] = me['csrf']
    assert len(env.store.participants(me['account_id'])) == 2
    assert env.client.delete('/api/me',headers=headers).status_code == 200
    assert env.store.identity_binding(source['subject']) is None
    assert env.store.archived_accounts(me['account_id']) == []
    assert env.client.get('/api/me').status_code == 401


def test_mcp_handoff_uses_the_linked_identity_not_the_legacy_primary(environment,monkeypatch):
    env = environment
    headers = prepare_link(env,monkeypatch,'github|new-synthetic')
    assert env.client.post('/api/me/login-methods/confirm',headers=headers,json={'keep_current_ledger':True}).status_code == 200
    ticket = jwt.encode({'sub':'github|new-synthetic','client_id':'new-client','jti':'linked-handoff',
        'iat':time.time(),'exp':time.time()+300,'aud':env.settings.origin},env.settings.action_secret,algorithm='HS256')
    assert env.client.get('/auth/action',params={'session_token':ticket,'state':'auth0-state'},follow_redirects=False).status_code == 303
    me = env.client.get('/api/me').json()
    assert me['provider'] == 'github'
    response = env.client.post('/api/me/authorize',headers={'Origin':env.settings.origin,'X-CSRF-Token':me['csrf']})
    from urllib.parse import urlsplit,parse_qs
    resume = parse_qs(urlsplit(response.json()['redirect']).query)['session_token'][0]
    claims = jwt.decode(resume,env.settings.action_secret,algorithms=['HS256'],audience=env.settings.origin)
    assert claims['sub'] == 'github|new-synthetic'


def test_old_browser_session_requires_recent_oidc_authentication_to_unlink(environment,monkeypatch):
    env = environment
    headers = prepare_link(env,monkeypatch,'github|new-synthetic')
    assert env.client.post('/api/me/login-methods/confirm',headers=headers,json={'keep_current_ledger':True}).status_code == 200
    owner = env.accounts[0][0]
    # A newly issued portal cookie is not evidence of recent identity authentication.
    env.client.cookies.clear()
    headers = browser(env,owner)
    second = next(m for m in env.client.get('/api/me').json()['login_methods'] if not m['current'])
    result = env.client.delete('/api/me/login-methods/'+second['id'],headers=headers)
    assert result.status_code == 409 and result.json()['error'] == 'recent_login_required'


def test_invalid_oidc_state_cannot_verify_linking(environment):
    from base64 import b64encode
    from itsdangerous import TimestampSigner
    env = environment
    owner = env.accounts[0][0]
    browser(env,owner)
    credential = env.store.authenticate(env.client.cookies.get('oi_session'),'session')
    flow = env.store.start_identity_flow(owner['id'],credential['id'],owner['subject'],'github')
    cookie = TimestampSigner(env.settings.session_secret).sign(b64encode(json.dumps({'identity_flow':flow}).encode())).decode()
    env.client.cookies.set('oi_oauth_state',cookie)
    assert env.client.get('/auth/callback?code=synthetic&state=forged').status_code == 401
    assert env.store.identity_flow(flow,credential['id'])['phase'] == 'primary'
    assert len(env.store.login_methods(owner['id'])) == 1


def test_restore_invalidates_pending_proofs_and_old_oauth_tokens(environment,tmp_path):
    from dataclasses import replace
    from test_hosted import MemoryArchive
    from openinvest.hosted.backup import database_copy,seal,restore_database
    env = environment
    owner = env.accounts[0][0]
    _token, credential = env.store.credential(owner['id'],'session',ttl=3600)
    env.store.start_identity_flow(owner['id'],credential['id'],owner['subject'],'github')
    archive = MemoryArchive()
    archive.put('backups/control/123.aesgcm',seal(env.settings,'control','control',database_copy(env.store.path)))
    recovery = replace(env.settings,root=tmp_path/'restored')
    restore_database(recovery,archive,'control','123')
    recovered = Store(recovery.root,recovery.encryption_key)
    assert recovered.identity_binding(owner['subject'])['tokens_after'] > 0
    with recovered.db() as db:
        assert db.execute('SELECT COUNT(*) FROM identity_flows').fetchone()[0] == 0
    assert recovered.authenticate(_token,'session') is None


def test_archived_ledger_remains_in_backup_and_operator_recovery(environment,monkeypatch):
    import asyncio
    from test_hosted import MemoryArchive
    from openinvest.hosted.backup import backup_all
    env = environment
    headers = prepare_link(env,monkeypatch)
    source = env.accounts[1][0]
    assert env.client.post('/api/me/login-methods/confirm',headers=headers,json={'keep_current_ledger':True}).status_code == 200
    archive = MemoryArchive()
    result = asyncio.run(backup_all(env.settings,env.store,env.research,env.pool,archive))
    assert f'backups/{source["id"]}/{result["backup"]}.aesgcm' in archive.objects
    from openinvest.hosted.app import create_app
    from starlette.testclient import TestClient
    app = create_app(env.settings,store=env.store,pool=env.pool,archive=archive)
    with TestClient(app,base_url=env.settings.origin) as client:
        response = client.post('/operator/restore',headers={'Authorization':'Bearer '+env.settings.admin_token},json={'owner':source['id'],'backup':result['backup']})
        assert response.status_code == 200
