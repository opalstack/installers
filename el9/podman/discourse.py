#!/usr/bin/python3
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random

API_HOST = (os.environ.get('API_URL') or 'https://my.opalstack.com').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'
CMD_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'UMASK': '0002'}

IMG_WEB   = 'docker.io/bitnami/discourse:latest'   # web + sidekiq use the SAME image
IMG_REDIS = 'docker.io/bitnami/redis:7.2'

# ----- tiny API wrapper (Ghost style) -----
class OpalstackAPITool():
    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host; self.base_uri = base_uri
        if not authtoken:
            endpoint = self.base_uri + '/login/'
            payload = json.dumps({'username': user, 'password': password})
            conn = http.client.HTTPSConnection(self.host)
            conn.request('POST', endpoint, payload, headers={'Content-type': 'application/json'})
            result = json.loads(conn.getresponse().read() or b'{}')
            if not result.get('token'):
                logging.warning('Invalid username or password and no auth token provided, exiting.')
                sys.exit(1)
            authtoken = result['token']
        self.headers = {'Content-type':'application/json','Authorization': f'Token {authtoken}'}
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

# ----- helpers (Ghost idiom) -----
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

# ----- create PG on same server via panel API (Ghost pattern) -----
def create_pg_same_server(api, appinfo, prefix='disc'):
    uname = f"{prefix}_{appinfo['id'][:8]}".lower()
    # user
    api.post('/psqluser/create/', json.dumps([{'server': appinfo['server'], 'name': uname}]))
    users = api.get('/psqluser/list/')
    u = next((x for x in users if x.get('name') == uname), None)
    if not u: logging.error('Failed to create PG user'); sys.exit(1)
    upass = gen_password(24)
    # db
    api.post('/psqldb/create/', json.dumps([{'name': uname, 'server': appinfo['server'], 'dbusers_readwrite': [u['id']]}]))
    dbs = api.get('/psqldb/list/')
    d = next((x for x in dbs if x.get('name') == uname), None)
    if not d: logging.error('Failed to create PG database'); sys.exit(1)
    # ready flags (simple poll like Ghost installers typically do)
    while not api.get(f"/psqldb/read/{d['id']}").get('ready'): pass
    while not api.get(f"/psqluser/read/{u['id']}").get('ready'): pass
    host = api.get(f"/psqldb/read/{d['id']}").get('hostname') or '127.0.0.1'
    return {'host': host, 'port': 5432, 'user': uname, 'password': upass, 'db': uname}

def main():
    p = argparse.ArgumentParser(description='Installs Discourse (Podman) on Opalstack')
    p.add_argument('-i', dest='app_uuid',   default=os.environ.get('UUID'))
    p.add_argument('-n', dest='app_name',   default=os.environ.get('APPNAME'))
    p.add_argument('-t', dest='opal_token', default=os.environ.get('OPAL_TOKEN'))
    p.add_argument('-u', dest='opal_user',  default=os.environ.get('OPAL_USER'))
    p.add_argument('-p', dest='opal_pass',  default=os.environ.get('OPAL_PASS'))
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
    if not args.app_uuid:
        logging.error('Missing UUID (-i)'); sys.exit(1)

    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_pass)
    app = api.get(f'/app/read/{args.app_uuid}')
    if not app.get('name'):
        logging.error('App not found'); sys.exit(1)

    appdir  = f"/home/{app['osuser_name']}/apps/{app['name']}"
    port    = int(app['port'])
    logdir  = f"/home/{app['osuser_name']}/logs/apps/{app['name']}"

    # dirs
    run_command(f'mkdir -p {appdir}/data/discourse')
    run_command(f'mkdir -p {appdir}/tmp')
    run_command(f'mkdir -p {logdir}')

    # PG (same server)
    pg = create_pg_same_server(api, app, prefix='discours')

    # .env (Bitnami vars)
    env = textwrap.dedent(f"""\
    # Public hostname (set after domain is assigned):
    DISCOURSE_HOSTNAME="forum.example.com"
    DISCOURSE_ENABLE_HTTPS="no"

    # Bootstrap admin (complete via /wizard):
    DISCOURSE_USERNAME="admin"
    DISCOURSE_PASSWORD="{gen_password(16)}"
    DISCOURSE_EMAIL="admin@example.com"

    # Database (Bitnami Discourse expects these names)
    DISCOURSE_DATABASE_HOST="{pg['host']}"
    DISCOURSE_DATABASE_PORT_NUMBER="{pg['port']}"
    DISCOURSE_DATABASE_USER="{pg['user']}"
    DISCOURSE_DATABASE_PASSWORD="{pg['password']}"
    DISCOURSE_DATABASE_NAME="{pg['db']}"

    # Force TCP (avoid Unix socket fallback)
    DISCOURSE_DB_SOCKET=""
    DISCOURSE_DB_HOST="{pg['host']}"
    DISCOURSE_DB_PORT="{pg['port']}"
    DISCOURSE_DB_NAME="{pg['db']}"
    DISCOURSE_DB_USERNAME="{pg['user']}"
    DISCOURSE_DB_PASSWORD="{pg['password']}"

    # Redis runs inside the pod
    DISCOURSE_REDIS_HOST="127.0.0.1"
    DISCOURSE_REDIS_PORT_NUMBER="6379"
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

    podman rm -f "$APP-redis" "$APP-sidekiq" "$APP" >/dev/null 2>&1 || true
    podman pod rm -f "$POD" >/dev/null 2>&1 || true

    podman pod create --name "$POD" -p 127.0.0.1:${{PORT}}:3000

    # Redis inside pod (no host port exposed)
    podman run -d --name "$APP-redis" --pod "$POD" -e ALLOW_EMPTY_PASSWORD=yes "$IMG_REDIS"

    # Web (mount log dir into container log path)
    podman run -d --name "$APP" --pod "$POD" \\
      -v "$APPDIR/data/discourse:/bitnami/discourse" \\
      -v "$LOGDIR:/bitnami/discourse/log" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG_WEB"

    # Sidekiq (same image, log dir mounted too)
    podman run -d --name "$APP-sidekiq" --pod "$POD" \\
      -v "$APPDIR/data/discourse:/bitnami/discourse" \\
      -v "$LOGDIR:/bitnami/discourse/log" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG_WEB" /opt/bitnami/scripts/discourse-sidekiq/run.sh

    echo "Started Discourse for {app['name']} on 127.0.0.1:{port}"
    """)
    stop = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    podman rm -f {app['name']}-sidekiq {app['name']} {app['name']}-redis >/dev/null 2>&1 || true
    podman pod rm -f {app['name']}-pod >/dev/null 2>&1 || true
    echo "Stopped Discourse for {app['name']}"
    """)
    # Tail app files in the same folder as install.log (production.log/sidekiq.log)
    logs = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    tail -F "{logdir}/production.log" "{logdir}/sidekiq.log"
    """)
    update = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    "{appdir}/stop"
    "{appdir}/start"
    """)
    # Only restart if web container is NOT running (avoid killing migrations)
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
    # Opalstack Discourse

    **App:** {app['name']}
    **Port:** {port} → container 3000
    **Data:** {appdir}/data/discourse
    **Env:**  {appdir}/.env
    **Logs:** {logdir}/ (production.log, sidekiq.log)  ← rotated by Opalstack

    ## Commands
    - Start:  {appdir}/start
    - Stop:   {appdir}/stop
    - Logs:   {appdir}/logs
    - Update: {appdir}/update
    - Health: cron runs {appdir}/check (restarts only if not running)

    ## First boot
    Discourse may take time to initialize (migrations, assets). 502s are expected until Puma is up.

    ## After assigning a domain
    1) Set `DISCOURSE_HOSTNAME` in {appdir}/.env
    2) (Optional) set SMTP envs (`DISCOURSE_SMTP_*`)
    3) Run `{appdir}/update`
    4) Complete setup:
       - http(s)://YOUR-DOMAIN/wizard
       - http(s)://YOUR-DOMAIN/admin
    """)
    create_file(f'{appdir}/README.md', readme, perms=0o600)

    # cron (check often; nightly update)
    m = random.randint(0,9)
    add_cronjob(f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check > /dev/null 2>&1')
    hh = random.randint(1,5); mm = random.randint(0,59)
    add_cronjob(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

    # start once
    run_command(f'{appdir}/start')

    # panel signals
    api.post('/app/installed/', json.dumps([{'id': args.app_uuid}]))
    msg = f'Discourse installed for app {app["name"]}. See README.md in {appdir} for commands and post-install steps.'
    api.post('/notice/create/', json.dumps([{'type': 'M', 'content': msg}]))

    logging.info(f'Completed installation of Discourse app {args.app_name}')

if __name__ == '__main__':
    main()
