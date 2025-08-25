#!/usr/bin/python3
"""
Opalstack Discourse (Podman, tiredofit) — robust installer with debug/probes.

What this does:
- Auth to panel API (token or username/password)
- Create PG user+db on the SAME server; grant RW and wait until 'ready'
- Write .env with DB/Redis/LOGS and auto-detected PGSSLMODE (prefer/require)
- Create pod (redis + web/sidekiq), fix log bind perms, start services
- Wait for Redis PING and Unicorn to listen on :3000
- Install helper scripts: start/stop/update/check/logs/diagnose
- Add cron for periodic health and nightly update

Tested assumptions:
- rootless podman; host Postgres reachable from containers via host.containers.internal
- tiredofit/discourse image bundles unicorn + sidekiq in one container
"""
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random, time

API_HOST     = (os.environ.get('API_URL') or 'https://my.opalstack.com').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'
CMD_ENV      = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'UMASK': '0002'}

IMG_WEB      = 'docker.io/tiredofit/discourse:latest'
IMG_REDIS    = 'docker.io/library/redis:7-alpine'
IMG_PSQLCLI  = 'docker.io/library/postgres:16-alpine'

# ---------- HTTP/Panel API ----------
class OpalstackAPITool:
    def __init__(self, host, base_uri, authtoken, user, password):
        self.host, self.base_uri = host, base_uri
        if not authtoken:
            endpoint = self.base_uri + '/login/'
            payload = json.dumps({'username': user, 'password': password})
            conn = http.client.HTTPSConnection(self.host)
            conn.request('POST', endpoint, payload, headers={'Content-type':'application/json'})
            resp = conn.getresponse(); raw = resp.read() or b'{}'
            if resp.status != 200:
                logging.error(f'Login failed HTTP {resp.status}')
                sys.exit(1)
            result = json.loads(raw)
            if not result.get('token'):
                logging.error('Invalid username/password and no token provided.')
                sys.exit(1)
            authtoken = result['token']
        self.headers = {'Content-type':'application/json', 'Authorization': f'Token {authtoken}'}
        self.token   = authtoken

    def _req(self, method, endpoint, payload=None):
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request(method, endpoint, payload, headers=self.headers)
        resp = conn.getresponse(); raw = resp.read() or b'{}'
        if resp.status not in (200, 201, 202, 204):
            logging.error(f'API {method} {endpoint} -> HTTP {resp.status}: {raw[:300]}')
            sys.exit(1)
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def get(self, endpoint):         return self._req('GET',  endpoint)
    def post(self, endpoint, body):  return self._req('POST', endpoint, body)

# ---------- helpers ----------
def create_file(path, contents, writemode='w', perms=0o600):
    with open(path, writemode) as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f'Created file {path} perms={oct(perms)}')

def gen_password(length=20):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

def run_command(cmd, cwd=None, env=CMD_ENV, check=True, capture=True):
    logging.info(f'$ {cmd}')
    try:
        if capture:
            out = subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env, stderr=subprocess.STDOUT)
            if out:
                logging.debug(out.decode(errors='ignore').rstrip())
            return out
        else:
            return subprocess.run(shlex.split(cmd), cwd=cwd, env=env, check=check)
    except subprocess.CalledProcessError as e:
        logging.error(f'Command failed ({e.returncode}): {cmd}')
        if hasattr(e, 'output') and e.output:
            logging.error(e.output.decode(errors='ignore').rstrip())
        if check:
            sys.exit(e.returncode)
        return b""

def add_cronjob(cron_line):
    homedir = os.path.expanduser('~')
    tmpname = f'{homedir}/.tmp{gen_password(8)}'
    # capture existing; ignore non-zero if no crontab yet
    existing = run_command('crontab -l', check=False)
    with open(tmpname, 'wb') as tmp:
        tmp.write(existing or b'')
        if existing and not existing.endswith(b'\n'):
            tmp.write(b'\n')
        tmp.write((cron_line + '\n').encode())
    run_command(f'crontab {tmpname}')
    run_command(f'rm -f {tmpname}')
    logging.info(f'Added cron: {cron_line}')

# ---------- PG create + wait ----------
def create_pg_same_server(api, appinfo, prefix='discours'):
    """
    - Create psql user WITH password
    - Wait for user presence
    - Create DB owned by user with RW grants
    - Wait for DB presence AND .ready flag
    """
    uname = f"{prefix}_{appinfo['id'][:8]}".lower()
    upass = gen_password(24)
    server = appinfo['server']

    logging.info(f'Creating PG user {uname} on server {server}...')
    api.post('/psqluser/create/', json.dumps([{'server': server, 'name': uname, 'password': upass}]))

    uid = None
    t0 = time.time()
    for _ in range(120):
        users = api.get('/psqluser/list/')
        match = [u for u in users if u.get('name') == uname]
        if match:
            uid = match[0].get('id')
            logging.info(f'PG user id={uid} is present ({int(time.time()-t0)}s).')
            break
        time.sleep(1)
    if not uid:
        logging.error('Timeout waiting for PG user to appear.')
        sys.exit(1)

    logging.info(f'Creating PG db {uname} owned by {uname} with RW grants...')
    api.post('/psqldb/create/', json.dumps([{
        'name': uname,
        'server': server,
        'dbusers_readwrite': [uid]
    }]))

    dbid = None
    t0 = time.time()
    for _ in range(180):
        dbs = api.get('/psqldb/list/')
        match = [d for d in dbs if d.get('name') == uname]
        if match:
            dbid = match[0].get('id')
            logging.info(f'PG db id={dbid} is present ({int(time.time()-t0)}s).')
            break
        time.sleep(1)
    if not dbid:
        logging.error('Timeout waiting for PG db to appear.')
        sys.exit(1)

    logging.info('Waiting for PG db .ready status...')
    t0 = time.time()
    for _ in range(300):
        db = api.get(f'/psqldb/read/{dbid}')
        if db.get('ready'):
            logging.info(f'PG db is ready ({int(time.time()-t0)}s).')
            break
        time.sleep(1)
    else:
        logging.error('Timeout waiting for PG db to be ready.')
        sys.exit(1)

    # host for rootless podman
    return {'host': 'host.containers.internal', 'port': 5432, 'user': uname, 'password': upass, 'db': uname}

# ---------- PG SSL probe ----------
def probe_pg_ssl(host, port, db, user, password):
    """
    Try sslmode=disable first; if that fails, try require.
    Returns: 'disable' (we map to 'prefer'), 'require', or 'prefer' fallback.
    """
    run_command(f'podman pull {IMG_PSQLCLI}', check=False)

    base_conn = f'host={host} port={port} dbname={db} user={user} connect_timeout=4'
    q = "select coalesce((select ssl from pg_stat_ssl where pid=pg_backend_pid()), false);"

    def try_mode(mode):
        conn = f'"sslmode={mode} {base_conn}"'
        cmd  = (
            f'podman run --rm -e PGPASSWORD="{password}" {IMG_PSQLCLI} '
            f'psql {conn} -tAc "{q}"'
        )
        out = run_command(cmd, check=False)
        if out:
            s = out.decode().strip()
            logging.info(f'PG probe sslmode={mode} -> {s!r}')
            return True, s
        return False, ''

    ok, _ = try_mode('disable')
    if ok:
        return 'prefer'        # disable worked → no SSL required

    ok, _ = try_mode('require')
    if ok:
        return 'require'       # require works → server needs SSL

    logging.warning('PG SSL probe inconclusive; defaulting to prefer.')
    return 'prefer'

# ---------- main ----------
def main():
    p = argparse.ArgumentParser(description='Install Discourse (Podman) on Opalstack (tiredofit image) with robust checks')
    p.add_argument('-i', dest='app_uuid',   default=os.environ.get('UUID'), help='App UUID (panel)')
    p.add_argument('-n', dest='app_name',   default=os.environ.get('APPNAME'), help='App name (panel)')
    p.add_argument('-t', dest='opal_token', default=os.environ.get('OPAL_TOKEN'), help='Panel API token')
    p.add_argument('-u', dest='opal_user',  default=os.environ.get('OPAL_USER'), help='Panel username (if no token)')
    p.add_argument('-p', dest='opal_pass',  default=os.environ.get('OPAL_PASS'), help='Panel password (if no token)')
    p.add_argument('--debug', action='store_true', help='Debug logging')
    args = p.parse_args()

    logging.basicConfig(level=(logging.DEBUG if args.debug else logging.INFO),
                        format='[%(asctime)s] %(levelname)s: %(message)s')

    if not args.app_uuid:
        logging.error('Missing UUID (-i).'); sys.exit(1)

    logging.info(f'Beginning Discourse install for app "{args.app_name or args.app_uuid}"')
    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_pass)
    app = api.get(f'/app/read/{args.app_uuid}')
    if not app.get('name'):
        logging.error('App not found by UUID.'); sys.exit(1)

    appdir = f"/home/{app['osuser_name']}/apps/{app['name']}"
    logdir = f"/home/{app['osuser_name']}/logs/apps/{app['name']}"
    port   = int(app['port'])
    appname= app['name']

    # Directories
    run_command(f'mkdir -p {appdir}/data')
    run_command(f'mkdir -p {logdir}')

    # PG: create, grant RW, wait ready
    pg = create_pg_same_server(api, app, prefix='discours')

    # Probe SSL requirement BEFORE writing .env
    sslmode = probe_pg_ssl(pg['host'], pg['port'], pg['db'], pg['user'], pg['password'])

    # .env (raw values; no quotes)
    env = textwrap.dedent(f"""\
    # Discourse DB
    DB_HOST={pg['host']}
    DB_PORT={pg['port']}
    DB_NAME={pg['db']}
    DB_USER={pg['user']}
    DB_PASS={pg['password']}
    PGSSLMODE={ 'require' if sslmode=='require' else 'prefer' }
    DB_SSLMODE={ 'require' if sslmode=='require' else 'prefer' }

    # Redis (same pod)
    REDIS_HOST=127.0.0.1
    REDIS_PORT=6379

    # Admin bootstrap (first run)
    ADMIN_USER=admin
    ADMIN_EMAIL=admin@example.com
    ADMIN_PASS={gen_password(16)}

    # Logs
    LOG_PATH=/data/logs
    LOG_LEVEL=info
    """)
    create_file(f'{appdir}/.env', env, perms=0o600)

    # Helper scripts ----------------------------------------------------------
    start = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    trap 'echo "[start] error at line $LINENO"; exit 1' ERR

    APP="{appname}"
    POD="$APP-pod"
    PORT="{port}"
    APPDIR="{appdir}"
    LOGDIR="{logdir}"
    IMG_WEB="{IMG_WEB}"
    IMG_REDIS="{IMG_REDIS}"
    source "$APPDIR/.env"

    echo "[start] pulling images..."
    podman pull "$IMG_WEB" >/dev/null || true
    podman pull "$IMG_REDIS" >/dev/null || true

    echo "[start] cleaning previous containers/pod (if any)..."
    podman rm -f "$APP-redis" "$APP" >/dev/null 2>&1 || true
    podman pod rm -f "$POD"    >/dev/null 2>&1 || true

    echo "[start] preparing log bind mount perms (rootless-friendly)..."
    podman unshare chown -R 0:0 "$LOGDIR" || true
    chmod 0777 "$LOGDIR" || true

    echo "[start] creating pod and port mapping 127.0.0.1:$PORT -> :3000"
    podman pod create --name "$POD" -p 127.0.0.1:${{PORT}}:3000

    echo "[start] starting redis..."
    podman run -d --name "$APP-redis" --pod "$POD" --restart=always \\
      "$IMG_REDIS"

    echo "[start] waiting for redis to respond to PING..."
    for i in $(seq 1 30); do
      if podman exec "$APP-redis" redis-cli -h 127.0.0.1 -p 6379 ping | grep -q PONG; then
        echo "[start] redis is up."
        break
      fi
      sleep 1
    done

    echo "[start] starting discourse (web+sidekiq)..."
    podman run -d --name "$APP" --pod "$POD" --restart=always \\
      -v "$APPDIR/data:/data" \\
      -v "$LOGDIR:/data/logs" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG_WEB"

    echo "[start] waiting for unicorn to listen on :3000..."
    for i in $(seq 1 120); do
      # busybox nc may not exist; use ruby (bundled) to check TCP
      if podman exec "$APP" sh -lc 'ruby -e "require %q(socket); begin; TCPSocket.new(%q(127.0.0.1),3000).close; puts %q(ok); rescue; exit 1; end"' >/dev/null 2>&1; then
        echo "[start] unicorn is listening."
        break
      fi
      sleep 2
    done

    echo "Started Discourse for {appname} on http://127.0.0.1:{port}/"
    """)

    stop = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{appname}"
    echo "[stop] stopping containers and pod..."
    podman rm -f "$APP" "$APP-redis" >/dev/null 2>&1 || true
    podman pod rm -f "$APP-pod"     >/dev/null 2>&1 || true
    echo "Stopped Discourse for {appname}"
    """)

    logs = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    LOGDIR="{logdir}"
    echo "[logs] tailing (Ctrl-C to exit)..."
    tail -F "$LOGDIR/discourse.log" "$LOGDIR/unicorn.log" "$LOGDIR/unicorn-error.log" "$LOGDIR/sidekiq.log" 2>/dev/null
    """)

    update = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APPDIR="{appdir}"
    echo "[update] restarting app to pick up new image/env..."
    "$APPDIR/stop" || true
    "$APPDIR/start"
    """)

    check = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{appname}"
    PORT="{port}"
    APPDIR="{appdir}"
    echo "[check] verifying $APP container running..."
    RUNNING=$(podman inspect -f '{{{{.State.Running}}}}' "$APP" 2>/dev/null || echo "false")
    if [ "$RUNNING" != "true" ]; then
      echo "[check] container down, starting..."
      "$APPDIR/start"
      exit 0
    fi
    # quick HTTP probe
    code=$(curl -sS -o /dev/null -m 3 -w '%{{http_code}}' "http://127.0.0.1:$PORT/")
    echo "[check] HTTP status: $code"
    """)

    diagnose = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{appname}"
    POD="$APP-pod"
    APPDIR="{appdir}"
    LOGDIR="{logdir}"
    PORT="{port}"
    echo "=== POD/CONTAINERS ==="
    podman pod ps
    podman ps --format "table {{.Names}}\\t{{.Status}}\\t{{.Image}}\\t{{.Ports}}"
    echo
    echo "=== PORTS ==="
    podman port "$APP" 2>/dev/null || true
    ss -ltnp | grep -E "127\\.0\\.0\\.1:($PORT)\\b" || echo "host not listening on $PORT (expected, we use mapping)"
    echo
    echo "=== RESOLUTION INSIDE POD ==="
    podman exec "$APP" sh -lc 'getent hosts host.containers.internal || echo no-host-alias'
    echo
    echo "=== REDIS PING ==="
    podman exec "$APP-redis" redis-cli -h 127.0.0.1 -p 6379 ping || true
    echo
    echo "=== CURL HOST -> APP ==="
    curl -sv --max-time 3 "http://127.0.0.1:$PORT/" -o /dev/null || true
    echo
    echo "=== LAST LOGS ==="
    ls -lh "$LOGDIR"
    for f in "$LOGDIR"/*.log; do echo "--- $f"; tail -n 120 "$f"; echo; done
    """)

    create_file(f'{appdir}/start',     start,     perms=0o700)
    create_file(f'{appdir}/stop',      stop,      perms=0o700)
    create_file(f'{appdir}/logs',      logs,      perms=0o700)
    create_file(f'{appdir}/update',    update,    perms=0o700)
    create_file(f'{appdir}/check',     check,     perms=0o700)
    create_file(f'{appdir}/diagnose',  diagnose,  perms=0o700)

    readme = textwrap.dedent(f"""\
    # Opalstack Discourse (tiredofit) — {appname}

    **Port:** {port} → container 3000 (127.0.0.1 only)  
    **Data:** {appdir}/data  
    **Env:**  {appdir}/.env  
    **Logs:** {logdir}/  (discourse.log, unicorn.log, unicorn-error.log, sidekiq.log)

    ## Commands
    - Start:   {appdir}/start
    - Stop:    {appdir}/stop
    - Update:  {appdir}/update
    - Check:   {appdir}/check
    - Logs:    {appdir}/logs
    - Diagnose:{appdir}/diagnose

    ## Notes
    - DB host is `host.containers.internal` so the container can reach host Postgres.
    - Installer probed your PG for SSL. Current mode: **{ 'require' if sslmode=='require' else 'prefer (no SSL required)' }**.
    - First boot compiles assets and runs migrations; app answers on :3000 when ready.
    """)
    create_file(f'{appdir}/README.md', readme, perms=0o600)

    # Cron: ~10 min health; nightly update 01:00–05:59 randomized
    m = random.randint(0,9)
    add_cronjob(f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check > /dev/null 2>&1')
    hh = random.randint(1,5); mm = random.randint(0,59)
    add_cronjob(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

    # One-time start
    run_command(f'{appdir}/start', capture=False)

    # Panel signals
    api.post('/app/installed/', json.dumps([{'id': args.app_uuid}]))
    msg = f'Discourse installed for app {appname} (tiredofit). See README.md in {appdir}.'
    api.post('/notice/create/', json.dumps([{'type': 'M', 'content': msg}]))

    logging.info(f'Completed installation of Discourse app {appname}')
    logging.info(f'URL: http://127.0.0.1:{port}/ (proxied by your web server)')

if __name__ == '__main__':
    main()
