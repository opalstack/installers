#!/usr/bin/python3
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random
from urllib.parse import urlsplit

# ---------- config ----------
API_URL = os.environ.get('API_URL') or 'https://my.opalstack.com'
_host = urlsplit(API_URL).netloc or urlsplit('//' + API_URL).netloc or API_URL
API_HOST = _host
API_BASE_URI = '/api/v1'
IMG = 'docker.io/vaultwarden/server:latest'
# ----------------------------

class OpalstackAPITool():
    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host; self.base_uri = base_uri
        if not authtoken:
            endpoint = self.base_uri + '/login/'
            payload = json.dumps({'username': user, 'password': password})
            conn = http.client.HTTPSConnection(self.host); conn.request('POST', endpoint, payload, headers={'Content-type':'application/json'})
            result = json.loads(conn.getresponse().read() or b'{}')
            if not result.get('token'):
                logging.warning('Invalid username/password and no token, exiting.')
                sys.exit(1)
            authtoken = result['token']
        self.headers = {'Content-type':'application/json', 'Authorization': f'Token {authtoken}'}

    def get(self, endpoint):
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host); conn.request('GET', endpoint, headers=self.headers)
        return json.loads(conn.getresponse().read() or b'{}')

    def post(self, endpoint, payload):
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request('POST', endpoint, payload, headers=self.headers)
        return json.loads(conn.getresponse().read() or b'{}')

def create_file(path, contents, writemode='w', perms=0o600):
    with open(path, writemode) as f: f.write(contents)
    os.chmod(path, perms); logging.info(f'Created file {path} {oct(perms)}')

def gen_password(length=20):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

BASE_ENV = os.environ.copy()
BASE_ENV['PATH'] = '/usr/local/bin:/usr/bin:/bin:' + BASE_ENV.get('PATH','')

def run_command(cmd, cwd=None, env=None):
    if env is None:
        env = BASE_ENV
    logging.info(f'Running: {cmd}')
    try:
        return subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env)
    except subprocess.CalledProcessError as e:
        logging.debug(getattr(e, 'output', b''))
        sys.exit(e.returncode)

def add_cronjob(cronjob):
    homedir = os.path.expanduser('~'); tmpname = f'{homedir}/.tmp{gen_password()}'
    with open(tmpname, 'w') as tmp:
        subprocess.run('crontab -l'.split(), stdout=tmp)
        tmp.write(f'{cronjob}\n')
    run_command(f'crontab {tmpname}'); run_command(f'rm -f {tmpname}')
    logging.info(f'Added cron job: {cronjob}')

def main():
    # sane umask for shared hosting
    os.umask(0o002)

    p = argparse.ArgumentParser(description='Installs Vaultwarden (Podman) on Opalstack')
    p.add_argument('-i', dest='app_uuid',   default=os.environ.get('UUID'))
    p.add_argument('-n', dest='app_name',   default=os.environ.get('APPNAME'))
    p.add_argument('-t', dest='opal_token', default=os.environ.get('OPAL_TOKEN'))
    p.add_argument('-u', dest='opal_user',  default=os.environ.get('OPAL_USER'))
    p.add_argument('-p', dest='opal_pass',  default=os.environ.get('OPAL_PASS'))
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
    if not a.app_uuid: logging.error('Missing UUID'); sys.exit(1)

    api = OpalstackAPITool(API_HOST, API_BASE_URI, a.opal_token, a.opal_user, a.opal_pass)
    app = api.get(f'/app/read/{a.app_uuid}')
    if not app.get('name'): logging.error('App not found'); sys.exit(1)

    appdir = f'/home/{app["osuser_name"]}/apps/{app["name"]}'
    port   = app['port']
    run_command(f'mkdir -p {appdir}/data')

    env = textwrap.dedent(f"""\
    DOMAIN=""
    ADMIN_TOKEN="{os.urandom(16).hex()}"
    SIGNUPS_ALLOWED=false
    SMTP_HOST=""
    SMTP_PORT=587
    SMTP_FROM="vaultwarden@yourdomain"
    SMTP_USERNAME=""
    SMTP_PASSWORD=""
    SMTP_SECURITY=starttls
    """)
    create_file(f'{appdir}/.env', env, perms=0o600)

    # -------- start script with self-heal for rootless podman --------
    start = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail

    APP="{app['name']}"
    PORT="{port}"
    APPDIR="{appdir}"
    IMG="{IMG}"

    # env + runtime dirs for rootless podman
    export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
    export XDG_RUNTIME_DIR="${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}"
    mkdir -p "$XDG_RUNTIME_DIR/libpod/tmp"

    # nuke any stale pause pid in expected spots (harmless if absent)
    rm -f "$XDG_RUNTIME_DIR/libpod/tmp/pause.pid" 2>/dev/null || true
    rm -f "$APPDIR/libpod/tmp/pause.pid" 2>/dev/null || true

    # attempt lightweight repairs; ignore failures
    podman system renumber >/dev/null 2>&1 || true
    podman system migrate  >/dev/null 2>&1 || true

    source "$APPDIR/.env"

    # pull visibly (propagate failures); images cache after first run
    podman pull "$IMG"

    # idempotent stop
    podman rm -f "$APP" >/dev/null 2>&1 || true

    # run with explicit tmpdir so libpod doesn't write pause.pid in CWD
    podman run -d --name "$APP" \\
      --tmpdir "$XDG_RUNTIME_DIR/libpod/tmp" \\
      -p 127.0.0.1:${{PORT}}:80 \\
      -v "$APPDIR/data:/data" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG"

    echo "Started Vaultwarden for {app['name']} on 127.0.0.1:{port}"
    """)
    # ---------------------------------------------------------------

    stop = f"""#!/bin/bash
set -Eeuo pipefail
podman rm -f {app['name']} >/dev/null 2>&1 || true
echo "Stopped {app['name']}"
"""

    logs = f"""#!/bin/bash
podman logs -f {app['name']}
"""

    update = f"""#!/bin/bash
set -Eeuo pipefail
"{appdir}/stop"
"{appdir}/start"
"""

    check  = f"""#!/bin/bash
set -Eeuo pipefail
curl -fsS http://127.0.0.1:{port}/ >/dev/null || "{appdir}/start"
"""

    create_file(f'{appdir}/start',  start,  perms=0o700)
    create_file(f'{appdir}/stop',   stop,   perms=0o700)
    create_file(f'{appdir}/logs',   logs,   perms=0o700)
    create_file(f'{appdir}/update', update, perms=0o700)
    create_file(f'{appdir}/check',  check,  perms=0o700)
    create_file(f'{appdir}/README.txt', f"Vaultwarden on port {port}. Data in {appdir}/data\n", perms=0o600)

    # staggered crons so hosts don't thundering-herd
    m = random.randint(0,9); add_cronjob(f'0{m},2{m},4{m} * * * * {appdir}/check > /dev/null 2>&1')
    hh = random.randint(1,5); mm = random.randint(0,59); add_cronjob(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

    # Start once
    run_command(f'{appdir}/start')

    # ---- REQUIRED PANEL SIGNALS ----
    msg = f'Vaultwarden installed on port:{port}.'
    installed_payload = json.dumps([{'id': a.app_uuid}])
    api.post('/app/installed/', installed_payload)  # marks app as installed
    notice_payload = json.dumps([{'type': 'D', 'content': msg}])
    api.post('/notice/create/', notice_payload)     # dashboard notice

    logging.info(f'Completed installation of Vaultwarden app {a.app_name} - {msg}')

if __name__ == '__main__':
    main()
