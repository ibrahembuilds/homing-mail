"""The Docker image holds only app/ and wsgi.py (see Dockerfile). This starts
the app from a copy of exactly those files, in a fresh process, and serves the
main pages, so a file the image would be missing fails here."""
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

CHECK = r'''
import re, sys
import wsgi, app.worker
from app import __version__
client = wsgi.app.test_client()
assert client.get('/campaigns/health').get_json()['status'] == 'ok'
token = re.search(r'name="csrf" value="([^"]+)"', client.get('/campaigns/login').text).group(1)
assert client.post('/campaigns/login', data={'csrf': token, 'password': 'check-pw'}).status_code == 302
token = re.search(r'name="csrf" content="([^"]+)"', client.get('/campaigns/').text).group(1)
campaign = client.post('/campaigns/campaigns/new', data={'csrf': token}).headers['Location']
for url in ('/campaigns/', campaign, '/campaigns/analytics', '/campaigns/sending-health', '/campaigns/settings',
            '/campaigns/agent', '/campaigns/inbox', '/campaigns/static/app.css', '/campaigns/static/app.js'):
    assert client.get(url, follow_redirects=True).status_code == 200, url
page = client.get(campaign).text
assert 'data-sequence' in page and 'Test a second subject line' in page and 'v' + __version__ in page
from app.db import MIGRATIONS
assert wsgi.app.db.val('PRAGMA user_version') == len(MIGRATIONS)
print('image ok')
'''


def test_the_image_files_are_enough(tmp_path):
    copied = [line.split()[1].rstrip('/') for line in (ROOT / 'Dockerfile').read_text().splitlines()
              if line.startswith('COPY ') and 'requirements' not in line]
    assert copied == ['app', 'wsgi.py']
    image = tmp_path / 'srv'
    shutil.copytree(ROOT / 'app', image / 'app', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy(ROOT / 'wsgi.py', image)
    env = dict(os.environ, CAMPAIGNS_DATA=str(tmp_path / 'data'), CAMPAIGNS_ADMIN_PASSWORD='check-pw',
               CAMPAIGNS_AUTH='password', PUBLIC_URL='https://mail.example.com', PYTHONPATH=str(image))
    result = subprocess.run([sys.executable, '-c', CHECK], cwd=image, env=env, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    assert re.search('image ok', result.stdout)
