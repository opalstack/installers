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

IMG_WEB   = 'docker.io/bitnami/discourse:latest'
IMG_SQ    = 'docker.io/bitnami/discourse-sidekiq:latest'
IMG_REDIS = 'docker.io/bitnami/redis:7.2'

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
        description='Installs Discourse web app on Opalstack account (podman)'
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
    logging.info(f'Started installation of Discourse app {args.app_name}')
    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)
    appinfo = api.get(f'/app/read/{args.app_uuid}')
    appdir = f'/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}'
    port = int(appinfo['port'])

    # === Create PostgreSQL user + database on the same server as the app ===
    db_name = f"{args.app_name[:8]}_{args.app_uuid[:8]}"
    # create DB user
    payload_user = json.dumps([{
        "server": appinfo["server"],
        "name": db_name
    }])
    user_attempts = 0
    while True:
        logging.info(f"Trying to create PostgreSQL user {db_name}")
        user_resp = api.post("/psqluser/create/", payload_user)
        time.sleep(5)
        # read back created user (grab id, name, default_password)
        users = api.get("/psqluser/list/")
        created = None
        for u in users:
            if u.get("name") == db_name:
                created = u
                break
        if created:
            DBUSERID = created["id"]
            DBUSER = created["name"]
            DBPWD = created.get("default_password") or gen_password()
            logging.info(f"PostgreSQL user {DBUSER} created (id {DBUSERID})")
            break
        user_attempts += 1
        if user_attempts > 10:
            logging.info(f"Could not create PostgreSQL user {db_name}")
            sys.exit()

    # create DB
    payload_db = json.dumps([{
        "name": db_name,
        "server": appinfo["server"],
        "dbusers_readwrite": [DBUSERID]
    }])
    db_attempts = 0
    while True:
        logging.info(f"Trying to create PostgreSQL database {db_name}")
        db_resp = api.post("/psqldb/create/", payload_db)
        time.sleep(5)
        dbs = api.get("/psqldb/list/")
        created_db = None
        for d in dbs:
            if d.get("name") == db_name:
                created_db = d
                break
        if created_db:
            DBID = created_db["id"]
            DBNAME = created_db["name"]
            logging.info(f"PostgreSQL database {DBNAME} created (id {DBID})")
            break
        db_attempts += 1
        if db_attempts > 10:
            logging.info(f"Could not create PostgreSQL database {db_name}")
            sys.exit()

    # wait for ready flags (db & user)
    # DB
    DBOKJSON = api.get(f"/psqldb/read/{DBID}")
    DBOK = DBOKJSON.get('ready')
    while DBOK is False:
        time.sleep(5)
        DBOKJSON = api.get(f"/psqldb/read/{DBID}")
        DBOK = DBOKJSON.get('ready')
    # USER
    DBUOKJSON = api.get(f"/psqluser/read/{DBUSERID}")
    DBUOK = DBUOKJSON.get('ready')
    while DBUOK is False:
        time.sleep(5)
        DBUOKJSON = api.get(f"/psqluser/read/{DBUSERID}")
        DBUOK = DBUOKJSON.get('ready')

    # dirs
    cmd = f'mkdir -p {appdir}/data/discourse'
    doit = run_command(cmd)
    cmd = f'mkdir -p {appdir}/tmp'
    doit = run_command(cmd)

    # .env for Bitnami Discourse
    env = textwrap.dedent(f'''\
    # Required Discourse settings
    DISCOURSE_HOST="forum.example.com"
    DISCOURSE_USERNAME="admin"
    DISCOURSE_PASSWORD="{gen_password(16)}"
    DISCOURSE_EMAIL="admin@example.com"
    DISCOURSE_ENABLE_HTTPS="no"

    # PostgreSQL settings
    POSTGRESQL_HOST="{DBNAME}.db.local"  # optional if your panel provides a per-DB host; else use the shared host below
    POSTGRESQL_PORT="5432"
    POSTGRESQL_USERNAME="{DBUSER}"
    POSTGRESQL_PASSWORD="{DBPWD}"
    POSTGRESQL_DATABASE="{DBNAME}"

    # If your environment uses a single shared host, comment the HOST above and set this:
    # POSTGRESQL_HOST="127.0.0.1"
    # (or your panel-provided shared PG hostname)

    # Redis will run alongside Discourse in the pod
    REDIS_HOST="{appinfo['name']}-redis"
    REDIS_PASSWORD=""
    ''')
    create_file(f'{appdir}/.env', env, perms=0o600)

    # start / stop / logs / update / check scripts (Podman pod on 127.0.0.1:{port} -> 3000)
    start_script = textwrap.dedent(f'''\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{appinfo['name']}"
    POD="$APP-pod"
    PORT="{port}"
    APPDIR="{appdir}"
    IMG_WEB="{IMG_WEB}"
    IMG_SQ="{IMG_SQ}"
    IMG_REDIS="{IMG_REDIS}"
    source "$APPDIR/.env"

    podman pull "$IMG_WEB" >/dev/null || true
    podman pull "$IMG_SQ"  >/dev/null || true
    podman pull "$IMG_REDIS" >/dev/null || true

    podman rm -f "$APP-redis" "$APP-sidekiq" "$APP" >/dev/null 2>&1 || true
    podman pod rm -f "$POD" >/dev/null 2>&1 || true

    podman pod create --name "$POD" -p 127.0.0.1:${{PORT}}:3000

    podman run -d --name "$APP-redis" --pod "$POD" -e ALLOW_EMPTY_PASSWORD=yes "$IMG_REDIS"

    podman run -d --name "$APP" --pod "$POD" \\
      -v "$APPDIR/data/discourse:/bitnami/discourse" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG_WEB"

    podman run -d --name "$APP-sidekiq" --pod "$POD" \\
      -v "$APPDIR/data/discourse:/bitnami/discourse" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG_SQ"

    echo "Started Discourse for {appinfo["name"]} on 127.0.0.1:{port}"
    ''')
    create_file(f'{appdir}/start', start_script, perms=0o700)

    stop_script = textwrap.dedent(f'''\
    #!/bin/bash
    set -Eeuo pipefail
    podman rm -f {appinfo["name"]}-sidekiq {appinfo["name"]} {appinfo["name"]}-redis >/dev/null 2>&1 || true
    podman pod rm -f {appinfo["name"]}-pod >/dev/null 2>&1 || true
    echo "Stopped Discourse for {appinfo["name"]}."
    ''')
    create_file(f'{appdir}/stop', stop_script, perms=0o700)

    logs_script = f'''#!/bin/bash
podman logs -f {appinfo["name"]}
'''
    create_file(f'{appdir}/logs', logs_script, perms=0o700)

    update_script = textwrap.dedent(f'''\
    #!/bin/bash
    set -Eeuo pipefail
    "{appdir}/stop"
    "{appdir}/start"
    ''')
    create_file(f'{appdir}/update', update_script, perms=0o700)

    check_script = textwrap.dedent(f'''\
    #!/bin/bash
    set -Eeuo pipefail
    curl -fsS "http://127.0.0.1:{port}/" >/dev/null || "{appdir}/start"
    ''')
    create_file(f'{appdir}/check', check_script, perms=0o700)

    # README
    readme = textwrap.dedent(f'''\
    # Opalstack Discourse README

    App: {appinfo["name"]}
    Port: {port}

    PostgreSQL DB: {DBNAME}
    PostgreSQL User: {DBUSER}

    Data: {appdir}/data/discourse

    After assigning this app to a site in your control panel, complete setup at:
      • https://YOUR-DOMAIN/wizard  (initial setup)
      • https://YOUR-DOMAIN/admin   (admin)
    ''')
    create_file(f'{appdir}/README', readme)

    # cron (like Ghost)
    m = random.randint(0,9)
    croncmd = f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check > /dev/null 2>&1'
    cronjob = add_cronjob(croncmd)

    # start once
    doit = run_command(f'{appdir}/start')

    # finished, mark installed & create notice with links
    payload = json.dumps([{'id': args.app_uuid}])
    finished = api.post('/app/installed/', payload)

    msg = (f'Discourse installed. Assign to a site, then finish setup:\n'
           f'Wizard: https://YOUR-DOMAIN/wizard  •  Admin: https://YOUR-DOMAIN/admin')
    notice_payload = json.dumps([{'type': 'D', 'content': msg}])
    notice_res = api.post('/notice/create/', notice_payload)

    logging.info(f'Completed installation of Discourse app {args.app_name}')

if __name__ == '__main__':
    main()
