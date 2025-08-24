#!/usr/local/bin/python3.11
import argparse, sys, logging, os, http.client, json, textwrap, secrets, string, subprocess, shlex, random
API_HOST=(os.environ.get('API_URL') or 'https://my.opalstack.com').strip('https://').strip('http://')
API_BASE_URI='/api/v1'
CMD_ENV={'PATH':'/usr/local/bin:/usr/bin:/bin','UMASK':'0002'}
IMG='docker.io/getmeili/meilisearch:latest'

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

def main():
    p=argparse.ArgumentParser(description='Installs Meilisearch (Podman) on Opalstack')
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
    run_command(f'mkdir -p {appdir}/data')

    env=f'MEILI_MASTER_KEY="{os.urandom(24).hex()}"\nMEILI_ENV="production"\n'
    create_file(f'{appdir}/.env',env,perms=0o600)

    start=f"""#!/bin/bash
set -Eeuo pipefail
APP="{app['name']}"; PORT="{port}"; APPDIR="{appdir}"; IMG="{IMG}"
source "$APPDIR/.env"
podman pull "$IMG" >/dev/null || true
podman rm -f "$APP" >/dev/null 2>&1 || true
podman run -d --name "$APP" \\
  -p 127.0.0.1:${{PORT}}:7700 \\
  -v "$APPDIR/data:/meili_data" \\
  -e MEILI_ENV -e MEILI_MASTER_KEY \\
  --label io.containers.autoupdate=registry \\
  "$IMG"
echo "Started Meilisearch for {app['name']} on 127.0.0.1:{port}"
"""
    stop=f"#!/bin/bash\nset -Eeuo pipefail\npodman rm -f {app['name']} >/dev/null 2>&1 || true\necho Stopped {app['name']}\n"
    logs=f"#!/bin/bash\npodman logs -f {app['name']}\n"
    update=f"#!/bin/bash\nset -Eeuo pipefail\n\"{appdir}/stop\"; \"{appdir}/start\"\n"
    check=f"#!/bin/bash\nset -Eeuo pipefail\ncurl -fsS http://127.0.0.1:{port}/health >/dev/null || \"{appdir}/start\"\n"

    create_file(f'{appdir}/start',start,perms=0o700)
    create_file(f'{appdir}/stop',stop,perms=0o700)
    create_file(f'{appdir}/logs',logs,perms=0o700)
    create_file(f'{appdir}/update',update,perms=0o700)
    create_file(f'{appdir}/check',check,perms=0o700)
    create_file(f'{appdir}/README.txt',f"Meilisearch on port {port}. Data in {appdir}/data\n",perms=0o600)
    m=random.randint(0,9); add_cronjob(f'0{m},2{m},4{m} * * * * {appdir}/check > /dev/null 2>&1')
    hh=random.randint(1,5); mm=random.randint(0,59); add_cronjob(f'{mm} {hh} * * * {appdir}/update > /dev/null 2>&1')

if __name__=='__main__': main()
