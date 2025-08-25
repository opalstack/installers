#!/usr/bin/python3
"""
Discourse on Opalstack (Podman, self-contained)
- Pod layout: [postgres, redis, discourse] in a single pod
- Avoids host Postgres, no pg_hba/SSL headaches
- Verbose logging and a `diagnose` helper
"""

import argparse, sys, os, json, time, logging, http.client, subprocess, shlex, secrets, string, textwrap, random

API_HOST = (os.environ.get('API_URL') or 'https://my.opalstack.com').replace('https://', '').replace('http://', '')
API_BASE_URI = '/api/v1'
CMD_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'UMASK': '0002'}

# Images
IMG_WEB     = 'docker.io/tiredofit/discourse:latest'   # web + sidekiq in one
IMG_REDIS   = 'docker.io/library/redis:7-alpine'
IMG_POSTGIS = 'docker.io/library/postgres:16-alpine'   # postgresql (kept lean)

# ------------- API -------------
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

# ------------- helpers -------------
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
    # append (keep existing)
    with open(tmp, 'w') as t:
        sh('crontab -l', check=False)  # warms cache; ignore rc
        subprocess.run('crontab -l'.split(), stdout=t)
        t.write(line + '\n')
    sh(f'crontab {tmp}')
    sh(f'rm -f {tmp}')
    logging.info(f'Added cron: {line}')

# ------------- main -------------
def main():
    ap = argparse.ArgumentParser(description='Install Discourse (Podman, self-contained DB) on Opalstack')
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

    logging.info(f'Beginning Discourse install for app "{appname}"')

    # dirs
    sh(f'mkdir -p {appdir}/data')
    sh(f'mkdir -p {logdir}')

    # --- generate DB + admin creds (local Postgres; not using panel PG at all) ---
    db_user = f"disc_{args.uuid[:8]}".lower()
    db_name = db_user
    db_pass = pw(24)

    admin_user  = 'admin'
    admin_email = f'{admin_user}@example.com'
    admin_pass  = pw(16)

    # --- .env for Discourse container ---
    env = textwrap.dedent(f"""\
    # Postgres inside the pod
    DB_HOST=127.0.0.1
    DB_PORT=5432
    DB_NAME={db_name}
    DB_USER={db_user}
    DB_PASS={db_pass}

    # Redis inside the pod
    REDIS_HOST=127.0.0.1
    REDIS_PORT=6379

    # Admin bootstrap
    ADMIN_USER={admin_user}
    ADMIN_EMAIL={admin_email}
    ADMIN_PASS={admin_pass}

    # Logs (container writes to /data/logs -> mounted to {logdir})
    LOG_PATH=/data/logs
    LOG_LEVEL=info
    """)
    write(f'{appdir}/.env', env, 0o600)

    # --- scripts ---
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
    IMG_PG="{IMG_POSTGIS}"

    echo "[start] pulling images..."
    podman pull "$IMG_WEB"   >/dev/null || true
    podman pull "$IMG_REDIS" >/dev/null || true
    podman pull "$IMG_PG"    >/dev/null || true

    echo "[start] cleaning previous containers/pod (if any)..."
    podman rm -f "$APP" "$APP-redis" "$APP-postgres" >/dev/null 2>&1 || true
    podman pod rm -f "$POD" >/dev/null 2>&1 || true

    echo "[start] preparing log bind mount perms (rootless-friendly)..."
    mkdir -p "$LOGDIR"
    chmod 0777 "$LOGDIR" || true

    echo "[start] creating pod and port mapping 127.0.0.1:${{PORT}} -> :3000"
    podman pod create --name "$POD" -p 127.0.0.1:${{PORT}}:3000 >/dev/null

    echo "[start] ensuring pgdata dir + perms..."
    mkdir -p "$APPDIR/pgdata"
    # postgres image runs as uid 999; grant write
    podman unshare chown -R 999:999 "$APPDIR/pgdata" || true
    chmod 0777 "$APPDIR/pgdata" || true

    echo "[start] sourcing DB env..."
    set -a; source "$APPDIR/.env"; set +a

    echo "[start] starting postgres..."
    podman run -d --name "$APP-postgres" --pod "$POD" \\
      -e POSTGRES_USER="$DB_USER" \\
      -e POSTGRES_PASSWORD="$DB_PASS" \\
      -e POSTGRES_DB="$DB_NAME" \\
      -v "$APPDIR/pgdata:/var/lib/postgresql/data" \\
      "$IMG_PG" >/dev/null

    echo "[start] waiting for postgres readiness..."
    # wait for pg to accept connections on 127.0.0.1:5432
    for i in $(seq 1 120); do
      if podman exec "$APP-postgres" sh -lc 'pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1'; then
        echo "[start] postgres is up."
        break
      fi
      sleep 1
      [[ "$i" -eq 120 ]] && echo "[start] postgres did not become ready in time" && exit 1
    done

    echo "[start] ensuring pg extensions (hstore, pg_trgm)..."
    podman exec "$APP-postgres" sh -lc 'psql -U postgres -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -Atc "CREATE EXTENSION IF NOT EXISTS hstore; CREATE EXTENSION IF NOT EXISTS pg_trgm;"' || true

    echo "[start] starting redis..."
    podman run -d --name "$APP-redis" --pod "$POD" "$IMG_REDIS" >/dev/null

    echo "[start] waiting for redis to respond to PING..."
    for i in $(seq 1 60); do
      if podman exec "$APP-redis" sh -lc 'redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q PONG'; then
        echo "[start] redis is up."
        break
      fi
      sleep 1
      [[ "$i" -eq 60 ]] && echo "[start] redis did not respond in time" && exit 1
    done

    echo "[start] starting discourse (web+sidekiq)..."
    podman run -d --name "$APP" --pod "$POD" \\
      -v "$APPDIR/data:/data" \\
      -v "$LOGDIR:/data/logs" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG_WEB" >/dev/null

    echo "[start] waiting for unicorn to listen on :3000..."
    # try up to 5 minutes, print container logs tail while waiting
    for i in $(seq 1 300); do
      if podman exec "$APP" sh -lc 'ss -ltn | grep -q ":3000"'; then
        echo "[start] unicorn is listening. app is up on 127.0.0.1:${{PORT}}"
        exit 0
      fi
      if (( i % 10 == 0 )); then
        echo "[start] still waiting... (logs tail)"
        podman logs --tail=10 "$APP" 2>/dev/null || true
      fi
      sleep 1
    done
    echo "[start] unicorn failed to bind :3000 in time"; exit 1
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
    need_restart=0
    for c in "$APP-postgres" "$APP-redis" "$APP"; do
      state=$(podman inspect -f '{{{{.State.Running}}}}' "$c" 2>/dev/null || echo "false")
      [[ "$state" != "true" ]] && need_restart=1
    done
    if [[ "$need_restart" -eq 1 ]]; then
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
    APPDIR="{appdir}"
    PORT="{port}"

    echo "=== POD/CONTAINERS ==="
    podman pod ps
    podman ps --format "table {{.Names}}\\t{{.Status}}\\t{{.Image}}\\t{{.Ports}}"

    echo -e "\\n=== PORTS ==="
    podman port "$APP" 2>/dev/null || podman inspect -f '{{{{range $p,$v := .NetworkSettings.Ports}}}}{{{{printf "%s -> %v\\n" $p $v}}}}{{{{end}}}}' "$APP"
    ss -ltnp | grep -E "127\\.0\\.0\\.1:(${port})\\b" || echo "host NOT listening on $PORT"

    echo -e "\\n=== RESOLUTION INSIDE POD ==="
    podman exec "$APP" sh -lc 'getent hosts host.containers.internal || echo "no-host-alias"'

    echo -e "\\n=== REDIS PING ==="
    podman exec "$APP-redis" sh -lc 'redis-cli -h 127.0.0.1 -p 6379 ping' || echo "redis ping failed"

    echo -e "\\n=== PG READY? ==="
    podman exec "$APP-postgres" sh -lc 'pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

    echo -e "\\n=== CURL HOST -> APP ==="
    curl -sv --max-time 3 "http://127.0.0.1:${{PORT}}/" -o /dev/null || true

    echo -e "\\n=== LAST LOGS ==="
    ls -lh "{logdir}" | sed 's/^/    /'
    for f in "{logdir}"/discourse.log "{logdir}"/unicorn*.log "{logdir}"/sidekiq.log; do
      echo "--- $f"
      tail -n 100 "$f" 2>/dev/null || true
      echo
    done
    """)

    readme = textwrap.dedent(f"""\
    # Discourse (self-contained) on Opalstack

    **App:** {appname}  
    **Port:** {port} (host) → 3000 (container)  
    **Data:** {appdir}/data (Discourse), {appdir}/pgdata (Postgres)  
    **Env:**  {appdir}/.env  
    **Logs:** {logdir}/ (discourse.log, unicorn*.log, sidekiq.log)

    ## Commands
    - Start:  {appdir}/start
    - Stop:   {appdir}/stop
    - Update: {appdir}/update
    - Logs:   {appdir}/logs
    - Diagnose: {appdir}/diagnose
    - Health: cron runs {appdir}/check (restarts only if down)

    ## Notes
    - Everything runs inside the pod: Postgres (16), Redis (7), Discourse (tiredofit).
    - First boot will run DB migrations; expect 1–3 minutes before 127.0.0.1:{port} responds.
    - To enable email, add SMTP_* vars to `.env` and run `update`.
    """)

    write(f'{appdir}/start',     start,   0o700)
    write(f'{appdir}/stop',      stop,    0o700)
    write(f'{appdir}/update',    update,  0o700)
    write(f'{appdir}/check',     check,   0o700)
    write(f'{appdir}/logs',      logs_sh, 0o700)
    write(f'{appdir}/diagnose',  diagnose,0o700)
    write(f'{appdir}/README.md', readme,  0o600)

    # --- Cron: health every ~10m; daily restart during low hours ---
    m  = random.randint(0,9)
    hh = random.randint(2,5)
    mm = random.randint(0,59)
    add_cron(f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check > /dev/null 2>&1')
    add_cron(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

    # --- Kick it once ---
    sh(f'{appdir}/start')

    # Signal panel
    api.post('/app/installed/', json.dumps([{'id': args.uuid}]))
    api.post('/notice/create/', json.dumps([{
        'type':'M',
        'content': f'Discourse installed (self-contained) for app {appname}. See {appdir}/README.md'
    }]))

    logging.info('Install complete.')

if __name__ == '__main__':
    main()
