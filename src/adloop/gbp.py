"""Read-only Google Business Profile account, location and performance reads."""
from __future__ import annotations
import json
import os
import re
from datetime import date
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession

SCOPE = 'https://www.googleapis.com/auth/business.manage'
ENV_PATH = Path(__file__).resolve().parents[2] / '.env.local'
METRICS = ['BUSINESS_IMPRESSIONS_DESKTOP_MAPS', 'BUSINESS_IMPRESSIONS_DESKTOP_SEARCH',
           'BUSINESS_IMPRESSIONS_MOBILE_MAPS', 'BUSINESS_IMPRESSIONS_MOBILE_SEARCH',
           'WEBSITE_CLICKS', 'CALL_CLICKS', 'BUSINESS_DIRECTION_REQUESTS']

def _session():
    raw = os.environ.get('GBP_OAUTH_JSON')
    if not raw and ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if line.startswith('GBP_OAUTH_JSON='):
                raw = line.partition('=')[2]
                break
    if not raw:
        raise RuntimeError('Business Profile authorization required; run scripts/connect_gbp.py')
    info = json.loads(raw)
    if SCOPE not in info.get('scopes', []):
        raise RuntimeError('Business Profile authorization is missing business.manage')
    return AuthorizedSession(Credentials.from_authorized_user_info(info))

def _get(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    if not response.ok:
        error = response.json().get('error', {})
        reason = ', '.join(d.get('reason', '') for d in error.get('details', []) if d.get('reason'))
        raise RuntimeError(f"GBP HTTP {response.status_code}: {error.get('message', 'Request failed')} {reason}")
    return response.json()

def _pages(session, url, key, params=None):
    result, seen = [], set()
    params = dict(params or {})
    while True:
        data = _get(session, url, params)
        result.extend(data.get(key, []))
        token = data.get('nextPageToken')
        if not token:
            return result
        if token in seen:
            raise RuntimeError('Repeated GBP pagination token; refusing incomplete inventory')
        seen.add(token)
        params['pageToken'] = token

def list_gbp_accounts():
    with _session() as session:
        accounts = _pages(session, 'https://mybusinessaccountmanagement.googleapis.com/v1/accounts', 'accounts', {'pageSize': 20})
    return {'accounts': accounts, 'total_accounts': len(accounts)}

def list_gbp_locations(account_name: str):
    if not re.fullmatch(r'accounts/[0-9]+', account_name):
        raise ValueError('account_name must be accounts/ID')
    with _session() as session:
        locations = _pages(session, f'https://mybusinessbusinessinformation.googleapis.com/v1/{account_name}/locations', 'locations',
            {'readMask': 'name,title,websiteUri,storefrontAddress,metadata', 'pageSize': 100})
    return {'account': account_name, 'locations': locations, 'total_locations': len(locations)}

def run_gbp_report(location_name: str, date_range_start: str, date_range_end: str):
    if not re.fullmatch(r'locations/[0-9]+', location_name):
        raise ValueError('location_name must be locations/ID')
    start, end = date.fromisoformat(date_range_start), date.fromisoformat(date_range_end)
    if start > end:
        raise ValueError('Start date must precede end date')
    params = [('dailyMetrics', metric) for metric in METRICS]
    for prefix, day in [('start_date', start), ('end_date', end)]:
        params.extend((f'dailyRange.{prefix}.{part}', getattr(day, part)) for part in ['year', 'month', 'day'])
    with _session() as session:
        data = _get(session, f'https://businessprofileperformance.googleapis.com/v1/{location_name}:fetchMultiDailyMetricsTimeSeries', params)
    rows = []
    for group in data.get('multiDailyMetricTimeSeries', []):
        for series in group.get('dailyMetricTimeSeries', []):
            for item in series.get('timeSeries', {}).get('datedValues', []):
                day = item['date']
                rows.append({'date': date(day['year'], day['month'], day['day']).isoformat(),
                    'metric': series['dailyMetric'], 'value': int(item.get('value', '0'))})
    return {'location': location_name, 'date_range': {'start': str(start), 'end': str(end)},
            'rows': rows, 'raw': data, 'read_only': True}
