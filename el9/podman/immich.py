#!/usr/bin/python3
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random
API_HOST=(os.environ.get('API_URL') or 'https://my.opalstack.com').strip('https://').strip('http://')
API_BASE_URI='/api/v1'
CMD_ENV={'PATH':'/usr/local/bin:/usr/bin:/bin','UMASK':'0002'}

IMG_SERVER='ghcr.io/immich-app/immich-server:release'
IMG_ML='ghcr.io/immich-app/immich-machine-learning:release'
IMG_REDIS='docker.io/bitnami/redis:7.2'

class OpalstackAPITool():
    def __init__(self,h,b,t,u,p):
        self.h=h; self.b=b
        if not t:
            conn=http.client.HTTPSConnection(self.h)
            conn.request('POST',self.b+'/login/',json.dumps({'username':u,'password':p}),headers={'Content-type':'application/json'})
            r=json.loads(conn.getresponse().read() or b'{}')
            if not r.get('token'): sys.exit(1)
            t=r['token']
        self.H={'Content-type':'application/json','Authorization':f'Token {t}'}
        self.token=t
    def get(self,path):
        conn=http.client.HTTPSConnection(self.h); conn.request('GET',self.b+path,headers=self.H)
        return json.loads(conn.getresponse().read() or b'{}')

def create_file(p,c,w='w',perms=0o600):
    with open(p,w) as f: f.write(c); os.chmod(p,perms)
def gen_password(n=20):
    import secrets,string; return ''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(n))
def run_command(cmd,cwd=None,env=CMD_ENV):
    try: return subprocess.check_output(shlex.split(cmd),cwd=cwd,env=env)
    except subprocess.CalledProcessError as e: sys.exit(e.returncode)
def add_cronjob(cr):
    h=os.path.expanduser('~'); tmp=f'{h}/.tmp{gen_password()}'
    with open(tmp,'w') as t: subprocess.run('crontab -l'.split(),stdout=t); t.write(f'{cr}\n')
    run_command(f'crontab {tmp}'); run_command(f'rm -f {tmp}')

def ensure_pg_same_server(opal_token, appinfo, prefix):
    try:
        import opalstack
    except ImportError:
        run_command("python3 -m pip install --user --upgrade opalstack")
        import opalstack
    from opalstack.util import one, filt_one
    api=opalstack.Api(token=opal_token)
    osuser_name=appinfo.get('osuser_name') or appinfo.get('osuser') or ''
    osusers=api.osusers.list_all(embed=['server'])
    osuser=filt_one(osusers, {'name': osuser_name})
    if not osuser or not osuser.get('server'): sys.exit(1)
    web_server=osuser['server']
    servers=api.servers.list_all()
    pg=servers.get('pgsql_servers') or servers.get('db_servers') or []
    if not pg: sys.exit(1)
    def pick():
        for s in pg:
            if s.get('id')==web_server.get('id'): return s
        for s in pg:
            if s.get('hostname') and s['hostname']==web_server.get('hostname'): return s
        for k in ('datacenter','dc','region','location'):
            dc=web_server.get(k)
            if dc:
                m=[x for x in pg if x.get(k)==dc]
                if m: return m[0]
        suf='.'.join((web_server.get('hostname') or '').split('.')[-2:])
        if suf:
            m=[x for x in pg if (x.get('hostname') or '').endswith(suf)]
            if m: return m[0]
        return pg[0]
    pg_server=pick()
    uname=f"{prefix}_{secrets.token_hex(3)}".lower()
    dname=f"{prefix}_{secrets.token_hex(2)}".lower()
    upass=secrets.token_urlsafe(24)
    u=one(api.pgsql_users.create([{'name':uname,'server':pg_server['id'],'password':upass}]))
    d=one(api.pgsql_databases.create([{'name':dname,'server':pg_server['id'],'users':[u['id']]}]))
    host=pg_server.get('hostname') or 'localhost'
    return {'host':host,'port':5432,'user':uname,'password':upass,'db':dname}

def main():
    p=argparse.ArgumentParser(description='Installs Immich (Podman) on Opalstack')
    p.add_argument('-i',dest='app_uuid',default=os.environ.get('UUID'))
    p.add_argument('-n',dest='app_name',default=os.environ.get('APPNAME'))
    p.add_argument('-t',dest='opal_token',default=os.environ.get('OPAL_TOKEN'))
    p.add_argument('-u',dest='opal_user',default=os.environ.get('OPAL_USER'))
    p.add_argument('-p',dest='opal_pass',default=os.environ.get('OPAL_PASS'))
    a=p.parse_args()
    logging.basicConfig(level=logging.INFO,format='[%(asctime)s] %(levelname)s: %(message)s')
    if not a.app_uuid: sys.exit(1)
    api=OpalstackAPITool(API_HOST,API_BASE_URI,a.opal_token,a.opal_user,a.opal_pass)
    app=api.get(f'/app/read/{a.app_uuid}')
    if not app.get('name'): sys.exit(1)

    appdir=f"/home/{app['osuser_name']}/apps/{app['name']}"; port=app['port']
    for d in ('upload','library','thumbs','tmp'): run_command(f'mkdir -p {appdir}/{d}')

    pg=ensure_pg_same_server(api.token, app, prefix='im')

    env=textwrap.dedent(f"""\
    DB_HOST="{pg['host']}"
    DB_PORT="{pg['port']}"
    DB_USERNAME="{pg['user']}"
    DB_PASSWORD="{pg['password']}"
    DB_DATABASE="{pg['db']}"
    REDIS_HOST="immich-redis"
    REDIS_PORT="6379"
    IMMICH_LOG_LEVEL="log"
    TZ="America/Los_Angeles"
    # IMMICH_SERVER_URL="https://photos.example.com"
    """); create_file(f'{appdir}/.env',env,perms=0o600)

    start=f"""#!/bin/bash
set -Eeuo pipefail
APP="{app['name']}"; POD="$APP-pod"; PORT="{port}"; APPDIR="{appdir}"
IMG_SERVER="{IMG_SERVER}"; IMG_ML="{IMG_ML}"; IMG_REDIS="{IMG_REDIS}"
source "$APPDIR/.env"
podman pull "$IMG_SERVER" >/dev/null || true
podman pull "$IMG_ML" >/dev/null || true
podman pull "$IMG_REDIS" >/dev/null || true
podman rm -f "$APP-ml" "$APP-redis" "$APP" >/dev/null 2>&1 || true
podman pod rm -f "$POD" >/dev/null 2>&1 || true
podman pod create --name "$POD" -p 127.0.0.1:${{PORT}}:2283
podman run -d --name "$APP-redis" --pod "$POD" -e ALLOW_EMPTY_PASSWORD=yes "$IMG_REDIS"
podman run -d --name "$APP" --pod "$POD" \\
  --env-file "$APPDIR/.env" \\
  -v "$APPDIR/upload:/usr/src/app/upload" \\
  -v "$APPDIR/library:/usr/src/app/library" \\
  -v "$APPDIR/thumbs:/usr/src/app/thumbs" \\
  --label io.containers.autoupdate=registry \\
  "$IMG_SERVER"
podman run -d --name "$APP-ml" --pod "$POD" \\
  --env-file "$APPDIR/.env" \\
  -v "$APPDIR/tmp:/cache" \\
  --label io.containers.autoupdate=registry \\
  "$IMG_ML"
echo "Started Immich for {app['name']} on 127.0.0.1:{port}"
"""
    stop=f"""#!/bin/bash
set -Eeuo pipefail
podman rm -f {app['name']}-ml {app['name']} {app['name']}-redis >/dev/null 2>&1 || true
podman pod rm -f {app['name']}-pod >/dev/null 2>&1 || true
echo "Stopped {app['name']}"
"""
    logs=f"#!/bin/bash\npodman logs -f {app['name']}\n"
    update=f"#!/bin/bash\nset -Eeuo pipefail\n\"{appdir}/stop\"; \"{appdir}/start\"\n"
    check=f"#!/bin/bash\nset -Eeuo pipefail\ncurl -fsS http://127.0.0.1:{port}/ >/dev/null || \"{appdir}/start\"\n"

    create_file(f'{appdir}/start',start,perms=0o700)
    create_file(f'{appdir}/stop',stop,perms=0o700)
    create_file(f'{appdir}/logs',logs,perms=0o700)
    create_file(f'{appdir}/update',update,perms=0o700)
    create_file(f'{appdir}/check',check,perms=0o700)
    create_file(f'{appdir}/README.txt',f"Immich on {port}. Managed PG on same server.\n",perms=0o600)
    m=random.randint(0,9); add_cronjob(f'0{m},2{m},4{m} * * * * {appdir}/check > /dev/null 2>&1')
    hh=random.randint(1,5); mm=random.randint(0,59); add_cronjob(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

if __name__=='__main__': main()
