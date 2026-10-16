"""Authorize GBP separately; store secrets only in restrictive .env.local."""
import json
import os
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

root = Path(__file__).resolve().parents[1]
source = Path.home()/'.adloop/credentials.json'
flow = InstalledAppFlow.from_client_secrets_file(str(source), scopes=['https://www.googleapis.com/auth/business.manage'], autogenerate_code_verifier=True)
credentials = flow.run_local_server(host='localhost', port=0, open_browser=True,
    authorization_prompt_message='Google authorization opened in your browser. Complete Business Profile consent there.',
    success_message='Business Profile authorization received. You can close this tab.',
    timeout_seconds=600, access_type='offline', prompt='consent')
if not credentials.refresh_token:
    raise SystemExit('No offline refresh token returned; authorization cannot run unattended.')
target = root/'.env.local'
content = target.read_text() if target.exists() else ''
content = '\n'.join(line for line in content.splitlines() if not line.startswith('GBP_OAUTH_JSON='))
fd = os.open(str(target), os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(content.strip()+'\nGBP_OAUTH_JSON='+json.dumps(json.loads(credentials.to_json()), separators=(',', ':'))+'\n')
os.chmod(target, 0o600)
print('GBP authorization saved securely in .env.local; existing Ads/GA4 token unchanged.')
