#!/usr/local/bin/python3.11
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random, re
from urllib.parse import urlparse

API_HOST = (os.environ.get('API_URL') or 'https://my.opalstack.com').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'
CMD_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'UMASK': '0002'}

IMG_WEB   = 'docker.io/bitnami/discourse:latest'
IMG_SQ    = 'docker.io/bitnami/discourse-sidekiq:latest'
IMG_REDIS = 'docker.io/bitnami/redis:7.2'

# ----- tiny API wrapper (same as Ghost) -----
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
            result = json.loads(conn.getresponse().read() or b'{}')
            if not result.get('token'):
                logging.warning('Invalid username or password and no auth token provided, exiting.')
                sys.exit(1)
            else:
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

# ----- helpers (same idiom as Ghost) -----
def create_file(path, contents, writemode='w', perms=0o600):
    with open(path, writemode) as f:
        f.write(contents)
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
    tmpname = f'{homedir}/.tmp{gen_password()}'
    with open(tmpname, 'w') as tmp:
        subprocess.run('crontab -l'.split(), stdout=tmp)
        tmp.write(f'{cronjob}\n')
    run_command(f'crontab {tmpname}')
    run_command(f'rm -f {tmpname}')
    logging.info(f'Added cron job: {cronjob}')

# ----- inline PG creation on SAME server (using opalstack-python) -----
def ensure_pg_same_server(opal_token, appinfo, prefix):
    # install client lib if missing
    try:
        import opalstack  # noqa
    except ImportError:
        run_command("python3 -m pip install --user --upgrade opalstack")

    import opalstack
    from opalstack.util import one, filt_one

    api = opalstack.Api(token=opal_token)

    # find this app's web server via OSUser
    osuser_name = appinfo.get('osuser_name') or appinfo.get('osuser') or ''
    osusers = api.osusers.list_all(embed=['server'])
    osuser = filt_one(osusers, {'name': osuser_name})
    if not osuser or not osuser.get('server'):
        logging.error('Cannot resolve OSUser server')
        sys.exit(1)
    web_server = osuser['server']

    # pick a PG server that is the same server/hostname, else closest
    servers = api.servers.list_all()
    pg_servers = (servers.get('pgsql_servers') or servers.get('db_servers') or [])
    if not pg_servers:
        logging.error('No PostgreSQL servers available')
        sys.exit(1)

    def pick():
        # exact id
        for s in pg_servers:
            if s.get('id') == web_server.get('id'):
                return s
        # exact hostname
        for s in pg_servers:
            if s.get('hostname') and s['hostname'] == web_server.get('hostname'):
                return s
        # same dc/region
        for k in ('datacenter', 'dc', 'region', 'location'):
            dc = web_server.get(k)
            if dc:
                m = [x for x in pg_servers if x.get(k) == dc]
                if m: return m[0]
        # same suffix
        suf = '.'.join((web_server.get('hostname') or '').split('.')[-2:])
        if suf:
            m = [x for x in pg_servers if (x.get('hostname') or '').endswith(suf)]
            if m: return m[0]
        return pg_servers[0]

    pg_server = pick()
    uname = f"{prefix}_{secrets.token_hex(3)}".lower()
    dname = f"{prefix}_{secrets.token_hex(2)}".lower()
    upass = secrets.token_urlsafe(24)

    u = one(api.pgsql_users.create([{'name': uname, 'server': pg_server['id'], 'password': upass}]))
    d = one(api.pgsql_databases.create([{'name': dname, 'server': pg_server['id'], 'users': [u['id']]}]))

    host = pg_server.get('hostname') or 'localhost'
    port = 5432
    url  = f"postgres://{uname}:{upass}@{host}:{port}/{dname}?sslmode=require"
    logging.info(f"PG created on {host}: db={d['name']} user={u['name']}")
    return {'host': host, 'port': port, 'user': uname, 'password': upass, 'db': dname, 'url': url}

def main():
    # args like Ghost
    parser = argparse.ArgumentParser(description='Installs Discourse (Podman) on Opalstack')
    parser.add_argument('-i', dest='app_uuid',    default=os.environ.get('UUID'))
    parser.add_argument('-n', dest='app_name',    default=os.environ.get('APPNAME'))
    parser.add_argument('-t', dest='opal_token',  default=os.environ.get('OPAL_TOKEN'))
    parser.add_argument('-u', dest='opal_user',   default=os.environ.get('OPAL_USER'))
    parser.add_argument('-p', dest='opal_pass',   default=os.environ.get('OPAL_PASS'))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')

    if not args.app_uuid:
        logging.error('Missing UUID (-i)')
        sys.exit(1)

    logging.info(f'Started installation of Discourse app {args.app_name}')
    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_pass)
    appinfo = api.get(f'/app/read/{args.app_uuid}')
    if not appinfo.get('name'):
        logging.error('App not found')
        sys.exit(1)

    appdir = f'/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}'
    port   = appinfo['port']

    # dirs
    run_command(f'mkdir -p {appdir}/data/discourse')
    run_command(f'mkdir -p {appdir}/tmp')

    # PG same server
    pg = ensure_pg_same_server(api.token, appinfo, prefix='disc')

    # .env
    env = textwrap.dedent(f"""\
    DISCOURSE_HOST="forum.example.com"
    DISCOURSE_USERNAME="admin"
    DISCOURSE_PASSWORD="{gen_password(16)}"
    DISCOURSE_EMAIL="admin@example.com"
    POSTGRESQL_HOST="{pg['host']}"
    POSTGRESQL_PORT="{pg['port']}"
    POSTGRESQL_USERNAME="{pg['user']}"
    POSTGRESQL_PASSWORD="{pg['password']}"
    POSTGRESQL_DATABASE="{pg['db']}"
    SMTP_HOST=""
    SMTP_PORT="587"
    SMTP_USER=""
    SMTP_PASSWORD=""
    SMTP_TLS="true"
    DISCOURSE_ENABLE_HTTPS="no"
    REDIS_HOST="{appinfo['name']}-redis"
    REDIS_PASSWORD=""
    """)
    create_file(f'{appdir}/.env', env, perms=0o600)

    # start/stop/logs/update/check (Ghost style)
    start = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{appinfo['name']}"
    POD="$APP-pod"
    PORT="{port}"
    APPDIR="{appdir}"
    IMG_WEB="{IMG_WEB}"
    IMG_SQ="{IMG_SQ}"
    IMG_REDIS="{IMG_REDIS}"
    source "$APPDIR/.env"
    podman pull "$IMG_WEB" >/dev/null || true
    podman pull "$IMG_SQ"  >/dev/null || true
    podman pull "$IMG_REDIS" >/dev/null || true
    podman rm -f "$APP-redis" "$APP-sidekiq" "$APP" >/dev/null 2>&1 || true
    podman pod rm -f "$POD" >/dev/null 2>&1 || true
    podman pod create --name "$POD" -p 127.0.0.1:${{PORT}}:3000
    podman run -d --name "$APP-redis" --pod "$POD" -e ALLOW_EMPTY_PASSWORD=yes "$IMG_REDIS"
    podman run -d --name "$APP" --pod "$POD" \\
      -v "$APPDIR/data/discourse:/bitnami/discourse" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG_WEB"
    podman run -d --name "$APP-sidekiq" --pod "$POD" \\
      -v "$APPDIR/data/discourse:/bitnami/discourse" \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG_SQ"
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

    readme = textwrap.dedent(f"""\
    # Opalstack Discourse README

    App: {appinfo['name']}
    Port: {port}
    Data: {appdir}/data/discourse
    Postgres: {pg['host']} (db {pg['db']} user {pg['user']})
    """)
    create_file(f'{appdir}/README.txt', readme, perms=0o600)

    # cron like Ghost
    m = random.randint(0,9)
    add_cronjob(f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check > /dev/null 2>&1')
    hh = random.randint(1,5); mm = random.randint(0,59)
    add_cronjob(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

if __name__ == '__main__':
    main()
