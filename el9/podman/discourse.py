#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
Discourse on Opalstack (EL9 + Podman) — one-click installer
Lifecycle matches Mastodon/Ghost/WordPress SOP:
- Installer: writes files, cron, README, posts notice. **Does not start/pull**.
- User later runs: start/stop/update/check/diagnose (README explains).
"""

import argparse, sys, os, json, logging, http.client, subprocess, shlex, secrets, string, textwrap, random

# ---------- Config ----------
API_URL = os.environ.get('OPAL_API_URL') or os.environ.get('API_URL') or 'https://my.opalstack.com'
API_HOST = API_URL.replace('https://', '').replace('http://', '')
API_BASE_URI = '/api/v1'
CMD_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'UMASK': '0002'}

IMG_WEB   = os.environ.get('DISCOURSE_IMAGE') or 'docker.io/tiredofit/discourse:latest'   # web+sidekiq
IMG_REDIS = os.environ.get('REDIS_IMAGE')     or 'docker.io/library/redis:7-alpine'
IMG_PG    = os.environ.get('POSTGRES_IMAGE')  or 'docker.io/library/postgres:16-alpine'   # uid 999

# ---------- API ----------
class OpalAPI:
    def __init__(self, host, base_uri, token=None, user=None, password=None):
        self.host, self.base = host, base_uri
        if not token:
            conn = http.client.HTTPSConnection(self.host)
            payload = json.dumps({'username': user, 'password': password})
            conn.request('POST', f'{self.base}/login/', payload, headers={'Content-type':'application/json'})
            data = json.loads(conn.getresponse().read() or b'{}')
            if not data.get('token'):
                logging.error('Auth failed (no token).'); sys.exit(1)
            token = data['token']
        self.h = {'Content-type': 'application/json', 'Authorization': f'Token {token}'}

    def get(self, path):
        conn = http.client.HTTPSConnection(self.host)
        conn.request('GET', f'{self.base}{path}', headers=self.h)
        return json.loads(conn.getresponse().read() or b'{}')

    def post(self, path, payload):
        conn = http.client.HTTPSConnection(self.host)
        conn.request('POST', f'{self.base}{path}', payload, headers=self.h)
        return json.loads(conn.getresponse().read() or b'{}')

# ---------- helpers ----------
def pw(n=24):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(n))

def sh(cmd, cwd=None, env=CMD_ENV, check=True):
    logging.info(f'$ {cmd}')
    try:
        out = subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env, stderr=subprocess.STDOUT)
        return out.decode('utf-8', 'ignore')
    except subprocess.CalledProcessError as e:
        logging.error(f'Command failed ({e.returncode}): {cmd}')
        logging.error(e.output.decode('utf-8', 'ignore'))
        if check: sys.exit(e.returncode)
        return e.output.decode('utf-8', 'ignore')

def write(path, content, mode=0o600):
    with open(path, 'w') as f: f.write(content)
    os.chmod(path, mode)
    logging.info(f'Created file {path} perms={oct(mode)}')

def add_cron(line):
    home = os.path.expanduser('~')
    tmp = f'{home}/.tmp{pw(8)}'
    with open(tmp, 'w') as t:
        sh('crontab -l', check=False)
        subprocess.run('crontab -l'.split(), stdout=t)
        t.write(line + '\n')
    sh(f'crontab {tmp}')
    sh(f'rm -f {tmp}')
    logging.info(f'Added cron: {line}')

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description='Install Discourse (Podman) on Opalstack')
    ap.add_argument('-i', dest='uuid',       default=os.environ.get('UUID'))
    ap.add_argument('-n', dest='name',       default=os.environ.get('APPNAME'))
    ap.add_argument('-t', dest='token',      default=os.environ.get('OPAL_TOKEN'))
    ap.add_argument('-u', dest='user',       default=os.environ.get('OPAL_USER'))
    ap.add_argument('-p', dest='password',   default=os.environ.get('OPAL_PASS'))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
    if not args.uuid:
        logging.error('Missing app UUID (-i).'); sys.exit(1)

    api = OpalAPI(API_HOST, API_BASE_URI, args.token, args.user, args.password)
    app = api.get(f'/app/read/{args.uuid}')
    if not app.get('name'):
        logging.error('App not found.'); sys.exit(1)

    appname = app['name']
    osuser  = app['osuser_name']
    port    = int(app['port'])
    appdir  = f'/home/{osuser}/apps/{appname}'
    logdir  = f'/home/{osuser}/logs/apps/{appname}'
    podname = f'{appname}-pod'

    logging.info(f'Preparing Discourse for app "{appname}"')

    # dirs
    sh(f'mkdir -p {appdir}/data')
    sh(f'mkdir -p {appdir}/pgdata')
    sh(f'mkdir -p {logdir}')

    # ---- creds/env (no external DB; self-contained) ----
    db_user = f"disc_{args.uuid[:8]}".lower()
    db_name = db_user
    db_pass = pw(24)
    admin_user  = 'admin'
    admin_email = f'{admin_user}@example.com'
    admin_pass  = pw(16)

    env = textwrap.dedent(f"""\
    # ==== Discourse env ====
    # Postgres (container in pod)
    DB_HOST=127.0.0.1
    DB_PORT=5432
    DB_NAME={db_name}
    DB_USER={db_user}
    DB_PASS={db_pass}

    # Redis (container in pod)
    REDIS_HOST=127.0.0.1
    REDIS_PORT=6379

    # Admin bootstrap (change if desired)
    ADMIN_USER={admin_user}
    ADMIN_EMAIL={admin_email}
    ADMIN_PASS={admin_pass}

    # Logging
    LOG_PATH=/data/logs
    LOG_LEVEL=info
    """)
    write(f'{appdir}/.env', env, 0o600)

    # ---- scripts (no long waits, no pulls in installer) ----
    start = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{appname}"
    POD="$APP-pod"
    APPDIR="{appdir}"
    LOGDIR="{logdir}"
    PORT="{port}"
    IMG_WEB="{IMG_WEB}"
    IMG_REDIS="{IMG_REDIS}"
    IMG_PG="{IMG_PG}"

    echo "[start] create pod + port 127.0.0.1:${{PORT}} -> :3000"
    podman pod exists "$POD" || podman pod create --name "$POD" -p 127.0.0.1:${{PORT}}:3000 >/dev/null

    echo "[start] ensure dirs + perms"
    mkdir -p "$LOGDIR" "$APPDIR/pgdata" "$APPDIR/data"
    chmod 0777 "$LOGDIR" || true
    chmod 0770 "$APPDIR/pgdata" || true
    podman unshare chown -R 999:999 "$APPDIR/pgdata" || true
    podman unshare chmod -R 0770 "$APPDIR/pgdata" || true

    echo "[start] env"
    set -a; source "$APPDIR/.env"; set +a

    echo "[start] starting postgres..."
    podman rm -f "$APP-postgres" >/dev/null 2>&1 || true
    podman run -d --name "$APP-postgres" --pod "$POD" \\
      -e POSTGRES_USER="$DB_USER" \\
      -e POSTGRES_PASSWORD="$DB_PASS" \\
      -e POSTGRES_DB="$DB_NAME" \\
      -v "$APPDIR/pgdata:/var/lib/postgresql/data" \\
      "$IMG_PG" >/dev/null

    echo "[start] starting redis..."
    podman rm -f "$APP-redis" >/dev/null 2>&1 || true
    podman run -d --name "$APP-redis" --pod "$POD" "$IMG_REDIS" >/dev/null

    echo "[start] starting discourse (web+sidekiq)..."
    podman rm -f "$APP" >/dev/null 2>&1 || true
    podman run -d --name "$APP" --pod "$POD" \\
      -v "$APPDIR/data:/data" \\
      -v "$LOGDIR:/data/logs" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG_WEB" >/dev/null

    echo "[start] launched. First start runs DB migrations/assets inside container; watch logs in $LOGDIR."
    """)

    stop = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{appname}"
    POD="$APP-pod"
    podman rm -f "$APP" "$APP-redis" "$APP-postgres" >/dev/null 2>&1 || true
    podman pod rm -f "$POD" >/dev/null 2>&1 || true
    echo "[stop] stopped {appname}"
    """)

    update = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    "{appdir}/stop" || true
    "{appdir}/start"
    """)

    check = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{appname}"
    need=0
    for c in "$APP-postgres" "$APP-redis" "$APP"; do
      state=$(podman inspect -f '{{{{.State.Running}}}}' "$c" 2>/dev/null || echo "false")
      [[ "$state" != "true" ]] && need=1
    done
    if [[ "$need" -eq 1 ]]; then
      echo "[check] one or more containers down; restarting..."
      "{appdir}/start"
    fi
    """)

    logs_sh = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    tail -F "{logdir}/discourse.log" "{logdir}/unicorn.log" "{logdir}/unicorn-error.log" "{logdir}/sidekiq.log" 2>/dev/null
    """)

    diagnose = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{appname}"
    PORT="{port}"
    LOGDIR="{logdir}"

    echo "=== POD/CONTAINERS ==="
    podman pod ps
    podman ps --format "table {{.Names}}\\t{{.Status}}\\t{{.Image}}\\t{{.Ports}}"

    echo -e "\\n=== HOST PORT CHECK ==="
    if command -v curl >/dev/null; then
      curl -sS -o /dev/null -w "HTTP %{http_code}\\n" "http://127.0.0.1:${{PORT}}/" || true
    else
      echo "curl not available"
    fi

    echo -e "\\n=== REDIS PING ==="
    podman exec "$APP-redis" sh -lc 'redis-cli -h 127.0.0.1 -p 6379 ping' || echo "redis ping failed"

    echo -e "\\n=== PG READY? ==="
    podman exec "$APP-postgres" sh -lc 'pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

    echo -e "\\n=== LAST LOGS ==="
    ls -lh "$LOGDIR" | sed 's/^/    /'
    for f in "$LOGDIR"/discourse.log "$LOGDIR"/unicorn*.log "$LOGDIR"/sidekiq.log; do
      echo "--- $f"
      tail -n 100 "$f" 2>/dev/null || true
      echo
    done
    """)

    # optional: manual DB extension kick (user-run; safe to skip)
    dbext = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{appname}"
    echo "[dbext] creating hstore/pg_trgm (requires postgres up)..."
    for i in $(seq 1 120); do
      if podman exec "$APP-postgres" sh -lc 'pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1'; then
        podman exec "$APP-postgres" sh -lc 'psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -Atc "CREATE EXTENSION IF NOT EXISTS hstore; CREATE EXTENSION IF NOT EXISTS pg_trgm;"'
        echo "[dbext] done."
        exit 0
      fi
      sleep 1
    done
    echo "[dbext] postgres not ready"; exit 1
    """)

    migrate = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{appname}"
    echo "[migrate] running rails db:migrate + assets:precompile (inside container)..."
    podman exec "$APP" bash -lc 'cd /app && RAILS_ENV=production bundle exec rake db:migrate assets:precompile' || true
    echo "[migrate] complete (if image supports manual rake)."
    """)

    readme = textwrap.dedent(f"""\
    # Discourse (Podman) on Opalstack

    **App:** {appname}  
    **Port:** {port} (host) → 3000 (container)  
    **Data:** {appdir}/data (Discourse), {appdir}/pgdata (Postgres)  
    **Env:**  {appdir}/.env  
    **Logs:** {logdir}/ (discourse.log, unicorn*.log, sidekiq.log)

    ## Finish Setup (run later via SSH)
    1. Start services (will pull images on first run and may take a few minutes on first boot):
       ```
       {appdir}/start
       ```
    2. (Optional) ensure DB extensions after Postgres is up:
       ```
       {appdir}/dbext
       ```
       Discourse generally enables required extensions automatically; this is provided just in case.
    3. Watch:
       ```
       {appdir}/diagnose
       podman logs -f {appname}
       ```
    4. Configure email by adding SMTP_* vars to `{appdir}/.env`, then:
       ```
       {appdir}/update
       ```

    ## Commands
    - Start:    {appdir}/start
    - Stop:     {appdir}/stop
    - Update:   {appdir}/update
    - Health:   cron runs {appdir}/check (restarts if down)
    - Diagnose: {appdir}/diagnose
    - Logs:     {appdir}/logs
    - DB Ext:   {appdir}/dbext
    - Migrate:  {appdir}/migrate (optional manual rake; image usually handles on start)

    ## Credentials
    - Admin: `{admin_user}` / `{admin_pass}` (email: `{admin_email}`) — change in the UI after first login.
    """)

    # write files
    write(f'{appdir}/start',     start,   0o700)
    write(f'{appdir}/stop',      stop,    0o700)
    write(f'{appdir}/update',    update,  0o700)
    write(f'{appdir}/check',     check,   0o700)
    write(f'{appdir}/logs',      logs_sh, 0o700)
    write(f'{appdir}/diagnose',  diagnose,0o700)
    write(f'{appdir}/dbext',     dbext,   0o700)
    write(f'{appdir}/migrate',   migrate, 0o700)
    write(f'{appdir}/README.md', readme,  0o600)

    # cron: health every ~10m; daily restart during low hours
    m  = random.randint(0,9)
    hh = random.randint(2,5)
    mm = random.randint(0,59)
    add_cron(f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check > /dev/null 2>&1')
    add_cron(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

    # DO NOT start/pull here (user does that later)

    # panel notice
    api.post('/app/installed/', json.dumps([{'id': args.uuid}]))
    api.post('/notice/create/', json.dumps([{
        'type':'M',
        'content': f'Discourse prepared for app {appname}. SSH and run {appdir}/start when ready. '
                   f'First run will pull images and initialize; see {appdir}/README.md. '
                   f'Initial admin: {admin_user}/{admin_pass} ({admin_email}).'
    }]))

    logging.info('Install complete (no long-running tasks).')

if __name__ == '__main__':
    main()
