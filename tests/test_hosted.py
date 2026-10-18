import asyncio
import hashlib
import json

import httpx
import pytest
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from adloop.hosted import (HostedApplication, EnvironmentCredentialsProvider,
                           current_actor, load_hosted_config)
from adloop.runtime import current_tenant


@pytest.fixture(autouse=True)
def restore_global_runtime():
    from adloop import auth, runtime
    from adloop.safety.preview import get_plan_store, set_plan_store
    from adloop.safety.audit import get_audit_sink, set_audit_sink
    provider = auth.get_credentials_provider()
    plan_store = get_plan_store()
    audit_sink = get_audit_sink()
    mode = runtime.deployment_mode()
    default = runtime._default_config
    try:
        yield
    finally:
        auth.set_credentials_provider(provider)
        set_plan_store(plan_store)
        set_audit_sink(audit_sink)
        runtime.set_default_config(default)
        runtime.set_deployment_mode(mode)


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setenv('ADLOOP_HOSTED_CONFIG_JSON', '{}')
    monkeypatch.setenv('ADLOOP_GOOGLE_OAUTH_JSON', json.dumps({
        'refresh_token': 'fixture', 'client_id': 'fixture', 'client_secret': 'fixture'}))
    monkeypatch.setenv('ADLOOP_ACCESS_KEYS_JSON', json.dumps([
        {'name': name, 'role': role, 'tenant': 'delvedigital',
         'token_sha256': hashlib.sha256(name.encode()).hexdigest()}
        for name, role in [('brennan', 'owner'), ('james', 'reader')]]))
    server = FastMCP('fixture')

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def who():
        await asyncio.sleep(0.01)
        return {'actor': current_actor().name, 'tenant': current_tenant()}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False))
    def write():
        return 'should not run for reader'

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def sync_who():
        from adloop.runtime import current_config
        return {'actor': current_actor().name, 'tenant': current_tenant(),
                'source': current_config().source_path}

    return HostedApplication(server, configure_storage=False)


@pytest.mark.asyncio
async def test_missing_config_fails_closed(monkeypatch):
    monkeypatch.delenv('ADLOOP_HOSTED_CONFIG_JSON', raising=False)
    app = HostedApplication(configure_storage=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        assert (await client.get('/mcp')).status_code == 503
        assert (await client.get('/health')).json() == {'status': 'unavailable'}


@pytest.mark.asyncio
async def test_http_auth_and_identity(hosted):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(hosted), base_url='http://test') as client:
        assert (await client.get('/identity')).status_code == 401
        assert (await client.get('/identity', headers={'Authorization': 'Bearer bad'})).status_code == 401
        results = await asyncio.gather(*[
            client.get('/identity', headers={'Authorization': f'Bearer {name}', 'X-Role': 'owner'})
            for name in ['james', 'brennan'] * 10])
        assert [r.json()['name'] for r in results] == ['james', 'brennan'] * 10
        assert results[0].json()['role'] == 'reader'


@pytest.mark.asyncio
async def test_mcp_readers_cannot_write_and_context_isolated(hosted):
    async with hosted.inner.router.lifespan_context(hosted.inner):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(hosted), base_url='http://test') as client:
            async def call(name, method, params):
                response = await client.post('/mcp', headers={
                    'Authorization': f'Bearer {name}', 'X-Role': 'owner',
                    'Accept': 'application/json, text/event-stream'}, json={
                    'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
                assert response.status_code == 200, response.text
                return response.json()
            tools = await call('james', 'tools/list', {})
            assert {t['name'] for t in tools['result']['tools']} == {'who', 'sync_who'}
            denied = await call('james', 'tools/call', {'name': 'write', 'arguments': {}})
            assert denied.get('error') or denied['result'].get('isError')
            results = await asyncio.gather(*[
                call(n, 'tools/call', {'name': 'who', 'arguments': {}})
                for n in ['james', 'brennan'] * 5])
            for name, result in zip(['james', 'brennan'] * 5, results):
                data = json.loads(result['result']['content'][0]['text'])
                assert data == {'actor': name, 'tenant': f'delvedigital:{name}'}
            synced = await asyncio.gather(*[
                call(n, 'tools/call', {'name': 'sync_who', 'arguments': {}})
                for n in ['james', 'brennan'] * 5])
            for name, result in zip(['james', 'brennan'] * 5, synced):
                data = json.loads(result['result']['content'][0]['text'])
                assert data == {'actor': name, 'tenant': f'delvedigital:{name}',
                                'source': 'environment:ADLOOP_HOSTED_CONFIG_JSON'}
            allowed = await call('brennan', 'tools/call', {'name': 'write', 'arguments': {}})
            assert not allowed['result'].get('isError')


def test_no_files_or_adc(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Local fallback used')
    monkeypatch.setattr('adloop.auth._local_credentials', forbidden)
    monkeypatch.setattr('google.auth.default', forbidden)
    provider = EnvironmentCredentialsProvider({'refresh_token': 'x', 'client_id': 'x', 'client_secret': 'x'})
    config = load_hosted_config({})
    for service in ['ga4', 'ads', 'gtm', 'gsc', 'merchant']:
        assert getattr(provider, service + '_credentials')(config).refresh_token == 'x'
    assert config.google.token_path == ''
    with pytest.raises(ValueError):
        load_hosted_config({'google': {'token_path': '/tmp/token'}})
    with pytest.raises(ValueError):
        load_hosted_config({'safety': {'log_file': '/tmp/audit'}})

@pytest.mark.asyncio
async def test_invalid_identity_config_fails_closed(hosted, monkeypatch):
    monkeypatch.setenv('ADLOOP_ACCESS_KEYS_JSON', '[{"name":"james","role":"owner","tenant":"foreign","token_sha256":"' + '0' * 64 + '"}]')
    app = HostedApplication(FastMCP('fixture'), configure_storage=False)
    assert app.inner is None


def test_safety_config_preserved():
    cfg = load_hosted_config({'safety': {'require_dry_run': True,
        'two_phase_apply': True, 'max_daily_budget': 23, 'blocked_operations': ['remove_campaign']},
        'ga4': {'property_id': '123'}})
    assert cfg.safety.require_dry_run is True
    assert cfg.safety.two_phase_apply is True
    assert cfg.safety.max_daily_budget == 23
    assert cfg.safety.blocked_operations == ['remove_campaign']
    assert cfg.ga4.property_id == 'properties/123'
