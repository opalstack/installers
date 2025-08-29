#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, http.client, json, logging, os, re, secrets, shlex, string, subprocess, sys, tarfile, textwrap, time, urllib.request, hashlib, shutil

# ---------------- Opalstack API / env ----------------
API_URL = (os.environ.get("OPAL_API_URL") or os.environ.get("API_URL") or "https://my.opalstack.com").rstrip("/")
API_HOST = API_URL.replace("https://", "").replace("http://", "")
API_BASE_URI = "/api/v1"

# EL9 SCL toolchains (same posture as Mastodon installer)
CMD_PREFIX = "/bin/scl enable nodejs20 ruby33 -- "

# Discourse tag pinning
DISCOURSE_TAG = os.environ.get("DISCOURSE_TAG", "")  # e.g. v3.4.7 ; if empty, auto-resolve latest vX.Y.Z
GITHUB_TAGS_API = "https://api.github.com/repos/discourse/discourse/tags?per_page=100"
SEMVER_RE = re.compile(r"^v\d+\.\d+\.\d+$")

# Behavior toggles
OPAL_SKIP_DB = os.environ.get("OPAL_SKIP_DB", "0") == "1"  # set OPAL_SKIP_DB=1 to use existing DB creds

# ---------------- API wrapper ------------------------
class OpalstackAPITool:
    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host
        self.base_uri = base_uri
        if not authtoken:
            payload = json.dumps({"username": user, "password": password})
            conn = http.client.HTTPSConnection(self.host)
            conn.request("POST", self.base_uri + "/login/", payload, headers={"Content-type": "application/json"})
            resp = conn.getresponse()
            result = json.loads(resp.read() or b"{}")
            token = result.get("token")
            if not token:
                logging.error(f"Invalid credentials; HTTP {resp.status}")
                sys.exit(1)
            authtoken = token
        self.headers = {"Content-type": "application/json", "Authorization": f"Token {authtoken}"}

    def get(self, endpoint):
        conn = http.client.HTTPSConnection(self.host)
        conn.request("GET", self.base_uri + endpoint, headers=self.headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        if resp.status >= 400:
            logging.error(f"GET {endpoint} -> HTTP {resp.status}")
            sys.exit(1)
        return data

    def post(self, endpoint, payload):
        conn = http.client.HTTPSConnection(self.host)
        conn.request("POST", self.base_uri + endpoint, payload, headers=self.headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        if resp.status >= 400:
            logging.error(f"POST {endpoint} -> HTTP {resp.status}: {data}")
        return data

# ---------------- helpers ----------------------------
def gen_password(n=24):
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(n))

def run_command(cmd, env, cwd=None, use_shlex=True):
    """Run under SCL Ruby/Node; raise on failure."""
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

def create_file(path, contents, perms=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f"wrote {path} perms={oct(perms)}")

def add_cronjob(line):
    """Append a cron job if not already present."""
    home = os.path.expanduser("~")
    tmp = f"{home}/.tmp{gen_password(8)}"
    try:
        existing = subprocess.check_output("crontab -l".split(), stderr=subprocess.STDOUT).decode()
    except subprocess.CalledProcessError:
        existing = ""
    if line in existing:
        logging.info("cron already present")
        return
    with open(tmp, "w") as t:
        t.write(existing)
        if existing and not existing.endswith("\n"):
            t.write("\n")
        t.write(line + "\n")
    subprocess.check_call(shlex.split(f"crontab {tmp}"))
    os.remove(tmp)
    logging.info(f"added cron: {line}")

def latest_stable_tag():
    with urllib.request.urlopen(GITHUB_TAGS_API, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    for t in data:
        name = t.get("name", "")
        if SEMVER_RE.match(name):
            return name
    raise RuntimeError("No stable SemVer tag found in GitHub tags")

def download_and_extract_tag(tag, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    url = f"https://github.com/discourse/discourse/archive/refs/tags/{tag}.tar.gz"
    tarpath = os.path.join(dest_dir, f"discourse-{tag}.tar.gz")
    with urllib.request.urlopen(url, timeout=60) as r, open(tarpath, "wb") as f:
        shutil.copyfileobj(r, f)
    sha256 = hashlib.sha256()
    with open(tarpath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    logging.info(f"fetched {tag} sha256={sha256.hexdigest()}")
    with tarfile.open(tarpath, "r:gz") as tf:
        top = tf.getmembers()[0].name.split("/")[0]  # e.g. discourse-3.4.7
        tf.extractall(dest_dir)
    src = os.path.join(dest_dir, top)
    dst = os.path.join(dest_dir, "discourse")
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.rename(src, dst)
    os.remove(tarpath)

# ---------------- main -------------------------------
def main():
    parser = argparse.ArgumentParser(description="Install Discourse from source (no Docker) on Opalstack")
    parser.add_argument("-i", dest="uuid", default=os.environ.get("UUID"))
    parser.add_argument("-n", dest="name", default=os.environ.get("APPNAME"))  # accept -n to match launcher
    parser.add_argument("-t", dest="token", default=os.environ.get("OPAL_TOKEN"))
    parser.add_argument("-u", dest="user", default=os.environ.get("OPAL_USER"))
    parser.add_argument("-p", dest="password", default=os.environ.get("OPAL_PASS"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
    if not args.uuid:
        logging.error("Missing app UUID (-i)")
        sys.exit(1)

    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.token, args.user, args.password)
    app = api.get(f"/app/read/{args.uuid}")
    if not app.get("name"):
        logging.error("App not found")
        sys.exit(1)

    appname = app["name"]; osuser = app["osuser_name"]; port = int(app["port"])
    appdir = f'/home/{osuser}/apps/{appname}'
    logdir = f'/home/{osuser}/logs/apps/{appname}'
    srcdir = f"{appdir}/discourse"
    pids   = f"{appdir}/tmp/pids"

    os.makedirs(appdir, exist_ok=True)
    os.makedirs(logdir, exist_ok=True)
    os.makedirs(pids, exist_ok=True)
    os.makedirs(f"{appdir}/tmp", exist_ok=True)

    # -------- DB user + DB via Opalstack API (unless skipped) --------
    if OPAL_SKIP_DB:
        db_name = os.environ.get("DB_NAME") or f"disc_{args.uuid[:8]}".lower()
        db_user = os.environ.get("DB_USER") or db_name
        db_pass = os.environ.get("DB_PASS") or gen_password()
        logging.info("OPAL_SKIP_DB=1 -> using provided/existing DB creds")
    else:
        db_name = f"{appname[:8]}_{args.uuid[:8]}".lower()
        db_user = db_name
        db_pass = gen_password()
        payload_user = json.dumps([{"server": app["server"], "name": db_user, "password": db_pass, "external": "false"}])
        api.post("/psqluser/create/", payload_user)
        time.sleep(3)
        users = api.get("/psqluser/list/")
        uid = None
        for u in users:
            if u.get("name") == db_user:
                uid = u["id"]; break
        if not uid:
            logging.error("Failed to create/find DB user")
            sys.exit(1)
        payload_db = json.dumps([{"server": app["server"], "name": db_name, "dbusers_readwrite": [uid]}])
        api.post("/psqldb/create/", payload_db)
        time.sleep(3)
        api.post("/psqluser/update/", json.dumps([{"id": [uid], "password": db_pass, "external": "false"}]))
        logging.info(f"DB ready: user={db_user} db={db_name}")

    # -------- fetch Discourse tagged source --------
    tag = DISCOURSE_TAG or latest_stable_tag()
    logging.info(f"Using Discourse tag: {tag}")
    download_and_extract_tag(tag, appdir)

    # -------- build env (Ruby+Node via SCL) --------
    CMD_ENV = {
        "RAILS_ENV": "production",
        "PATH": f'{appdir}/node/bin:{srcdir}/bin:/usr/local/bin:/usr/bin:/bin',
        "GEM_HOME": f"{srcdir}/.gems",
        "BUNDLE_PATH": f"{srcdir}/vendor/bundle",
        "BUNDLE_DEPLOYMENT": "1",
        "UMASK": "0002",
        "HOME": f"/home/{osuser}",
        "TMPDIR": f"{appdir}/tmp",
    }

    # Corepack + Yarn classic
    run_command("corepack enable", CMD_ENV)
    run_command("corepack prepare yarn@1.22.19 --activate", CMD_ENV)

    # Bundle / Yarn
    run_command("bundle config set without 'development test'", CMD_ENV, cwd=srcdir)
    run_command("bundle install --jobs 4", CMD_ENV, cwd=srcdir)
    run_command("yarn install --frozen-lockfile --network-timeout 600000", CMD_ENV, cwd=srcdir)

    # -------- write config/discourse.conf --------
    secret_hex = secrets.token_hex(64)  # 128 hex chars
    hostname_placeholder = "forum.example.com"

    # per-forum Redis via UNIX socket
    redis_sock = f"{srcdir}/tmp/sockets/redis.sock"

    discourse_conf = textwrap.dedent(f"""\
    # Discourse production config
    hostname = "{hostname_placeholder}"

    # Postgres
    db_host = localhost
    db_port = 5432
    db_name = {db_name}
    db_username = {db_user}
    db_password = {db_pass}

    # Redis (UNIX socket)
    redis_url = unix://{redis_sock}

    # Required secret
    secret_key_base = {secret_hex}

    # Discourse serves assets via Rails; Opalstack Nginx fronts this port.
    serve_static_assets = true
    """)
    create_file(f"{srcdir}/config/discourse.conf", discourse_conf, perms=0o600)

    # -------- create redis.conf (unix socket, no TCP) --------
    redis_conf = textwrap.dedent(f"""\
    port 0
    unixsocket {redis_sock}
    unixsocketperm 700
    daemonize yes
    pidfile {srcdir}/tmp/pids/redis.pid
    logfile {logdir}/redis.log
    save ""
    appendonly no
    """)
    create_file(f"{appdir}/redis.conf", redis_conf, perms=0o600)

    # -------- database & assets --------
    run_command("bundle exec rake db:migrate", CMD_ENV, cwd=srcdir)
    run_command("bundle exec rake assets:precompile", CMD_ENV, cwd=srcdir)

    # -------- scripts (Redis via SCL rh-redis5, Puma, Sidekiq) --------
    setenv = textwrap.dedent(f"""\
    #!/bin/bash
    # Load Ruby/Node toolchains for Discourse
    APPDIR="{appdir}"
    SRCDIR="{srcdir}"
    export RAILS_ENV=production
    export GEM_HOME="$SRCDIR/.gems"
    export BUNDLE_PATH="$SRCDIR/vendor/bundle"
    export PATH="$APPDIR/node/bin:$SRCDIR/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
    export TMPDIR="$APPDIR/tmp"
    source scl_source enable nodejs20 ruby33
    """)

    start = textwrap.dedent(f"""\
    #!/bin/bash
    # Start Redis (unix socket via SCL rh-redis5), Puma on app port, Sidekiq.
    set -Eeuo pipefail
    APPNAME="{appname}"
    APPDIR="{appdir}"
    SRCDIR="{srcdir}"
    LOGDIR="{logdir}"
    PIDS="{pids}"
    PORT="{port}"
    export RAILS_ENV=production

    mkdir -p "$SRCDIR/tmp/pids" "$SRCDIR/tmp/sockets" "$LOGDIR"

    # env
    source "$APPDIR/setenv"

    # ---- Redis (socket, no TCP) ----
    if [ -f "$SRCDIR/tmp/pids/redis.pid" ] && kill -0 $(cat "$SRCDIR/tmp/pids/redis.pid") 2>/dev/null; then
      echo "==> redis already running"
    else
      echo "==> starting redis via SCL rh-redis5"
      /bin/scl enable rh-redis5 -- redis-server "$APPDIR/redis.conf"
      sleep 1
    fi

    # Puma (bind to Opalstack app port)
    if [ -f "$SRCDIR/tmp/pids/puma.pid" ] && kill -0 $(cat "$SRCDIR/tmp/pids/puma.pid") 2>/dev/null; then
      echo "==> puma already running"
    else
      echo "==> starting puma on 127.0.0.1:$PORT"
      cd "$SRCDIR"
      bundle exec puma -e production -b tcp://127.0.0.1:$PORT -d --pidfile "$SRCDIR/tmp/pids/puma.pid" --redirect-stdout "$LOGDIR/puma.stdout.log" --redirect-stderr "$LOGDIR/puma.stderr.log"
      sleep 1
    fi

    # Sidekiq
    if [ -f "$SRCDIR/tmp/pids/sidekiq.pid" ] && kill -0 $(cat "$SRCDIR/tmp/pids/sidekiq.pid") 2>/dev/null; then
      echo "==> sidekiq already running"
    else
      echo "==> starting sidekiq"
      cd "$SRCDIR"
      bundle exec sidekiq -e production -d -L "$LOGDIR/sidekiq.log" -P "$SRCDIR/tmp/pids/sidekiq.pid"
    fi

    echo "OK"
    """)

    stop = textwrap.dedent(f"""\
    #!/bin/bash
    # Stop Puma, Sidekiq, and local Redis
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
    if [ -f "$SRCDIR/tmp/pids/redis.pid" ]; then
      /bin/scl enable rh-redis5 -- redis-cli -s "$SRCDIR/tmp/sockets/redis.sock" shutdown || kill $(cat "$SRCDIR/tmp/pids/redis.pid") || true
      rm -f "$SRCDIR/tmp/pids/redis.pid"
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

    - Front nginx is Opalstack's; this app binds **Puma** to 127.0.0.1:{port}.
    - **Sidekiq** handles jobs.
    - **Redis** runs locally as a **UNIX socket** at: `{srcdir}/tmp/sockets/redis.sock`

    ## Start/Stop
    {appdir}/start  
    {appdir}/stop  
    {appdir}/restart

    ## First admin
    cd {srcdir}
    {appdir}/setenv
    bundle exec rake admin:create

    ## Config
    - Edit `{srcdir}/config/discourse.conf`: set `hostname = "your.domain"`
    - DB created: name/user `{db_name}` (password is in this file); or set OPAL_SKIP_DB=1 and provide your own.

    ## Logs
    - Puma:    /home/{osuser}/logs/apps/{appname}/puma.*.log
    - Sidekiq: /home/{osuser}/logs/apps/{appname}/sidekiq.log
    - Redis:   /home/{osuser}/logs/apps/{appname}/redis.log
    """)

    # write files
    create_file(f"{appdir}/setenv",   setenv,  perms=0o700)
    create_file(f"{appdir}/start",    start,   perms=0o700)
    create_file(f"{appdir}/stop",     stop,    perms=0o700)
    create_file(f"{appdir}/restart",  restart, perms=0o700)
    create_file(f"{appdir}/README",   readme,  perms=0o644)

    # cron keepalive (every 10m staggered)
    m = int(time.time()) % 10
    add_cronjob(f"{m} */1 * * * {appdir}/start > /dev/null 2>&1")

    # notify panel
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
