#!/usr/bin/python3
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random, re

API_HOST = (os.environ.get('API_URL') or 'https://my.opalstack.com').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'

# ===== Helpers =====
def run(cmd, cwd=None, env=None, check=True, capture=True):
    logging.info(f'Running: {cmd}')
    if capture:
        p = subprocess.run(shlex.split(cmd), cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if check and p.returncode != 0:
            logging.error(p.stdout)
            sys.exit(p.returncode)
        return p.stdout.strip()
    else:
        p = subprocess.run(shlex.split(cmd), cwd=cwd, env=env)
        if check and p.returncode != 0:
            sys.exit(p.returncode)
        return ""

def create_file(path, contents, perms=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f: f.write(contents)
    os.chmod(path, perms)
    logging.info(f'Created {path} ({oct(perms)})')

def rand_token(n=32):
    import secrets, string
    return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))

def gh_api(host, path):
    conn = http.client.HTTPSConnection(host, timeout=20)
    conn.request('GET', path, headers={'User-Agent':'vw-installer'})
    resp = conn.getresponse()
    data = resp.read()
    if resp.status != 200:
        logging.warning(f'GitHub API {path} -> {resp.status}')
        return {}
    try:
        return json.loads(data)
    except Exception:
        return {}

def get_latest_release_tag(repo):
    # repo: "dani-garcia/vaultwarden"
    data = gh_api('api.github.com', f'/repos/{repo}/releases/latest')
    tag = (data.get('tag_name') or '').strip()
    return tag

def get_latest_asset_url(repo, pattern):
    # find first asset whose name matches regex pattern
    data = gh_api('api.github.com', f'/repos/{repo}/releases/latest')
    assets = data.get('assets', []) or []
    rx = re.compile(pattern)
    for a in assets:
        name = a.get('name') or ''
        if rx.search(name):
            return a.get('browser_download_url')
    return ""

class OpalstackAPI:
    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host
        self.base_uri = base_uri
        if not authtoken:
            conn = http.client.HTTPSConnection(self.host)
            payload = json.dumps({'username': user, 'password': password})
            conn.request('POST', self.base_uri + '/login/', payload, headers={'Content-type':'application/json'})
            result = json.loads(conn.getresponse().read() or b'{}')
            if not result.get('token'):
                logging.error('Invalid Opalstack username/password (no token).')
                sys.exit(1)
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

def main():
    p = argparse.ArgumentParser(description='Prepare Vaultwarden (source build) on Opalstack')
    p.add_argument('-i', dest='app_uuid',   default=os.environ.get('UUID'))
    p.add_argument('-n', dest='app_name',   default=os.environ.get('APPNAME'))
    p.add_argument('-t', dest='opal_token', default=os.environ.get('OPAL_TOKEN'))
    p.add_argument('-u', dest='opal_user',  default=os.environ.get('OPAL_USER'))
    p.add_argument('-p', dest='opal_pass',  default=os.environ.get('OPAL_PASS'))
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
    if not a.app_uuid:
        logging.error('Missing UUID'); sys.exit(1)

    api = OpalstackAPI(API_HOST, API_BASE_URI, a.opal_token, a.opal_user, a.opal_pass)
    app = api.get(f'/app/read/{a.app_uuid}')
    if not app.get('name'):
        logging.error('App not found'); sys.exit(1)

    appdir = f'/home/{app["osuser_name"]}/apps/{app["name"]}'
    port   = app['port']

    # pull latest tags/urls up front (bake into finish script so it doesn't need jq/etc)
    vw_tag = get_latest_release_tag('dani-garcia/vaultwarden') or 'main'
    # we’ll use prebuilt web vault from bw_web_builds (avoids node)
    web_url = get_latest_asset_url('dani-garcia/bw_web_builds', r'(?:bw_web|web\-vault).*\.(?:tar\.gz|zip)$') or ''

    logging.info(f'Using Vaultwarden tag: {vw_tag}')
    if not web_url:
        logging.warning('Could not locate a prebuilt Web Vault asset in the latest bw_web_builds release; finish_install will still proceed but will skip web UI.')

    # layout
    run(f'mkdir -p {shlex.quote(appdir)}/{{src,build,run,data,logs}}', check=True, capture=False)

    # .env for Vaultwarden (safe defaults; edit later)
    env = textwrap.dedent(f"""\
        # Vaultwarden config
        ROCKET_ADDRESS=127.0.0.1
        ROCKET_PORT={port}
        WEB_VAULT_ENABLED=true
        SIGNUPS_ALLOWED=false
        ADMIN_TOKEN={rand_token(40)}
        DATA_FOLDER={appdir}/data

        # SMTP (edit for email)
        SMTP_HOST=
        SMTP_PORT=587
        SMTP_FROM=vaultwarden@yourdomain
        SMTP_SECURITY=starttls
        SMTP_USERNAME=
        SMTP_PASSWORD=
        """)
    create_file(f'{appdir}/run/.env', env, 0o600)

    # finish_install script (build from source, install web vault, drop run scripts)
    finish = textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -Eeuo pipefail
        APPDIR="{appdir}"
        PORT="{port}"
        VW_TAG="{vw_tag}"
        BW_WEB_URL="{web_url}"
        PATH="$HOME/.cargo/bin:$PATH"

        echo "[1/7] Checking build prerequisites..."
        need(){{ command -v "$1" >/dev/null 2>&1 || {{ echo "Missing required tool: $1" >&2; exit 2; }}; }}
        need git; need curl; need tar
        need pkg-config || need pkgconf
        # headers/libs must be present on the box already per SOP (openssl-devel, sqlite-devel, gcc, make)

        echo "[2/7] Installing Rust toolchain (user-local) if needed..."
        if ! command -v cargo >/dev/null 2>&1; then
          curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
          source "$HOME/.cargo/env"
        fi

        echo "[3/7] Fetching Vaultwarden source @ $VW_TAG ..."
        mkdir -p "$APPDIR/src"
        if [ ! -d "$APPDIR/src/vaultwarden" ]; then
          git clone --depth 1 --branch "$VW_TAG" https://github.com/dani-garcia/vaultwarden.git "$APPDIR/src/vaultwarden"
        else
          git -C "$APPDIR/src/vaultwarden" fetch --tags origin
          git -C "$APPDIR/src/vaultwarden" checkout "$VW_TAG" || true
          git -C "$APPDIR/src/vaultwarden" pull --ff-only || true
        fi

        echo "[4/7] Building Vaultwarden (SQLite backend)... this can take a few minutes"
        cd "$APPDIR/src/vaultwarden"
        export RUSTFLAGS="-C target-cpu=native"
        cargo build --features sqlite --release

        echo "[5/7] Installing binary..."
        install -Dm755 "$APPDIR/src/vaultwarden/target/release/vaultwarden" "$APPDIR/run/vaultwarden"

        echo "[6/7] Installing Web Vault (prebuilt)..."
        mkdir -p "$APPDIR/run/web-vault"
        if [ -n "$BW_WEB_URL" ]; then
          tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
          fname="$tmp/webvault.tar.gz"
          curl -fsSL "$BW_WEB_URL" -o "$fname"
          # accept .tar.gz or .zip
          if file "$fname" | grep -qi zip; then
            (cd "$APPDIR/run/web-vault" && unzip -o "$fname" >/dev/null)
          else
            tar -C "$APPDIR/run/web-vault" -xzf "$fname"
          fi
        else
          echo "WARNING: No Web Vault asset URL baked in; skipping web UI files."
        fi

        echo "[7/7] Creating run scripts..."
        cat > "$APPDIR/run/start" <<'EOS'
        #!/usr/bin/env bash
        set -Eeuo pipefail
        cd "$(dirname "$0")"
        export $(grep -E '^[A-Z0-9_]+=' .env | xargs -d '\\n' -I{{}} echo {{}}) >/dev/null 2>&1 || true
        exec ./vaultwarden
        EOS
        chmod 700 "$APPDIR/run/start"

        cat > "$APPDIR/run/stop" <<'EOS'
        #!/usr/bin/env bash
        set -Eeuo pipefail
        pkill -f '[v]aultwarden' || true
        echo "Stopped Vaultwarden"
        EOS
        chmod 700 "$APPDIR/run/stop"

        cat > "$APPDIR/run/logs" <<'EOS'
        #!/usr/bin/env bash
        set -Eeuo pipefail
        pgrep -a vaultwarden || true
        journalctl --user -u vaultwarden.service -f 2>/dev/null || tail -F ./vaultwarden.log 2>/dev/null || echo "No logs yet."
        EOS
        chmod 700 "$APPDIR/run/logs"

        echo "Done. Start it with: {appdir}/run/start"
        echo "Make sure your Opalstack proxy points to 127.0.0.1:{port}"
        """)

    create_file(f'{appdir}/finish_install.sh', finish, 0o700)

    # tiny helper run scripts (so you have something before finishing)
    start_sh = f"""#!/usr/bin/env bash
set -Eeuo pipefail
exec "{appdir}/run/start"
"""
    stop_sh = f"""#!/usr/bin/env bash
set -Eeuo pipefail
exec "{appdir}/run/stop"
"""
    logs_sh = f"""#!/usr/bin/env bash
set -Eeuo pipefail
exec "{appdir}/run/logs"
"""
    create_file(f'{appdir}/start', start_sh, 0o700)
    create_file(f'{appdir}/stop',  stop_sh,  0o700)
    create_file(f'{appdir}/logs',  logs_sh,  0o700)

    # README
    readme = f"""\
Vaultwarden (source build) — Read Me
===================================

App name: {app['name']}
App dir : {appdir}
Port    : {port}

Phase 1 (already done):
- This installer created:
  - {appdir}/finish_install.sh   -> Build + install
  - {appdir}/run/.env            -> Vaultwarden config (edit as needed)
  - {appdir}/run/ (empty until you run finish_install)

Phase 2 (you run this):
  bash {appdir}/finish_install.sh

That will:
- Install a user-local Rust toolchain (via rustup) if missing
- Clone Vaultwarden @ latest release tag (“{vw_tag}”)
- Build (SQLite backend), copy binary to {appdir}/run/vaultwarden
- Download and unpack a prebuilt Web Vault (no node/npm on box)
- Create run helpers: start/stop/logs

To run:
  {appdir}/run/start
  # then hit https://YOUR_DOMAIN/ (Opalstack proxy → 127.0.0.1:{port})

Configuration:
- Edit {appdir}/run/.env (ADMIN_TOKEN, SMTP_*, SIGNUPS_ALLOWED, etc.)
- We bind 127.0.0.1:{port}; Opalstack’s front proxy publishes it.
- Data lives in {appdir}/data (SQLite).

Notes:
- Build needs ~1.5 GB RAM; runtime is light. See Vaultwarden docs for deps and options.
- If you ever want MySQL/Postgres instead of SQLite, rebuild with cargo features:
    cargo build --features mysql --release
  or
    cargo build --features postgresql --release

"""
    create_file(f'{appdir}/README.txt', readme, 0o600)

    # mark installed in Opalstack panel + notice
    msg = f'Vaultwarden (source) prepared on port:{port}. Run finish_install.sh to build.'
    api.post('/app/installed/', json.dumps([{'id': a.app_uuid}]))
    api.post('/notice/create/', json.dumps([{'type': 'D', 'content': msg}]))

    logging.info('Phase 1 complete. Now run: ' + f'{appdir}/finish_install.sh')

if __name__ == '__main__':
    main()
