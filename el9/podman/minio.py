#!/usr/bin/env python3
# Opalstack MinIO installer (rootless Podman) — app-only, dual port via 2 apps
# - Uses invoking app (UUID) as S3 endpoint (9000 -> base port)
# - Ensures/uses a companion console app (9001 -> console port)
# - No site/domain creation here.

import argparse, os, sys, json, http.client, logging, time, subprocess, shlex, secrets, string, textwrap, random, re

API_HOST = os.environ.get('API_URL', '').strip('https://').strip('http://') or 'my.opalstack.com'
API_BASE = '/api/v1'
IMAGE    = 'docker.io/minio/minio:latest'
MC_IMAGE = 'docker.io/minio/mc:latest'

def sh(cmd, check=False, quiet=False):
    if not quiet: logging.info("$ %s", cmd)
    r = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
    if check and r.returncode != 0:
        logging.error(r.stderr.strip()); sys.exit(r.returncode)
    return r

def write(path, content, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f: f.write(content)
    os.chmod(path, mode); logging.info("write %s (%s)", path, oct(mode))

def rand(n=24):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(n))

def sanitize(s):
    s = s.lower()
    s = re.sub(r'[^a-z0-9\-]+', '-', s).strip('-')
    return s or 'minio'

class API:
    def __init__(self, host, base='/api/v1', token=None, user=None, password=None):
        self.host, self.base = host, base
        if not token:
            conn = http.client.HTTPSConnection(self.host)
            payload = json.dumps({'username': user, 'password': password})
            conn.request('POST', self.base + '/login/', payload, headers={'Content-type': 'application/json'})
            resp = conn.getresponse(); data = json.loads(resp.read() or b'{}')
            token = data.get('token')
            if not token: logging.error('Auth failed; set OPAL_TOKEN or OPAL_USER/OPAL_PASS'); sys.exit(1)
        self.h = {'Content-type': 'application/json', 'Authorization': f'Token {token}'}

    def get(self, path):
        conn = http.client.HTTPSConnection(self.host)
        conn.request('GET', self.base + path, headers=self.h)
        return json.loads(conn.getresponse().read() or b'{}')

    def post(self, path, payload):
        conn = http.client.HTTPSConnection(self.host)
        conn.request('POST', self.base + path, json.dumps(payload), headers=self.h)
        return json.loads(conn.getresponse().read() or b'{}')

    def wait_ready(self, app_id, timeout=180):
        t0 = time.time()
        while time.time() - t0 < timeout:
            a = self.get(f'/app/read/{app_id}')
            if a.get('status') == 'ready' and a.get('port'): return a
            time.sleep(2.5)
        return self.get(f'/app/read/{app_id}')

def main():
    ap = argparse.ArgumentParser(description='Install MinIO (S3+Console) via two apps; no sites.')
    ap.add_argument('-i', '--uuid', dest='app_uuid', default=os.environ.get('UUID'))
    ap.add_argument('-n', '--name', dest='app_name', default=os.environ.get('APPNAME'))
    ap.add_argument('-t', '--token', dest='opal_token', default=os.environ.get('OPAL_TOKEN'))
    ap.add_argument('-u', '--user',  dest='opal_user',  default=os.environ.get('OPAL_USER'))
    ap.add_argument('-p', '--pass',  dest='opal_pass',  default=os.environ.get('OPAL_PASS'))
    ap.add_argument('--console-uuid', dest='console_uuid', default=os.environ.get('CONSOLE_APP_UUID'))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')

    if not args.app_uuid: logging.error('Missing app UUID (-i/--uuid or $UUID)'); sys.exit(1)
    if not sh('which podman', quiet=True).stdout.strip(): logging.error('podman not found'); sys.exit(1)

    api = API(API_HOST, API_BASE, args.opal_token, args.opal_user, args.opal_pass)

    # Base app -> S3 port
    base = api.get(f'/app/read/{args.app_uuid}')
    if not base.get('name'): logging.error('Base app not found'); sys.exit(1)
    base_name = sanitize(base['name'])
    port_s3   = base['port']
    osuser    = base.get('osuser_name') or base.get('osuser', '')
    if not (base_name and port_s3 and osuser): logging.error('Base app missing name/port/osuser'); sys.exit(1)

    # Resolve OSUser id
    osusers = api.get('/osuser/list/')
    osuser_id = next((u['id'] for u in osusers if u.get('name') == osuser), None)
    if not osuser_id: logging.error('OSUser %s not found', osuser); sys.exit(1)

    # Console app -> 9001 port
    console = None
    if args.console_uuid:
        console = api.get(f'/app/read/{args.console_uuid}')
        if console.get('osuser_name') != osuser:
            logging.error('Console app OSUser mismatch'); sys.exit(1)
    else:
        # find by name or create
        apps = api.get('/app/list/')
        wanted_name = sanitize(f'{base_name}-console')
        console = next((a for a in apps if a.get('name') == wanted_name and a.get('osuser_name') == osuser), None)
        if not console:
            created = api.post('/app/create/', [{
                'name': wanted_name,
                'osuser': osuser_id,
                'type': 'CUS',   # proxied port app
            }])
            if not isinstance(created, list) or not created:
                logging.error('Console app create failed: %s', created); sys.exit(1)
            console = api.wait_ready(created[0]['id'], timeout=180)

    port_console = console.get('port')
    console_name = console.get('name')
    if not port_console: logging.error('Console app has no port'); sys.exit(1)

    # Paths
    homedir = f"/home/{osuser}"
    appdir  = f"{homedir}/apps/{base_name}"
    os.makedirs(f"{appdir}/data", exist_ok=True)
    os.makedirs(f"{appdir}/tmp",  exist_ok=True)

    # .env with root creds
    env_path = f"{appdir}/.env"
    if not os.path.exists(env_path):
        root_user = f"minio-{rand(8)}"
        root_pass = rand(32)
        write(env_path, textwrap.dedent(f"""\
            MINIO_ROOT_USER="{root_user}"
            MINIO_ROOT_PASSWORD="{root_pass}"
            # Optional for presigned URLs behind proxy:
            # MINIO_SERVER_URL="https://s3.example.com"
        """), 0o600)

    # Scripts
    start = textwrap.dedent(f"""\
        #!/bin/bash
        set -Eeuo pipefail
        APP="{base_name}"
        APPDIR="$HOME/apps/$APP"
        IMG="{IMAGE}"
        PORT_S3="{port_s3}"
        PORT_CONSOLE="{port_console}"
        source "$APPDIR/.env"
        mkdir -p "$APPDIR/data" "$APPDIR/tmp"
        podman pull "$IMG" >/dev/null || true
        podman rm -f "$APP" >/dev/null 2>&1 || true
        podman run -d --name "$APP" \\
          -p 127.0.0.1:${{PORT_S3}}:9000 \\
          -p 127.0.0.1:${{PORT_CONSOLE}}:9001 \\
          -v "$APPDIR/data:/data:Z" \\
          --env-file "$APPDIR/.env" \\
          --label io.containers.autoupdate=registry \\
          "$IMG" server /data --console-address ":9001"
    """)
    stop = f"""#!/bin/bash
set -Eeuo pipefail
podman stop {base_name} >/dev/null 2>&1 || true
podman rm   {base_name} >/dev/null 2>&1 || true
echo "stopped {base_name}"
"""
    logs = f"#!/bin/bash\npodman logs -f {base_name}\n"
    update = textwrap.dedent(f"""\
        #!/bin/bash
        set -Eeuo pipefail
        APPDIR="$HOME/apps/{base_name}"
        podman pull {IMAGE}
        "$APPDIR/stop"
        "$APPDIR/start"
    """)
    check = textwrap.dedent(f"""\
        #!/bin/bash
        set -Eeuo pipefail
        if ! curl -fsS "http://127.0.0.1:{port_s3}/minio/health/live" >/dev/null; then
          echo "minio not healthy; restarting..."
          "$HOME/apps/{base_name}/start"
        fi
    """)
    setup = textwrap.dedent(f"""\
        #!/bin/bash
        set -Eeuo pipefail
        APP="{base_name}"
        APPDIR="$HOME/apps/$APP"
        PORT_S3="{port_s3}"
        MC_IMG="{MC_IMAGE}"
        source "$APPDIR/.env"
        for i in {{1..30}}; do
          curl -fsS "http://127.0.0.1:${{PORT_S3}}/minio/health/live" >/dev/null && break
          sleep 1
        done
        podman pull "$MC_IMG" >/dev/null || true
        podman run --rm --name "mc-$APP" --net host "$MC_IMG" \\
          mc alias set local "http://127.0.0.1:{port_s3}" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
        echo "Alias 'local' set. Example: podman run --rm --net host {MC_IMAGE} mc ls local"
        echo "Console app: {console_name} (port {port_console})"
    """)

    readme = textwrap.dedent(f"""\
        MinIO (rootless Podman) — app-only, dual app

        Apps
        - S3 API app:   {base_name}       port {port_s3}   -> container :9000
        - Console app:  {console_name}    port {port_console} -> container :9001

        Files
        - Dir:      {appdir}
        - Data:     {appdir}/data  (-> /data)
        - Scripts:  start | stop | logs | update | check | setup
        - Creds:    {appdir}/.env (MINIO_ROOT_USER / MINIO_ROOT_PASSWORD)

        Health: GET /minio/health/live on S3 port.
        Notes: This installer manages **apps only**. Domains/sites/proxy are handled elsewhere.
    """)

    write(f"{appdir}/start",  start,  0o700)
    write(f"{appdir}/stop",   stop,   0o700)
    write(f"{appdir}/logs",   logs,   0o700)
    write(f"{appdir}/update", update, 0o700)
    write(f"{appdir}/check",  check,  0o700)
    write(f"{appdir}/setup",  setup,  0o700)
    write(f"{appdir}/README.txt", readme, 0o600)

    # Cron: self-heal + nightly update
    m = random.randint(0,9)
    sh(f'(crontab -l 2>/dev/null; echo "0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check >/dev/null 2>&1") | crontab -')
    hh = random.randint(1,5); mm = random.randint(0,59)
    sh(f'(crontab -l 2>/dev/null; echo "{mm} {hh} * * * {appdir}/update >/dev/null 2>&1") | crontab -')

    logging.info('Done. S3 app=%s (port %s)  console app=%s (port %s)',
                 base_name, port_s3, console_name, port_console)

if __name__ == '__main__':
    main()
