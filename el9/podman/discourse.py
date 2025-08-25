#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
Discourse on Opalstack (EL9 + Podman, rootless) — one-click installer

WHAT THIS DOES (SOP like Mastodon/Ghost/WordPress):
- Only writes files, cron, and a panel notice. **No image pulls, no starts**.
- User later runs: start/stop/update/check/diagnose (README explains).

KEY FIXES (from your pain points):
- **Exact app port**: pod is recreated each start with `-p 127.0.0.1:${PORT}:3000` (no stale port).
- **Rootless cgroups**: export `CONTAINERS_CGROUP_MANAGER=cgroupfs` + run with `--cgroups=disabled`.
- **PG perms**: use `podman unshare` (no host chmod EPERM).
- **Static asset visibility**: start script is **ultra-verbose** (`set -x` with timestamps), follows live app logs,
  and runs an **asset counters** ticker every 5s (files+size under /app/public/assets) so you SEE progress.
- **No duplicate Automation plugin**: nuke `$APPDIR/data/plugins/automation` if present.
- **Fontconfig spam gone**: writable cache at `/data/cache` with `XDG_CACHE_HOME=/data/cache`.
- **Escaping**: `curl -w "HTTP %{http_code}\n"` and podman Go templates are properly escaped.

Change image via env if you want:
  DISCOURSE_IMAGE, REDIS_IMAGE, POSTGRES_IMAGE
Default images:
  Web/Sidekiq: docker.io/tiredofit/discourse:latest
  Redis:       docker.io/library/redis:7-alpine
  Postgres:    docker.io/library/postgres:16-alpine
"""

import argparse, sys, os, json, logging, http.client, subprocess, shlex, secrets, string, textwrap, random, time


# ---------- Config ----------
API_URL = (
    os.environ.get('OPAL_API_URL')
    or os.environ.get('API_URL')
    or 'https://my.opalstack.live'
).rstrip('/')
API_HOST = API_URL.replace('https://', '').replace('http://', '')
API_BASE_URI = '/api/v1'

CMD_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'UMASK': '0002'}

IMG_WEB   = os.environ.get('DISCOURSE_IMAGE') or 'docker.io/tiredofit/discourse:latest'
IMG_REDIS = os.environ.get('REDIS_IMAGE')     or 'docker.io/library/redis:7-alpine'
IMG_PG    = os.environ.get('POSTGRES_IMAGE')  or 'docker.io/library/postgres:16-alpine'  # UID 999

# ---------- API ----------
class OpalAPI:
    def __init__(self, host, base_uri, token=None, user=None, password=None):
        self.host, self.base = host, base_uri
        if not token:
            conn = http.client.HTTPSConnection(self.host)
            payload = json.dumps({'username': user, 'password': password})
            conn.request('POST', f'{self.base}/login/', payload, headers={'Content-type':'application/json'})
            resp = conn.getresponse()
            data = json.loads(resp.read() or b'{}')
            if not data.get('token'):
                logging.error(f'Auth failed (HTTP {resp.status}).'); sys.exit(1)
            token = data['token']
        self.h = {'Content-type': 'application/json', 'Authorization': f'Token {token}'}

    def get(self, path):
        conn = http.client.HTTPSConnection(self.host)
        conn.request('GET', f'{self.base}{path}', headers=self.h)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b'{}')
        if resp.status >= 400:
            logging.error(f'GET {path} -> HTTP {resp.status}'); sys.exit(1)
        return data

    def post(self, path, payload):
        conn = http.client.HTTPSConnection(self.host)
        conn.request('POST', f'{self.base}{path}', payload, headers=self.h)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b'{}')
        if resp.status >= 400:
            logging.error(f'POST {path} -> HTTP {resp.status}: {data}')
        return data

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
    # de-dupe: grab existing, append if missing
    try:
        existing = subprocess.check_output('crontab -l'.split(), stderr=subprocess.STDOUT).decode()
    except subprocess.CalledProcessError:
        existing = ''
    if line in existing:
        logging.info(f'Cron already present: {line}')
        return
    with open(tmp, 'w') as t:
        t.write(existing)
        if existing and not existing.endswith('\n'):
            t.write('\n')
        t.write(line + '\n')
    sh(f'crontab {tmp}')
    os.remove(tmp)
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
        logging.error('Missing app UUID (-i)'); sys.exit(1)

    api = OpalAPI(API_HOST, API_BASE_URI, args.token, args.user, args.password)
    app = api.get(f'/app/read/{args.uuid}')
    if not app.get('name'):
        logging.error('App not found.'); sys.exit(1)

    appname = app['name']
    osuser  = app['osuser_name']
    port    = int(app['port'])
    appdir  = f'/home/{osuser}/apps/{appname}'
    logdir  = f'/home/{osuser}/logs/apps/{appname}'

    logging.info(f'Preparing Discourse for app "{appname}" (port {port})')

    # dirs
    sh(f'mkdir -p {appdir}/data')
    sh(f'mkdir -p {appdir}/pgdata')
    sh(f'mkdir -p {logdir}')

    # ---- creds/env (self-contained DB) ----
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

    # ---- scripts (no pulls/long waits here) ----
    start = textwrap.dedent(f"""\
    #!/bin/bash
    # Ultra-verbose start: timestamps + xtrace everywhere
    set -Eeuo pipefail
    export CONTAINERS_CGROUP_MANAGER=cgroupfs
    export TIMEFORMAT='[time] %3lR'
    export PS4='+ $(date "+%Y-%m-%d %H:%M:%S") [${{BASH_SOURCE##*/}}:${{LINENO}}] '

    set -x

    APP="{appname}"
    POD="$APP-pod"
    APPDIR="{appdir}"
    LOGDIR="{logdir}"
    PORT="{port}"
    IMG_WEB="{IMG_WEB}"
    IMG_REDIS="{IMG_REDIS}"
    IMG_PG="{IMG_PG}"
    HEALTH_URL="http://127.0.0.1:${{PORT}}/"

    echo "### [start] Using images:"
    echo "    WEB:   $IMG_WEB"
    echo "    REDIS: $IMG_REDIS"
    echo "    PG:    $IMG_PG"
    echo "### [start] App port (from API): $PORT"

    echo "### [start] Recreate pod to lock correct port"
    podman pod rm -f "$POD" || true
    # If port in use, show who grabs it
    if ss -ltn '( sport = :'$PORT' )' | grep -q LISTEN; then
      echo "!!! Port $PORT is already in use on host:"
      ss -ltnp '( sport = :'$PORT' )' || true
      exit 1
    fi
    podman --log-level=debug pod create --name "$POD" -p 127.0.0.1:${{PORT}}:3000

    echo "### [start] Ensure dirs + perms"
    mkdir -p "$LOGDIR" "$APPDIR/pgdata" "$APPDIR/data" "$APPDIR/data/logs" "$APPDIR/data/plugins" "$APPDIR/data/cache"
    chmod 0777 "$LOGDIR" || true
    # avoid duplicate Automation plugin (image already includes it)
    rm -rf "$APPDIR/data/plugins/automation" || true
    # postgres perms inside userns
    podman unshare chown -R 999:999 "$APPDIR/pgdata" || true
    podman unshare chmod -R 0770 "$APPDIR/pgdata" || true

    echo "### [start] Export env"
    set -a; source "$APPDIR/.env"; set +a

    echo "### [start] Start Postgres"
    podman rm -f "$APP-postgres" || true
    podman --log-level=debug run --cgroups=disabled -d --name "$APP-postgres" --pod "$POD" \\
      -e POSTGRES_USER="$DB_USER" \\
      -e POSTGRES_PASSWORD="$DB_PASS" \\
      -e POSTGRES_DB="$DB_NAME" \\
      -v "$APPDIR/pgdata:/var/lib/postgresql/data" \\
      "$IMG_PG"

    echo "### [start] Wait for PG ready"
    for i in $(seq 1 120); do
      if podman exec "$APP-postgres" sh -lc 'pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1'; then
        echo "[pg] ready"
        break
      fi
      sleep 1
      if [[ $i -eq 120 ]]; then
        echo "!!! postgres not ready, abort"
        exit 1
      fi
    done

    echo "### [start] Create PG extensions (hstore, pg_trgm)"
    podman exec "$APP-postgres" sh -lc 'psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -Atc "CREATE EXTENSION IF NOT EXISTS hstore; CREATE EXTENSION IF NOT EXISTS pg_trgm;"'

    echo "### [start] Start Redis"
    podman rm -f "$APP-redis" || true
    podman --log-level=debug run --cgroups=disabled -d --name "$APP-redis" --pod "$POD" "$IMG_REDIS"

    echo "### [start] Launch Discourse (web+sidekiq)"
    podman rm -f "$APP" || true
    podman --log-level=debug run --cgroups=disabled -d --name "$APP" --pod "$POD" \\
      -v "$APPDIR/data:/data" \\
      -v "$LOGDIR:/data/logs" \\
      --env-file "$APPDIR/.env" \\
      -e XDG_CACHE_HOME=/data/cache \\
      --label io.containers.autoupdate=registry \\
      "$IMG_WEB"

    echo "### [start] Containers:"
    podman ps --format "table {{{{.Names}}}}\\t{{{{.Status}}}}\\t{{{{.Image}}}}\\t{{{{.Ports}}}}"

    echo "### [start] LIVE LOG + ASSET PROGRESS (Ctrl-C to stop following; app keeps running)"
    # follow logs + show asset dir growth every 5s while the app warms up
    (
      set +x
      podman logs -f "$APP" &
      LOGPID=$!
      trap "kill -9 $LOGPID >/dev/null 2>&1 || true" INT TERM EXIT

      # asset counters
      for i in $(seq 1 300); do
        files=$(podman exec "$APP" bash -lc 'ls -1 /app/public/assets 2>/dev/null | wc -l' 2>/dev/null || echo 0)
        size=$(podman exec "$APP" bash -lc 'du -sh /app/public/assets 2>/dev/null | cut -f1' 2>/dev/null || echo 0)
        echo "[assets] files=$files size=$size"
        # quick health probe
        code=$(curl -sS -o /dev/null -w "HTTP %{{http_code}}" "$HEALTH_URL" || true)
        echo "[health] $HEALTH_URL -> $code"
        sleep 5
      done

      kill -9 $LOGPID >/dev/null 2>&1 || true
      trap - INT TERM EXIT
    )

    echo "### [start] Done. If first boot, migrations/asset compile may continue in background."
    echo "### [start] Try: curl -sS -o /dev/null -w 'HTTP %{{http_code}}\\n' $HEALTH_URL"
    """)

    stop = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    export CONTAINERS_CGROUP_MANAGER=cgroupfs
    set -x
    APP="{appname}"
    POD="$APP-pod"
    podman rm -f "$APP" "$APP-redis" "$APP-postgres" || true
    podman pod rm -f "$POD" || true
    echo "[stop] stopped {appname}"
    """)

    update = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    export CONTAINERS_CGROUP_MANAGER=cgroupfs
    set -x
    "{appdir}/stop" || true
    "{appdir}/start"
    """)

    check = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    export CONTAINERS_CGROUP_MANAGER=cgroupfs
    set -x
    APP="{appname}"
    need=0
    for c in "$APP-postgres" "$APP-redis" "$APP"; do
      state=$(podman inspect -f '{{{{.State.Running}}}}' "$c" 2>/dev/null || echo "false")
      [[ "$state" != "true" ]] && need=1
    done
    if [[ "$need" -eq 1 ]]; then
      echo "[check] one or more containers down; restarting..."
      "{appdir}/start"
    else
      echo "[check] all good"
    fi
    """)

    logs_sh = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    export CONTAINERS_CGROUP_MANAGER=cgroupfs
    set -x
    tail -F "{logdir}/discourse.log" "{logdir}/unicorn.log" "{logdir}/unicorn-error.log" "{logdir}/sidekiq.log" 2>/dev/null
    """)

    diagnose = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    export CONTAINERS_CGROUP_MANAGER=cgroupfs
    set -x
    APP="{appname}"
    PORT="{port}"
    LOGDIR="{logdir}"

    echo "=== POD ==="
    podman pod ps

    echo "=== CONTAINERS ==="
    podman ps --format "table {{{{.Names}}}}\\t{{{{.Status}}}}\\t{{{{.Image}}}}\\t{{{{.Ports}}}}"

    echo -e "\\n=== HOST PORT CHECK ==="
    curl -sS -o /dev/null -w "HTTP %{{http_code}}\\n" "http://127.0.0.1:${{PORT}}/" || true

    echo -e "\\n=== REDIS PING ==="
    podman exec "$APP-redis" sh -lc 'redis-cli -h 127.0.0.1 -p 6379 ping' || echo "redis ping failed"

    echo -e "\\n=== PG READY? ==="
    podman exec "$APP-postgres" sh -lc 'pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

    echo -e "\\n=== LAST LOGS ==="
    ls -lh "$LOGDIR" | sed 's/^/    /'
    for f in "$LOGDIR"/discourse.log "$LOGDIR"/unicorn*.log "$LOGDIR"/sidekiq.log; do
      echo "--- $f"
      tail -n 200 "$f" 2>/dev/null || true
      echo
    done
    """)

    dbext = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    export CONTAINERS_CGROUP_MANAGER=cgroupfs
    set -x
    APP="{appname}"
    echo "[dbext] creating hstore/pg_trgm..."
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
    export CONTAINERS_CGROUP_MANAGER=cgroupfs
    set -x
    APP="{appname}"
    echo "[migrate] running rails db:migrate + assets:precompile (inside container)..."
    podman exec "$APP" bash -lc 'cd /app && RAILS_ENV=production bundle exec rake db:migrate assets:precompile' || true
    echo "[migrate] complete (if image supports manual rake)."
    """)

    readme = textwrap.dedent(f"""\
    # Discourse (Podman) on Opalstack

    **App:** {appname}  
    **Port:** {port} (host) → 3000 (container)  
    **Data:** {appdir}/data (Discourse) · {appdir}/pgdata (Postgres)  
    **Env:**  {appdir}/.env  
    **Logs:** {logdir}/ (discourse.log, unicorn*.log, sidekiq.log)

    ## Finish Setup (run later via SSH)
    1. Start services (first run pulls images & initializes):
       ```
       {appdir}/start
       ```
       This start is **very chatty** and will:
       - Recreate the pod on **port {port}**,
       - Start PG/Redis,
       - Launch Discourse,
       - **Follow live logs** and print an **asset counter every 5s** (`/app/public/assets` files + size),
       - Print health probe codes to `http://127.0.0.1:{port}/`.

    2. Watch more:
       ```
       {appdir}/diagnose
       podman logs -f {appname}
       ```

    3. (Optional) DB extensions (if missed):
       ```
       {appdir}/dbext
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
    - Migrate:  {appdir}/migrate (manual rake if desired)

    ## Notes
    - Images can be swapped via env vars: DISCOURSE_IMAGE, REDIS_IMAGE, POSTGRES_IMAGE.
    - We remove any duplicate `data/plugins/automation` to avoid plugin redefinition spam.
    - Fontconfig cache is set to `/data/cache` to stop "No writable cache directories".
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

    # cron: health every ~10m; daily update during low hours
    m  = random.randint(0,9)
    hh = random.randint(2,5)
    mm = random.randint(0,59)
    add_cron(f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check > /dev/null 2>&1')
    add_cron(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

    # DO NOT start/pull here (user does that later)

    # give the panel a moment before notifying it (avoids racey 4xx)
    time.sleep(10)

    # /app/installed/ (retry once if transient)
    try:
        api.post('/app/installed/', json.dumps([{'id': args.uuid}]))
    except Exception as e:
        logging.warning(f'/app/installed/ failed once: {e}; retrying in 3s')
        time.sleep(3)
        try:
            api.post('/app/installed/', json.dumps([{'id': args.uuid}]))
        except Exception as e2:
            logging.error(f'/app/installed/ failed after retry: {e2}')

    # panel notice (don’t fail installer if this flakes)
    try:
        api.post('/notice/create/', json.dumps([{
            'type':'M',
            'content': (
                f'Discourse prepared for app {appname}. SSH and run {appdir}/start when ready. '
                f'Start is ultra-verbose and shows asset progress and health checks. '
                f'Initial admin: {admin_user}/{admin_pass} ({admin_email}).'
            )
        }]))
    except Exception as e:
        logging.warning(f'/notice/create/ failed: {e}')

    logging.info('Install complete (no long-running tasks).')

if __name__ == '__main__':
    main()
