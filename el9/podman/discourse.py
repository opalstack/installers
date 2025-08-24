#!/usr/bin/python3
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random

API_HOST = (os.environ.get('API_URL') or 'https://my.opalstack.com').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'
CMD_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'UMASK': '0002'}

IMG_WEB   = 'docker.io/bitnami/discourse:latest'   # use SAME image for web and sidekiq
IMG_REDIS = 'docker.io/bitnami/redis:7.2'

# ----- tiny API wrapper (same as Ghost) -----
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
        self.headers = {'Content-type': 'application/json', 'Authorization': f'Token {authtoken}'}
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
        result = subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env)
    except subprocess.CalledProcessError as e:
        logging.debug(getattr(e, 'output', b''))
        result = getattr(e, 'output', b'')  # avoid UnboundLocalError
    return result

def add_cronjob(cronjob):
    homedir = os.path.expanduser('~'); tmpname = f'{homedir}/.tmp{gen_password()}'
    with open(tmpname, 'w') as tmp:
        subprocess.run('crontab -l'.split(), stdout=tmp)
        tmp.write(f'{cronjob}\n')
    run_command(f'crontab {tmpname}')
    run_command(f'rm -f {tmpname}')
    logging.info(f'Added cron job: {cronjob}')

# ----- inline PG creation on SAME server (using your panel API like Ghost) -----
def ensure_pg_same_server(api, appinfo, prefix):
    # create user
    uname = f"{prefix}_{appinfo['id'][:8]}".lower()
    user_payload = json.dumps([{'server': appinfo['server'], 'name': uname}])
    api.post('/psqluser/create/', user_payload)
    # read back
    users = api.get('/psqluser/list/')
    u = next((x for x in users if x.get('name') == uname), None)
    if not u:
        logging.error('Failed to create PG user'); sys.exit(1)
    upass = gen_password(24)

    # create db
    db_payload = json.dumps([{'name': uname, 'server': appinfo['server'], 'dbusers_readwrite': [u['id']]}])
    api.post('/psqldb/create/', db_payload)
    dbs = api.get('/psqldb/list/')
    d = next((x for x in dbs if x.get('name') == uname), None)
    if not d:
        logging.error('Failed to create PG database'); sys.exit(1)

    # resolve host
    dbinfo = api.get(f"/psqldb/read/{d['id']}")
    host = dbinfo.get('hostname') or '127.0.0.1'
    return {'host': host, 'port': 5432, 'user': uname, 'password': upass, 'db': uname}

def main():
    # args like Ghost
    parser = argparse.ArgumentParser(description='Installs Discourse (Podman) on Opalstack')
    parser.add_argument('-i', dest='app_uuid',   default=os.environ.get('UUID'))
    parser.add_argument('-n', dest='app_name',   default=os.environ.get('APPNAME'))
    parser.add_argument('-t', dest='opal_token', default=os.environ.get('OPAL_TOKEN'))
    parser.add_argument('-u', dest='opal_user',  default=os.environ.get('OPAL_USER'))
    parser.add_argument('-p', dest='opal_pass',  default=os.environ.get('OPAL_PASS'))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
    if not args.app_uuid:
        logging.error('Missing UUID (-i)'); sys.exit(1)

    logging.info(f'Started installation of Discourse app {args.app_name}')
    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_pass)
    appinfo = api.get(f'/app/read/{args.app_uuid}')
    if not appinfo.get('name'):
        logging.error('App not found'); sys.exit(1)

    appdir = f"/home/{appinfo['osuser_name']}/apps/{appinfo['name']}"
    port   = int(appinfo['port'])

    # dirs
    run_command(f'mkdir -p {appdir}/data/discourse')
    run_command(f'mkdir -p {appdir}/tmp')

    # PG same server
    pg = ensure_pg_same_server(api, appinfo, prefix='discours')

    # .env (align with current Bitnami vars)
    env = textwrap.dedent(f"""\
    # Public host you'll assign later:
    DISCOURSE_HOST="forum.example.com"

    # Admin bootstrap user (finish via wizard):
    DISCOURSE_USERNAME="admin"
    DISCOURSE_PASSWORD="{gen_password(16)}"
    DISCOURSE_EMAIL="admin@example.com"
    DISCOURSE_ENABLE_HTTPS="no"

    # PostgreSQL
    DISCOURSE_DATABASE_HOST="{pg['host']}"
    DISCOURSE_DATABASE_PORT_NUMBER="{pg['port']}"
    DISCOURSE_DATABASE_USER="{pg['user']}"
    DISCOURSE_DATABASE_PASSWORD="{pg['password']}"
    DISCOURSE_DATABASE_NAME="{pg['db']}"

    # Redis (same pod network namespace)
    DISCOURSE_REDIS_HOST="127.0.0.1"
    DISCOURSE_REDIS_PORT_NUMBER="6379"
    """)
    create_file(f'{appdir}/.env', env, perms=0o600)

    # start/stop/logs/update/check
    start = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{appinfo['name']}"
    POD="$APP-pod"
    PORT="{port}"
    APPDIR="{appdir}"
    IMG_WEB="{IMG_WEB}"
    IMG_REDIS="{IMG_REDIS}"
    source "$APPDIR/.env"

    podman pull "$IMG_WEB" >/dev/null || true
    podman pull "$IMG_REDIS" >/dev/null || true

    podman rm -f "$APP-redis" "$APP-sidekiq" "$APP" >/dev/null 2>&1 || true
    podman pod rm -f "$POD" >/dev/null 2>&1 || true

    podman pod create --name "$POD" -p 127.0.0.1:${{PORT}}:3000

    # Redis inside pod (reachable at 127.0.0.1:6379)
    podman run -d --name "$APP-redis" --pod "$POD" -e ALLOW_EMPTY_PASSWORD=yes "$IMG_REDIS"

    # Web
    podman run -d --name "$APP" --pod "$POD" \\
      -v "$APPDIR/data/discourse:/bitnami/discourse" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG_WEB"

    # Sidekiq (same image, different command)
    podman run -d --name "$APP-sidekiq" --pod "$POD" \\
      -v "$APPDIR/data/discourse:/bitnami/discourse" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG_WEB" /opt/bitnami/scripts/discourse-sidekiq/run.sh

    echo "Started Discourse for {appinfo['name']} on 127.0.0.1:{port}"
    """)
    stop = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    podman rm -f {appinfo['name']}-sidekiq {appinfo['name']} {appinfo['name']}-redis >/dev/null 2>&1 || true
    podman pod rm -f {appinfo['name']}-pod >/dev/null 2>&1 || true
    echo "Stopped Discourse for {appinfo['name']}."
    """)
    logs = f"#!/bin/bash\npodman logs -f {appinfo['name']}\n"
    update = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    "{appdir}/stop"
    "{appdir}/start"
    """)
    check = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    curl -fsS "http://127.0.0.1:{port}/" >/dev/null || "{appdir}/start"
    """)

    create_file(f'{appdir}/start',  start,  perms=0o700)
    create_file(f'{appdir}/stop',   stop,   perms=0o700)
    create_file(f'{appdir}/logs',   logs,   perms=0o700)
    create_file(f'{appdir}/update', update, perms=0o700)
    create_file(f'{appdir}/check',  check,  perms=0o700)

    # README
    readme = textwrap.dedent(f"""\
    # Opalstack Discourse README

    App: {appinfo['name']}
    Port: {port}
    Data: {appdir}/data/discourse
    PostgreSQL: {pg['host']} db={pg['db']} user={pg['user']}

    After assigning this app to a site in the control panel:
      • Wizard: https://YOUR-DOMAIN/wizard
      • Admin:  https://YOUR-DOMAIN/admin
    """)
    create_file(f'{appdir}/README.txt', readme, perms=0o600)

    # cron like Ghost
    m = random.randint(0,9)
    add_cronjob(f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check > /dev/null 2>&1')
    hh = random.randint(1,5); mm = random.randint(0,59)
    add_cronjob(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

    # start once (last mile)
    run_command(f'{appdir}/start')

    # finished: mark installed + notice with links
    api.post('/app/installed/', json.dumps([{'id': args.app_uuid}]))

    msg = ("Discourse installed. Assign this app to a site, then finish setup:\n"
           "• Wizard: https://YOUR-DOMAIN/wizard\n"
           "• Admin:  https://YOUR-DOMAIN/admin")
    api.post('/notice/create/', json.dumps([{'type': 'D', 'content': msg}]))

    logging.info(f'Completed installation of Discourse app {args.app_name}')

if __name__ == '__main__':
    main()
