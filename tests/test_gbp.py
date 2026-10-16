from unittest.mock import MagicMock, patch
import pytest
from adloop import gbp

def test_pagination_preserves_all_locations():
    session = MagicMock()
    with patch.object(gbp, '_get', side_effect=[{'locations':[{'name':'locations/1'}], 'nextPageToken':'next'}, {'locations':[{'name':'locations/2'}]}]) as get:
        assert len(gbp._pages(session, 'url', 'locations')) == 2
        assert get.call_args.args[2]['pageToken'] == 'next'

def test_repeated_cursor_fails_closed():
    with patch.object(gbp, '_get', return_value={'locations':[], 'nextPageToken':'same'}):
        with pytest.raises(RuntimeError, match='Repeated'):
            gbp._pages(None, 'url', 'locations')

@pytest.mark.parametrize('name', ['accounts/1/../../other', 'https://evil.example', 'locations/123'])
def test_rejects_non_account_resource(name):
    with pytest.raises(ValueError): gbp.list_gbp_locations(name)

def test_report_queries_dates_and_normalizes_values():
    payload = {'multiDailyMetricTimeSeries':[{'dailyMetricTimeSeries':[{'dailyMetric':'WEBSITE_CLICKS','timeSeries':{'datedValues':[{'date':{'year':2026,'month':10,'day':1},'value':'4'}]}}]}]}
    with patch.object(gbp,'_session'), patch.object(gbp,'_get',return_value=payload) as get:
        result=gbp.run_gbp_report('locations/1','2026-10-01','2026-10-07')
        assert result['rows']==[{'date':'2026-10-01','metric':'WEBSITE_CLICKS','value':4}]
        assert ('dailyRange.end_date.day',7) in get.call_args.args[2]

def test_missing_series_is_not_zero():
    with patch.object(gbp,'_session'),patch.object(gbp,'_get',return_value={}):
        assert gbp.run_gbp_report('locations/1','2026-10-01','2026-10-07')['rows']==[]

def test_no_credentials_does_not_start_consent(tmp_path,monkeypatch):
    monkeypatch.delenv('GBP_OAUTH_JSON',raising=False)
    monkeypatch.setattr(gbp,'ENV_PATH',tmp_path/'.env.local')
    with pytest.raises(RuntimeError,match='authorization required'): gbp._session()

def test_permissions_error_does_not_return_empty_data():
    session=MagicMock();session.get.return_value.ok=False
    session.get.return_value.status_code=403
    session.get.return_value.json.return_value={'error':{'message':'insufficient scopes'}}
    with pytest.raises(RuntimeError,match='403'):gbp._get(session,'url')

@pytest.mark.asyncio
async def test_business_tools_are_registered_read_only():
    from adloop.server import mcp
    for name in ['list_gbp_accounts','list_gbp_locations','run_gbp_report']:
        tool=await mcp.get_tool(name)
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.tags == {'gbp'}
