"""Exercise the deployed FastAPI mount with the real AdLoop tool catalog."""
import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


@pytest.fixture(autouse=True)
def restore_runtime():
    from adloop import auth, runtime
    from adloop.safety.preview import get_plan_store, set_plan_store
    from adloop.safety.audit import get_audit_sink, set_audit_sink
    original = (auth.get_credentials_provider(), get_plan_store(), get_audit_sink(),
                runtime.deployment_mode(), runtime._default_config)
    try:
        yield
    finally:
        auth.set_credentials_provider(original[0])
        set_plan_store(original[1])
        set_audit_sink(original[2])
        runtime.set_deployment_mode(original[3])
        runtime.set_default_config(original[4])


def load_entrypoint():
    spec = importlib.util.spec_from_file_location('entrypoint_under_test', Path(__file__).parents[1] / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_fastapi_mounted_service_missing_env(monkeypatch):
    monkeypatch.delenv('ADLOOP_HOSTED_CONFIG_JSON', raising=False)
    module = load_entrypoint()
    async with module.app.router.lifespan_context(module.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(module.app), base_url='http://test') as client:
            assert (await client.get('/health')).status_code == 503
            assert (await client.get('/mcp')).status_code == 503


@pytest.mark.asyncio
async def test_fastapi_mounted_real_catalog_roles_and_read(monkeypatch):
    monkeypatch.setenv('ADLOOP_HOSTED_CONFIG_JSON', '{}')
    monkeypatch.setenv('ADLOOP_GOOGLE_OAUTH_JSON', json.dumps({
        'refresh_token': 'fixture', 'client_id': 'fixture', 'client_secret': 'fixture'}))
    monkeypatch.setenv('ADLOOP_ACCESS_KEYS_JSON', json.dumps([
        {'name': name, 'role': role, 'tenant': 'delvedigital',
         'token_sha256': hashlib.sha256(name.encode()).hexdigest()}
        for name, role in [('brennan', 'owner'), ('james', 'reader')]]))
    monkeypatch.setattr('adloop.hosted_store.configure_store', lambda: None)
    from adloop.runtime import current_tenant, current_config

    def account_fixture(config):
        assert config is current_config()
        return {'accounts': [], 'scope': current_tenant()}

    monkeypatch.setattr('adloop.ga4.reports.get_account_summaries', account_fixture)
    from adloop.server import mcp
    prior_middleware = list(mcp.middleware)
    try:
        module = load_entrypoint()
        assert module.hosted.inner is not None
        async with module.app.router.lifespan_context(module.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(module.app), base_url='http://test') as client:
                assert (await client.get('/health')).json() == {'status': 'ok'}
                assert (await client.post('/mcp', json={})).status_code == 401

                async def rpc(actor, method, params):
                    response = await client.post('/mcp', headers={
                        'Authorization': f'Bearer {actor}', 'X-Role': 'owner',
                        'Accept': 'application/json, text/event-stream'}, json={
                        'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
                    assert response.status_code == 200, response.text
                    return response.json()

                reader = await rpc('james', 'tools/list', {})
                owner = await rpc('brennan', 'tools/list', {})
                assert all(t['annotations']['readOnlyHint'] is True for t in reader['result']['tools'])
                assert len(owner['result']['tools']) > len(reader['result']['tools'])
                assert 'confirm_and_apply' not in {t['name'] for t in reader['result']['tools']}
                denied = await rpc('james', 'tools/call', {'name': 'confirm_and_apply', 'arguments': {'plan_id': 'fixture'}})
                assert denied.get('error') or denied['result'].get('isError')
                for actor in ['james', 'brennan']:
                    result = await rpc(actor, 'tools/call', {'name': 'get_account_summaries', 'arguments': {}})
                    assert not result['result'].get('isError')
                    data = json.loads(result['result']['content'][0]['text'])
                    assert data == {'accounts': [], 'scope': f'delvedigital:{actor}'}
    finally:
        mcp.middleware[:] = prior_middleware
