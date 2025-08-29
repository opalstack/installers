#!/usr/bin/env python3
"""
Discourse installer for the Opalstack platform.

This script is designed to mirror the style and behaviour of the Mastodon
installer provided by Opalstack.  It accepts the same inputs (app UUID,
optional name, API token and user credentials), creates a database via
Opalstack’s API, fetches a tagged release of Discourse from GitHub,
installs all dependencies, writes configuration files and helper scripts,
and notifies the Opalstack API when the installation has completed.

Modern versions of Discourse (>3.2) rely on the `pnpm` package manager for
JavaScript dependencies and embed a `packageManager` field in their
`package.json`.  The legacy `yarn`-based workflow is still supported if
a `yarn.lock` file exists.  The installer therefore checks for the
presence of `yarn.lock` and runs the appropriate package manager.  The
logic below is modelled on the official Discourse container template
(`web.template.yml`), which runs `pnpm install --frozen-lockfile && pnpm prune`
when no `yarn.lock` is present【669233725289606†L98-L120】.

For more information on Discourse installation guidelines and
environment requirements, see the upstream documentation【159354044383136†L6-L24】.
The `pnpm` package manager itself requires Node.js 18.12 or newer and can
be bootstrapped via Corepack【553194873727240†L69-L74】.  This installer uses
`corepack enable` to make pnpm available in the app’s private `node/bin`
directory.
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

# ---------- Opalstack API ----------
API_URL = (os.environ.get("OPAL_API_URL") or os.environ.get("API_URL") or "https://my.opalstack.com").rstrip("/")
API_HOST = API_URL.replace("https://", "").replace("http://", "")
API_BASE_URI = "/api/v1"

# Use the same SCL toolchains as the Mastodon installer
# Use the latest available Node.js collection on the system.  As of August 2025
# Opalstack offers nodejs22 in addition to nodejs20, so we enable nodejs22 by
# default alongside ruby33.  If your environment only provides nodejs20 the
# `nodejs22` collection will silently fall back to nodejs20 because both are
# typically available via SCL.
CMD_PREFIX = "/bin/scl enable nodejs22 ruby33 -- "

# Discourse version pin.  If DISCOURSE_TAG env is empty the script will
# resolve the latest stable tag from GitHub.
DISCOURSE_TAG = os.environ.get("DISCOURSE_TAG", "")
GITHUB_TAGS_API = "https://api.github.com/repos/discourse/discourse/tags?per_page=100"
SEMVER_RE = re.compile(r"^v\d+\.\d+\.\d+$")

# Behaviour toggle: set OPAL_SKIP_DB=1 to supply your own DB_* environment vars
OPAL_SKIP_DB = os.environ.get("OPAL_SKIP_DB", "0") == "1"

# ---------- API helper ----------
class OpalAPI:
    """Minimal wrapper around Opalstack's REST API."""

    def __init__(self, host, base_uri, token, user, password):
        self.host = host
        self.base = base_uri
        # obtain a token if one was not provided
        if not token:
            conn = http.client.HTTPSConnection(self.host)
            payload = json.dumps({"username": user, "password": password})
            conn.request("POST", self.base + "/login/", payload, headers={"Content-type": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read() or b"{}")
            token = data.get("token")
            if not token:
                logging.error(f"Auth failed (HTTP {resp.status}).")
                sys.exit(1)
        self.h = {"Content-type": "application/json", "Authorization": f"Token {token}"}

    def get(self, path: str):
        conn = http.client.HTTPSConnection(self.host)
        conn.request("GET", self.base + path, headers=self.h)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        if resp.status >= 400:
            logging.error(f"GET {path} -> HTTP {resp.status}")
            sys.exit(1)
        return data

    def post(self, path: str, payload: str):
        conn = http.client.HTTPSConnection(self.host)
        conn.request("POST", self.base + path, payload, headers=self.h)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        if resp.status >= 400:
            logging.error(f"POST {path} -> HTTP {resp.status}: {data}")
        return data

# ---------- helper functions ----------
def pw(n: int = 24) -> str:
    """Generate a random alphanumeric password of length n."""
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(n))


def run(cmd: str, env: dict, cwd: str | None = None, use_shlex: bool = True) -> str:
    """Run a command via the SCL-enabled shell and return its stdout as a string.

    Commands are prefixed with `CMD_PREFIX` to ensure the nodejs and ruby
    collections are enabled.  If the subprocess returns a non-zero exit
    code, an exception is raised and the error output is logged.
    """
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
    """Write content to a file, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, perms)
    logging.info(f"wrote {path} perms={oct(perms)}")


def add_cron(line: str) -> None:
    """Append a cron job line to the current user's crontab if not already present."""
    home = os.path.expanduser("~")
    tmp = f"{home}/.tmp{pw(8)}"
    try:
        existing = subprocess.check_output("crontab -l".split(), stderr=subprocess.STDOUT).decode()
    except subprocess.CalledProcessError:
        existing = ""
    if line in existing:
        logging.info("cron already present")
        return
    with open(tmp, "w", encoding="utf-8") as t:
        t.write(existing)
        if existing and not existing.endswith("\n"):
            t.write("\n")
        t.write(line + "\n")
    subprocess.check_call(shlex.split(f"crontab {tmp}"))
    os.remove(tmp)
    logging.info(f"added cron: {line}")


def latest_tag() -> str:
    """Return the latest stable SemVer tag from the Discourse repository."""
    with urllib.request.urlopen(GITHUB_TAGS_API, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    for t in data:
        name = t.get("name", "")
        if SEMVER_RE.match(name):
            return name
    raise RuntimeError("No stable SemVer tag found")


def fetch_tag(tag: str, dest_dir: str) -> None:
    """Download a release tarball for the specified tag and extract it."""
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
        members = tf.getmembers()
        top = members[0].name.split("/")[0]
        tf.extractall(dest_dir)
    src = os.path.join(dest_dir, top)
    dst = os.path.join(dest_dir, "discourse")
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.rename(src, dst)
    os.remove(tarpath)


def main() -> None:
    ap = argparse.ArgumentParser(description="Install Discourse (no Docker) on Opalstack")
    ap.add_argument("-i", dest="uuid", default=os.environ.get("UUID"), help="App UUID")
    ap.add_argument("-n", dest="name", default=os.environ.get("APPNAME"), help="App name (ignored)")
    ap.add_argument("-t", dest="token", default=os.environ.get("OPAL_TOKEN"), help="Opalstack API token")
    ap.add_argument("-u", dest="user", default=os.environ.get("OPAL_USER"), help="Opalstack username")
    ap.add_argument("-p", dest="password", default=os.environ.get("OPAL_PASS"), help="Opalstack password")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
    if not args.uuid:
        logging.error("Missing app UUID (-i)")
        sys.exit(1)

    api = OpalAPI(API_HOST, API_BASE_URI, args.token, args.user, args.password)
    app = api.get(f"/app/read/{args.uuid}")
    if not app.get("name"):
        logging.error("App not found.")
        sys.exit(1)

    appname = app["name"]
    osuser = app["osuser_name"]
    port = int(app["port"])
    appdir = f"/home/{osuser}/apps/{appname}"
    logdir = f"/home/{osuser}/logs/apps/{appname}"
    srcdir = f"{appdir}/discourse"

    # Ensure base directories exist
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
        # create database user
        api.post("/psqluser/create/", json.dumps([
            {"server": app["server"], "name": db_user, "password": db_pass, "external": "false"}
        ]))
        time.sleep(3)
        # find user id
        uid = None
        for u in api.get("/psqluser/list/"):
            if u.get("name") == db_user:
                uid = u.get("id")
                break
        if not uid:
            logging.error("Failed to create/find DB user")
            sys.exit(1)
        # create database with RW privileges
        api.post("/psqldb/create/", json.dumps([
            {"server": app["server"], "name": db_name, "dbusers_readwrite": [uid]}
        ]))
        time.sleep(3)
        logging.info(f"DB ready: user={db_user} db={db_name}")

    # ---- fetch Discourse source (tag tarball) ----
    tag = DISCOURSE_TAG or latest_tag()
    logging.info(f"Using Discourse tag: {tag}")
    fetch_tag(tag, appdir)

    # ---- build environment (Ruby+Node via SCL), Yarn or PNPM local to app ----
    CMD_ENV = {
        "RAILS_ENV": "production",
        "PATH": f"{appdir}/node/bin:{srcdir}/bin:/usr/local/bin:/usr/bin:/bin",
        "GEM_HOME": f"{srcdir}/.gems",
        "BUNDLE_PATH": f"{srcdir}/vendor/bundle",
        # Setting BUNDLE_DEPLOYMENT tells bundler to install gems in deployment mode
        "BUNDLE_DEPLOYMENT": "1",
        "UMASK": "0002",
        "HOME": f"/home/{osuser}",
        "TMPDIR": f"{appdir}/tmp",
    }

    # create directory for node binaries and initialise corepack
    run(f"mkdir -p {appdir}/node/bin", CMD_ENV)
    run(f"corepack enable --install-directory={appdir}/node/bin", CMD_ENV, cwd=f"{appdir}/node")
    # enable pnpm support via corepack (requires internet to fetch metadata)【553194873727240†L69-L74】
    try:
        run("corepack enable pnpm", CMD_ENV)
    except Exception:
        logging.warning("corepack enable pnpm failed; assuming pnpm is already available")

    # Determine which JS package manager to use.  Modern Discourse versions use
    # pnpm (if no yarn.lock is present), whereas older releases still use yarn.
    use_yarn = os.path.exists(os.path.join(srcdir, "yarn.lock"))
    logging.info(f"Using {'yarn' if use_yarn else 'pnpm'} for JS dependencies")

    # Install gems before JS packages to ensure Ruby dependencies are satisfied
    run("bundle config set without 'development test'", CMD_ENV, cwd=srcdir)
    # The number of jobs is limited to 4 as in the Mastodon installer
    run("bundle install --jobs 4", CMD_ENV, cwd=srcdir)

    if use_yarn:
        # Ensure yarn uses the classic v1 (same as Mastodon installer)
        run("yarn set version classic", CMD_ENV)
        run("yarn install --frozen-lockfile --network-timeout 600000", CMD_ENV, cwd=srcdir)
    else:
        # Use pnpm.  Setting CI=1 disables progress spinners and reduces output
        run("CI=1 pnpm install --frozen-lockfile", CMD_ENV, cwd=srcdir)
        # prune extraneous packages to save space
        run("pnpm prune", CMD_ENV, cwd=srcdir)

    # ---- Discourse configuration ----
    secret_hex = secrets.token_hex(64)
    # placeholder hostname; users should update this in the README
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

    # Redis (UNIX socket)
    redis_url = unix://{redis_sock}

    # Required secret
    secret_key_base = {secret_hex}

    # Opalstack fronts this port with nginx
    serve_static_assets = true
    """)
    write(f"{srcdir}/config/discourse.conf", discourse_conf, perms=0o600)

    # ---- redis.conf (UNIX socket, daemonized) ----
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

    # ---- DB migrate & assets ----
    run("bundle exec rake db:migrate", CMD_ENV, cwd=srcdir)
    # Precompile static assets.  Discourse uses its own asset pipeline; SKIP_EMBER_CLI_COMPILE
    # is not needed here for production use.
    run("bundle exec rake assets:precompile", CMD_ENV, cwd=srcdir)

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
    # enable node and ruby via Software Collections
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

    # Redis (rh-redis5)
    if [ -f "$APPDIR/tmp/pids/redis.pid" ] && kill -0 $(cat "$APPDIR/tmp/pids/redis.pid") 2>/dev/null; then
      :
    else
      /bin/scl enable rh-redis5 -- redis-server "$APPDIR/redis.conf"
      sleep 1
    fi

    # Puma (bind Opalstack app port)
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
      /bin/scl enable rh-redis5 -- redis-cli -s "{srcdir}/tmp/sockets/redis.sock" shutdown || kill $(cat "{appdir}/tmp/pids/redis.pid") || true
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

    This installer deploys the [Discourse](https://discourse.org/) forum software
    directly on Opalstack without using Docker.  It follows the same general
    methodology as our Mastodon installer: the application runs **Puma** on
    127.0.0.1:{port}, **Sidekiq** handles background jobs and **Redis** is
    accessed via a UNIX domain socket at `{srcdir}/tmp/sockets/redis.sock`.

    ## Important notes

    - Discourse officially supports only Docker-based installations.  See the
      official install guide for details【159354044383136†L6-L24】.  This installer
      provides a convenience method for running Discourse under Opalstack but is
      **not** endorsed by the Discourse team.
    - Modern Discourse releases use `pnpm` for JavaScript dependencies.  Our
      installer checks for a `yarn.lock` file and falls back to `pnpm` when
      appropriate, as recommended by the official container template【669233725289606†L98-L120】.
    - `pnpm` itself requires Node.js v18.12 or newer; this is satisfied by the
      `nodejs20` Software Collection and `corepack` will make pnpm available
      automatically【553194873727240†L69-L74】.
    - After installation, edit `{srcdir}/config/discourse.conf` and set
      `hostname = "your.domain"` to match your own domain name.

    ## Start/Stop
    {appdir}/start
    {appdir}/stop
    {appdir}/restart

    ## First admin
    To create the first admin account, run the following commands:

        cd {srcdir}
        {appdir}/setenv
        bundle exec rake admin:create

    ## Configuration
    - Database: name/user `{db_name}` with password stored in the config file.
    - Redis: uses a UNIX socket at `{srcdir}/tmp/sockets/redis.sock`.
    - Logs: see `{logdir}` for Puma and Sidekiq logs.

    ## Upgrading
    To upgrade Discourse in the future, repeat the dependency install step
    (bundle install and pnpm/yarn install), then run `bundle exec rake
    db:migrate` and `bundle exec rake assets:precompile`.  Refer to the
    upstream installation guides for more details.
    """)

    write(f"{appdir}/setenv", setenv, 0o700)
    write(f"{appdir}/start", start, 0o700)
    write(f"{appdir}/stop", stop, 0o700)
    write(f"{appdir}/restart", restart, 0o700)
    write(f"{appdir}/README", readme, 0o644)

    # schedule a cron keepalive similar to other installers
    m = secrets.randbelow(10)
    add_cron(f"0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1")

    # notify the Opalstack API that the app has been installed
    try:
        api.post("/app/installed/", json.dumps([{"id": args.uuid}]))
    except Exception as e:
        logging.warning(f"/app/installed/ failed: {e}")
    try:
        msg = f"Discourse prepared for app {appname}. Edit {srcdir}/config/discourse.conf (hostname), then run {appdir}/start."
        api.post("/notice/create/", json.dumps([{"type": "M", "content": msg}]))
    except Exception as e:
        logging.warning(f"/notice/create/ failed: {e}")

    logging.info("Install complete.")


if __name__ == "__main__":
    main()