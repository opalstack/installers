#!/usr/bin/env python3
# Vaultwarden — rootless Podman, single container
import argparse, os, sys, json, http.client, logging, subprocess, shlex, textwrap, random

API_HOST=os.environ.get('API_URL','').strip('https://').strip('http://') or 'my.opalstack.com'
API_BASE='/api/v1'; IMG='docker.io/vaultwarden/server:latest'

def sh(c,check=False,quiet=False):
    if not quiet: logging.info("$ %s",c)
    r=subprocess.run(shlex.split(c),capture_output=True,text=True)
    if check and r.returncode!=0: logging.error(r.stderr.strip()); sys.exit(r.returncode)
    return r
def w(p,c,m=0o600): os.makedirs(os.path.dirname(p),exist_ok=True); open(p,'w').write(c); os.chmod(p,m)
class API:
    def __init__(s,h,b,t=None,u=None,p=None):
        s.h,s.b=h,b
        if not t:
            import json,http.client
            c=http.client.HTTPSConnection(h); c.request('POST',b+'/login/',json.dumps({'username':u,'password':p}),headers={'Content-type':'application/json'})
            t=json.loads(c.getresponse().read()).get('token'); 
            if not t: sys.exit(1)
        s.H={'Content-type':'application/json','Authorization':f'Token {t}'}
    def get(s,p):
        c=http.client.HTTPSConnection(s.h); c.request('GET',s.b+p,headers=s.H)
        import json; return json.loads(c.getresponse().read() or b'{}')

ap=argparse.ArgumentParser(); ap.add_argument('-i',dest='uuid',default=os.environ.get('UUID'))
ap.add_argument('-t',dest='tok',default=os.environ.get('OPAL_TOKEN')); ap.add_argument('-u',dest='usr',default=os.environ.get('OPAL_USER')); ap.add_argument('-p',dest='pwd',default=os.environ.get('OPAL_PASS'))
args=ap.parse_args()
logging.basicConfig(level=logging.INFO,format='[%(asctime)s] %(levelname)s: %(message)s')
if not args.uuid: sys.exit('Missing UUID')
if not sh('which podman',quiet=True).stdout.strip(): sys.exit('podman not found')
api=API(API_HOST,API_BASE,args.tok,args.usr,args.pwd)
app=api.get(f'/app/read/{args.uuid}'); name=app['name']; port=app['port']; osuser=app.get('osuser_name') or app.get('osuser','')
home=f"/home/{osuser}"; appdir=f"{home}/apps/{name}"
os.makedirs(f"{appdir}/data",exist_ok=True)

env=textwrap.dedent(f"""\
# Optional public URL (improves links)
DOMAIN=""
# Admin token (required to access /admin)
ADMIN_TOKEN="{os.urandom(16).hex()}"
# Disable open signup by default
SIGNUPS_ALLOWED=false
# SMTP (optional)
SMTP_HOST=""
SMTP_PORT=587
SMTP_FROM="vaultwarden@yourdomain"
SMTP_USERNAME=""
SMTP_PASSWORD=""
SMTP_SECURITY=starttls
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
  -p 127.0.0.1:${{PORT}}:80 \\
  -v "$APPDIR/data:/data:Z" \\
  --env-file "$APPDIR/.env" \\
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
curl -fsS "http://127.0.0.1:{port}/" >/dev/null || "$HOME/apps/{name}/start"
""")
readme=f"Vaultwarden on port {port}. Data in {appdir}/data. Configure .env (DOMAIN, SMTP, ADMIN_TOKEN, SIGNUPS_ALLOWED)."

w(f"{appdir}/start",start,0o700); w(f"{appdir}/stop",stop,0o700); w(f"{appdir}/logs",logs,0o700); w(f"{appdir}/update",update,0o700); w(f"{appdir}/check",check,0o700); w(f"{appdir}/README.txt",readme,0o600)
m=random.randint(0,9); sh(f'(crontab -l 2>/dev/null; echo "0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/check >/dev/null 2>&1") | crontab -')
hh=random.randint(1,5); mm=random.randint(0,59); sh(f'(crontab -l 2>/dev/null; echo "{mm} {hh} * * * {appdir}/update >/dev/null 2>&1") | crontab -')
print("vaultwarden installed",name,port,osuser)
