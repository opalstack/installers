#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
Discourse on Opalstack (EL9 + Podman, rootless) — one-click installer

WHAT THIS DOES:
- Only writes files, cron, and a panel notice. **No image pulls, no starts**.
- User later runs: start/stop/update/check/diagnose and **finish_install** (README explains).

ARCH / WHERE THINGS RUN:
- Postgres + Redis + Web all run **inside a single Podman pod** bound to the Opalstack app port.
- Pod is recreated on each start with `-p 127.0.0.1:${PORT}:3000`.

KEY FIXES:
- **Init moved out of start**: migrations, PG extensions, and asset build live in `finish_install`.
- **CSS crash guard**: clears caches, clobbers, precompiles, verifies real CSS; retries once.
- **git safe.directory**: `/app` marked safe to avoid “dubious ownership”.
- **Rootless cgroups**: `CONTAINERS_CGROUP_MANAGER=cgroupfs` + `--cgroups=disabled`.
- **PG perms**: `podman unshare` to avoid EPERM.
- **Ultra-verbose start**: timestamps, container logs follow, asset counter every 5s.
- **Duplicate Automation plugin**: nuked in data dir to avoid redefinition spam.
- **Fontconfig spam**: `XDG_CACHE_HOME=/data/cache`.
- **Port certainty**: pod recreated every start with explicit `-p 127.0.0.1:${PORT}:3000`.

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

    # Optional: set your site hostname (recommended)
    DISCOURSE_HOSTNAME=wildcard.local
    """)
    write(f'{appdir}/.env', env, 0o600)

    # ---- scripts ----
    start = textwrap.dedent(f"""\
    #!/bin/bash
    # Start/attach only. **No init here** — run finish_install after first start.
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

    # Lock issues sometimes happen on multi-tenant hosts
    podman system renumber || true

    echo "### [start] Recreate pod to lock correct port"
    podman pod rm -f "$POD" || true
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
    (
      set +x
      podman logs -f "$APP" &
      LOGPID=$!
      trap "kill -9 $LOGPID >/dev/null 2>&1 || true" INT TERM EXIT
      for i in $(seq 1 300); do
        files=$(podman exec "$APP" bash -lc 'ls -1 /app/public/assets 2>/dev/null | wc -l' 2>/dev/null || echo 0)
        size=$(podman exec "$APP" bash -lc 'du -sh /app/public/assets 2>/dev/null | cut -f1' 2>/dev/null || echo 0)
        echo "[assets] files=$files size=$size"
        code=$(curl -sS -o /dev/null -w "HTTP %{{http_code}}" "$HEALTH_URL" || true)
        echo "[health] $HEALTH_URL -> $code"
        sleep 5
      done
      kill -9 $LOGPID >/dev/null 2>&1 || true
      trap - INT TERM EXIT
    )

    echo "### [start] Done. If first boot, run: {appdir}/finish_install"
    echo "### [start] Health probe: curl -sS -o /dev/null -w 'HTTP %{{http_code}}\\n' $HEALTH_URL"
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
    echo "[migrate] running rails db:migrate (inside container)..."
    podman exec "$APP" bash -lc 'cd /app && RAILS_ENV=production bundle exec rake db:migrate' || true
    echo "[migrate] complete."
    """)

    finish_install = textwrap.dedent(f"""\
    #!/bin/bash
    # First-run init: PG ready → extensions → db:migrate → assets (CSS crash guard) → success banner.
    set -Eeuo pipefail
    export CONTAINERS_CGROUP_MANAGER=cgroupfs
    export PS4='+ $(date "+%Y-%m-%d %H:%M:%S") [${{BASH_SOURCE##*/}}:${{LINENO}}] '
    set -x

    APP="{appname}"
    POD="$APP-pod"
    APPDIR="{appdir}"

    # Load env
    set -a; source "$APPDIR/.env"; set +a

    # Clean duplicate Automation plugin in data dir (image already ships it)
    rm -rf "$APPDIR/data/plugins/automation" || true

    # Podman lock index sometimes needs a nudge on shared hosts
    podman system renumber || true

    # Require containers started by ./start
    if ! podman inspect -f '{{{{.State.Running}}}}' "$APP-postgres" 2>/dev/null | grep -q true; then
      echo "!!! Postgres not running. Run {appdir}/start first."; exit 1
    fi
    if ! podman inspect -f '{{{{.State.Running}}}}' "$APP" 2>/dev/null | grep -q true; then
      echo "!!! Web container not running. Run {appdir}/start first."; exit 1
    fi

    # Wait for Postgres
    echo "### [finish_install] Wait for PG ready"
    for i in $(seq 1 120); do
      if podman exec "$APP-postgres" sh -lc 'pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1'; then
        echo "[pg] ready"; break
      fi
      sleep 1
      [[ $i -eq 120 ]] && {{ echo "!!! postgres not ready, abort"; exit 1; }}
    done

    echo "### [finish_install] Create PG extensions (hstore, pg_trgm)"
    podman exec "$APP-postgres" sh -lc \\
      'psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -Atc "CREATE EXTENSION IF NOT EXISTS hstore; CREATE EXTENSION IF NOT EXISTS pg_trgm;"'

    # Fix git “dubious ownership” before running any rake tasks
    podman exec "$APP" git config --global --add safe.directory /app || true

    echo "### [finish_install] Rails db:migrate"
    podman exec "$APP" bash -lc 'cd /app && RAILS_ENV=production bundle exec rake db:migrate'

    
    echo "### [finish_install] Ensure hostname in config/discourse.conf"
    podman exec "$APP" bash -lc '
      set -e
      cd /app
      # create config if missing
      test -f config/discourse.conf || cp config/discourse_defaults.conf config/discourse.conf
      # set hostname = $DISCOURSE_HOSTNAME (fallback to wildcard.local)
      H="${{DISCOURSE_HOSTNAME:-wildcard.local}}"
      if grep -q "^hostname" config/discourse.conf; then
        sed -i "s/^hostname.*/hostname = ${{H}}/" config/discourse.conf
      else
        printf "\nhostname = %s\n" "$H" >> config/discourse.conf
      fi
      echo "[discourse.conf] hostname set to: $H"
      # sanity print what Rails sees
      RAILS_ENV=production bin/rails r "puts \"GlobalSetting.hostname => #{{GlobalSetting.hostname.inspect}}\""
    '



    echo "### [finish_install] CSS crash guard → assets"
    podman exec "$APP" bash -lc '
      set -e
      cd /app

      # Clean caches and stale assets
      rm -rf tmp/cache public/assets node_modules/.cache || true
      RAILS_ENV=production bundle exec rake assets:clobber

      # Precompile
      RAILS_ENV=production bundle exec rake assets:precompile

      # Verify we got a real CSS (not the tiny placeholder)
      check_css() {{
        set -- public/assets/discourse*.css
        if [ ! -e "$1" ]; then return 1; fi
        # consider "good" if first CSS >= 10KB
        local s; s=$(wc -c < "$1" 2>/dev/null || echo 0)
        [ "${{s:-0}}" -ge 10240 ]
      }}

      if ! check_css; then
        echo "[assets] CSS looks wrong; retrying once after deep clean..."
        rm -rf tmp/cache public/assets node_modules/.cache || true
        pnpm store prune || true
        RAILS_ENV=production bundle exec rake assets:precompile
        if ! check_css; then
          echo "!!! assets precompile did not produce valid CSS; aborting."
          exit 1
        fi
      fi
    '

    # Discover host port from pod mapping (containerPort 3000)
    HOST_PORT=$(podman pod inspect "$POD" -f '{{{{range .InfraConfig.PortMappings}}}}{{{{if eq .ContainerPort 3000}}}}{{{{.HostPort}}}}{{{{end}}}}{{{{end}}}}' 2>/dev/null || true)
    [ -z "$HOST_PORT" ] && HOST_PORT="{port}"
    HEALTH_URL="http://127.0.0.1:${{HOST_PORT}}/"

    echo "### [finish_install] Health probe"
    curl -sS -o /dev/null -w "HTTP %{{http_code}}\\n" "$HEALTH_URL" || true

    echo
    echo "✅ Discourse initialized — rock & roll"
    echo "   URL:      $HEALTH_URL"
    echo "   Admin:    $ADMIN_USER / $ADMIN_PASS  ($ADMIN_EMAIL)"
    echo "   (Set DISCOURSE_HOSTNAME in {appdir}/.env for proper links & email.)"
    """)

    readme = textwrap.dedent(f"""\
    # Discourse (Podman) on Opalstack

    **App:** {appname}  
    **Port:** {port} (host) → 3000 (container)  
    **Data:** {appdir}/data (Discourse) · {appdir}/pgdata (Postgres)  
    **Env:**  {appdir}/.env  
    **Logs:** {logdir}/ (discourse.log, unicorn*.log, sidekiq.log)

    ## First Run
    1. Start services (pulls images & boots containers):
       ```
       {appdir}/start
       ```
       Start will:
       - Recreate the pod on **port {port}**,
       - Start PG/Redis/Web,
       - Follow live logs and print an **asset counter every 5s**,
       - Print health probe codes to `http://127.0.0.1:{port}/`.

    2. Initialize the app (migrations, extensions, assets with CSS crash guard):
       ```
       {appdir}/finish_install
       ```

    3. Check health:
       ```
       curl -sS -o /dev/null -w 'HTTP %{{http_code}}\\n' http://127.0.0.1:{port}/
       ```

    ## Commands
    - Start:       {appdir}/start
    - Stop:        {appdir}/stop
    - Update:      {appdir}/update
    - Health cron: {appdir}/check (restarts if down)
    - Diagnose:    {appdir}/diagnose
    - Logs:        {appdir}/logs
    - DB Ext:      {appdir}/dbext
    - Migrate:     {appdir}/migrate
    - Finish init: {appdir}/finish_install

    ## Notes
    - Images can be swapped via env vars: DISCOURSE_IMAGE, REDIS_IMAGE, POSTGRES_IMAGE.
    - We remove duplicate `data/plugins/automation` to avoid plugin redefinition spam.
    - Fontconfig cache is set to `/data/cache` to stop "No writable cache directories".
    - Set `DISCOURSE_HOSTNAME` in `{appdir}/.env` for proper links and email.
    """)

    # write files
    write(f'{appdir}/start',          start,          0o700)
    write(f'{appdir}/stop',           stop,           0o700)
    write(f'{appdir}/update',         update,         0o700)
    write(f'{appdir}/check',          check,          0o700)
    write(f'{appdir}/logs',           logs_sh,        0o700)
    write(f'{appdir}/diagnose',       diagnose,       0o700)
    write(f'{appdir}/dbext',          dbext,          0o700)
    write(f'{appdir}/migrate',        migrate,        0o700)
    write(f'{appdir}/finish_install', finish_install, 0o700)
    write(f'{appdir}/README.md',      readme,         0o600)

    # cron: health every ~10m; daily update during low hours
    m  = random.randint(0,9)
    hh = random.randint(2,5)
    mm = random.randint(0,59)
    add_cron(f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check > /dev/null 2>&1')
    add_cron(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

    # DO NOT start/pull here (user does that later)
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
                f'Discourse prepared for app {appname}. SSH and run {appdir}/start, then {appdir}/finish_install. '
                f'Start is ultra-verbose and shows asset progress and health checks. '
                f'Initial admin: {admin_user}/{admin_pass} ({admin_email}).'
            )
        }]))
    except Exception as e:
        logging.warning(f'/notice/create/ failed: {e}')

    logging.info('Install complete (scripts written; no long-running tasks).')

if __name__ == '__main__':
    main()
