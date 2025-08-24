#!/usr/bin/python3
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random, time

API_HOST = (os.environ.get('API_URL') or 'https://my.opalstack.com').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'
CMD_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'UMASK': '0002'}

# Images (no Bitnami)
IMG_WEB   = 'docker.io/tiredofit/discourse:latest'   # web + sidekiq in one container
IMG_REDIS = 'docker.io/library/redis:7-alpine'       # official Redis

# ---------- Opalstack API (Ghost style) ----------
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
                logging.warning('Invalid username or password and no auth token provided, exiting.')
                sys.exit(1)
            authtoken = result['token']
        self.headers = {'Content-type':'application/json', 'Authorization': f'Token {authtoken}'}
        self.token = authtoken
    def get(self, endpoint):
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request('GET', endpoint, headers=self.headers)
        return json.loads(conn.getresponse().read() or b'{}')
    def post(self, endpoint, payload):
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request('POST', endpoint, payload, headers=self.headers)
        return json.loads(conn.getresponse().read() or b'{}')

# ---------- helpers (Ghost idiom) ----------
def create_file(path, contents, writemode='w', perms=0o600):
    with open(path, writemode) as f: f.write(contents)
    os.chmod(path, perms)
    logging.info(f'Created file {path} with permissions {oct(perms)}')

def gen_password(length=20):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

def run_command(cmd, cwd=None, env=CMD_ENV):
    logging.info(f'Running: {cmd}')
    try:
        return subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env)
    except subprocess.CalledProcessError as e:
        logging.debug(getattr(e, 'output', b''))
        sys.exit(e.returncode)

def add_cronjob(cronjob):
    homedir = os.path.expanduser('~')
    tmpname = f'{homedir}/.tmp{gen_password(8)}'
    with open(tmpname, 'w') as tmp:
        subprocess.run('crontab -l'.split(), stdout=tmp)
        tmp.write(f'{cronjob}\n')
    run_command(f'crontab {tmpname}')
    run_command(f'rm -f {tmpname}')
    logging.info(f'Added cron job: {cronjob}')

# ---------- PG create on SAME server (panel API) ----------
def create_pg_same_server(api, appinfo, prefix='disc'):
    uname = f"{prefix}_{appinfo['id'][:8]}".lower()
    upass = gen_password(24)

    # create user WITH password
    api.post('/psqluser/create/', json.dumps([{'server': appinfo['server'], 'name': uname, 'password': upass}]))

    # wait until user exists
    uid = None
    for _ in range(60):
        users = api.get('/psqluser/list/')
        for u in users:
            if u.get('name') == uname:
                uid = u.get('id')
                break
        if uid: break
        time.sleep(1)
    if not uid:
        logging.error('Failed to create PG user'); sys.exit(1)

    # create db owned by user
    api.post('/psqldb/create/', json.dumps([{'name': uname, 'server': appinfo['server'], 'dbusers_readwrite': [uid]}]))

    # wait until db exists and ready
    dbid = None
    for _ in range(60):
        dbs = api.get('/psqldb/list/')
        for d in dbs:
            if d.get('name') == uname:
                dbid = d.get('id'); break
        if dbid: break
        time.sleep(1)
    if not dbid:
        logging.error('Failed to create PG database'); sys.exit(1)
    while not api.get(f'/psqldb/read/{dbid}').get('ready'):
        time.sleep(1)

    # use host.containers.internal so rootless podman can reach host PG
    return {'host': 'host.containers.internal', 'port': 5432, 'user': uname, 'password': upass, 'db': uname}

# ---------- main ----------
def main():
    p = argparse.ArgumentParser(description='Installs Discourse (Podman) on Opalstack (tiredofit image)')
    p.add_argument('-i', dest='app_uuid',   default=os.environ.get('UUID'))
    p.add_argument('-n', dest='app_name',   default=os.environ.get('APPNAME'))
    p.add_argument('-t', dest='opal_token', default=os.environ.get('OPAL_TOKEN'))
    p.add_argument('-u', dest='opal_user',  default=os.environ.get('OPAL_USER'))
    p.add_argument('-p', dest='opal_pass',  default=os.environ.get('OPAL_PASS'))
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
    if not args.app_uuid:
        logging.error('Missing UUID (-i)'); sys.exit(1)

    logging.info(f'Started installation of Discourse app {args.app_name}')
    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_pass)
    app = api.get(f'/app/read/{args.app_uuid}')
    if not app.get('name'):
        logging.error('App not found'); sys.exit(1)

    appdir = f"/home/{app['osuser_name']}/apps/{app['name']}"
    port   = int(app['port'])
    logdir = f"/home/{app['osuser_name']}/logs/apps/{app['name']}"

    # dirs
    run_command(f'mkdir -p {appdir}/data')
    run_command(f'mkdir -p {logdir}')

    # PG on same server
    pg = create_pg_same_server(api, app, prefix='discours')

    # .env (NOTE: NO QUOTES; podman --env-file uses raw values)
    env = textwrap.dedent(f"""\
    # Discourse DB
    DB_HOST={pg['host']}
    DB_PORT={pg['port']}
    DB_NAME={pg['db']}
    DB_USER={pg['user']}
    DB_PASS={pg['password']}

    # Redis (same pod)
    REDIS_HOST=127.0.0.1
    REDIS_PORT=6379

    # Admin bootstrap (first run)
    ADMIN_USER=admin
    ADMIN_EMAIL=admin@example.com
    ADMIN_PASS={gen_password(16)}

    # Logs (container writes to /data/logs; mounted to {logdir})
    LOG_PATH=/data/logs
    LOG_LEVEL=info

    # Optional SMTP (set later, then run update)
    # SMTP_HOST=smtp.example.com
    # SMTP_PORT=587
    # SMTP_USER=smtp-user
    # SMTP_PASS=smtp-pass
    # SMTP_START_TLS=TRUE
    """)
    create_file(f'{appdir}/.env', env, perms=0o600)

    # scripts
    start = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{app['name']}"
    POD="$APP-pod"
    PORT="{port}"
    APPDIR="{appdir}"
    LOGDIR="{logdir}"
    IMG_WEB="{IMG_WEB}"
    IMG_REDIS="{IMG_REDIS}"
    source "$APPDIR/.env"

    podman pull "$IMG_WEB" >/dev/null || true
    podman pull "$IMG_REDIS" >/dev/null || true

    # clean
    podman rm -f "$APP-redis" "$APP" >/dev/null 2>&1 || true
    podman pod rm -f "$POD" >/dev/null 2>&1 || true

    # pod & port
    podman pod create --name "$POD" -p 127.0.0.1:${{PORT}}:3000

    # redis (no host port)
    podman run -d --name "$APP-redis" --pod "$POD" \\
      "$IMG_REDIS"

    # discourse (web+sidekiq) - mount data & logs
    podman run -d --name "$APP" --pod "$POD" \\
      -v "$APPDIR/data:/data" \\
      -v "$LOGDIR:/data/logs" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG_WEB"

    echo "Started Discourse for {app['name']} on 127.0.0.1:{port}"
    """)
    stop = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    podman rm -f {app['name']} {app['name']}-redis >/dev/null 2>&1 || true
    podman pod rm -f {app['name']}-pod >/dev/null 2>&1 || true
    echo "Stopped Discourse for {app['name']}"
    """)
    logs = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    tail -F "{logdir}/discourse.log" "{logdir}/unicorn.log" "{logdir}/unicorn_error.log" "{logdir}/sidekiq.log" 2>/dev/null
    """)
    update = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    "{appdir}/stop"
    "{appdir}/start"
    """)
    # Only restart if main container isn't running (avoid interrupting first-boot tasks)
    check = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{app['name']}"
    RUNNING=$(podman inspect -f '{{{{.State.Running}}}}' "$APP" 2>/dev/null || echo "false")
    if [ "$RUNNING" != "true" ]; then
      "{appdir}/start"
    fi
    """)

    create_file(f'{appdir}/start',  start,  perms=0o700)
    create_file(f'{appdir}/stop',   stop,   perms=0o700)
    create_file(f'{appdir}/logs',   logs,   perms=0o700)
    create_file(f'{appdir}/update', update, perms=0o700)
    create_file(f'{appdir}/check',  check,  perms=0o700)

    readme = textwrap.dedent(f"""\
    # Opalstack Discourse (tiredofit)

    **App:** {app['name']}
    **Port:** {port} → container 3000
    **Data:** {appdir}/data
    **Env:**  {appdir}/.env
    **Logs:** {logdir}/ (discourse.log, unicorn*.log, sidekiq.log) ← rotated by Opalstack

    ## Commands
    - Start:  {appdir}/start
    - Stop:   {appdir}/stop
    - Logs:   {appdir}/logs
    - Update: {appdir}/update
    - Health: cron runs {appdir}/check (restarts only if not running)

    ## Notes
    - DB host is set to `host.containers.internal` so the container can reach the host Postgres on this server.
    - First boot may take a few minutes for DB migrations and asset compile; 502s are normal until Unicorn listens on :3000.
    - To enable email, add SMTP_* vars in {appdir}/.env then run `{appdir}/update`.
    """)
    create_file(f'{appdir}/README.md', readme, perms=0o600)

    # cron: check every ~10m; nightly update at random hour
    m = random.randint(0,9)
    add_cronjob(f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check > /dev/null 2>&1')
    hh = random.randint(1,5); mm = random.randint(0,59)
    add_cronjob(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

    # start once
    run_command(f'{appdir}/start')

    # panel signals
    api.post('/app/installed/', json.dumps([{'id': args.app_uuid}]))
    msg = f'Discourse installed for app {app["name"]} (tiredofit). See README.md in {appdir} for commands and post-install steps.'
    api.post('/notice/create/', json.dumps([{'type': 'M', 'content': msg}]))

    logging.info(f'Completed installation of Discourse app {args.app_name}')

if __name__ == '__main__':
    main()
