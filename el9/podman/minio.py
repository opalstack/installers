#!/usr/bin/python3
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random, time

API_HOST = (os.environ.get('API_URL') or 'https://my.opalstack.com').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'
CMD_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'UMASK': '0002'}

IMG = 'docker.io/minio/minio:latest'
CONSOLE_SUFFIX = '-console'  # name for the 2nd app

# ----- API wrapper -----
class OpalstackAPITool():
    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host; self.base_uri = base_uri
        if not authtoken:
            endpoint = self.base_uri + '/login/'
            payload = json.dumps({'username': user, 'password': password})
            conn = http.client.HTTPSConnection(self.host)
            conn.request('POST', endpoint, payload, headers={'Content-type':'application/json'})
            result = json.loads(conn.getresponse().read() or b'{}')
            if not result.get('token'):
                logging.error('Invalid username/password and no token'); sys.exit(1)
            authtoken = result['token']
        self.headers = {'Content-type':'application/json', 'Authorization': f'Token {authtoken}'}
    def get(self, endpoint):
        conn = http.client.HTTPSConnection(self.host)
        conn.request('GET', self.base_uri + endpoint, headers=self.headers)
        return json.loads(conn.getresponse().read() or b'{}')
    def post(self, endpoint, payload):
        conn = http.client.HTTPSConnection(self.host)
        conn.request('POST', self.base_uri + endpoint, payload, headers=self.headers)
        return json.loads(conn.getresponse().read() or b'{}')

# ----- helpers -----
def create_file(path, contents, writemode='w', perms=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, writemode) as f: f.write(contents)
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
        out = getattr(e, 'output', b'').decode(errors='ignore')
        logging.error(f'Command failed: {cmd}\n{out}')
        sys.exit(e.returncode)

def add_cronjob(cronjob):
    homedir = os.path.expanduser('~'); tmpname = f'{homedir}/.tmp{gen_password(8)}'
    # capture existing crontab if any
    try:
        existing = subprocess.check_output('crontab -l', shell=True, stderr=subprocess.STDOUT).decode()
    except subprocess.CalledProcessError:
        existing = ''
    with open(tmpname, 'w') as tmp:
        if existing.strip(): tmp.write(existing.strip() + '\n')
        tmp.write(f'{cronjob}\n')
    run_command(f'crontab {tmpname}')
    os.remove(tmpname)
    logging.info(f'Added cron job: {cronjob}')

def wait_until_ready(api, app_uuid, timeout=300, interval=3):
    start = time.time()
    while True:
        info = api.get(f'/app/read/{app_uuid}')
        if info.get('ready') is True:
            return info
        if time.time() - start > timeout:
            logging.error(f'App {app_uuid} not ready after {timeout}s')
            sys.exit(1)
        time.sleep(interval)

def create_console_app(api, primary_app):
    name = f"{primary_app['name']}{CONSOLE_SUFFIX}"
    payload = [{
        'name': name,
        # CUS = Custom Proxied Port (a plain proxy-port style app that routes to a local port)
        # This does NOT need an installer_url; we just need the port reservation + routing.
        'type': 'CUS',
        'osuser': primary_app['osuser'],
        'server': primary_app['server'],
    }]
    resp = api.post('/app/create/', json.dumps(payload))
    # Response formats have varied; handle a few shapes.
    console_uuid = None
    if isinstance(resp, list) and resp and isinstance(resp[0], dict) and resp[0].get('id'):
        console_uuid = resp[0]['id']
    elif isinstance(resp, dict):
        if 'ids' in resp and resp['ids']:
            console_uuid = resp['ids'][0]
        elif 'results' in resp and resp['results'] and resp['results'][0].get('id'):
            console_uuid = resp['results'][0]['id']
        elif resp.get('id'):
            console_uuid = resp['id']
    if not console_uuid:
        logging.error(f'Unexpected app/create response: {resp}')
        sys.exit(1)
    logging.info(f'Created console app {name} uuid={console_uuid}')
    info = wait_until_ready(api, console_uuid)
    return info

def main():
    p = argparse.ArgumentParser(description='Installs MinIO (Podman, dual-app routing) on Opalstack')
    p.add_argument('-i', dest='app_uuid',   default=os.environ.get('UUID'))
    p.add_argument('-n', dest='app_name',   default=os.environ.get('APPNAME'))
    p.add_argument('-t', dest='opal_token', default=os.environ.get('OPAL_TOKEN'))
    p.add_argument('-u', dest='opal_user',  default=os.environ.get('OPAL_USER'))
    p.add_argument('-p', dest='opal_pass',  default=os.environ.get('OPAL_PASS'))
    # override to reuse an existing console app if you already created one:
    p.add_argument('--console-uuid', dest='console_uuid', default=os.environ.get('CONSOLE_UUID'))
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
    if not a.app_uuid:
        logging.error('Missing UUID (-i)'); sys.exit(1)

    api = OpalstackAPITool(API_HOST, API_BASE_URI, a.opal_token, a.opal_user, a.opal_pass)

    # Primary app (this installer is running under it)
    app = api.get(f'/app/read/{a.app_uuid}')
    if not app.get('name'):
        logging.error('App not found'); sys.exit(1)
    if not app.get('ready'):
        app = wait_until_ready(api, a.app_uuid)

    appdir = f"/home/{app['osuser_name']}/apps/{app['name']}"
    port   = int(app['port'])

    # Create or read the console app on same server/osuser
    if a.console_uuid:
        console = wait_until_ready(api, a.console_uuid)
    else:
        console = create_console_app(api, app)
    console_port = int(console['port'])

    # Prep dirs
    run_command(f'mkdir -p {appdir}/data')
    run_command(f'mkdir -p {appdir}/config')

    # .env for MinIO
    env = textwrap.dedent(f"""\
    # MinIO root credentials
    MINIO_ROOT_USER="{app['name'][:12]}-admin"
    MINIO_ROOT_PASSWORD="{gen_password(24)}"

    # Optional URLs (set these after you assign domains):
    # MINIO_SERVER_URL="https://objects.example.com"
    # MINIO_BROWSER_REDIRECT_URL="https://console.example.com"
    """)
    create_file(f'{appdir}/.env', env, perms=0o600)

    # Scripts
    start = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{app['name']}"; APPDIR="{appdir}"
    API_PORT="{port}"; CONSOLE_PORT="{console_port}"
    IMG="{IMG}"
    source "$APPDIR/.env"

    podman pull "$IMG" >/dev/null || true
    podman rm -f "$APP" >/dev/null 2>&1 || true

    # bind API and Console to the 2 separate Opalstack-assigned ports
    podman run -d --name "$APP" \\
      -p 127.0.0.1:${{API_PORT}}:9000 \\
      -p 127.0.0.1:${{CONSOLE_PORT}}:9001 \\
      -v "$APPDIR/data:/data" \\
      -v "$APPDIR/config:/root/.minio" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG" server /data --console-address ":9001"

    echo "Started MinIO for {app['name']} (API:127.0.0.1:{port} → 9000, Console:127.0.0.1:{console_port} → 9001)"
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
    # MinIO on Opalstack (dual-app)

    API app:    {app['name']}  (port {port} → container 9000)
    Console app:{console['name']} (port {console_port} → container 9001)

    Data:   {appdir}/data
    Config: {appdir}/config
    Env:    {appdir}/.env

    Next steps:
    - Assign your preferred domains:
        * API app → e.g. objects.example.com
        * Console app → e.g. console.example.com
    - (Optional) then set MINIO_SERVER_URL / MINIO_BROWSER_REDIRECT_URL in .env and restart.
    """)
    create_file(f'{appdir}/README.txt', readme, perms=0o600)

    # Cron: self-heal + nightly update
    m = random.randint(0,9)
    add_cronjob(f'0{m},2{m},4{m} * * * * {appdir}/check > /dev/null 2>&1')
    hh = random.randint(1,5); mm = random.randint(0,59)
    add_cronjob(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

    # Start once
    run_command(f'{appdir}/start')

    # ---- REQUIRED PANEL SIGNALS ----
    msg = (f"MinIO installed. API app {app['name']} → 127.0.0.1:{port} (9000), "
           f"Console app {console['name']} → 127.0.0.1:{console_port} (9001). "
           f"Data: {appdir}/data, Config: {appdir}/config.")
    # mark BOTH apps installed
    api.post('/app/installed/', json.dumps([{'id': app['id']}, {'id': console['id']}]))

    api.post('/notice/create/', json.dumps([{'type': 'D', 'content': msg}]))

    logging.info(f'Completed installation: {msg}')

if __name__ == '__main__':
    main()
