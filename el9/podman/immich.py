#!/usr/bin/env python3
# Immich — rootless Podman, pod (server + machine-learning + redis), external Postgres
# Exposes app.port -> container :2283 (server)
import argparse, os, sys, json, http.client, logging, time, subprocess, shlex, textwrap, random

API_HOST=os.environ.get('API_URL','').strip('https://').strip('http://') or 'my.opalstack.com'
API_BASE='/api/v1'
IMG_SERVER='ghcr.io/immich-app/immich-server:release'
IMG_ML='ghcr.io/immich-app/immich-machine-learning:release'
IMG_REDIS='docker.io/bitnami/redis:7.2'

def sh(c,check=False,quiet=False):
    if not quiet: logging.info("$ %s",c)
    r=subprocess.run(shlex.split(c),capture_output=True,text=True)
    if check and r.returncode!=0: logging.error(r.stderr.strip()); sys.exit(r.returncode)
    return r
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
for d in ('upload','library','thumbs','tmp'): os.makedirs(f"{appdir}/{d}",exist_ok=True)

env=textwrap.dedent("""\
# External Postgres (required)
DB_HOST=""
DB_PORT="5432"
DB_USERNAME=""
DB_PASSWORD=""
DB_DATABASE="immich"
# Redis (internal in pod)
REDIS_HOST="immich-redis"
REDIS_PORT="6379"
# Optional
IMMICH_LOG_LEVEL="log"
TZ="America/Los_Angeles"
# For public URLs behind proxy
# IMMICH_SERVER_URL="https://photos.example.com"
""")
if not os.path.exists(f"{appdir}/.env"): w(f"{appdir}/.env",env,0o600)

start=textwrap.dedent(f"""\
#!/bin/bash
set -Eeuo pipefail
APP="{name}"; POD="{name}-pod"; PORT="{port}"; APPDIR="$HOME/apps/$APP"
IMG_SERVER="{IMG_SERVER}"; IMG_ML="{IMG_ML}"; IMG_REDIS="{IMG_REDIS}"
source "$APPDIR/.env"
podman pull "$IMG_SERVER" >/dev/null || true
podman pull "$IMG_ML" >/dev/null || true
podman pull "$IMG_REDIS" >/dev/null || true
# clean
podman rm -f "$APP-ml" "$APP-redis" "$APP" >/dev/null 2>&1 || true
podman pod rm -f "$POD" >/dev/null 2>&1 || true
# pod: map 2283 -> {port}
podman pod create --name "$POD" -p 127.0.0.1:${{PORT}}:2283
# redis
podman run -d --name "$APP-redis" --pod "$POD" -e ALLOW_EMPTY_PASSWORD=yes "$IMG_REDIS"
# server
podman run -d --name "$APP" --pod "$POD" \\
  --env-file "$APPDIR/.env" \\
  -v "$APPDIR/upload:/usr/src/app/upload:Z" \\
  -v "$APPDIR/library:/usr/src/app/library:Z" \\
  -v "$APPDIR/thumbs:/usr/src/app/thumbs:Z" \\
  "$IMG_SERVER"
# machine learning service (no external port)
podman run -d --name "$APP-ml" --pod "$POD" \\
  --env-file "$APPDIR/.env" \\
  -v "$APPDIR/tmp:/cache:Z" \\
  "$IMG_ML"
""")
stop=f"""#!/bin/bash
set -Eeuo pipefail
podman rm -f {name}-ml {name} {name}-redis >/dev/null 2>&1 || true
podman pod rm -f {name}-pod >/dev/null 2>&1 || true
echo stopped {name}
"""
logs=f"#!/bin/bash\npodman logs -f {name}\n"
update=textwrap.dedent(f"""#!/bin/bash
set -Eeuo pipefail
APPDIR="$HOME/apps/{name}"
"$APPDIR/stop"; "$APPDIR/start"
""")
check=textwrap.dedent(f"""#!/bin/bash
set -Eeuo pipefail
curl -fsS "http://127.0.0.1:{port}/" >/dev/null || "$HOME/apps/{name}/start"
""")
readme=f"""Immich on port {port}. External Postgres required; Redis runs in-pod.
Volumes: upload, library, thumbs. Set DB_* in {appdir}/.env. First boot runs migrations; allow time.
"""

w(f"{appdir}/start",start,0o700); w(f"{appdir}/stop",stop,0o700); w(f"{appdir}/logs",logs,0o700); w(f"{appdir}/update",update,0o700); w(f"{appdir}/check",check,0o700); w(f"{appdir}/README.txt",readme,0o600)
m=random.randint(0,9); sh(f'(crontab -l 2>/dev/null; echo "0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check >/dev/null 2>&1") | crontab -',quiet=True)
hh=random.randint(1,5); mm=random.randint(0,59); sh(f'(crontab -l 2>/dev/null; echo "{mm} {hh} * * * {appdir}/update >/dev/null 2>&1") | crontab -',quiet=True)
print("immich installed",name,port,osuser)
