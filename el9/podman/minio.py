#!/usr/bin/python3
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random

API_HOST = (os.environ.get('API_URL') or 'https://my.opalstack.com').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'
CMD_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'UMASK': '0002'}

IMG = 'docker.io/minio/minio:latest'

# ----- API wrapper (Ghost style) -----
class OpalstackAPITool():
    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host
        self.base_uri = base_uri
        if not authtoken:
            endpoint = self.base_uri + '/login/'
            payload = json.dumps({'username': user, 'password': password})
            conn = http.client.HTTPSConnection(self.host)
            conn.request('POST', endpoint, payload, headers={'Content-type':'application/json'})
            result = json.loads(conn.getresponse().read() or b'{}')
            if not result.get('token'):
                logging.warning('Invalid username/password and no token, exiting.')
                sys.exit(1)
            authtoken = result['token']
        self.headers = {'Content-type':'application/json', 'Authorization': f'Token {authtoken}'}

    def get(self, endpoint):
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request('GET', endpoint, headers=self.headers)
        return json.loads(conn.getresponse().read() or b'{}')

    def post(self, endpoint, payload):
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request('POST', endpoint, payload, headers=self.headers)
        return json.loads(conn.getresponse().read() or b'{}')

# ----- helpers (Ghost style) -----
def create_file(path, contents, writemode='w', perms=0o600):
    with open(path, writemode) as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f'Created file {path} {oct(perms)}')

def gen_password(length=22):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

def run_command(cmd, cwd=None, env=CMD_ENV):
    logging.info(f'Running: {cmd}')
    try:
        return subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env)
    except subprocess.CalledProcessError as e:
        logging.debug(getattr(e, 'output', b''))
        sys.exit(e.returncode)

def add_cronjob(cronjob):
    homedir = os.path.expanduser('~')
    tmpname = f'{homedir}/.tmp{gen_password(8)}'
    with open(tmpname, 'w') as tmp:
        subprocess.run('crontab -l'.split(), stdout=tmp)
        tmp.write(f'{cronjob}\n')
    run_command(f'crontab {tmpname}')
    run_command(f'rm -f {tmpname}')
    logging.info(f'Added cron job: {cronjob}')

def main():
    p = argparse.ArgumentParser(description='Installs MinIO (Podman) on Opalstack')
    p.add_argument('-i', dest='app_uuid',   default=os.environ.get('UUID'))
    p.add_argument('-n', dest='app_name',   default=os.environ.get('APPNAME'))
    p.add_argument('-t', dest='opal_token', default=os.environ.get('OPAL_TOKEN'))
    p.add_argument('-u', dest='opal_user',  default=os.environ.get('OPAL_USER'))
    p.add_argument('-p', dest='opal_pass',  default=os.environ.get('OPAL_PASS'))
    # optional: let operator choose console port (defaults to PORT+1)
    p.add_argument('--console-port', dest='console_port', default=os.environ.get('CONSOLE_PORT'))
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
    if not a.app_uuid:
        logging.error('Missing UUID (-i)')
        sys.exit(1)

    api = OpalstackAPITool(API_HOST, API_BASE_URI, a.opal_token, a.opal_user, a.opal_pass)
    app = api.get(f'/app/read/{a.app_uuid}')
    if not app.get('name'):
        logging.error('App not found')
        sys.exit(1)

    appdir = f"/home/{app['osuser_name']}/apps/{app['name']}"
    port   = int(app['port'])

    # Prepare directories
    run_command(f'mkdir -p {appdir}/data')
    run_command(f'mkdir -p {appdir}/config')

    # Console port: by default, use PORT+1 (bound to 127.0.0.1 too)
    console_port = int(a.console_port) if a.console_port else port + 1

    # .env
    env = textwrap.dedent(f"""\
    # MinIO root credentials
    MINIO_ROOT_USER="{app['name'][:12]}-admin"
    MINIO_ROOT_PASSWORD="{gen_password(24)}"

    # Optional URLs (set these when wiring your proxy):
    # MINIO_SERVER_URL="https://objects.example.com"
    # MINIO_BROWSER_REDIRECT_URL="https://console.example.com"
    """)
    create_file(f'{appdir}/.env', env, perms=0o600)

    # Scripts
    start = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{app['name']}"; APPDIR="{appdir}"
    PORT="{port}"; CONSOLE_PORT="{console_port}"
    IMG="{IMG}"
    source "$APPDIR/.env"

    podman pull "$IMG" >/dev/null || true
    podman rm -f "$APP" >/devnull 2>&1 || true

    # Note: exposes BOTH ports on 127.0.0.1 (panel routes one; the other is for SSH tunnel or a second App if you make one)
    podman run -d --name "$APP" \\
      -p 127.0.0.1:${{PORT}}:9000 \\
      -p 127.0.0.1:${{CONSOLE_PORT}}:9001 \\
      -v "$APPDIR/data:/data" \\
      -v "$APPDIR/config:/root/.minio" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG" server /data --console-address ":9001"

    echo "Started MinIO for {app['name']} (API on 127.0.0.1:{port}, Console on 127.0.0.1:{console_port})"
    """)

    stop = f"""#!/bin/bash
set -Eeuo pipefail
podman rm -f {app['name']} >/dev/null 2>&1 || true
echo "Stopped MinIO for {app['name']}"
"""

    logs = f"#!/bin/bash\npodman logs -f {app['name']}\n"

    update = f"""#!/bin/bash
set -Eeuo pipefail
"{appdir}/stop"
"{appdir}/start"
"""

    # MinIO health endpoint (API port)
    check = f"""#!/bin/bash
set -Eeuo pipefail
curl -fsS "http://127.0.0.1:{port}/minio/health/live" >/dev/null || "{appdir}/start"
"""

    create_file(f'{appdir}/start',  start,  perms=0o700)
    create_file(f'{appdir}/stop',   stop,   perms=0o700)
    create_file(f'{appdir}/logs',   logs,   perms=0o700)
    create_file(f'{appdir}/update', update, perms=0o700)
    create_file(f'{appdir}/check',  check,  perms=0o700)

    readme = textwrap.dedent(f"""\
    # MinIO on Opalstack

    App: {app['name']}
    API Port: {port}        (mapped to container 9000)
    Console Port: {console_port} (mapped to container 9001)
    Data:  {appdir}/data
    Config:{appdir}/config
    Env:   {appdir}/.env

    Notes:
    - Only the *app-assigned* port ({port}) is routed by the panel. If you want to expose the console too,
      either create a second port app and map it to {console_port}, or use SSH tunneling.
    """)
    create_file(f'{appdir}/README.txt', readme, perms=0o600)

    # Cron: self-heal + nightly update (randomized minutes)
    m = random.randint(0,9)
    add_cronjob(f'0{m},2{m},4{m} * * * * {appdir}/check > /dev/null 2>&1')
    hh = random.randint(1,5); mm = random.randint(0,59)
    add_cronjob(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

    # Start once
    run_command(f'{appdir}/start')

    # ---- REQUIRED PANEL SIGNALS ----
    msg = (
        f"MinIO installed. API on 127.0.0.1:{port}, Console on 127.0.0.1:{console_port}. "
        f"Data: {appdir}/data, Config: {appdir}/config"
    )
    installed_payload = json.dumps([{'id': a.app_uuid}])
    api.post('/app/installed/', installed_payload)  # marks the app as installed
    notice_payload = json.dumps([{'type': 'D', 'content': msg}])
    api.post('/notice/create/', notice_payload)     # dashboard notice

    logging.info(f'Completed installation of MinIO app {a.app_name} - {msg}')

if __name__ == '__main__':
    main()
