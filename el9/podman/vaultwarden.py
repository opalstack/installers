#!/usr/bin/python3
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random
from urllib.parse import urlparse

# ---------- Config ----------
DEFAULT_API_URL = os.environ.get('API_URL') or 'https://my.opalstack.com'
def _host_from_url(u):
    p = urlparse(u if '://' in u else ('https://' + u))
    return p.netloc or p.path or 'my.opalstack.com'
API_HOST = _host_from_url(DEFAULT_API_URL)
API_BASE_URI = '/api/v1'

IMG = None  # not using containers in this installer

BASE_ENV = os.environ.copy()
BASE_ENV['PATH'] = '/usr/local/bin:/usr/bin:/bin:' + BASE_ENV.get('PATH','')
# ---------- Helpers ----------
def run_command(cmd, cwd=None, env=None):
    if env is None: env = BASE_ENV
    logging.info(f'Running: {cmd}')
    try:
        return subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env)
    except subprocess.CalledProcessError as e:
        logging.debug(getattr(e, 'output', b''))
        sys.exit(e.returncode)

def create_file(path, contents, writemode='w', perms=0o600):
    with open(path, writemode) as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f'Created file {path} {oct(perms)}')

def gen_password(length=20):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

def add_cronjob(cron_line):
    """Idempotent cron add."""
    homedir = os.path.expanduser('~')
    tmpname = f'{homedir}/.tmp_cron_{gen_password(8)}'
    try:
        current = subprocess.check_output('crontab -l', shell=True, env=BASE_ENV).decode('utf-8', 'ignore')
    except subprocess.CalledProcessError:
        current = ''
    if cron_line in current:
        logging.info(f'Cron already present: {cron_line}')
        return
    with open(tmpname, 'w') as tmp:
        if current.strip():
            tmp.write(current.strip() + '\n')
        tmp.write(cron_line + '\n')
    run_command(f'crontab {tmpname}')
    run_command(f'rm -f {tmpname}')
    logging.info(f'Added cron job: {cron_line}')

# ---------- API ----------
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

# ---------- Main ----------
def main():
    p = argparse.ArgumentParser(description='Installs Vaultwarden (build-from-source) on Opalstack')
    p.add_argument('-i', dest='app_uuid',   default=os.environ.get('UUID'))
    p.add_argument('-n', dest='app_name',   default=os.environ.get('APPNAME'))
    p.add_argument('-t', dest='opal_token', default=os.environ.get('OPAL_TOKEN'))
    p.add_argument('-u', dest='opal_user',  default=os.environ.get('OPAL_USER'))
    p.add_argument('-p', dest='opal_pass',  default=os.environ.get('OPAL_PASS'))
    p.add_argument('--vw-tag', dest='vw_tag', default=os.environ.get('VW_TAG','1.34.3'))
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] INFO: %(message)s')

    if not a.app_uuid:
        logging.error('Missing UUID'); sys.exit(1)

    api = OpalstackAPITool(API_HOST, API_BASE_URI, a.opal_token, a.opal_user, a.opal_pass)
    app = api.get(f'/app/read/{a.app_uuid}')
    if not app.get('name'):
        logging.error('App not found'); sys.exit(1)

    username = app['osuser_name']
    appname  = app['name']
    port     = app['port']
    homedir  = f'/home/{username}'
    appdir   = f'{homedir}/apps/{appname}'
    logdir   = f'{homedir}/logs/apps/{appname}'
    bindir   = f'{appdir}/bin'
    datadir  = f'{appdir}/data'
    webdir   = f'{appdir}/web'
    srcdir   = f'{appdir}/src'

    # Create directories (once)
    run_command(f'mkdir -p {appdir}/run')
    run_command(f'mkdir -p {srcdir}')
    run_command(f'mkdir -p {appdir}/tmp')
    run_command(f'mkdir -p {bindir}')
    run_command(f'mkdir -p {datadir}')
    run_command(f'mkdir -p {webdir}')
    run_command(f'mkdir -p {logdir}')

    # finish_install.sh
    finish_install = textwrap.dedent(f"""\
    #!/usr/bin/env bash
    set -Eeuo pipefail

    ts() {{ date +"[%Y-%m-%d %H:%M:%S]"; }}
    log() {{ echo "$(ts) $*"; }}

    APPDIR="{appdir}"
    SRCDIR="$APPDIR/src"
    BINDIR="$APPDIR/bin"
    WEBDIR="$APPDIR/web"
    LOGDIR="{logdir}"
    ENVFILE="$APPDIR/.env"
    VW_TAG="${{VW_TAG:-{a.vw_tag}}}"

    mkdir -p "$SRCDIR" "$BINDIR" "$WEBDIR" "$LOGDIR"

    step() {{ log "[$1] $2"; }}

    step "1/8" "Checking build prerequisites..."
    for t in git curl tar grep sed awk; do command -v "$t" >/dev/null || {{ echo "Missing $t" >&2; exit 1; }}; done

    step "2/8" "Ensuring rustup (user-local) is installed and stable toolchain available..."
    if ! command -v rustup >/dev/null 2>&1; then
      export RUSTUP_INIT_SKIP_PATH_CHECK=yes
      curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
    fi
    # shellcheck source=/dev/null
    . "$HOME/.cargo/env" 2>/dev/null || true
    export PATH="$HOME/.cargo/bin:$PATH"
    rustup show >/dev/null 2>&1 || true
    rustup toolchain install stable -q >/dev/null 2>&1 || true
    rustup default stable -q >/dev/null 2>&1 || true

    step "3/8" "Fetching Vaultwarden source @ $VW_TAG ..."
    if [[ -d "$SRCDIR/vaultwarden/.git" ]]; then
      git -C "$SRCDIR/vaultwarden" fetch --tags --quiet
      git -C "$SRCDIR/vaultwarden" reset --hard --quiet
      git -C "$SRCDIR/vaultwarden" checkout -q "tags/$VW_TAG" || git -C "$SRCDIR/vaultwarden" checkout -q "$VW_TAG"
    else
      git clone --quiet https://github.com/dani-garcia/vaultwarden "$SRCDIR/vaultwarden"
      git -C "$SRCDIR/vaultwarden" checkout -q "tags/$VW_TAG" || git -C "$SRCDIR/vaultwarden" checkout -q "$VW_TAG"
    fi

    step "4/8" "Building Vaultwarden (SQLite) with locked deps (stable)..."
    mkdir -p "$LOGDIR"
    # Build
    ( cd "$SRCDIR/vaultwarden" && cargo build --locked --release --features sqlite ) || {{
      echo "Build failed"; exit 1;
    }}

    step "5/8" "Installing binary..."
    install -m 0700 "$SRCDIR/vaultwarden/target/release/vaultwarden" "$BINDIR/vaultwarden"

    step "6/8" "Fetching Bitwarden Web Vault assets..."
    rm -rf "$WEBDIR"/* || true
    WEB_OK=0
    # Try GitHub API for latest bw_web_builds tarball
    API_JSON="$(curl -sL https://api.github.com/repos/dani-garcia/bw_web_builds/releases/latest || true)"
    if echo "$API_JSON" | grep -q '"browser_download_url"'; then
      URL="$(echo "$API_JSON" | grep -Eo 'https[^"]+\\.tar\\.gz' | head -n1 || true)"
      if [[ -n "$URL" ]]; then
        if curl -fsSL "$URL" | tar xz -C "$WEBDIR" --strip-components=1; then
          WEB_OK=1
        fi
      fi
    fi
    if [[ "$WEB_OK" -eq 0 ]]; then
      log "Web vault download failed or not found; leaving headless (you can retry later)."
    fi

    step "7/8" "Writing env file..."
    touch "$ENVFILE"
    chmod 600 "$ENVFILE"

    ensure_kv() {{
      local k="$1" v="$2"
      if grep -q "^$k=" "$ENVFILE" 2>/dev/null; then
        sed -i "s|^$k=.*|$k=$v|" "$ENVFILE"
      else
        echo "$k=$v" >> "$ENVFILE"
      fi
    }}

    ensure_kv "DATA_FOLDER" "{datadir}"
    ensure_kv "ROCKET_ADDRESS" "127.0.0.1"
    ensure_kv "ROCKET_PORT" "{port}"
    ensure_kv "WEBSOCKET_ENABLED" "true"
    ensure_kv "WEB_VAULT_FOLDER" "{webdir}"
    if [[ "$WEB_OK" -eq 1 ]]; then
      ensure_kv "WEB_VAULT_ENABLED" "true"
    else
      ensure_kv "WEB_VAULT_ENABLED" "false"
    fi
    ensure_kv "LOG_FILE" "{logdir}/vaultwarden.log"
    # Only create ADMIN_TOKEN once
    if ! grep -q "^ADMIN_TOKEN=" "$ENVFILE"; then
      ensure_kv "ADMIN_TOKEN" "$(dd if=/dev/urandom bs=16 count=1 2>/dev/null | od -An -tx1 | tr -d ' \\n')"
    fi
    # sensible defaults; adjust later if needed
    ensure_kv "SIGNUPS_ALLOWED" "false"
    ensure_kv "DOMAIN" ""
    ensure_kv "SMTP_HOST" ""
    ensure_kv "SMTP_PORT" "587"
    ensure_kv "SMTP_FROM" "vaultwarden@yourdomain"
    ensure_kv "SMTP_USERNAME" ""
    ensure_kv "SMTP_PASSWORD" ""
    ensure_kv "SMTP_SECURITY" "starttls"

    step "8/8" "Done. Start the app with:  {appdir}/start"
    echo "Binary: {bindir}/vaultwarden"
    echo "Logs:   {logdir}/vaultwarden.log"
    """)

    # start/stop/etc.
    start = textwrap.dedent(f"""\
    #!/usr/bin/env bash
    set -Eeuo pipefail
    APPDIR="{appdir}"
    ENVFILE="$APPDIR/.env"
    LOGFILE="{logdir}/vaultwarden.log"
    PIDFILE="$APPDIR/run/vaultwarden.pid"

    mkdir -p "$(dirname "$LOGFILE")" "$APPDIR/run"
    touch "$LOGFILE"; chmod 600 "$LOGFILE"

    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "Vaultwarden already running (pid $(cat "$PIDFILE"))."; exit 0
    fi

    set +e
    ulimit -n 4096 2>/dev/null || true
    set -e

    set -a
    [[ -f "$ENVFILE" ]] && . "$ENVFILE"
    set +a

    nohup "{bindir}/vaultwarden" >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    echo "Started Vaultwarden on 127.0.0.1:{port} (pid $(cat "$PIDFILE")). Logs: $LOGFILE"
    """)

    stop = textwrap.dedent(f"""\
    #!/usr/bin/env bash
    set -Eeuo pipefail
    PIDFILE="{appdir}/run/vaultwarden.pid"
    if [[ -f "$PIDFILE" ]]; then
      PID="$(cat "$PIDFILE")"
      if kill -0 "$PID" 2>/dev/null; then
        kill "$PID" || true
        sleep 1
      fi
      rm -f "$PIDFILE"
      echo "Stopped Vaultwarden."
    else
      echo "Not running."
    fi
    """)

    status = textwrap.dedent(f"""\
    #!/usr/bin/env bash
    PIDFILE="{appdir}/run/vaultwarden.pid"
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "Vaultwarden running (pid $(cat "$PIDFILE")), listening on 127.0.0.1:{port}"
      exit 0
    else
      echo "Vaultwarden not running"; exit 1
    fi
    """)

    restart = textwrap.dedent(f"""\
    #!/usr/bin/env bash
    set -Eeuo pipefail
    "{appdir}/stop" || true
    "{appdir}/start"
    """)

    check = textwrap.dedent(f"""\
    #!/usr/bin/env bash
    set -Eeuo pipefail
    if ! curl -fsS http://127.0.0.1:{port}/ >/dev/null 2>&1; then
      "{appdir}/start" >/dev/null 2>&1 || true
    fi
    """)

    viewlogs = textwrap.dedent(f"""\
    #!/usr/bin/env bash
    tail -n 200 -F {logdir}/vaultwarden.log
    """)

    readme = textwrap.dedent(f"""\
    Vaultwarden (Bitwarden-compatible) on Opalstack
    ===============================================

    Paths
    -----
    App dir:   {appdir}
    Binary:    {bindir}/vaultwarden
    Data:      {datadir}
    Web vault: {webdir}
    Logs:      {logdir}/vaultwarden.log
    Port:      127.0.0.1:{port}

    Commands
    --------
      {appdir}/finish_install.sh   # build/update binary + web vault
      {appdir}/start               # start service
      {appdir}/stop                # stop service
      {appdir}/status              # status
      {appdir}/restart             # restart
      {appdir}/view-logs           # tail logs

    Notes
    -----
    • Default DB is SQLite at $DATA_FOLDER/db.sqlite3 (no external DB needed).
    • If web vault fetch fails (GitHub rate limits, etc.), service starts headless.
      Re-run finish_install.sh later or set WEB_VAULT_ENABLED=false in .env.
    • Point an Opalstack Route to this app’s port to serve HTTP/HTTPS.
    """)

    # Write files
    create_file(f'{appdir}/finish_install.sh', finish_install, perms=0o700)
    create_file(f'{appdir}/start',  start,  perms=0o700)
    create_file(f'{appdir}/stop',   stop,   perms=0o700)
    create_file(f'{appdir}/status', status, perms=0o700)
    create_file(f'{appdir}/restart', restart, perms=0o700)
    create_file(f'{appdir}/check',  check,  perms=0o700)
    create_file(f'{appdir}/view-logs', viewlogs, perms=0o700)
    # Seed .env with essentials (finish_install will refine/ensure)
    env_seed = textwrap.dedent(f"""\
    DATA_FOLDER="{datadir}"
    ROCKET_ADDRESS="127.0.0.1"
    ROCKET_PORT="{port}"
    WEB_VAULT_FOLDER="{webdir}"
    WEB_VAULT_ENABLED=false
    WEBSOCKET_ENABLED=true
    LOG_FILE="{logdir}/vaultwarden.log"
    SIGNUPS_ALLOWED=false
    ADMIN_TOKEN="{os.urandom(16).hex()}"
    """)
    create_file(f'{appdir}/.env', env_seed, perms=0o600)
    create_file(f'{appdir}/README.txt', readme, perms=0o600)

    # Cron: health check only (no auto-rebuilds)
    m = random.randint(0,9)
    add_cronjob(f'0{m},2{m},4{m} * * * * {appdir}/check > /dev/null 2>&1')

    # ---- REQUIRED PANEL SIGNALS ----
    msg = f'Vaultwarden bootstrapped on port:{port}. Run finish_install.sh then start.'
    installed_payload = json.dumps([{'id': a.app_uuid}])
    api.post('/app/installed/', installed_payload)  # marks app as installed
    notice_payload = json.dumps([{'type': 'D', 'content': msg}])
    api.post('/notice/create/', notice_payload)     # dashboard notice

    logging.info(f'Completed bootstrap for {appname}. Next: {appdir}/finish_install.sh then {appdir}/start')

if __name__ == '__main__':
    main()
