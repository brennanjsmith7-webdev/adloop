# Google Business Profile reporting

This local integration adds three read-only tools: `list_gbp_accounts`,
`list_gbp_locations(account_name)` and `run_gbp_report(location_name,
date_range_start, date_range_end)`. Enable with `ADLOOP_TOOLSETS=gbp` or
leave toolsets unset to expose the complete catalog.

Authorize with `.venv/bin/python scripts/connect_gbp.py`. The existing
Google desktop OAuth app requests `business.manage` separately from Ads
and GA4. Google has no read-only scope for these performance endpoints;
the implementation uses GET requests only. Consent stores the refresh
credentials in the gitignored, mode-0600 `.env.local`. Existing
`~/.adloop/token.json` is not changed. Refreshes remain in memory.

Require account management, business information and performance API
access in the Google project. Authorization does not prove API approval
or location access: list accounts/locations and read an actual report.

Normalize website hosts to map profiles to clients. Preserve location
IDs and branches; multiple locations can belong to one client. Never
assign a name-only match when the website is absent or different.
Missing series/errors are unavailable. Respect reporting delay, and
describe call clicks as clicks rather than answered calls or leads.

Reference: https://developers.google.com/my-business/reference/performance/rest/v1/locations/fetchMultiDailyMetricsTimeSeries
