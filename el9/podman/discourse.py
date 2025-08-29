#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Discourse installer for the Opalstack platform (no Docker).
- Mirrors the style/flow of the Mastodon installer.
- Creates DB via Opalstack API, fetches tagged Discourse, installs deps,
  writes configs and helper scripts, notifies API.

Notes:
- Modern Discourse uses pnpm if no yarn.lock is present.
- We install Ruby gems first, then JS deps (pnpm or yarn).
- We spin up a temporary Redis (TCP + UNIX socket) just for migrations/assets.
"""

import argparse
import http.client
import json
import logging
import os
import re
import secrets
import shlex
import string
import subprocess
import sys
import tarfile
import textwrap
import time
import urllib.request
import hashlib
import shutil
from typing import Optional
import socket

# ---------- Opalstack API ----------
API_URL = (os.environ.get("OPAL_API_URL") or os.environ.get("API_URL") or "https://my.opalstack.com").rstrip("/")
API_HOST = API_URL.replace("https://", "").replace("http://", "")
API_BASE_URI = "/api/v1"

# EL9 SCL toolchains for Ruby/Node
CMD_PREFIX = "/bin/scl enable nodejs22 ruby33 -- "

# Discourse tag handling
DISCOURSE_TAG = os.environ.get("DISCOURSE_TAG", "")  # e.g. v3.5.0 ; auto-resolve if empty
GITHUB_TAGS_API = "https://api.github.com/repos/discourse/discourse/tags?per_page=100"
SEMVER_RE = re.compile(r"^v\d+\.\d+\.\d+$")

# Behavior toggles
OPAL_SKIP_DB = os.environ.get("OPAL_SKIP_DB", "0") == "1"  # provide DB_* yourself to skip API

# ---------- API helper ----------
class OpalAPI:
    def __init__(self, host, base_uri, token, user, password):
        self.host, self.base = host, base_uri
        if not token:
            conn = http.client.HTTPSConnection(self.host)
            payload = json.dumps({"username": user, "password": password})
            conn.request("POST", self.base + "/login/", payload, headers={"Content-type":"application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read() or b"{}")
            token = data.get("token")
            if not token:
                logging.error(f"Auth failed (HTTP {resp.status})."); sys.exit(1)
        self.h = {"Content-type":"application/json","Authorization":f"Token {token}"}

    def get(self, path: str):
        conn = http.client.HTTPSConnection(self.host)
        conn.request("GET", self.base + path, headers=self.h)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        if resp.status >= 400:
            logging.error(f"GET {path} -> HTTP {resp.status}"); sys.exit(1)
        return data

    def post(self, path: str, payload: str):
        conn = http.client.HTTPSConnection(self.host)
        conn.request("POST", self.base + path, payload, headers=self.h)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        if resp.status >= 400:
            logging.error(f"POST {path} -> HTTP {resp.status}: {data}")
        return data

# ---------- helpers ----------
def pw(n: int = 24) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(n))

def run(cmd: str, env: dict, cwd: Optional[str] = None, use_shlex: bool = True) -> str:
    full = CMD_PREFIX + cmd
    logging.info(f"$ {full}")
    try:
        if use_shlex:
            out = subprocess.check_output(shlex.split(full), cwd=cwd, env=env, stderr=subprocess.STDOUT)
        else:
            out = subprocess.check_output(full, cwd=cwd, env=env, stderr=subprocess.STDOUT, shell=True)
        return out.decode("utf-8", "ignore")
    except subprocess.CalledProcessError as e:
        logging.error(e.output.decode("utf-8", "ignore"))
        raise

def write(path: str, content: str, perms: int = 0o600) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f: f.write(content)
    os.chmod(path, perms)
    logging.info(f"wrote {path} perms={oct(perms)}")

def add_cron(line: str) -> None:
    home = os.path.expanduser("~")
    tmp = f"{home}/.tmp{pw(8)}"
    try:
        existing = subprocess.check_output("crontab -l".split(), stderr=subprocess.STDOUT).decode()
    except subprocess.CalledProcessError:
        existing = ""
    if line in existing:
        logging.info("cron already present"); return
    with open(tmp, "w", encoding="utf-8") as t:
        t.write(existing)
        if existing and not existing.endswith("\n"):
            t.write("\n")
        t.write(line + "\n")
    subprocess.check_call(shlex.split(f"crontab {tmp}"))
    os.remove(tmp)
    logging.info(f"added cron: {line}")

def latest_tag() -> str:
    with urllib.request.urlopen(GITHUB_TAGS_API, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    for t in data:
        name = t.get("name","")
        if SEMVER_RE.match(name):
            return name
    raise RuntimeError("No stable SemVer tag found")

def fetch_tag(tag: str, dest_dir: str) -> None:
    os.makedirs(dest_dir, exist_ok=True)
    url = f"https://github.com/discourse/discourse/archive/refs/tags/{tag}.tar.gz"
    tarpath = os.path.join(dest_dir, f"discourse-{tag}.tar.gz")
    with urllib.request.urlopen(url, timeout=60) as r, open(tarpath, "wb") as f:
        shutil.copyfileobj(r, f)
    sha256 = hashlib.sha256()
    with open(tarpath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""): sha256.update(chunk)
    logging.info(f"fetched {tag} sha256={sha256.hexdigest()}")
    with tarfile.open(tarpath, "r:gz") as tf:
        members = tf.getmembers()
        top = members[0].name.split("/")[0]
        tf.extractall(dest_dir)
    src = os.path.join(dest_dir, top)
    dst = os.path.join(dest_dir, "discourse")
    if os.path.exists(dst): shutil.rmtree(dst)
    os.rename(src, dst)
    os.remove(tarpath)

def tcp_port_open(host: str, port: int, timeout_s: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False

# ---------- main ----------
def main() -> None:
    ap = argparse.ArgumentParser(description="Install Discourse (no Docker) on Opalstack")
    ap.add_argument("-i", dest="uuid",     default=os.environ.get("UUID"))
    ap.add_argument("-n", dest="name",     default=os.environ.get("APPNAME"))  # accepted & ignored
    ap.add_argument("-t", dest="token",    default=os.environ.get("OPAL_TOKEN"))
    ap.add_argument("-u", dest="user",     default=os.environ.get("OPAL_USER"))
    ap.add_argument("-p", dest="password", default=os.environ.get("OPAL_PASS"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
    if not args.uuid:
        logging.error("Missing app UUID (-i)"); sys.exit(1)

    api = OpalAPI(API_HOST, API_BASE_URI, args.token, args.user, args.password)
    app = api.get(f"/app/read/{args.uuid}")
    if not app.get("name"):
        logging.error("App not found."); sys.exit(1)

    appname = app["name"]; osuser = app["osuser_name"]; port = int(app["port"])
    appdir = f"/home/{osuser}/apps/{appname}"
    logdir = f"/home/{osuser}/logs/apps/{appname}"
    srcdir = f"{appdir}/discourse"

    os.makedirs(appdir, exist_ok=True)
    os.makedirs(logdir, exist_ok=True)
    os.makedirs(f"{appdir}/tmp/pids", exist_ok=True)
    os.makedirs(f"{appdir}/tmp/sockets", exist_ok=True)

    # ---- DB via Opalstack API ----
    if OPAL_SKIP_DB:
        db_name = os.environ.get("DB_NAME") or f"disc_{args.uuid[:8]}".lower()
        db_user = os.environ.get("DB_USER") or db_name
        db_pass = os.environ.get("DB_PASS") or pw()
        logging.info("OPAL_SKIP_DB=1 -> using provided DB creds")
    else:
        db_name = f"{appname[:8]}_{args.uuid[:8]}".lower()
        db_user = db_name
        db_pass = pw()
        api.post("/psqluser/create/", json.dumps([{
            "server": app["server"], "name": db_user, "password": db_pass, "external": "false"
        }]))
        time.sleep(3)
        uid = None
        for u in api.get("/psqluser/list/"):
            if u.get("name") == db_user:
                uid = u.get("id"); break
        if not uid:
            logging.error("Failed to create/find DB user"); sys.exit(1)
        api.post("/psqldb/create/", json.dumps([{
            "server": app["server"], "name": db_name, "dbusers_readwrite": [uid]
        }]))
        time.sleep(3)
        logging.info(f"DB ready: user={db_user} db={db_name}")

    # ---- fetch Discourse source (tag tarball) ----
    tag = DISCOURSE_TAG or latest_tag()
    logging.info(f"Using Discourse tag: {tag}")
    fetch_tag(tag, appdir)

    # ---- build env (Ruby+Node via SCL), pnpm/yarn local to app ----
    CMD_ENV = {
        "RAILS_ENV": "production",
        "PATH": f"{appdir}/node/bin:{srcdir}/bin:/usr/local/bin:/usr/bin:/bin",
        "GEM_HOME": f"{srcdir}/.gems",
        "BUNDLE_PATH": f"{srcdir}/vendor/bundle",
        "BUNDLE_DEPLOYMENT": "1",
        "UMASK": "0002",
        "HOME": f"/home/{osuser}",
        "TMPDIR": f"{appdir}/tmp",
    }

    run(f"mkdir -p {appdir}/node/bin", CMD_ENV)
    # Install corepack shims into app-local bin dir (no global symlink)
    run(f"corepack enable --install-directory={appdir}/node/bin", CMD_ENV, cwd=f"{appdir}/node")

    # package manager choice
    use_yarn = os.path.exists(os.path.join(srcdir, "yarn.lock"))
    logging.info(f"Using {'yarn' if use_yarn else 'pnpm'} for JS dependencies")

    # Gems then JS deps
    run("bundle config set without 'development test'", CMD_ENV, cwd=srcdir)
    run("bundle install --jobs 4", CMD_ENV, cwd=srcdir)
    if use_yarn:
        run("yarn set version classic", CMD_ENV, cwd=srcdir)
        run("yarn install --frozen-lockfile --network-timeout 600000", CMD_ENV, cwd=srcdir)
    else:
        run("CI=1 pnpm install --frozen-lockfile", CMD_ENV, cwd=srcdir)
        run("pnpm prune", CMD_ENV, cwd=srcdir)

    # ---- config files ----
    secret_hex = secrets.token_hex(64)
    hostname = "forum.example.com"
    redis_sock = f"{srcdir}/tmp/sockets/redis.sock"

    discourse_conf = textwrap.dedent(f"""\
    # Discourse production config
    hostname = "{hostname}"

    # Postgres
    db_host = localhost
    db_port = 5432
    db_name = {db_name}
    db_username = {db_user}
    db_password = {db_pass}

    # Redis: we run it locally via a UNIX socket only.
    # AlmaLinux 9 ships redis-compatible binaries (redis-server/redis-cli) without SCL【237666484660381†L48-L59】.
    # Use a UNIX socket for all connections rather than TCP to avoid port conflicts and firewall issues.
    # Discourse will read this setting and connect over the socket.
    redis_url = unix://{redis_sock}

    # Required secret
    secret_key_base = {secret_hex}

    # Opalstack fronts this port with nginx
    serve_static_assets = true
    """)
    write(f"{srcdir}/config/discourse.conf", discourse_conf, perms=0o600)

    # redis.conf: use UNIX socket only. Disable TCP by setting port to 0.
    # This follows the Opalstack recommendation to run redis via a socket【908843088052382†L72-L78】.
    redis_conf = textwrap.dedent(f"""\
    port 0
    unixsocket {redis_sock}
    unixsocketperm 700
    daemonize yes
    pidfile {appdir}/tmp/pids/redis.pid
    logfile {logdir}/redis.log
    save ""
    appendonly no
    """)
    write(f"{appdir}/redis.conf", redis_conf, perms=0o600)

    # ---- run migrations & assets with a temporary redis ----
    # start redis (or valkey) using the socket-only configuration
    try:
        # Prefer valkey-server on EL9; fall back to redis-server if valkey isn't available.
        srv = shutil.which("valkey-server") or shutil.which("redis-server") or "valkey-server"
        subprocess.check_call([srv, f"{appdir}/redis.conf"])
        # wait until the UNIX socket exists (up to ~10 seconds)
        for _ in range(40):
            if os.path.exists(redis_sock):
                break
            time.sleep(0.25)
        else:
            # If the socket never appeared, log the last lines of the redis log for debugging.
            try:
                log_tail = subprocess.check_output(["tail","-n","100", f"{logdir}/redis.log"]).decode("utf-8","ignore")
            except Exception:
                log_tail = "(no redis log)"
            logging.error("Redis failed to start. Last 100 lines of redis.log:\n" + log_tail)
            raise RuntimeError("Redis did not start")
    except Exception as e:
        logging.error(f"Failed to start Redis/Valkey: {e}")
        raise

    # set REDIS_URL explicitly for safety during this phase (use UNIX socket)
    MIG_ENV = dict(CMD_ENV)
    MIG_ENV["REDIS_URL"] = f"unix://{redis_sock}"

    try:
        run("bundle exec rake db:migrate", MIG_ENV, cwd=srcdir)
        run("bundle exec rake assets:precompile", MIG_ENV, cwd=srcdir)
    finally:
        # stop redis/valkey using CLI over the socket, falling back to PID kill
        try:
            subprocess.check_call(["valkey-cli", "-s", redis_sock, "shutdown"])
        except Exception:
            try:
                subprocess.check_call(["redis-cli", "-s", redis_sock, "shutdown"])
            except Exception:
                # fallback: kill pidfile
                pidfile = f"{appdir}/tmp/pids/redis.pid"
                try:
                    if os.path.exists(pidfile):
                        with open(pidfile) as f:
                            pid = int(f.read().strip() or "0")
                        if pid > 1:
                            os.kill(pid, 15)
                except Exception:
                    pass

    # ---- scripts ----
    setenv = textwrap.dedent(f"""\
    #!/bin/bash
    APPDIR="{appdir}"
    SRCDIR="{srcdir}"
    export RAILS_ENV=production
    export GEM_HOME="$SRCDIR/.gems"
    export BUNDLE_PATH="$SRCDIR/vendor/bundle"
    export PATH="$APPDIR/node/bin:$SRCDIR/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
    export TMPDIR="$APPDIR/tmp"
    # Force all Ruby/JS processes to use the Redis UNIX socket
    # This ensures we never hit the TCP port and avoids issues with AlmaLinux 9
    export REDIS_URL="unix://$SRCDIR/tmp/sockets/redis.sock"
    # Enable node & ruby via SCL
    source scl_source enable nodejs22 ruby33
    """)

    start = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    APPDIR="{appdir}"
    SRCDIR="{srcdir}"
    LOGDIR="{logdir}"
    PORT="{port}"
    export RAILS_ENV=production

    mkdir -p "$SRCDIR/tmp/pids" "$SRCDIR/tmp/sockets" "$LOGDIR"
    source "$APPDIR/setenv"

    # Redis (system)
    # Prefer valkey-server on EL9; fall back to redis-server. Always use our redis.conf with socket-only settings.
    if [ -f "$APPDIR/tmp/pids/redis.pid" ] && kill -0 $(cat "$APPDIR/tmp/pids/redis.pid") 2>/dev/null; then
      :
    else
      if command -v valkey-server >/dev/null 2>&1; then
        valkey-server "$APPDIR/redis.conf"
      else
        redis-server "$APPDIR/redis.conf"
      fi
      sleep 1
    fi

    # Puma
    if [ -f "$SRCDIR/tmp/pids/puma.pid" ] && kill -0 $(cat "$SRCDIR/tmp/pids/puma.pid") 2>/dev/null; then
      :
    else
      cd "$SRCDIR"
      bundle exec puma -e production -b tcp://127.0.0.1:$PORT -d --pidfile "$SRCDIR/tmp/pids/puma.pid" --redirect-stdout "$LOGDIR/puma.stdout.log" --redirect-stderr "$LOGDIR/puma.stderr.log"
      sleep 1
    fi

    # Sidekiq
    if [ -f "$SRCDIR/tmp/pids/sidekiq.pid" ] && kill -0 $(cat "$SRCDIR/tmp/pids/sidekiq.pid") 2>/dev/null; then
      :
    else
      cd "$SRCDIR"
      bundle exec sidekiq -e production -d -L "$LOGDIR/sidekiq.log" -P "$SRCDIR/tmp/pids/sidekiq.pid"
    fi

    echo "OK"
    """)

    stop = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    SRCDIR="{srcdir}"

    # Puma
    if [ -f "$SRCDIR/tmp/pids/puma.pid" ]; then
      kill $(cat "$SRCDIR/tmp/pids/puma.pid") || true
      rm -f "$SRCDIR/tmp/pids/puma.pid"
    fi
    # Sidekiq
    if [ -f "$SRCDIR/tmp/pids/sidekiq.pid" ]; then
      kill $(cat "$SRCDIR/tmp/pids/sidekiq.pid") || true
      rm -f "$SRCDIR/tmp/pids/sidekiq.pid"
    fi
    # Redis
    if [ -f "{appdir}/tmp/pids/redis.pid" ]; then
      # Attempt graceful shutdown via socket. Try valkey-cli first then redis-cli; fall back to killing the PID.
      (valkey-cli -s "{srcdir}/tmp/sockets/redis.sock" shutdown \
        || redis-cli -s "{srcdir}/tmp/sockets/redis.sock" shutdown \
        || kill $(cat "{appdir}/tmp/pids/redis.pid")) || true
      rm -f "{appdir}/tmp/pids/redis.pid"
    fi
    echo "stopped"
    """)

    restart = textwrap.dedent(f"""\
    #!/bin/bash
    {appdir}/stop || true
    sleep 2
    {appdir}/start
    """)

    readme = textwrap.dedent(f"""\
    # Discourse on Opalstack (no Docker)

    - Front nginx is Opalstack's; this app runs **Puma** on 127.0.0.1:{port}.
    - **Sidekiq** handles jobs.
    - **Redis** runs locally via a UNIX socket at `{srcdir}/tmp/sockets/redis.sock` and does not expose a TCP port.

    ## Start/Stop
    {appdir}/start
    {appdir}/stop
    {appdir}/restart

    ## First admin
        cd {srcdir}
        {appdir}/setenv
        bundle exec rake admin:create

    ## Config
    - Edit `{srcdir}/config/discourse.conf` and set `hostname = "your.domain"`.
    - DB created: name/user `{db_name}` (password stored in config).
    - The installer already configures Discourse to use the socket by default via `redis_url` in `config/discourse.conf` and by exporting `REDIS_URL` in `setenv`.

    ## Logs
    - Puma:    {logdir}/puma.*.log
    - Sidekiq: {logdir}/sidekiq.log
    - Redis:   {logdir}/redis.log
    """)

    write(f"{appdir}/setenv",   setenv, 0o700)
    write(f"{appdir}/start",    start,  0o700)
    write(f"{appdir}/stop",     stop,   0o700)
    write(f"{appdir}/restart",  restart,0o700)
    write(f"{appdir}/README",   readme, 0o644)

    # cron keepalive (like other installers)
    m = secrets.randbelow(10)
    add_cron(f"0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1")

    # Notices
    try:
        api.post("/app/installed/", json.dumps([{"id": args.uuid}]))
    except Exception as e:
        logging.warning(f"/app/installed/ failed: {e}")
    try:
        msg = f"Discourse prepared for app {appname}. Edit {srcdir}/config/discourse.conf (hostname), then run {appdir}/start."
        api.post("/notice/create/", json.dumps([{"type":"M","content": msg}]))
    except Exception as e:
        logging.warning(f"/notice/create/ failed: {e}")

    logging.info("Install complete.")

if __name__ == "__main__":
    main()
