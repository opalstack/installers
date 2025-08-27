#!/usr/bin/env python3
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string
import subprocess, shlex, random, platform, tarfile, tempfile, re, urllib.request

API_HOST = (os.environ.get('API_URL') or 'https://my.opalstack.com').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'
IMG = 'vaultwarden-binary'  # just a label for scripts

BASE_ENV = os.environ.copy()
BASE_ENV['PATH'] = '/usr/local/bin:/usr/bin:/bin:' + BASE_ENV.get('PATH','')

# -------------------- helpers --------------------
def run_command(cmd, cwd=None, env=None):
    if env is None: env = BASE_ENV
    logging.info(f'Running: {cmd}')
    try:
        return subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env)
    except subprocess.CalledProcessError as e:
        out = getattr(e, 'output', b'')
        logging.error(out.decode('utf-8', 'ignore'))
        sys.exit(e.returncode)

def create_file(path, contents, writemode='w', perms=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, writemode) as f: f.write(contents)
    os.chmod(path, perms)
    logging.info(f'Created file {path} {oct(perms)}')

def gen_password(length=24):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

def add_cronjob(cronline):
    homedir = os.path.expanduser('~')
    tmpname = f'{homedir}/.tmp{gen_password(8)}'
    existing = subprocess.run('crontab -l'.split(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode()
    if cronline in existing:
        logging.info('Cron already present, skipping: %s', cronline)
        return
    with open(tmpname, 'w') as tmp:
        if existing.strip(): tmp.write(existing)
        tmp.write(f'{cronine(cronline)}\n')
    run_command(f'crontab {tmpname}')
    os.unlink(tmpname)
    logging.info(f'Added cron job: {cronline}')

def cronine(s):  # normalize whitespace
    return ' '.join(s.split())

def safe_extract_tar(tar_path, dest_dir, strip_top=False):
    def is_within_directory(directory, target):
        abs_directory = os.path.abspath(directory)
        abs_target = os.path.abspath(target)
        return os.path.commonprefix([abs_directory, abs_target]) == abs_directory
    with tarfile.open(tar_path, 'r:*') as tf:
        members = tf.getmembers()
        for m in members:
            nm = m.name
            if strip_top:
                nm = '/'.join(nm.split('/')[1:]) or ''
                if not nm: continue
                m.name = nm
            target_path = os.path.join(dest_dir, m.name)
            if not is_within_directory(dest_dir, target_path):
                raise RuntimeError('Unsafe path in tarball.')
        tf.extractall(dest_dir)

def http_get_json(host, path, headers=None):
    if headers is None: headers = {}
    conn = http.client.HTTPSConnection(host)
    conn.request('GET', path, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    if resp.status >= 400:
        raise RuntimeError(f'HTTP {resp.status} {resp.reason}: {data[:200]}')
    return json.loads(data or b'{}')

def download(url, dest):
    logging.info(f'Downloading {url} -> {dest}')
    req = urllib.request.Request(url, headers={'User-Agent':'vw-opal-installer'})
    with urllib.request.urlopen(req) as r, open(dest, 'wb') as f:
        f.write(r.read())

# -------------------- opal api --------------------
class OpalstackAPITool():
    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host; self.base_uri = base_uri
        if not authtoken:
            endpoint = self.base_uri + '/login/'
            payload = json.dumps({'username': user, 'password': password})
            conn = http.client.HTTPSConnection(self.host); conn.request('POST', endpoint, payload, headers={'Content-type':'application/json'})
            result = json.loads(conn.getresponse().read() or b'{}')
            if not result.get('token'):
                logging.error('Invalid username/password and no token, exiting.')
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

# -------------------- vaultwarden fetch/install --------------------
def pick_assets(assets):
    """
    Return (vaultwarden_tar_url, web_vault_tar_url)
    Prefers musl builds for x86_64/aarch64; falls back to gnu.
    """
    arch = platform.machine().lower()
    candidates = []
    if arch in ('x86_64','amd64'):
        candidates = ['x86_64-unknown-linux-musl', 'x86_64-unknown-linux-gnu']
    elif arch in ('aarch64','arm64'):
        candidates = ['aarch64-unknown-linux-musl', 'aarch64-unknown-linux-gnu']
    else:
        # last resort: plain linux build naming
        candidates = [arch]

    vw_url = None
    for c in candidates:
        for a in assets:
            name = a.get('name','')
            if name.startswith('vaultwarden-') and name.endswith('.tar.gz') and c in name:
                vw_url = a.get('browser_download_url')
                break
        if vw_url: break

    wv_url = None
    for a in assets:
        name = a.get('name','')
        if 'web-vault' in name and name.endswith('.tar.gz'):
            wv_url = a.get('browser_download_url'); break

    if not vw_url:
        raise RuntimeError('Could not find a matching vaultwarden binary in release assets.')
    if not wv_url:
        raise RuntimeError('Could not find web-vault tarball in release assets.')
    return vw_url, wv_url

def install_vaultwarden_binaries(appdir):
    # query latest release
    headers = {'User-Agent':'vw-opal-installer', 'Accept':'application/vnd.github+json'}
    rel = http_get_json('api.github.com', '/repos/dani-garcia/vaultwarden/releases/latest', headers)
    assets = rel.get('assets', [])
    vw_url, wv_url = pick_assets(assets)

    tmpdir = tempfile.mkdtemp(prefix='vw_')
    vw_tar = os.path.join(tmpdir, 'vaultwarden.tar.gz')
    wv_tar = os.path.join(tmpdir, 'web-vault.tar.gz')

    download(vw_url, vw_tar)
    download(wv_url, wv_tar)

    # extract vaultwarden binary
    safe_extract_tar(vw_tar, tmpdir)
    # try to find 'vaultwarden' file
    vw_bin = None
    for root, _, files in os.walk(tmpdir):
        if 'vaultwarden' in files:
            vw_bin = os.path.join(root, 'vaultwarden'); break
    if not vw_bin:
        raise RuntimeError('vaultwarden binary not found after extraction.')

    final_bin = os.path.join(appdir, 'vaultwarden')
    run_command(f'/bin/mv -f {shlex.quote(vw_bin)} {shlex.quote(final_bin)}')
    os.chmod(final_bin, 0o700)

    # extract web-vault to appdir/web-vault/
    wv_dir = os.path.join(appdir, 'web-vault')
    os.makedirs(wv_dir, exist_ok=True)
    # strip the top dir if the tar has one
    safe_extract_tar(wv_tar, wv_dir, strip_top=True)

    logging.info('Vaultwarden binary and web-vault installed.')

# -------------------- main --------------------
def main():
    p = argparse.ArgumentParser(description='Install Vaultwarden (binary) on Opalstack (no Podman)')
    p.add_argument('-i', dest='app_uuid',   default=os.environ.get('UUID'))
    p.add_argument('-n', dest='app_name',   default=os.environ.get('APPNAME'))
    p.add_argument('-t', dest='opal_token', default=os.environ.get('OPAL_TOKEN'))
    p.add_argument('-u', dest='opal_user',  default=os.environ.get('OPAL_USER'))
    p.add_argument('-p', dest='opal_pass',  default=os.environ.get('OPAL_PASS'))
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')

    if not a.app_uuid:
        logging.error('Missing UUID'); sys.exit(1)

    api = OpalstackAPITool(API_HOST, API_BASE_URI, a.opal_token, a.opal_user, a.opal_pass)
    app = api.get(f'/app/read/{a.app_uuid}')
    if not app.get('name'):
        logging.error('App not found'); sys.exit(1)

    appdir = f'/home/{app["osuser_name"]}/apps/{app["name"]}'
    port   = str(app['port'])
    os.makedirs(os.path.join(appdir, 'data'), exist_ok=True)

    # 1) fetch vaultwarden + web-vault
    install_vaultwarden_binaries(appdir)

    # 2) write .env (see official .env.template for meanings)
    env = textwrap.dedent(f"""\
    # --- minimal Vaultwarden config for Opalstack ---
    DOMAIN=""  # set this to https://vault.yourdomain.tld when you wire DNS
    DATA_FOLDER="{appdir}/data"
    WEB_VAULT_ENABLED=true
    WEB_VAULT_FOLDER="{appdir}/web-vault"
    ENABLE_WEBSOCKET=true

    # restrict to local reverse proxy and your assigned port
    ROCKET_ADDRESS=127.0.0.1
    ROCKET_PORT={port}

    # lock down signups & set admin
    SIGNUPS_ALLOWED=false
    ADMIN_TOKEN="{os.urandom(16).hex()}"

    # SMTP (fill in to enable email)
    SMTP_HOST=""
    SMTP_PORT=587
    SMTP_FROM="vaultwarden@yourdomain"
    SMTP_USERNAME=""
    SMTP_PASSWORD=""
    SMTP_SECURITY=starttls
    """)
    create_file(os.path.join(appdir,'.env'), env, perms=0o600)

    # 3) helper scripts
    start = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{app['name']}"; PORT="{port}"; APPDIR="{appdir}"
    cd "$APPDIR"
    export ENV_FILE="$APPDIR/.env"
    if [ -f "$APPDIR/vaultwarden.pid" ] && kill -0 "$(cat "$APPDIR/vaultwarden.pid")" >/dev/null 2>&1; then
      echo "Vaultwarden already running (pid $(cat "$APPDIR/vaultwarden.pid"))."
      exit 0
    fi
    nohup "$APPDIR/vaultwarden" >> "$APPDIR/vaultwarden.log" 2>&1 &
    echo $! > "$APPDIR/vaultwarden.pid"
    echo "Started Vaultwarden on 127.0.0.1:{port}"
    """)

    stop = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APPDIR="{appdir}"
    if [ -f "$APPDIR/vaultwarden.pid" ]; then
      PID=$(cat "$APPDIR/vaultwarden.pid" || true)
      if [ -n "$PID" ] && kill -0 "$PID" >/dev/null 2>&1; then
        kill "$PID" || true
        sleep 2
        kill -9 "$PID" >/dev/null 2>&1 || true
      fi
      rm -f "$APPDIR/vaultwarden.pid"
    fi
    echo "Stopped vaultwarden"
    """)

    logs = f"#!/bin/bash\nexec tail -n 200 -F {appdir}/vaultwarden.log\n"
    update = f"#!/bin/bash\nset -Eeuo pipefail\n\"{appdir}/stop\"; \"{appdir}/start\"\n"
    check  = f"#!/bin/bash\nset -Eeuo pipefail\ncurl -fsS http://127.0.0.1:{port}/ > /dev/null || \"{appdir}/start\"\n"

    create_file(os.path.join(appdir,'start'),  start,  perms=0o700)
    create_file(os.path.join(appdir,'stop'),   stop,   perms=0o700)
    create_file(os.path.join(appdir,'logs'),   logs,   perms=0o700)
    create_file(os.path.join(appdir,'update'), update, perms=0o700)
    create_file(os.path.join(appdir,'check'),  check,  perms=0o700)

    create_file(os.path.join(appdir,'README.txt'),
                f"Vaultwarden (binary) on port {port}.\nData: {appdir}/data\nLogs: {appdir}/vaultwarden.log\n", perms=0o600)

    # 4) cron: small health check every ~20m; daily restart between 01:00–05:59
    m = random.randint(0,9)
    add_cronjob(f'0{m},2{m},4{m} * * * * {appdir}/check > /dev/null 2>&1')
    hh = random.randint(1,5); mm = random.randint(0,59)
    add_cronjob(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

    # 5) start once
    run_command(f'{appdir}/start')

    # ---- required panel signals ----
    msg = f'Vaultwarden (binary) installed on port:{port}.'
    installed_payload = json.dumps([{'id': a.app_uuid}])
    api.post('/app/installed/', installed_payload)
    notice_payload = json.dumps([{'type': 'D', 'content': msg}])
    api.post('/notice/create/', notice_payload)

    logging.info(f'Completed installation of Vaultwarden app {a.app_name} - {msg}')

if __name__ == '__main__':
    main()
