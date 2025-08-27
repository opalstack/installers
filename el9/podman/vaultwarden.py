#!/usr/bin/python3
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random

# --------- Opalstack API ----------
API_HOST = (os.environ.get('API_URL') or 'https://my.opalstack.com').replace('https://','').replace('http://','')
API_BASE_URI = '/api/v1'

# --------- Exec env ----------
BASE_ENV = os.environ.copy()
BASE_ENV['PATH'] = '/usr/local/bin:/usr/bin:/bin:' + BASE_ENV.get('PATH','')

def run_command(cmd, cwd=None, env=None):
    if env is None:
        env = BASE_ENV
    logging.info(f'Running: {cmd}')
    try:
        return subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env)
    except subprocess.CalledProcessError as e:
        logging.debug(getattr(e, 'output', b'')); sys.exit(e.returncode)

def create_file(path, contents, writemode='w', perms=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, writemode) as f: f.write(contents)
    os.chmod(path, perms); logging.info(f'Created file {path} {oct(perms)}')

def gen_password(length=20):
    import secrets, string
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

# --------- API helper ----------
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

# --------- finish_install.sh (build-from-source) ----------
FINISH_SH = r'''#!/usr/bin/env bash
set -Eeuo pipefail

# --- paths ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPDIR="$SCRIPT_DIR"
SRCDIR="$APPDIR/src/vaultwarden"
RUNDIR="$APPDIR/run"
LOGDIR="$APPDIR/logs"
TMPDIR="$APPDIR/tmp"
BUILDLOG="$LOGDIR/build.log"

msg(){ printf '%s\n' "$*" >&2; }
step(){ msg "[$1] $2"; }

mkdir -p "$RUNDIR" "$LOGDIR" "$TMPDIR" "$APPDIR/src"

# Prefer user-local rust/cargo
export PATH="$HOME/.cargo/bin:$PATH"
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env" || true
hash -r

# [1/8] prerequisites
step "1/8" "Checking build prerequisites..."
missing=()
need_bins=(git curl tar gzip which awk sed pkg-config gcc g++ make)
for b in "${need_bins[@]}"; do command -v "$b" >/dev/null 2>&1 || missing+=("$b"); done
openssl_ok=1; sqlite_ok=1
pkg-config --exists openssl || openssl_ok=0
pkg-config --exists sqlite3 || sqlite_ok=0
if ((${#missing[@]})); then
  msg "WARN: Missing tools: ${missing[*]}"
  msg "     EL9: sudo dnf install -y gcc gcc-c++ make pkgconf-pkg-config openssl-devel sqlite-devel git curl tar"
fi
if ((openssl_ok==0 || sqlite_ok==0)); then
  msg "WARN: Missing devel headers (openssl/sqlite3)."
fi

# [2/8] rustup + stable
step "2/8" "Ensuring rustup (user-local) is installed and stable toolchain available..."
if ! command -v rustup >/dev/null 2>&1; then
  curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile=minimal --default-toolchain stable
  [ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env" || true
  export PATH="$HOME/.cargo/bin:$PATH"
  hash -r
fi
rustup toolchain list >/dev/null 2>&1 || rustup self update
rustup toolchain install stable -c rustc -c cargo >/dev/null 2>&1 || true
rustup default stable >/dev/null 2>&1 || true

# [3/8] fetch source
WV_TAG="${WV_TAG:-1.34.3}"
step "3/8" "Fetching Vaultwarden source @ ${WV_TAG} ..."
if [ ! -d "$SRCDIR/.git" ]; then
  git clone --depth=1 --branch "$WV_TAG" https://github.com/dani-garcia/vaultwarden.git "$SRCDIR"
else
  ( cd "$SRCDIR"
    git fetch --tags --prune
    git checkout -f "$WV_TAG"
    git reset --hard "refs/tags/$WV_TAG" || true
    git clean -fdx
  )
fi

# [4/8] build (stable) with locked deps
step "4/8" "Building Vaultwarden (SQLite) with locked deps (stable)..."
: > "$BUILDLOG"
cd "$SRCDIR"
set +e
cargo -V | tee -a "$BUILDLOG"
CARGO_PROFILE=release
cargo build --$CARGO_PROFILE --locked --features sqlite 2>&1 | tee -a "$BUILDLOG"
rc=${PIPESTATUS[0]}
set -e

# [4b/8] fallback to nightly
if [ $rc -ne 0 ]; then
  step "4b/8" "Stable build failed (rc=$rc). Installing nightly and retrying..."
  rustup toolchain install nightly -c rustc -c cargo
  set +e
  cargo +nightly build --$CARGO_PROFILE --locked --features sqlite 2>&1 | tee -a "$BUILDLOG"
  rc=${PIPESTATUS[0]}
  set -e
  if [ $rc -ne 0 ]; then
    msg "ERROR: Build failed again. See $BUILDLOG"
    exit 1
  fi
  USED_TOOLCHAIN="+nightly"
else
  USED_TOOLCHAIN="(stable)"
fi

# [5/8] place binary
step "5/8" "Placing binary into $RUNDIR ..."
cp -f "$SRCDIR/target/$CARGO_PROFILE/vaultwarden" "$RUNDIR/vaultwarden"
chmod 0755 "$RUNDIR/vaultwarden"

# [6/8] .env
ENVFILE="$RUNDIR/.env"
if [ ! -f "$ENVFILE" ]; then
  step "6/8" "Creating default .env in $RUNDIR ..."
  ADMIN_TOKEN="$(head -c 16 /dev/urandom | xxd -p)"
  cat > "$ENVFILE" <<EOF
ROCKET_ADDRESS=127.0.0.1
ROCKET_PORT=\${PORT:-8812}
RUST_LOG=info
DATA_FOLDER=$RUNDIR/data
WEB_VAULT_ENABLED=false
SIGNUPS_ALLOWED=false
ADMIN_TOKEN=$ADMIN_TOKEN
# DOMAIN=https://vault.example.com
EOF
fi
mkdir -p "$RUNDIR/data" "$RUNDIR/web-vault"

# [7/8] helpers
step "7/8" "Writing start/stop/status/logs helpers..."
cat > "$RUNDIR/start" <<'EOS'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
APPDIR="$PWD"
PIDFILE="$APPDIR/vaultwarden.pid"
LOGFILE="$APPDIR/vaultwarden.log"
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env" || true
export PATH="$HOME/.cargo/bin:$PATH"
source "$APPDIR/.env" 2>/dev/null || true
mkdir -p "$APPDIR/data"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "vaultwarden already running (pid $(cat "$PIDFILE"))"
  exit 0
fi
echo "Starting vaultwarden on ${ROCKET_ADDRESS:-127.0.0.1}:${ROCKET_PORT:-8812} ..."
nohup "$APPDIR/vaultwarden" >> "$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
sleep 0.5
echo "OK (pid $(cat "$PIDFILE"))"
EOS
chmod +x "$RUNDIR/start"

cat > "$RUNDIR/stop" <<'EOS'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
PIDFILE="$PWD/vaultwarden.pid"
if [ -f "$PIDFILE" ]; then
  pid=$(cat "$PIDFILE")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    for _ in {1..20}; do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" || true
  fi
  rm -f "$PIDFILE"
  echo "Stopped (pid $pid)"
else
  echo "Not running"
fi
EOS
chmod +x "$RUNDIR/stop"

cat > "$RUNDIR/status" <<'EOS'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
PIDFILE="$PWD/vaultwarden.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "vaultwarden running (pid $(cat "$PIDFILE"))"
  exit 0
else
  echo "vaultwarden not running"
  exit 1
fi
EOS
chmod +x "$RUNDIR/status"

cat > "$RUNDIR/logs" <<'EOS'
#!/usr/bin/env bash
cd "$(dirname "$0")"
exec tail -n 200 -F "./vaultwarden.log"
EOS
chmod +x "$RUNDIR/logs"

# [8/8] README
step "8/8" "Writing README..."
cat > "$APPDIR/README.md" <<EOF
# Vaultwarden (built from source) — EL9 / userland

## Paths
- Binary: \`$RUNDIR/vaultwarden\`
- Env:    \`$RUNDIR/.env\`
- Data:   \`$RUNDIR/data\`
- Logs:   \`$RUNDIR/vaultwarden.log\`
- PID:    \`$RUNDIR/vaultwarden.pid\`

## Start/Stop
\`\`\`bash
$RUNDIR/start
$RUNDIR/status
$RUNDIR/logs
$RUNDIR/stop
\`\`\`

## Reverse proxy
Terminate TLS on your panel and proxy to \`127.0.0.1:\${ROCKET_PORT:-8812}\`.
EOF

msg ""
msg "=== DONE ==="
msg "Binary:   $RUNDIR/vaultwarden"
msg "Helpers:  $RUNDIR/{start,stop,status,logs}"
msg "Config:   $RUNDIR/.env   (ADMIN_TOKEN set)"
msg "BuildLog: $BUILDLOG"
'''

# --------- thin wrappers in app root ----------
WRAP_START = '''#!/bin/bash
set -Eeuo pipefail
APPDIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -x "$APPDIR/run/start" ]; then
  echo "Finish step not completed. Run $APPDIR/finish_install.sh first."
  exit 1
fi
exec "$APPDIR/run/start"
'''
WRAP_STOP = '''#!/bin/bash
set -Eeuo pipefail
APPDIR="$(cd "$(dirname "$0")" && pwd)"
[ -x "$APPDIR/run/stop" ] && exec "$APPDIR/run/stop" || echo "Not installed yet."
'''
WRAP_STATUS = '''#!/bin/bash
set -Eeuo pipefail
APPDIR="$(cd "$(dirname "$0")" && pwd)"
[ -x "$APPDIR/run/status" ] && exec "$APPDIR/run/status" || echo "Not installed yet."
'''
WRAP_LOGS = '''#!/bin/bash
set -Eeuo pipefail
APPDIR="$(cd "$(dirname "$0")" && pwd)"
[ -x "$APPDIR/run/logs" ] && exec "$APPDIR/run/logs" || echo "No logs yet."
'''

def main():
    p = argparse.ArgumentParser(description='Prepare Vaultwarden (build-from-source) on Opalstack')
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

    # directories
    run_command(f'mkdir -p {appdir}/run {appdir}/logs {appdir}/src {appdir}/tmp')

    # finish installer
    create_file(f'{appdir}/finish_install.sh', FINISH_SH, perms=0o700)

    # lightweight wrappers in app root for panel buttons
    create_file(f'{appdir}/start',  WRAP_START,  perms=0o700)
    create_file(f'{appdir}/stop',   WRAP_STOP,   perms=0o700)
    create_file(f'{appdir}/status', WRAP_STATUS, perms=0o700)
    create_file(f'{appdir}/logs',   WRAP_LOGS,   perms=0o700)

    # README for the panel user
    readme = textwrap.dedent(f"""\
    Vaultwarden (build-from-source) scaffold created.

    Next step:
      1) Run: {appdir}/finish_install.sh
      2) Start: {appdir}/start  (calls {appdir}/run/start)
      3) Reverse proxy your domain to 127.0.0.1:8812 (default) or edit {appdir}/run/.env

    Notes:
      - Data directory: {appdir}/run/data
      - Logs:           {appdir}/run/vaultwarden.log
      - Port:           default 8812 (private). Your panel should front it on port 443 for your domain.
    """)
    create_file(f'{appdir}/README.txt', readme, perms=0o600)

    # ---- REQUIRED PANEL SIGNALS ----
    msg = f'Vaultwarden scaffold ready. Run finish_install.sh to build. Will listen on 127.0.0.1:8812 by default.'
    installed_payload = json.dumps([{'id': a.app_uuid}])
    api.post('/app/installed/', installed_payload)  # marks app as installed
    notice_payload = json.dumps([{'type': 'D', 'content': msg}])
    api.post('/notice/create/', notice_payload)     # dashboard notice

    logging.info(f'Completed scaffold for app {a.app_name} - {msg}')

if __name__ == '__main__':
    main()
