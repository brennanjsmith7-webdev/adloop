import pytest
from unittest.mock import patch
from adloop import runtime

@pytest.fixture(autouse=True)
def restore_mode():
    old=runtime.deployment_mode()
    yield
    runtime.set_deployment_mode(old)

def test_hosted_error_bodies_are_private():
    from adloop.server import _safe
    runtime.set_deployment_mode('server')
    @_safe
    def result():return {'data':[{'error':'refresh_token=private-value','details':'Authorization: Bearer private-value'}]}
    assert 'private-value' not in str(result())
    @_safe
    def raised():raise RuntimeError('client_secret=private-value')
    assert 'private-value' not in str(raised())

def test_gbp_never_reads_local_env_in_hosted_mode(monkeypatch,tmp_path):
    from adloop import gbp
    runtime.set_deployment_mode('server');monkeypatch.delenv('GBP_OAUTH_JSON',raising=False)
    local=tmp_path/'.env.local';local.write_text('GBP_OAUTH_JSON={"refresh_token":"not-to-be-read"}')
    monkeypatch.setattr(gbp,'ENV_PATH',local)
    with pytest.raises(RuntimeError,match='authorization required'):gbp._session()

def test_gbp_refuses_custom_token_endpoint(monkeypatch):
    from adloop import gbp
    monkeypatch.setenv('GBP_OAUTH_JSON','{"token_uri":"https://example.invalid/token"}')
    with pytest.raises(RuntimeError,match='endpoint'):gbp._session()

def test_hosted_merchant_read_never_registers():
    from adloop.merchant import read, client
    from adloop.config import AdLoopConfig
    runtime.set_deployment_mode('server')
    with patch.object(client,'merchant_get',side_effect=client.MerchantNotRegistered('registration required')),patch.object(client,'register_gcp') as register:
        result=read.get_merchant_feed_health(AdLoopConfig(),account_id='123')
    assert result['registration_required'] is True
    register.assert_not_called()
