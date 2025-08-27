#!/usr/bin/python3
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random

API_HOST = (os.environ.get('API_URL') or 'https://my.opalstack.com').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'

# ---------------- helpers ----------------

def create_file(path, contents, writemode='w', perms=0o600):
    with open(path, writemode) as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f'Created file {path} {oct(perms)}')

def gen_password(length=24):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

def run_command(cmd, cwd=None, env=None):
    env = env or os.environ.copy()
    logging.info(f'Running: {cmd}')
    try:
        return subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env)
    except subprocess.CalledProcessError as e:
        out = getattr(e, 'output', b'')
        if out:
            logging.error(out.decode(errors='ignore'))
        sys.exit(e.returncode)

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

# --------------- payloads ----------------

FINISH_INSTALL = r'''#!/usr/bin/env bash
set -Eeuo pipefail

APPDIR="__APPDIR__"
PORT="__PORT__"
LOGDIR="__LOGDIR__"

RUNDIR="$APPDIR/run"
SRCDIR="$APPDIR/src/vaultwarden"
BINDIR="$APPDIR/bin"
DATADIR="$APPDIR/data"
WEBDIR="$APPDIR/web"
TMPDIR="$APPDIR/tmp"
BUILDLOG="$LOGDIR/build.log"

mkdir -p "$RUNDIR" "$LOGDIR" "$TMPDIR" "$BINDIR" "$DATADIR" "$WEBDIR"
: > "$BUILDLOG"

step() { echo "[$(date +'%F %T')] $*" | tee -a "$BUILDLOG"; }

step "[1/8] Checking build prerequisites..."
need_tools=(git curl bash)
for t in "${need_tools[@]}"; do
  command -v "$t" >/dev/null || { echo "Missing $t. Install it and re-run." | tee -a "$BUILDLOG"; exit 1; }
done

step "[2/8] Ensuring rustup (user-local) is installed and stable toolchain available..."
if ! command -v rustup >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal | tee -a "$BUILDLOG"
fi
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
export PATH="$HOME/.cargo/bin:$PATH"
rustup toolchain install stable -y >/dev/null 2>&1 || true
rustup default stable >/dev/null 2>&1 || true

step "[3/8] Fetching Vaultwarden source @ 1.34.3 ..."
VW_TAG="1.34.3"
if [ ! -d "$SRCDIR/.git" ]; then
  mkdir -p "$(dirname "$SRCDIR")"
  git clone --depth=1 --branch "$VW_TAG" https://github.com/dani-garcia/vaultwarden "$SRCDIR" | tee -a "$BUILDLOG"
else
  (cd "$SRCDIR" && git fetch --tags --force && git checkout "$VW_TAG" && git reset --hard "refs/tags/$VW_TAG") | tee -a "$BUILDLOG"
fi

step "[4/8] Building Vaultwarden (SQLite) with locked deps (stable)..."
cd "$SRCDIR"
set +e
CARGO_TERM_COLOR=never cargo build --release --locked --features sqlite 2>&1 | tee -a "$BUILDLOG"
RC=${PIPESTATUS[0]}
set -e

if [ $RC -ne 0 ]; then
  step "[4b/8] Stable build failed (rc=$RC). Installing nightly and retrying..."
  rustup toolchain install nightly -y | tee -a "$BUILDLOG"
  CARGO_TERM_COLOR=never cargo +nightly build --release --locked --features sqlite 2>&1 | tee -a "$BUILDLOG"
fi

step "[5/8] Installing binary..."
install -D -m 0755 "$SRCDIR/target/release/vaultwarden" "$BINDIR/vaultwarden"

step "[6/8] Fetching Bitwarden Web Vault assets..."
WEB_URL="https://github.com/dani-garcia/bw_web_builds/releases/latest/download/bw_web_latest.tar.gz"
if curl -L --fail -o "$TMPDIR/bw_web_latest.tar.gz" "$WEB_URL" 2>>"$BUILDLOG"; then
  rm -rf "$WEBDIR"/* || true
  tar -xzf "$TMPDIR/bw_web_latest.tar.gz" -C "$WEBDIR" --strip-components=1
  step "Web vault unpacked to $WEBDIR"
else
  step "Skipped web vault download (asset missing?); server will run headless until WEB_VAULT_FOLDER is provided."
fi

step "[7/8] Writing env file..."
ENVFILE="$APPDIR/.env"
if [ ! -f "$ENVFILE" ]; then
  cat > "$ENVFILE" <<'EOF'
# Vaultwarden runtime configuration
DOMAIN=""
ADMIN_TOKEN="__ADMIN_TOKEN__"
SIGNUPS_ALLOWED=false

ROCKET_ADDRESS=127.0.0.1
ROCKET_PORT=__PORT__

DATA_FOLDER="__APPDIR__/data"
WEB_VAULT_FOLDER="__APPDIR__/web"

# SMTP (optional)
SMTP_HOST=""
SMTP_PORT=587
SMTP_FROM="vaultwarden@yourdomain"
SMTP_USERNAME=""
SMTP_PASSWORD=""
SMTP_SECURITY=starttls
EOF
  sed -i "s#__PORT#__PORT__#g; s#__APPDIR__#__APPDIR__#g; s#__ADMIN_TOKEN__#__ADMIN_TOKEN__#g" "$ENVFILE"
fi

step "[8/8] Done. Start the app with:  $APPDIR/start"
echo "Binary: $BINDIR/vaultwarden"
echo "Logs:   $LOGDIR/vaultwarden.log"
'''

START_SH = r'''#!/usr/bin/env bash
set -Eeuo pipefail
APPDIR="__APPDIR__"
PORT="__PORT__"
LOGDIR="__LOGDIR__"
BINDIR="$APPDIR/bin"
RUNDIR="$APPDIR/run"
ENVFILE="$APPDIR/.env"
PIDFILE="$RUNDIR/vaultwarden.pid"
mkdir -p "$RUNDIR" "$LOGDIR"

if [ ! -x "$BINDIR/vaultwarden" ]; then
  echo "vaultwarden binary not found. Run $APPDIR/finish_install.sh first."
  exit 1
fi

[ -f "$ENVFILE" ] && source "$ENVFILE"

export ROCKET_ADDRESS="${ROCKET_ADDRESS:-127.0.0.1}"
export ROCKET_PORT="${ROCKET_PORT:-$PORT}"
export DATA_FOLDER="${DATA_FOLDER:-$APPDIR/data}"
export WEB_VAULT_FOLDER="${WEB_VAULT_FOLDER:-$APPDIR/web}"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "Already running (pid $(cat "$PIDFILE"))."
  exit 0
fi

ulimit -n 65536 || true
mkdir -p "$DATA_FOLDER" "$WEB_VAULT_FOLDER"

nohup "$BINDIR/vaultwarden" >>"$LOGDIR/vaultwarden.log" 2>&1 &
echo $! > "$PIDFILE"
echo "Started Vaultwarden on 127.0.0.1:${ROCKET_PORT} (pid $(cat "$PIDFILE")). Logs: $LOGDIR/vaultwarden.log"
'''

STOP_SH = r'''#!/usr/bin/env bash
set -Eeuo pipefail
APPDIR="__APPDIR__"
RUNDIR="$APPDIR/run"
PIDFILE="$RUNDIR/vaultwarden.pid"

if [ -f "$PIDFILE" ]; then
  PID=$(cat "$PIDFILE")
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    for i in {1..30}; do
      kill -0 "$PID" 2>/dev/null || break
      sleep 1
    done
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" || true
  fi
  rm -f "$PIDFILE"
  echo "Stopped."
else
  echo "Not running."
fi
'''

STATUS_SH = r'''#!/usr/bin/env bash
set -Eeuo pipefail
APPDIR="__APPDIR__"
RUNDIR="$APPDIR/run"
PIDFILE="$RUNDIR/vaultwarden.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "Running (pid $(cat "$PIDFILE"))."
  exit 0
else
  echo "Not running."
  exit 1
fi
'''

RESTART_SH = r'''#!/usr/bin/env bash
set -Eeuo pipefail
"__APPDIR__/stop" || true
"__APPDIR__/start"
'''

VIEWLOGS_SH = r'''#!/usr/bin/env bash
set -Eeuo pipefail
LOGDIR="__LOGDIR__"
mkdir -p "$LOGDIR"
tail -n 200 -F "$LOGDIR/vaultwarden.log"
'''

README = r'''Vaultwarden (source-build) — Opalstack

Where things live
-----------------
App dir:            __APPDIR__
Logs dir (rotated): __LOGDIR__
Binary:             __APPDIR__/bin/vaultwarden
Data:               __APPDIR__/data
Web vault:          __APPDIR__/web
Env file:           __APPDIR__/.env

Commands
--------
Finish install (build from source, fetch web vault):
  __APPDIR__/finish_install.sh

Start/Stop/Status/Restart:
  __APPDIR__/start
  __APPDIR__/stop
  __APPDIR__/status
  __APPDIR__/restart

Follow logs:
  __APPDIR__/view-logs    (tails __LOGDIR__/vaultwarden.log)

After first start
-----------------
1) Edit __APPDIR__/.env
   - set DOMAIN to your public https URL.
   - set SMTP_* if you want email.
2) Point your Opalstack proxy/site to this app’s port: __PORT__
   Vaultwarden listens on 127.0.0.1:__PORT__ (Rocket).
3) Restart:
   __APPDIR__/restart

Notes
-----
- Uses SQLite by default (no external DB).
- Single port (__PORT__) is used internally; expose via Opalstack proxy.
- All logs land in __LOGDIR__ for your global rotation.
'''

# --------------- main ----------------

def main():
    p = argparse.ArgumentParser(description='Installs Vaultwarden (source build) on Opalstack')
    p.add_argument('-i', dest='app_uuid',   default=os.environ.get('UUID'))
    p.add_argument('-n', dest='app_name',   default=os.environ.get('APPNAME'))
    p.add_argument('-t', dest='opal_token', default=os.environ.get('OPAL_TOKEN'))
    p.add_argument('-u', dest='opal_user',  default=os.environ.get('OPAL_USER'))
    p.add_argument('-p', dest='opal_pass',  default=os.environ.get('OPAL_PASS'))
    a = p.parse_args()

    if not a.app_uuid:
        print('Missing UUID', file=sys.stderr); sys.exit(1)

    # basic logging to the global app log
    # we’ll swap in the real path once we know user/app
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')

    api = OpalstackAPITool(API_HOST, API_BASE_URI, a.opal_token, a.opal_user, a.opal_pass)
    app = api.get(f'/app/read/{a.app_uuid}')
    if not app.get('name'):
        logging.error('App not found'); sys.exit(1)

    appname = app['name']
    osuser  = app['osuser_name']
    port    = app['port']

    appdir  = f'/home/{osuser}/apps/{appname}'
    logdir  = f'/home/{osuser}/logs/apps/{appname}'

    # reconfigure logging to the proper install log
    for h in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(h)
    os.makedirs(logdir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s: %(message)s',
        handlers=[logging.FileHandler(f'{logdir}/install.log'), logging.StreamHandler(sys.stdout)]
    )

    # layout
    run_command(f'mkdir -p {appdir}/run {appdir}/src {appdir}/tmp {appdir}/bin {appdir}/data {appdir}/web')
    run_command(f'mkdir -p {logdir}')

    # materialize scripts with placeholders
    admin_token = os.urandom(16).hex()

    def sub(s: str) -> str:
        return (s.replace('__APPDIR__', appdir)
                 .replace('__PORT__', str(port))
                 .replace('__LOGDIR__', logdir)
                 .replace('__ADMIN_TOKEN__', admin_token))

    create_file(f'{appdir}/finish_install.sh', sub(FINISH_INSTALL), perms=0o700)
    create_file(f'{appdir}/start',             sub(START_SH),      perms=0o700)
    create_file(f'{appdir}/stop',              sub(STOP_SH),       perms=0o700)
    create_file(f'{appdir}/status',            sub(STATUS_SH),     perms=0o700)
    create_file(f'{appdir}/restart',           sub(RESTART_SH),    perms=0o700)
    create_file(f'{appdir}/view-logs',         sub(VIEWLOGS_SH),   perms=0o700)
    create_file(f'{appdir}/README.txt',        sub(README),        perms=0o600)

    # panel signals
    msg = f'Vaultwarden (source-build) installed. Run finish_install.sh, then start. Port:{port}.'
    api.post('/app/installed/', json.dumps([{'id': a.app_uuid}]))
    api.post('/notice/create/', json.dumps([{'type': 'D', 'content': msg}]))

    logging.info(f'Completed bootstrap for {appname}. Next: {appdir}/finish_install.sh then {appdir}/start')

if __name__ == '__main__':
    main()
