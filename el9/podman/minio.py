#!/usr/local/bin/python3.13
import argparse
import sys
import logging
import os
import http.client
import json
import textwrap
import secrets
import string
import subprocess
import shlex
import random
import time
from urllib.parse import urlparse

API_HOST = os.environ.get('API_URL').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'
CMD_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin','UMASK': '0002',}

class OpalstackAPITool():
    """simple wrapper for http.client get and post"""
    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host
        self.base_uri = base_uri

        # if there is no auth token, then try to log in with provided credentials
        if not authtoken:
            endpoint = self.base_uri + '/login/'
            payload = json.dumps({
                'username': user,
                'password': password
            })
            conn = http.client.HTTPSConnection(self.host)
            conn.request('POST', endpoint, payload, headers={'Content-type': 'application/json'})
            result = json.loads(conn.getresponse().read())
            if not result.get('token'):
                logging.warn('Invalid username or password and no auth token provided, exiting.')
                sys.exit()
            else:
                authtoken = result['token']

        self.headers = {
            'Content-type': 'application/json',
            'Authorization': f'Token {authtoken}'
        }

    def get(self, endpoint):
        """GETs an API endpoint"""
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request('GET', endpoint, headers=self.headers)
        connread = conn.getresponse().read()
        logging.info(connread)
        return json.loads(connread)

    def post(self, endpoint, payload):
        """POSTs data to an API endpoint"""
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request('POST', endpoint, payload, headers=self.headers)
        return json.loads(conn.getresponse().read())

def create_file(path, contents, writemode='w', perms=0o600):
    """make a file, perms are passed as octal"""
    with open(path, writemode) as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f'Created file {path} with permissions {oct(perms)}')

def download(url, localfile, writemode='wb', perms=0o600):
    """save a remote file, perms are passed as octal"""
    logging.info(f'Downloading {url} as {localfile} with permissions {oct(perms)}')
    u = urlparse(url)
    if u.scheme == 'http':
        conn = http.client.HTTPConnection(u.netloc)
    else:
        conn = http.client.HTTPSConnection(u.netloc)
    conn.request('GET', u.path)
    r = conn.getresponse()
    with open(localfile, writemode) as f:
        while True:
            data = r.read(4096)
            if data:
                f.write(data)
            else:
                break
    os.chmod(localfile, perms)
    logging.info(f'Downloaded {url} as {localfile} with permissions {oct(perms)}')

def gen_password(length=20):
    """makes a random password"""
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for i in range(length))

def run_command(cmd, cwd=None, env=CMD_ENV):
    """runs a command, returns output"""
    logging.info(f'Running: {cmd}')
    try:
        result = subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env)
    except subprocess.CalledProcessError as e:
        logging.debug(e.output)
    return result

def add_cronjob(cronjob):
    """appends a cron job to the user's crontab"""
    homedir = os.path.expanduser('~')
    tmpname = f'{homedir}/.tmp{gen_password()}'
    tmp = open(tmpname, 'w')
    subprocess.run('crontab -l'.split(),stdout=tmp)
    tmp.write(f'{cronjob}\n')
    tmp.close()
    cmd = f'crontab {tmpname}'
    doit = run_command(cmd)
    cmd = run_command(f'rm -f {tmpname}')
    logging.info(f'Added cron job: {cronjob}')

def main():
    """run it"""

    # grab args from cmd or env
    parser = argparse.ArgumentParser(
        description='Installs MinIO (dual-app) on Opalstack account'
    )
    parser.add_argument('-i', dest='app_uuid', help='UUID of the base app', default=os.environ.get('UUID'))
    parser.add_argument('-n', dest='app_name', help='name of the base app', default=os.environ.get('APPNAME'))
    parser.add_argument('-t', dest='opal_token', help='API auth token', default=os.environ.get('OPAL_TOKEN'))
    parser.add_argument('-u', dest='opal_user', help='Opalstack account name', default=os.environ.get('OPAL_USER'))
    parser.add_argument('-p', dest='opal_password', help='Opalstack account password', default=os.environ.get('OPAL_PASS'))
    args = parser.parse_args()

    # init logging
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')

    # go!
    logging.info(f'Started installation of MinIO app {args.app_name}')
    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)
    appinfo = api.get(f'/app/read/{args.app_uuid}')
    appdir = f'/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}'
    api_port = int(appinfo['port'])

    # create a second app for the console on the same server/osuser (type CUS)
    console_payload = json.dumps([{
        "name": f"{appinfo['name']}-console",
        "type": "CUS",
        "osuser": appinfo["osuser"],
        "server": appinfo["server"],
    }])
    logging.info("Creating console app for MinIO")
    console_resp = api.post("/app/create/", console_payload)
    # accept common shapes
    if isinstance(console_resp, list) and len(console_resp) and console_resp[0].get('id'):
        console_id = console_resp[0]['id']
    elif isinstance(console_resp, dict) and console_resp.get('id'):
        console_id = console_resp['id']
    elif isinstance(console_resp, dict) and console_resp.get('ids'):
        console_id = console_resp['ids'][0]
    elif isinstance(console_resp, dict) and console_resp.get('results') and console_resp['results'][0].get('id'):
        console_id = console_resp['results'][0]['id']
    else:
        logging.info(f'Unexpected app/create response: {console_resp}')
        sys.exit()

    consoleinfo = api.get(f'/app/read/{console_id}')
    console_port = int(consoleinfo['port'])

    # prepare directories
    cmd = f'mkdir -p {appdir}/data'
    doit = run_command(cmd)
    cmd = f'mkdir -p {appdir}/config'
    doit = run_command(cmd)

    # write .env
    env = textwrap.dedent(f'''\
    # MinIO root credentials
    MINIO_ROOT_USER="{appinfo['name'][:12]}-admin"
    MINIO_ROOT_PASSWORD="{gen_password(24)}"
    # Optional after domains are assigned:
    # MINIO_SERVER_URL="https://objects.example.com"
    # MINIO_BROWSER_REDIRECT_URL="https://console.example.com"
    ''')
    create_file(f'{appdir}/.env', env, perms=0o600)

    # start script (rootless podman with two routed ports)
    start_script = textwrap.dedent(f'''\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{appinfo['name']}"
    APPDIR="{appdir}"
    API_PORT="{api_port}"
    CONSOLE_PORT="{console_port}"
    IMG="docker.io/minio/minio:latest"
    source "$APPDIR/.env"

    podman pull "$IMG" >/dev/null || true
    podman rm -f "$APP" >/dev/null 2>&1 || true

    podman run -d --name "$APP" \\
      -p 127.0.0.1:${{API_PORT}}:9000 \\
      -p 127.0.0.1:${{CONSOLE_PORT}}:9001 \\
      -v "$APPDIR/data:/data" \\
      -v "$APPDIR/config:/root/.minio" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG" server /data --console-address ":9001"

    echo "Started MinIO for {appinfo["name"]}. API on 127.0.0.1:{api_port}, Console on 127.0.0.1:{console_port}"
    ''')
    create_file(f'{appdir}/start', start_script, perms=0o700)

    # stop script
    stop_script = textwrap.dedent(f'''\
    #!/bin/bash
    set -Eeuo pipefail
    podman rm -f {appinfo["name"]} >/dev/null 2>&1 || true
    echo "Stopped MinIO for {appinfo["name"]}."
    ''')
    create_file(f'{appdir}/stop', stop_script, perms=0o700)

    # cron (kick start periodically, like Ghost)
    m = random.randint(0,9)
    croncmd = f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1'
    cronjob = add_cronjob(croncmd)

    # README
    readme = textwrap.dedent(f'''\
    # Opalstack MinIO README

    API app:    {appinfo["name"]}        (port {api_port} -> container 9000)
    Console app: {consoleinfo["name"]}   (port {console_port} -> container 9001)

    Data:   {appdir}/data
    Config: {appdir}/config
    Env:    {appdir}/.env

    After assigning domains, you may set MINIO_SERVER_URL and MINIO_BROWSER_REDIRECT_URL in .env and restart.
    ''')
    create_file(f'{appdir}/README', readme)

    # start once
    doit = run_command(f'{appdir}/start')

    # finished, push panel signals
    msg = f'MinIO installed. API {appinfo["name"]}:127.0.0.1:{api_port} (9000), Console {consoleinfo["name"]}:127.0.0.1:{console_port} (9001).'
    payload = json.dumps([{'id': args.app_uuid}, {'id': console_id}])
    finished = api.post('/app/installed/', payload)
    notice_payload = json.dumps([{'type': 'D', 'content': msg}])
    notice_res = api.post('/notice/create/', notice_payload)

    logging.info(f'Completed installation of MinIO app {args.app_name}')

if __name__ == '__main__':
    main()
