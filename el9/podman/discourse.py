#!/usr/bin/env python3
# Discourse (bitnami) — rootless Podman, pod with redis + web + sidekiq
# Exposes app.port -> container :3000
# Requires: external Postgres + SMTP; we run Redis inside the pod.
import argparse, os, sys, json, http.client, logging, time, subprocess, shlex, secrets, string, textwrap, random, re

API_HOST=os.environ.get('API_URL','').strip('https://').strip('http://') or 'my.opalstack.com'
API_BASE='/api/v1'
IMG_WEB='docker.io/bitnami/discourse:latest'
IMG_SQ='docker.io/bitnami/discourse-sidekiq:latest'
IMG_REDIS='docker.io/bitnami/redis:7.2'

def sh(cmd,check=False,quiet=False):
    if not quiet: logging.info("$ %s",cmd)
    r=subprocess.run(shlex.split(cmd),capture_output=True,text=True)
    if check and r.returncode!=0: logging.error(r.stderr.strip()); sys.exit(r.returncode)
    return r
def w(p,c,mode=0o600): os.makedirs(os.path.dirname(p),exist_ok=True); open(p,'w').write(c); os.chmod(p,mode); logging.info("write %s (%s)",p,oct(mode))
def r(n=24): import string,secrets; a=string.ascii_letters+string.digits; return ''.join(secrets.choice(a) for _ in range(n))
def san(s): import re; s=s.lower(); s=re.sub(r'[^a-z0-9\-]+','-',s).strip('-'); return s or 'discourse'
class API:
    def __init__(self,h,b,token=None,user=None,pwd=None):
        self.h,self.b=h,b
        if not token:
            c=http.client.HTTPSConnection(h); c.request('POST',b+'/login/',json.dumps({'username':user,'password':pwd}),headers={'Content-type':'application/json'})
            d=json.loads(c.getresponse().read() or b'{}'); token=d.get('token')
            if not token: logging.error('Auth failed'); sys.exit(1)
        self.H={'Content-type':'application/json','Authorization':f'Token {token}'}
    def get(self,p):
        c=http.client.HTTPSConnection(self.h); c.request('GET',self.b+p,headers=self.H)
        return json.loads(c.getresponse().read() or b'{}')

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('-i',dest='uuid',default=os.environ.get('UUID'))
    ap.add_argument('-t',dest='tok',default=os.environ.get('OPAL_TOKEN')); ap.add_argument('-u',dest='usr',default=os.environ.get('OPAL_USER')); ap.add_argument('-p',dest='pwd',default=os.environ.get('OPAL_PASS'))
    args=ap.parse_args()
    logging.basicConfig(level=logging.INFO,format='[%(asctime)s] %(levelname)s: %(message)s')
    if not args.uuid: logging.error('Missing UUID'); sys.exit(1)
    if not sh('which podman',quiet=True).stdout.strip(): logging.error('podman not found'); sys.exit(1)
    api=API(API_HOST,API_BASE,args.tok,args.usr,args.pwd)
    app=api.get(f'/app/read/{args.uuid}')
    if not app.get('name'): logging.error('app not found'); sys.exit(1)
    name=san(app['name']); port=app['port']; osuser=app.get('osuser_name') or app.get('osuser','')
    if not (name and port and osuser): logging.error('missing fields'); sys.exit(1)
    home=f"/home/{osuser}"; appdir=f"{home}/apps/{name}"
    os.makedirs(f"{appdir}/data",exist_ok=True); os.makedirs(f"{appdir}/tmp",exist_ok=True)

    env=f"""\n# ---- REQUIRED ----
DISCOURSE_HOST="forum.example.com"
DISCOURSE_USERNAME="admin"
DISCOURSE_PASSWORD="{r(16)}"
DISCOURSE_EMAIL="admin@example.com"
POSTGRESQL_HOST=""
POSTGRESQL_PORT="5432"
POSTGRESQL_USERNAME=""
POSTGRESQL_PASSWORD=""
POSTGRESQL_DATABASE=""
# ---- SMTP ----
SMTP_HOST=""
SMTP_PORT="587"
SMTP_USER=""
SMTP_PASSWORD=""
SMTP_TLS="true"
# ---- OPTIONAL ----
DISCOURSE_ENABLE_HTTPS="no"
REDIS_HOST="{name}-redis"
REDIS_PASSWORD=""
"""
    if not os.path.exists(f"{appdir}/.env"): w(f"{appdir}/.env",env,0o600)

    start=textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APP="{name}"; APPDIR="$HOME/apps/$APP"
    POD="{name}-pod"; IMG_WEB="{IMG_WEB}"; IMG_SQ="{IMG_SQ}"; IMG_REDIS="{IMG_REDIS}"
    PORT="{port}"
    source "$APPDIR/.env"
    podman pull "$IMG_WEB" >/dev/null || true
    podman pull "$IMG_SQ"  >/dev/null || true
    podman pull "$IMG_REDIS" >/dev/null || true
    # clean
    podman rm -f "$APP-redis" "$APP-sidekiq" "$APP" >/dev/null 2>&1 || true
    podman pod rm -f "$POD" >/dev/null 2>&1 || true
    # pod with port 3000 -> host {port}
    podman pod create --name "$POD" -p 127.0.0.1:${{PORT}}:3000
    # redis (no password, persistent optional)
    podman run -d --name "$APP-redis" --pod "$POD" -e ALLOW_EMPTY_PASSWORD=yes "$IMG_REDIS"
    # web
    mkdir -p "$APPDIR/data/discourse"
    podman run -d --name "$APP" --pod "$POD" \\
      -v "$APPDIR/data/discourse:/bitnami/discourse:Z" \\
      --env-file "$APPDIR/.env" "$IMG_WEB"
    # sidekiq
    podman run -d --name "$APP-sidekiq" --pod "$POD" \\
      -v "$APPDIR/data/discourse:/bitnami/discourse:Z" \\
      --env-file "$APPDIR/.env" "$IMG_SQ"
    """)
    stop=f"""#!/bin/bash
set -Eeuo pipefail
podman rm -f {name}-sidekiq {name} {name}-redis >/dev/null 2>&1 || true
podman pod rm -f {name}-pod >/dev/null 2>&1 || true
echo stopped {name}
"""
    logs=f"#!/bin/bash\npodman logs -f {name}\n"
    update=textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    "$HOME/apps/{name}/stop"
    "$HOME/apps/{name}/start"
    """)
    check=textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    if ! curl -fsS "http://127.0.0.1:{port}/" >/dev/null; then
      echo "discourse unhealthy; restarting..."
      "$HOME/apps/{name}/start"
    fi
    """)
    readme=f"""Discourse — rootless podman (bitnami)
Port {port} -> container :3000 (Puma). Sidekiq + Redis run in the pod.
Set .env with Postgres + SMTP + DISCOURSE_HOST. First boot precompiles; give it time.
Data: {appdir}/data/discourse -> /bitnami/discourse
"""
    w(f"{appdir}/start",start,0o700); w(f"{appdir}/stop",stop,0o700); w(f"{appdir}/logs",logs,0o700)
    w(f"{appdir}/update",update,0o700); w(f"{appdir}/check",check,0o700); w(f"{appdir}/README.txt",readme,0o600)
    m=random.randint(0,9); sh(f'(crontab -l 2>/dev/null; echo "0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check >/dev/null 2>&1") | crontab -')
    hh=random.randint(1,5); mm=random.randint(0,59); sh(f'(crontab -l 2>/dev/null; echo "{mm} {hh} * * * {appdir}/update >/dev/null 2>&1") | crontab -')
    logging.info("installed discourse: app=%s port=%s user=%s",name,port,osuser)

if __name__=='__main__': main()
