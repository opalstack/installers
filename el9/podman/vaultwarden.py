#!/usr/bin/python3
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random
from urllib.parse import urlparse

# ---------- Opalstack API plumbing ----------
def normalize_host(h):
    if not h: return 'my.opalstack.com'
    if '://' not in h: h = 'https://' + h
    parsed = urlparse(h)
    return parsed.netloc or 'my.opalstack.com'

API_HOST = normalize_host(os.environ.get('API_URL') or 'https://my.opalstack.com')
API_BASE_URI = '/api/v1'

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

# ---------- helpers ----------
BASE_ENV = os.environ.copy()
BASE_ENV['PATH'] = os.path.expanduser('~/.cargo/bin:/usr/local/bin:/usr/bin:/bin:' + BASE_ENV.get('PATH',''))

def run_command(cmd, cwd=None, env=None, check=True, capture=False):
    if env is None: env = BASE_ENV
    logging.info(f'Running: {cmd}')
    try:
        if capture:
            return subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env)
        else:
            subprocess.check_call(shlex.split(cmd), cwd=cwd, env=env)
            return b''
    except subprocess.CalledProcessError as e:
        if check:
            logging.debug(getattr(e, 'output', b''))
            sys.exit(e.returncode)
        return getattr(e, 'output', b'')

def create_file(path, contents, writemode='w', perms=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, writemode) as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f'Created file {path} {oct(perms)}')

def add_cronjob_once(line):
    """Append cronjob if not present."""
    try:
        current = subprocess.check_output(['crontab','-l'], stderr=subprocess.STDOUT).decode('utf-8','ignore')
    except subprocess.CalledProcessError:
        current = ''
    if line.strip() in [l.strip() for l in current.splitlines()]:
        logging.info(f'Cron already present: {line}')
        return
    homedir = os.path.expanduser('~')
    tmpname = f'{homedir}/.tmp_cron_{secrets.token_hex(4)}'
    with open(tmpname,'w') as tmp:
        tmp.write(current)
        if current and not current.endswith('\n'): tmp.write('\n')
        tmp.write(f'{line}\n')
    run_command(f'crontab {tmpname}')
    run_command(f'rm -f {tmpname}')
    logging.info(f'Added cron job: {line}')

# ---------- main ----------
def main():
    p = argparse.ArgumentParser(description='Installs Vaultwarden (compiled from source) on Opalstack')
    p.add_argument('-i', dest='app_uuid',   default=os.environ.get('UUID'))
    p.add_argument('-n', dest='app_name',   default=os.environ.get('APPNAME'))
    p.add_argument('-t', dest='opal_token', default=os.environ.get('OPAL_TOKEN'))
    p.add_argument('-u', dest='opal_user',  default=os.environ.get('OPAL_USER'))
    p.add_argument('-p', dest='opal_pass',  default=os.environ.get('OPAL_PASS'))
    p.add_argument('--vw-tag', dest='vw_tag', default=os.environ.get('VW_TAG') or '1.34.3')
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')

    if not a.app_uuid:
        logging.error('Missing UUID')
        sys.exit(1)

    api = OpalstackAPITool(API_HOST, API_BASE_URI, a.opal_token, a.opal_user, a.opal_pass)
    app = api.get(f'/app/read/{a.app_uuid}')
    if not app.get('name'):
        logging.error('App not found')
        sys.exit(1)

    homedir = f'/home/{app["osuser_name"]}'
    appname = app['name']
    appdir  = f'{homedir}/apps/{appname}'
    port    = app['port']
    logsdir = f'{homedir}/logs/apps/{appname}'

    # make dirs (no duplicates)
    for d in [f'{appdir}/run', f'{appdir}/src', f'{appdir}/tmp', f'{appdir}/bin', f'{appdir}/data', f'{appdir}/web']:
        run_command(f'mkdir -p {d}')
    run_command(f'mkdir -p {logsdir}')

    # -------- finish_install.sh (build from source + fetch web vault) --------
    finish_install = textwrap.dedent(f'''\
    #!/usr/bin/env bash
    set -Eeuo pipefail

    APPDIR="$(cd "$(dirname "$0")" && pwd)"
    BINDIR="$APPDIR/bin"
    SRCDIR="$APPDIR/src/vaultwarden"
    DATADIR="$APPDIR/data"
    WEBDIR="$APPDIR/web"
    TMPDIR="$APPDIR/tmp"
    LOGDIR="$HOME/logs/apps/{appname}"
    VW_TAG="${{VW_TAG:-{a.vw_tag}}}"
    export PATH="$HOME/.cargo/bin:$PATH"

    _ts() {{ date +"%F %T"; }}
    log() {{ echo "[`_ts`] $*" | tee -a "$LOGDIR/finish_install.log"; }}
    mkdir -p "$LOGDIR" "$TMPDIR" "$BINDIR" "$DATADIR" "$WEBDIR"

    log "[1/8] Checking build prerequisites..."
    for dep in curl git tar; do
      command -v "$dep" >/dev/null 2>&1 || {{ echo "Missing $dep" >&2; exit 1; }}
    done

    log "[2/8] Ensuring rustup (user-local) is installed and stable toolchain available..."
    if ! command -v rustup >/dev/null 2>&1; then
      export RUSTUP_INIT_SKIP_PATH_CHECK=yes
      curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal >/dev/null
      . "$HOME/.cargo/env" 2>/dev/null || true
    fi
    export PATH="$HOME/.cargo/bin:$PATH"
    rustup show >/dev/null 2>&1 || rustup toolchain install stable -y >/dev/null

    log "[3/8] Fetching Vaultwarden source @ $VW_TAG ..."
    if [[ ! -d "$SRCDIR/.git" ]]; then
      mkdir -p "$(dirname "$SRCDIR")"
      git clone --depth=1 --branch "$VW_TAG" https://github.com/dani-garcia/vaultwarden "$SRCDIR" >/dev/null
    else
      (cd "$SRCDIR" && git fetch --tags --depth=1 >/dev/null && git checkout "$VW_TAG" >/dev/null)
    fi

    log "[4/8] Building Vaultwarden (SQLite) with locked deps (stable)..."
    set +e
    (cd "$SRCDIR" && cargo +stable build --release --locked --features sqlite) 2>&1 | tee "$LOGDIR/build.log"
    rc=${{PIPESTATUS[0]}}
    set -e
    if [[ $rc -ne 0 ]]; then
      log "[4b/8] Stable build failed (rc=$rc). Installing nightly and retrying..."
      rustup toolchain install nightly -y >/dev/null
      (cd "$SRCDIR" && cargo +nightly build --release --locked --features sqlite) 2>&1 | tee -a "$LOGDIR/build.log"
    fi

    log "[5/8] Installing binary..."
    install -m 0755 "$SRCDIR/target/release/vaultwarden" "$BINDIR/vaultwarden"

    log "[6/8] Fetching Bitwarden Web Vault assets..."
    rm -rf "$WEBDIR.tmp" && mkdir -p "$WEBDIR.tmp"
    got=0

    try_fetch() {{
      url="$1"
      if [[ -z "$url" ]]; then return 1; fi
      fname="$TMPDIR/webvault.$(echo "$url" | sed -E 's/.*\\.(tar\\.gz|zip)$/\\1/')"
      if [[ "$url" =~ \\.zip$ ]]; then
        command -v unzip >/dev/null 2>&1 || {{ log "  unzip not found; skipping zip asset"; return 1; }}
        curl -fL "$url" -o "$TMPDIR/webvault.zip" || return 1
        rm -rf "$WEBDIR.tmp" && mkdir -p "$WEBDIR.tmp"
        unzip -oq "$TMPDIR/webvault.zip" -d "$WEBDIR.tmp" || return 1
      else
        curl -fL "$url" -o "$TMPDIR/webvault.tgz" || return 1
        rm -rf "$WEBDIR.tmp" && mkdir -p "$WEBDIR.tmp"
        tar -xzf "$TMPDIR/webvault.tgz" -C "$WEBDIR.tmp" || return 1
      fi
      # find a dir that has index.html and copy its content into $WEBDIR
      target="$(find "$WEBDIR.tmp" -type f -name index.html -print -quit 2>/dev/null || true)"
      if [[ -n "$target" ]]; then
        srcdir="$(dirname "$target")"
        rm -rf "$WEBDIR" && mkdir -p "$WEBDIR"
        (cd "$srcdir" && tar -cf - .) | (cd "$WEBDIR" && tar -xf -)
        got=1
        return 0
      fi
      return 1
    }}

    # candidate URLs (official + legacy fallback)
    urls=()
    urls+=($(curl -fsSL https://api.github.com/repos/vaultwarden/web-vault/releases/latest \
      | grep -Eo '"browser_download_url": *"[^"]+\\.(tar\\.gz|zip)"' | cut -d'"' -f4 || true))
    urls+=("https://github.com/dani-garcia/bw_web_builds/releases/latest/download/bw_web_latest.tar.gz")

    for u in "${{urls[@]}}"; do
      log "  Trying $u"
      if try_fetch "$u"; then
        log "  Web vault installed to $WEBDIR"
        break
      fi
    done

    if [[ $got -eq 0 ]]; then
      log "  Skipped web vault download (asset missing?); server will run headless until WEB_VAULT_FOLDER is provided."
    fi

    log "[7/8] Writing env file..."
    ENVFILE="$APPDIR/.env"
    touch "$ENVFILE"
    grep -q '^DATA_FOLDER=' "$ENVFILE" || echo "DATA_FOLDER=$DATADIR" >> "$ENVFILE"
    if [[ -f "$WEBDIR/index.html" ]]; then
      grep -q '^WEB_VAULT_FOLDER=' "$ENVFILE" || echo "WEB_VAULT_FOLDER=$WEBDIR" >> "$ENVFILE"
    fi
    grep -q '^ROCKET_ADDRESS=' "$ENVFILE" || echo "ROCKET_ADDRESS=127.0.0.1" >> "$ENVFILE"
    # ROCKET_PORT is injected by start script

    log "[8/8] Done. Start the app with:  $APPDIR/start"
    echo "Binary: $BINDIR/vaultwarden"
    echo "Logs:   $LOGDIR/vaultwarden.log"
    ''')

    # -------- start/stop/status/restart/check/view-logs ----------
    start = textwrap.dedent(f'''\
    #!/usr/bin/env bash
    set -Eeuo pipefail

    APPDIR="$(cd "$(dirname "$0")" && pwd)"
    LOGDIR="$HOME/logs/apps/{appname}"
    RUNDIR="$APPDIR/run"
    BINDIR="$APPDIR/bin"
    PIDFILE="$RUNDIR/vaultwarden.pid"
    APP_NAME="{appname}"
    PORT="{port}"

    mkdir -p "$LOGDIR" "$RUNDIR"
    export PATH="$HOME/.cargo/bin:$PATH"
    # env overrides
    if [[ -f "$APPDIR/.env" ]]; then set -a; source "$APPDIR/.env"; set +a; fi
    export ROCKET_ADDRESS="${{ROCKET_ADDRESS:-127.0.0.1}}"
    export ROCKET_PORT="$PORT"
    export DATA_FOLDER="${{DATA_FOLDER:-$APPDIR/data}}"

    # web vault wiring
    if [[ -f "$APPDIR/web/index.html" ]]; then
      export WEB_VAULT_FOLDER="$APPDIR/web"
      unset WEB_VAULT_ENABLED
    else
      export WEB_VAULT_ENABLED=false
    fi

    # soft ulimit bump (ignore failure)
    ulimit -n 4096 || true

    # stop previous
    if [[ -s "$PIDFILE" ]] && ps -p "$(cat "$PIDFILE")" -o comm= | grep -q vaultwarden; then
      kill "$(cat "$PIDFILE")" || true
      sleep 0.2
    fi

    # launch
    nohup "$BINDIR/vaultwarden" >> "$LOGDIR/vaultwarden.log" 2>&1 &
    echo $! > "$PIDFILE"
    echo "Started Vaultwarden on 127.0.0.1:$PORT (pid $(cat "$PIDFILE")). Logs: $LOGDIR/vaultwarden.log"
    ''')

    stop = textwrap.dedent('''\
    #!/usr/bin/env bash
    set -Eeuo pipefail
    APPDIR="$(cd "$(dirname "$0")" && pwd)"
    PIDFILE="$APPDIR/run/vaultwarden.pid"
    if [[ -s "$PIDFILE" ]] && ps -p "$(cat "$PIDFILE")" >/dev/null 2>&1; then
      kill "$(cat "$PIDFILE")" || true
      sleep 0.3
    fi
    rm -f "$PIDFILE"
    echo "Stopped."
    ''')

    status = textwrap.dedent('''\
    #!/usr/bin/env bash
    set -Eeuo pipefail
    PIDFILE="$(cd "$(dirname "$0")" && pwd)/run/vaultwarden.pid"
    if [[ -s "$PIDFILE" ]] && ps -p "$(cat "$PIDFILE")" -o pid=,comm=; then
      exit 0
    else
      echo "Not running"
      exit 1
    fi
    ''')

    restart = textwrap.dedent('''\
    #!/usr/bin/env bash
    set -Eeuo pipefail
    "$(cd "$(dirname "$0")" && pwd)/stop" || true
    exec "$(cd "$(dirname "$0")" && pwd)/start"
    ''')

    check = textwrap.dedent(f'''\
    #!/usr/bin/env bash
    set -Eeuo pipefail
    APPDIR="$(cd "$(dirname "$0")" && pwd)"
    PORT="{port}"
    if ! curl -fsS "http://127.0.0.1:$PORT/alive" >/dev/null 2>&1; then
      "$APPDIR/start"
    fi
    ''')

    view_logs = textwrap.dedent(f'''\
    #!/usr/bin/env bash
    LOGFILE="$HOME/logs/apps/{appname}/vaultwarden.log"
    if command -v less >/dev/null 2>&1; then
      exec less +F "$LOGFILE"
    else
      exec tail -f "$LOGFILE"
    fi
    ''')

    readme = textwrap.dedent(f'''\
    Vaultwarden (from source) — Opalstack app "{appname}"

    Files & dirs:
      App dir:         {appdir}
      Binary:          {appdir}/bin/vaultwarden
      Data:            {appdir}/data
      Web vault:       {appdir}/web  (contains index.html once fetched)
      Logs:            {logsdir}/vaultwarden.log

    Commands:
      {appdir}/finish_install.sh    # builds/updates server + downloads Web Vault
      {appdir}/start                # starts on 127.0.0.1:{port}
      {appdir}/stop
      {appdir}/status
      {appdir}/restart
      {appdir}/view-logs

    Notes:
      • Web vault is the static UI. If not present, server runs headless (API only).
      • Configure env in {appdir}/.env (e.g., SMTP_*, ADMIN_TOKEN, SIGNUPS_ALLOWED).
      • Add a Route in the panel to expose your domain → this app.
    ''')

    # write files (no duplicates)
    create_file(f'{appdir}/finish_install.sh', finish_install, perms=0o700)
    create_file(f'{appdir}/start',  start,  perms=0o700)
    create_file(f'{appdir}/stop',   stop,   perms=0o700)
    create_file(f'{appdir}/status', status, perms=0o700)
    create_file(f'{appdir}/restart',restart,perms=0o700)
    create_file(f'{appdir}/check',  check,  perms=0o700)
    create_file(f'{appdir}/view-logs', view_logs, perms=0o700)
    create_file(f'{appdir}/README.txt', readme, perms=0o600)

    # minimal .env defaults (non-secret)
    base_env = textwrap.dedent(f"""\
    DATA_FOLDER={appdir}/data
    ROCKET_ADDRESS=127.0.0.1
    # ROCKET_PORT is set by start script to {port}
    # Example mail config (uncomment + edit):
    # SMTP_HOST=smtp.yourdomain
    # SMTP_FROM=vaultwarden@yourdomain
    # SMTP_PORT=587
    # SMTP_SECURITY=starttls
    # SMTP_USERNAME=
    # SMTP_PASSWORD=
    # SIGNUPS_ALLOWED=false
    """)
    create_file(f'{appdir}/.env', base_env, perms=0o600)

    # cron: health check every ~10 minutes (skewed) + nightly refresh of web-vault
    m = random.randint(0,9)
    add_cronjob_once(f'0{m},2{m},4{m} * * * * {appdir}/check > /dev/null 2>&1')
    hh = random.randint(3,5); mm = random.randint(0,59)
    add_cronjob_once(f'{mm} {hh} * * * {appdir}/finish_install.sh > /dev/null 2>&1')

    # ---- REQUIRED PANEL SIGNALS ----
    msg = f'Vaultwarden (from source) bootstrapped on port:{port}. Run finish_install.sh, then start.'
    installed_payload = json.dumps([{{'id': a.app_uuid}}])
    api.post('/app/installed/', installed_payload)  # marks app as installed
    notice_payload = json.dumps([{{'type': 'D', 'content': msg}}])
    api.post('/notice/create/', notice_payload)     # dashboard notice

    logging.info(f'Completed bootstrap for {appname}. Next: {appdir}/finish_install.sh then {appdir}/start')

if __name__ == '__main__':
    main()
