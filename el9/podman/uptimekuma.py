#! /bin/python3
# Opalstack Uptime Kuma installer (rootless Podman)
# Places app files in ~/apps/<appname>/ and writes start/stop/logs/update scripts.
# Container: docker.io/louislam/uptime-kuma:1 (listens on 3001). Docs confirm port and /app/data volume.  # refs
# - https://github.com/louislam/uptime-kuma/wiki/%F0%9F%94%A7-How-to-Install

import argparse, sys, logging, os, http.client, json, textwrap, subprocess, shlex, random, secrets, string
from urllib.parse import urlparse

# -------- Config (mirrors core/django/install.py style) ------------------------
API_HOST = os.environ.get('API_URL', '').strip('https://').strip('http://') or 'my.opalstack.com'
API_BASE_URI = '/api/v1'
CMD_ENV = {
    'PATH': '/usr/local/bin:/usr/bin:/bin',
    'UMASK': '0002',
}

# -------- Helpers (lifted/adapted from Django installer) ----------------------
class OpalstackAPITool():
    """simple wrapper for http.client get and post"""
    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host
        self.base_uri = base_uri
        if not authtoken:
            endpoint = self.base_uri + '/login/'
            payload = json.dumps({'username': user, 'password': password})
            conn = http.client.HTTPSConnection(self.host)
            conn.request('POST', endpoint, payload, headers={'Content-type': 'application/json'})
            result = json.loads(conn.getresponse().read())
            if not result.get('token'):
                logging.warning('Invalid username or password and no auth token provided, exiting.')
                sys.exit(1)
            authtoken = result['token']
        self.headers = {'Content-type': 'application/json', 'Authorization': f'Token {authtoken}'}

    def get(self, endpoint):
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request('GET', endpoint, headers=self.headers)
        return json.loads(conn.getresponse().read())

def create_file(path, contents, writemode='w', perms=0o600):
    with open(path, writemode) as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f'Created file {path} with permissions {oct(perms)}')

def gen_password(length=12):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

def run_command(cmd, env=CMD_ENV):
    logging.info(f'Running: {cmd}')
    try:
        return subprocess.check_output(shlex.split(cmd), env=env)
    except subprocess.CalledProcessError as e:
        logging.debug(e.output)
        return b''

def add_cronjob(cron_line):
    """Append a cron job to the user's crontab (idempotent-ish)."""
    homedir = os.path.expanduser('~')
    tmpname = f'{homedir}/.tmp{gen_password()}'
    # read existing
    existing = subprocess.run('crontab -l'.split(), capture_output=True, text=True)
    lines = existing.stdout.splitlines() if existing.returncode == 0 else []
    if cron_line.strip() in [l.strip() for l in lines]:
        logging.info('Cron job already present, skipping add.')
        return
    with open(tmpname, 'w') as tmp:
        if existing.returncode == 0:
            tmp.write(existing.stdout.rstrip() + '\n')
        tmp.write(cron_line + '\n')
    run_command(f'crontab {tmpname}')
    run_command(f'rm -f {tmpname}')
    logging.info(f'Added cron job: {cron_line}')

# -------- Main installer ------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Installs Uptime Kuma on an Opalstack account (rootless Podman)')
    parser.add_argument('-i', dest='app_uuid', help='UUID of the base app', default=os.environ.get('UUID'))
    parser.add_argument('-n', dest='app_name', help='name of the base app', default=os.environ.get('APPNAME'))
    parser.add_argument('-t', dest='opal_token', help='API auth token', default=os.environ.get('OPAL_TOKEN'))
    parser.add_argument('-u', dest='opal_user', help='Opalstack account name', default=os.environ.get('OPAL_USER'))
    parser.add_argument('-p', dest='opal_password', help='Opalstack account password', default=os.environ.get('OPAL_PASS'))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
    logging.info(f'Started installation of Uptime Kuma app {args.app_name}')

    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)
    appinfo = api.get(f'/app/read/{args.app_uuid}')
    # Expected fields: name, port, osuser_name
    appname = appinfo["name"]
    osuser  = appinfo["osuser_name"]
    port    = appinfo["port"]
    appdir  = f'/home/{osuser}/apps/{appname}'

    # sanity: ensure podman exists
    podman_path = run_command('which podman').decode().strip()
    if not podman_path:
        logging.error('podman not found in PATH; aborting.')
        sys.exit(1)

    # dirs
    for d in (appdir, f'{appdir}/data', f'{appdir}/tmp'):
        os.makedirs(d, mode=0o700, exist_ok=True)
        logging.info(f'Ensured directory {d}')
    CMD_ENV['TMPDIR'] = f'{appdir}/tmp'

    # constants
    IMAGE = 'docker.io/louislam/uptime-kuma:1'   # official stable tag
    CONTAINER = appname                          # name == app name keeps ops simple

    # start script
    start_script = textwrap.dedent(f'''\
        #!/bin/bash
        set -Eeuo pipefail
        APP="{appname}"
        PORT="{port}"
        APPDIR="$HOME/apps/$APP"
        IMG="{IMAGE}"

        mkdir -p "$APPDIR/data" "$APPDIR/tmp"
        # Pull latest image quietly (safe in rootless)
        podman pull "$IMG" >/dev/null

        # If container exists, remove it to ensure clean args
        if podman ps -a --format '{{{{.Names}}}}' | grep -xq "$APP"; then
          podman stop "$APP" >/dev/null 2>&1 || true
          podman rm   "$APP" >/dev/null 2>&1 || true
        fi

        # Run: bind localhost only; map persistent data with SELinux :Z; add autoupdate label
        exec podman run -d --name "$APP" \\
          -p 127.0.0.1:${{PORT}}:3001 \\
          -v "$APPDIR/data:/app/data:Z" \\
          -e TZ=America/Los_Angeles \\
          --label io.containers.autoupdate=registry \\
          "$IMG"
        ''')

    # stop script
    stop_script = textwrap.dedent(f'''\
        #!/bin/bash
        set -Eeuo pipefail
        APP="{CONTAINER}"
        podman stop "$APP" >/dev/null 2>&1 || true
        podman rm   "$APP" >/dev/null 2>&1 || true
        echo "Stopped $APP."
        ''')

    # logs script
    logs_script = textwrap.dedent(f'''\
        #!/bin/bash
        podman logs -f {CONTAINER}
        ''')

    # update script (safe rolling: pull, stop, start)
    update_script = textwrap.dedent(f'''\
        #!/bin/bash
        set -Eeuo pipefail
        APP="{CONTAINER}"
        IMG="{IMAGE}"
        APPDIR="$HOME/apps/$APP"
        podman pull "$IMG"
        "$APPDIR/stop"
        "$APPDIR/start"
        ''')

    # check script (health probe for cron/self-heal)
    check_script = textwrap.dedent(f'''\
        #!/bin/bash
        set -Eeuo pipefail
        PORT="{port}"
        if ! curl -fsS "http://127.0.0.1:${{PORT}}/api/status-page/heartbeat" >/dev/null 2>&1; then
          echo "Uptime Kuma not healthy; restarting..."
          "$HOME/apps/{appname}/start"
        fi
        ''')

    # README
    readme = textwrap.dedent(f'''\
        # Opalstack Uptime Kuma

        This app runs **Uptime Kuma** in a rootless Podman container bound to `127.0.0.1:{port}` and proxied by your Site.

        ## Files & paths
        - App dir: `{appdir}`
        - Data    : `{appdir}/data`  (mounted to `/app/data`)
        - Scripts : `{appdir}/start`, `{appdir}/stop`, `{appdir}/logs`, `{appdir}/update`, `{appdir}/check`
        - Logs    : `podman logs {appname}`

        ## Start / Stop
        ```bash
        {appdir}/start
        {appdir}/stop
        {appdir}/logs
        ```
        After starting, browse to your site URL (add this app to a Site in the dashboard). The internal web UI listens on port 3001; we map it to `{port}`. (Upstream docs confirm 3001 and `/app/data`.)  # refs

        ## Updates
        Nightly auto-update label is set; additionally a cron runs the `update` script. Safe update does: pull → stop → start.

        ## Notes
        - Bound to localhost; TLS/host routing handled by your Site proxy.
        - SELinux `:Z` applied to the data volume on EL9.
        - Avoid remote/NFS for data to prevent SQLite lock issues (upstream warning).
        ''')

    # write files
    create_file(f'{appdir}/start', start_script, perms=0o700)
    create_file(f'{appdir}/stop', stop_script, perms=0o700)
    create_file(f'{appdir}/logs', logs_script, perms=0o700)
    create_file(f'{appdir}/update', update_script, perms=0o700)
    create_file(f'{appdir}/check', check_script, perms=0o700)
    create_file(f'{appdir}/README.txt', readme, perms=0o600)

    # cron: keep it running + periodic updates (follow Django pattern)
    m = random.randint(0,9)
    # self-heal every 10m with a per-user minute offset
    add_cronjob(f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check > /dev/null 2>&1')
    # nightly update at a semi-random hour:minute
    nightly_hour = random.randint(1,5)   # 01:00 - 05:59 local
    nightly_min  = random.randint(0,59)
    add_cronjob(f'{nightly_min} {nightly_hour} * * * {appdir}/update > /dev/null 2>&1')

    logging.info('Uptime Kuma installer complete.')
    logging.info(f'App: {appname}  Port: {port}  User: {osuser}')
    logging.info(f'Next: add this app to a Site in the dashboard, then open the URL.')

if __name__ == '__main__':
    main()
