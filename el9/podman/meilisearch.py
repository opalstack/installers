#!/usr/bin/env python3
# Meilisearch — rootless Podman, single container
import argparse, os, sys, json, http.client, logging, subprocess, shlex, textwrap, random
API_HOST=os.environ.get('API_URL','').strip('https://').strip('http://') or 'my.opalstack.com'
API_BASE='/api/v1'; IMG='docker.io/getmeili/meilisearch:latest'
def sh(c,q=False): 
    if not q: logging.info("$ %s",c)
    r=subprocess.run(shlex.split(c),capture_output=True,text=True); return r
def w(p,c,m=0o600): os.makedirs(os.path.dirname(p),exist_ok=True); open(p,'w').write(c); os.chmod(p,m)
class API:
    def __init__(s,h,b,t=None,u=None,p=None):
        if not t:
            c=http.client.HTTPSConnection(h); c.request('POST',b+'/login/',json.dumps({'username':u,'password':p}),headers={'Content-type':'application/json'})
            t=json.loads(c.getresponse().read()).get('token'); 
            if not t: sys.exit(1)
        s.h=h; s.b=b; s.H={'Content-type':'application/json','Authorization':f'Token {t}'}
    def get(s,p):
        c=http.client.HTTPSConnection(s.h); c.request('GET',s.b+p,headers=s.H); 
        return json.loads(c.getresponse().read() or b'{}')

ap=argparse.ArgumentParser(); ap.add_argument('-i',dest='uuid',default=os.environ.get('UUID'))
ap.add_argument('-t',dest='tok',default=os.environ.get('OPAL_TOKEN')); ap.add_argument('-u',dest='usr',default=os.environ.get('OPAL_USER')); ap.add_argument('-p',dest='pwd',default=os.environ.get('OPAL_PASS'))
args=ap.parse_args(); logging.basicConfig(level=logging.INFO,format='[%(asctime)s] %(levelname)s: %(message)s')
api=API(API_HOST,API_BASE,args.tok,args.usr,args.pwd); app=api.get(f'/app/read/{args.uuid}')
name=app['name']; port=app['port']; osuser=app.get('osuser_name') or app.get('osuser','')
home=f"/home/{osuser}"; appdir=f"{home}/apps/{name}"
os.makedirs(f"{appdir}/data",exist_ok=True)

env=textwrap.dedent(f"""\
MEILI_MASTER_KEY="{os.urandom(24).hex()}"
MEILI_ENV="production"
""")
if not os.path.exists(f"{appdir}/.env"): w(f"{appdir}/.env",env,0o600)

start=textwrap.dedent(f"""\
#!/bin/bash
set -Eeuo pipefail
APP="{name}"; PORT="{port}"; APPDIR="$HOME/apps/$APP"; IMG="{IMG}"
source "$APPDIR/.env"
podman pull "$IMG" >/dev/null || true
podman rm -f "$APP" >/dev/null 2>&1 || true
podman run -d --name "$APP" \\
  -p 127.0.0.1:${{PORT}}:7700 \\
  -v "$APPDIR/data:/meili_data:Z" \\
  -e MEILI_ENV \\
  -e MEILI_MASTER_KEY \\
  --label io.containers.autoupdate=registry \\
  "$IMG"
""")
stop=f"#!/bin/bash\npodman rm -f {name} >/dev/null 2>&1 || true\n"
logs=f"#!/bin/bash\npodman logs -f {name}\n"
update=textwrap.dedent(f"""#!/bin/bash
set -Eeuo pipefail
APPDIR="$HOME/apps/{name}"; podman pull {IMG}; "$APPDIR/stop"; "$APPDIR/start"
""")
check=textwrap.dedent(f"""#!/bin/bash
set -Eeuo pipefail
curl -fsS "http://127.0.0.1:{port}/health" >/dev/null || "$HOME/apps/{name}/start"
""")
readme=f"Meilisearch on port {port}. Master key in {appdir}/.env. Data in {appdir}/data."

w(f"{appdir}/start",start,0o700); w(f"{appdir}/stop",stop,0o700); w(f"{appdir}/logs",logs,0o700); w(f"{appdir}/update",update,0o700); w(f"{appdir}/check",check,0o700); w(f"{appdir}/README.txt",readme,0o600)
m=random.randint(0,9); sh(f'(crontab -l 2>/dev/null; echo "0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check >/dev/null 2>&1") | crontab -',q=True)
hh=random.randint(1,5); mm=random.randint(0,59); sh(f'(crontab -l 2>/dev/null; echo "{mm} {hh} * * * {appdir}/update >/dev/null 2>&1") | crontab -',q=True)
print("meilisearch installed",name,port,osuser)
