#!/usr/local/bin/python3.11
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random

API_HOST = (os.environ.get('API_URL') or 'https://my.opalstack.com').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'
CMD_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'UMASK': '0002'}

IMG = 'docker.io/umamisoftware/umami:postgres-latest'

class OpalstackAPITool():
    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host; self.base_uri = base_uri
        if not authtoken:
            endpoint = self.base_uri + '/login/'
            payload = json.dumps({'username': user, 'password': password})
            conn = http.client.HTTPSConnection(self.host); conn.request('POST', endpoint, payload, headers={'Content-type':'application/json'})
            result = json.loads(conn.getresponse().read() or b'{}')
            if not result.get('token'):
                logging.warning('Invalid username/password and no token, exiting.')
                sys.exit(1)
            authtoken = result['token']
        self.headers = {'Content-type':'application/json', 'Authorization': f'Token {authtoken}'}
        self.token = authtoken
    def get(self, endpoint):
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host); conn.request('GET', endpoint, headers=self.headers)
        return json.loads(conn.getresponse().read() or b'{}')

def create_file(path, contents, writemode='w', perms=0o600):
    with open(path, writemode) as f: f.write(contents)
    os.chmod(path, perms); logging.info(f'Created file {path} {oct(perms)}')

def gen_password(length=20):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

def run_command(cmd, cwd=None, env=CMD_ENV):
    logging.info(f'Running: {cmd}')
    try:
        return subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env)
    except subprocess.CalledProcessError as e:
        logging.debug(getattr(e, 'output', b'')); sys.exit(e.returncode)

def add_cronjob(cronjob):
    homedir = os.path.expanduser('~'); tmpname = f'{homedir}/.tmp{gen_password()}'
    with open(tmpname, 'w') as tmp:
        subprocess.run('crontab -l'.split(), stdout=tmp)
        tmp.write(f'{cronjob}\n')
    run_command(f'crontab {tmpname}'); run_command(f'rm -f {tmpname}')
    logging.info(f'Added cron job: {cronjob}')

def ensure_pg_same_server(opal_token, appinfo, prefix):
    try:
        import opalstack  # noqa
    except ImportError:
        run_command("python3 -m pip install --user --upgrade opalstack")
    import opalstack
    from opalstack.util import one, filt_one
    api = opalstack.Api(token=opal_token)
    osuser_name = appinfo.get('osuser_name') or appinfo.get('osuser') or ''
    osusers = api.osusers.list_all(embed=['server'])
    osuser = filt_one(osusers, {'name': osuser_name})
    if not osuser or not osuser.get('server'):
        logging.error('Cannot resolve OSUser server'); sys.exit(1)
    web_server = osuser['server']
    servers = api.servers.list_all()
    pg_servers = (servers.get('pgsql_servers') or servers.get('db_servers') or [])
    if not pg_servers: logging.error('No PostgreSQL servers available'); sys.exit(1)
    def pick():
        for s in pg_servers:
            if s.get('id') == web_server.get('id'): return s
        for s in pg_servers:
            if s.get('hostname') and s['hostname'] == web_server.get('hostname'): return s
        for k in ('datacenter','dc','region','location'):
            dc = web_server.get(k)
            if dc:
                m = [x for x in pg_servers if x.get(k) == dc]
                if m: return m[0]
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
    return f"postgres://{uname}:{upass}@{host}:5432/{dname}?sslmode=require"

def main():
    p = argparse.ArgumentParser(description='Installs Umami (Podman) on Opalstack')
    p.add_argument('-i', dest='app_uuid',   default=os.environ.get('UUID'))
    p.add_argument('-n', dest='app_name',   default=os.environ.get('APPNAME'))
    p.add_argument('-t', dest='opal_token', default=os.environ.get('OPAL_TOKEN'))
    p.add_argument('-u', dest='opal_user',  default=os.environ.get('OPAL_USER'))
    p.add_argument('-p', dest='opal_pass',  default=os.environ.get('OPAL_PASS'))
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
    if not a.app_uuid: logging.error('Missing UUID'); sys.exit(1)
    api = OpalstackAPITool(API_HOST, API_BASE_URI, a.opal_token, a.opal_user, a.opal_pass)
    app = api.get(f'/app/read/{a.app_uuid}')
    if not app.get('name'): logging.error('App not found'); sys.exit(1)

    appdir = f'/home/{app["osuser_name"]}/apps/{app["name"]}'
    port   = app['port']
    run_command(f'mkdir -p {appdir}/data')

    dburl = ensure_pg_same_server(api.token, app, prefix='um')

    env = textwrap.dedent(f"""\
    DATABASE_URL="{dburl}"
    APP_SECRET="{os.urandom(24).hex()}"
    UMAMI_DISABLE_TELEMETRY=1
    HOSTNAME=""
    """)
    create_file(f'{appdir}/.env', env, perms=0o600)

    start = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{app['name']}"; PORT="{port}"; APPDIR="{appdir}"; IMG="{IMG}"
    source "$APPDIR/.env"
    podman pull "$IMG" >/dev/null || true
    podman rm -f "$APP" >/dev/null 2>&1 || true
    podman run -d --name "$APP" \\
      -p 127.0.0.1:${{PORT}}:3000 \\
      --env-file "$APPDIR/.env" \\
      --label io.containers.autoupdate=registry \\
      "$IMG"
    echo "Started Umami for {app['name']} on 127.0.0.1:{port}"
    """)
    stop = f"#!/bin/bash\nset -Eeuo pipefail\npodman rm -f {app['name']} >/dev/null 2>&1 || true\necho Stopped {app['name']}\n"
    logs = f"#!/bin/bash\npodman logs -f {app['name']}\n"
    update = f"#!/bin/bash\nset -Eeuo pipefail\n\"{appdir}/stop\"; \"{appdir}/start\"\n"
    check  = f"#!/bin/bash\nset -Eeuo pipefail\ncurl -fsS http://127.0.0.1:{port}/ >/dev/null || \"{appdir}/start\"\n"

    create_file(f'{appdir}/start',  start,  perms=0o700)
    create_file(f'{appdir}/stop',   stop,   perms=0o700)
    create_file(f'{appdir}/logs',   logs,   perms=0o700)
    create_file(f'{appdir}/update', update, perms=0o700)
    create_file(f'{appdir}/check',  check,  perms=0o700)
    create_file(f'{appdir}/README.txt', f"Umami on port {port}. Managed PG on same server.\n", perms=0o600)

    m = random.randint(0,9); add_cronjob(f'0{m},2{m},4{m} * * * * {appdir}/check > /dev/null 2>&1')
    hh = random.randint(1,5); mm = random.randint(0,59); add_cronjob(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

if __name__ == '__main__':
    main()
