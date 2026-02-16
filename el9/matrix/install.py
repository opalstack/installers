#!/usr/local/bin/python3.13
import argparse
import sys
import logging
import os
import http.client
import json
import textwrap
import secrets
import string
import subprocess
import shlex
import random
import time
import re
from urllib.parse import urlparse

API_HOST = os.environ.get("API_URL", "").strip().strip("https://").strip("http://")
API_BASE_URI = "/api/v1"

CMD_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "UMASK": "0002",
}

# Pin Synapse to a known-good version (you can bump later)
SYNAPSE_VERSION = "1.147.1"

# Synapse expects psycopg2 for Postgres engine "psycopg2".
# Use wheel-only psycopg2-binary to avoid source builds on EL9.
PSYCOPG2_BINARY_VERSION = "2.9.11"


class OpalstackAPITool:
    """simple wrapper for http.client get and post"""

    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host
        self.base_uri = base_uri

        # if there is no auth token, then try to log in with provided credentials
        if not authtoken:
            endpoint = self.base_uri + "/login/"
            payload = json.dumps(
                {
                    "username": user,
                    "password": password,
                }
            )
            conn = http.client.HTTPSConnection(self.host)
            conn.request(
                "POST",
                endpoint,
                payload,
                headers={"Content-type": "application/json"},
            )
            result = json.loads(conn.getresponse().read())
            if not result.get("token"):
                logging.warning(
                    "Invalid username or password and no auth token provided, exiting."
                )
                sys.exit(1)
            else:
                authtoken = result["token"]

        self.headers = {
            "Content-type": "application/json",
            "Authorization": f"Token {authtoken}",
        }

    def get(self, endpoint):
        """GETs an API endpoint"""
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request("GET", endpoint, headers=self.headers)
        connread = conn.getresponse().read()
        logging.info(connread)
        return json.loads(connread)

    def post(self, endpoint, payload):
        """POSTs data to an API endpoint"""
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request("POST", endpoint, payload, headers=self.headers)
        return json.loads(conn.getresponse().read())


def create_file(path, contents, writemode="w", perms=0o600):
    """make a file, perms are passed as octal"""
    with open(path, writemode) as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f"Created file {path} with permissions {oct(perms)}")


def download(url, localfile, writemode="wb", perms=0o600):
    """save a remote file, perms are passed as octal"""
    logging.info(f"Downloading {url} as {localfile} with permissions {oct(perms)}")
    u = urlparse(url)
    if u.scheme == "http":
        conn = http.client.HTTPConnection(u.netloc)
    else:
        conn = http.client.HTTPSConnection(u.netloc)
    conn.request("GET", u.path)
    r = conn.getresponse()
    with open(localfile, writemode) as f:
        while True:
            data = r.read(4096)
            if data:
                f.write(data)
            else:
                break
    os.chmod(localfile, perms)
    logging.info(f"Downloaded {url} as {localfile} with permissions {oct(perms)}")


def gen_password(length=20):
    """makes a random password"""
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def run_command(cmd, cwd=None, env=CMD_ENV, strict=False):
    """runs a command, returns output"""
    prefix = "Running (strict)" if strict else "Running"
    logging.info(f"{prefix}: {cmd}")
    try:
        result = subprocess.check_output(
            shlex.split(cmd),
            cwd=cwd,
            env=env,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as e:
        logging.debug(e.output)
        result = e.output
        if strict:
            logging.error(result)
            sys.exit(1)
    return result


def add_cronjob(cronjob):
    """appends a cron job to the user's crontab"""
    homedir = os.path.expanduser("~")
    tmpname = f"{homedir}/.tmp{gen_password()}"
    tmp = open(tmpname, "w")
    subprocess.run("crontab -l".split(), stdout=tmp)
    tmp.write(f"{cronjob}\n")
    tmp.close()
    cmd = f"crontab {tmpname}"
    run_command(cmd)
    run_command(f"rm -f {tmpname}")
    logging.info(f"Added cron job: {cronjob}")


def _replace_yaml_top_level_block(path, key, new_block_text):
    """
    Replace a top-level YAML key block (e.g. 'database:', 'listeners:') with new text.
    Assumes block is formatted like:
      key:
        ...
    and ends at next non-indented top-level key.
    """
    with open(path, "r") as f:
        content = f.read()

    # Match start of the key at beginning of line, capture until next top-level key or EOF.
    # Top-level keys start with non-space and end with ":".
    pattern = rf"(?ms)^(?P<k>{re.escape(key)}\s*:\s*\n)(?P<body>.*?)(?=^[^\s].*?:\s*$|\Z)"
    m = re.search(pattern, content)
    if not m:
        # If key doesn't exist, prepend at top (safe for our keys).
        content = new_block_text.rstrip() + "\n\n" + content
        with open(path, "w") as f:
            f.write(content)
        return

    start, end = m.span()
    content = content[:start] + new_block_text.rstrip() + "\n" + content[end:]
    with open(path, "w") as f:
        f.write(content)


def _ensure_yaml_key(path, key, value_text):
    """
    Ensure a simple top-level scalar exists (e.g. report_stats: false).
    If key exists, overwrite its line. If not, add near top.
    """
    with open(path, "r") as f:
        lines = f.readlines()

    key_re = re.compile(rf"^{re.escape(key)}\s*:\s*.*$")
    found = False
    for i, line in enumerate(lines):
        if key_re.match(line):
            lines[i] = f"{key}: {value_text}\n"
            found = True
            break

    if not found:
        # Insert after initial comment header if present.
        insert_at = 0
        while insert_at < len(lines) and lines[insert_at].lstrip().startswith("#"):
            insert_at += 1
        lines.insert(insert_at, f"{key}: {value_text}\n")

    with open(path, "w") as f:
        f.writelines(lines)


def main():
    """run it"""
    parser = argparse.ArgumentParser(
        description="Installs Matrix Synapse homeserver app on Opalstack account"
    )
    parser.add_argument(
        "-i",
        dest="app_uuid",
        help="UUID of the base app",
        default=os.environ.get("UUID"),
    )
    parser.add_argument(
        "-n",
        dest="app_name",
        help="name of the base app",
        default=os.environ.get("APPNAME"),
    )
    parser.add_argument(
        "-t",
        dest="opal_token",
        help="API auth token",
        default=os.environ.get("OPAL_TOKEN"),
    )
    parser.add_argument(
        "-u",
        dest="opal_user",
        help="Opalstack account name",
        default=os.environ.get("OPAL_USER"),
    )
    parser.add_argument(
        "-p",
        dest="opal_password",
        help="Opalstack account password",
        default=os.environ.get("OPAL_PASS"),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s",
    )

    logging.info(f"Started installation of Matrix (Synapse) app {args.app_name}")

    api = OpalstackAPITool(
        API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password
    )
    appinfo = api.get(f"/app/read/{args.app_uuid}")

    appdir = f'/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}'
    projectdir = f"{appdir}/matrix"
    venvdir = f"{projectdir}/venv"
    configdir = f"{projectdir}/config"
    datadir = f"{projectdir}/data"
    config_path = f"{configdir}/homeserver.yaml"

    # Create database + db user
    db_name = f"{args.app_name[:8]}_{args.app_uuid[:8]}"

    payload = json.dumps([{"server": appinfo["server"], "name": db_name}])

    user_attempts = 0
    db_pass = None
    psql_user_id = None

    while True:
        logging.info(f"Trying to create database user {db_name}")
        psql_user = api.post("/psqluser/create/", payload)

        # Capture password from API response (your platform supports this)
        if psql_user and len(psql_user) > 0 and "default_password" in psql_user[0]:
            db_pass = psql_user[0]["default_password"]
            logging.info("Received database password from API")

        time.sleep(5)

        existing_psql_users = api.get("/psqluser/list/")
        for check in json.loads(json.dumps(existing_psql_users)):
            if check["name"] == db_name and check["ready"]:
                psql_user_id = check["id"]
                logging.info(f"Database user {db_name} created with ID {psql_user_id}")
                break
        if psql_user_id:
            break

        user_attempts += 1
        if user_attempts > 10:
            logging.info(f"Could not create database user {db_name}")
            sys.exit(1)

    if not db_pass:
        logging.error("Failed to retrieve database password from API")
        sys.exit(1)
    if not psql_user_id:
        logging.error("Failed to retrieve database user ID")
        sys.exit(1)

    payload = json.dumps(
        [
            {
                "server": appinfo["server"],
                "name": db_name,
                "dbusers_readwrite": [psql_user_id],
            }
        ]
    )

    db_attempts = 0
    while True:
        logging.info(f"Trying to create database {db_name}")
        api.post("/psqldb/create/", payload)
        time.sleep(5)

        existing_psql_databases = api.get("/psqldb/list/")
        db_created = False
        for check in json.loads(json.dumps(existing_psql_databases)):
            if check["name"] == db_name and check["ready"]:
                logging.info(f"Database {db_name} created and user permissions assigned")
                db_created = True
                break

        if db_created:
            break

        db_attempts += 1
        if db_attempts > 10:
            logging.info(f"Could not create database {db_name}")
            sys.exit(1)

    # Make dirs
    run_command(f"mkdir -p {projectdir}", strict=True)
    run_command(f"mkdir -p {configdir}", strict=True)
    run_command(f"mkdir -p {datadir}", strict=True)

    # Venv + install Synapse
    run_command(f"/usr/local/bin/python3.13 -m venv {venvdir}", strict=True)
    run_command(f"{venvdir}/bin/pip install --upgrade pip setuptools wheel", strict=True)

    # Install Synapse + Postgres driver
    run_command(f'{venvdir}/bin/pip install "matrix-synapse=={SYNAPSE_VERSION}"', strict=True)

    # IMPORTANT: avoid psycopg2 source builds on Opalstack/EL9.
    # Force wheels only: if a wheel isn't available, this FAILS (which is what we want).
    run_command(
        f'{venvdir}/bin/pip install --no-cache-dir --only-binary=:all: "psycopg2-binary=={PSYCOPG2_BINARY_VERSION}"',
        strict=True,
    )

    # Verify psycopg2 import early so we fail at install-time, not at runtime.
    run_command(
        f'{venvdir}/bin/python -c "import psycopg2; print(\'psycopg2 OK\', psycopg2.__version__)"',
        strict=True,
    )

    # Generate Synapse config if missing (your platform expectation)
    if not os.path.exists(config_path):
        # We cannot know the final domain at install time; use example.com and document update.
        # The critical bits (port/listeners/db/report_stats) are forced below.
        cmd = (
            f"{venvdir}/bin/python -m synapse.app.homeserver "
            f"--generate-config "
            f"-H example.com "
            f"-c {config_path} "
            f"--report-stats no "
            f"--data-directory {datadir} "
            f"--config-directory {configdir}"
        )
        run_command(cmd, strict=True)

    # ---- FIXES / HARDENING ----

    # 1) report_stats MUST be explicit or Synapse refuses to start
    _ensure_yaml_key(config_path, "report_stats", "false")

    # 2) Suppress matrix.org key server warning (safe default)
    _ensure_yaml_key(config_path, "suppress_key_server_warning", "true")

    # 3) Force listeners: bind + port to Opal-assigned port, listen on 0.0.0.0
    listeners_block = textwrap.dedent(
        f"""\
        listeners:
          - port: {appinfo["port"]}
            tls: false
            type: http
            x_forwarded: true
            bind_addresses: ['0.0.0.0']
            resources:
              - names: [client, federation]
                compress: false
        """
    )
    _replace_yaml_top_level_block(config_path, "listeners", listeners_block)

    # 4) Force Postgres DB config.
    #    Synapse's allow_unsafe_locale is a database-level option (NOT a psycopg2 connect arg).
    #    It must live alongside "name", not inside "args".
    db_block = textwrap.dedent(
        f"""\
        database:
          name: psycopg2
          allow_unsafe_locale: true
          args:
            user: "{db_name}"
            password: "{db_pass}"
            dbname: "{db_name}"
            host: "127.0.0.1"
            port: 5432
            connect_timeout: 10
        """
    )
    _replace_yaml_top_level_block(config_path, "database", db_block)

    # Start/stop scripts (same pattern as your n8n installer)
    start_script = textwrap.dedent(
        f"""\
        #!/bin/bash
        APPDIR="{projectdir}"
        CFG="{config_path}"
        PIDFILE="{projectdir}/synapse.pid"
        LOGFILE="{projectdir}/synapse.log"

        cd "$APPDIR" || exit 1

        # Kill any existing process
        if [ -f "$PIDFILE" ]; then
          OLD_PID=$(cat "$PIDFILE")
          if ps -p "$OLD_PID" > /dev/null 2>&1; then
            kill "$OLD_PID" 2>/dev/null || true
            sleep 2
          fi
          rm -f "$PIDFILE"
        fi

        # Start Synapse
        nohup "{venvdir}/bin/python" -m synapse.app.homeserver -c "$CFG" >> "$LOGFILE" 2>&1 &
        NEW_PID=$!
        echo "$NEW_PID" > "$PIDFILE"
        echo "Started Synapse for {appinfo["name"]} (PID $NEW_PID) on port {appinfo["port"]}."
        """
    )
    create_file(f"{appdir}/start", start_script, perms=0o700)

    stop_script = textwrap.dedent(
        f"""\
        #!/bin/bash
        PIDFILE="{projectdir}/synapse.pid"
        if [ ! -f "$PIDFILE" ]; then
          echo "No PID file found, nothing to stop for {appinfo["name"]}."
          exit 0
        fi
        PID=$(cat "$PIDFILE")
        if ps -p "$PID" > /dev/null 2>&1; then
          kill "$PID" 2>/dev/null || true
          echo "Stopped Synapse for {appinfo["name"]} (PID $PID)."
        else
          echo "Process with PID $PID not running for {appinfo["name"]}."
        fi
        rm -f "$PIDFILE"
        """
    )
    create_file(f"{appdir}/stop", stop_script, perms=0o700)

    # Cron keepalive every 10 minutes (same cadence format as your installers)
    m = random.randint(0, 9)
    croncmd = f"0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1"
    add_cronjob(croncmd)

    readme = textwrap.dedent(
        f"""\
        # Opalstack Matrix Synapse README (EL9)

        ## What you installed
        - Matrix Synapse {SYNAPSE_VERSION} in a Python venv
        - psycopg2-binary {PSYCOPG2_BINARY_VERSION} (wheel-only) for Postgres
        - Config/data under:
          - {configdir}
          - {datadir}

        ## IMPORTANT: set your real domain (server_name + public_baseurl)
        Synapse was generated with server name: example.com
        Once you assign this app to a Site in the Opalstack control panel, edit:
          {config_path}

        Update at minimum:
        - server_name: "YOUR.DOMAIN"
        - public_baseurl: "https://YOUR.DOMAIN/"

        Then restart:
          {appdir}/stop
          {appdir}/start

        ## Port / routing
        Opalstack assigns and routes the port automatically via nginx.
        This installer forces Synapse to listen on:
          0.0.0.0:{appinfo["port"]}

        ## Database
        - Database: {db_name}
        - User: {db_name}
        - Password: stored in homeserver.yaml
        NOTE: Because managed Postgres locale/collation may not be 'C', this installer sets:
          allow_unsafe_locale: true
        to bypass Synapse's locale check.

        ## Control
        Start: {appdir}/start
        Stop:  {appdir}/stop
        Logs:  tail -f {projectdir}/synapse.log

        ## Auto-restart
        A cron job runs every 10 minutes to ensure Synapse stays running.
        """
    )
    create_file(f"{appdir}/README", readme, perms=0o600)

    # Start once
    run_command(f"{appdir}/start")

    # Mark installed
    payload = json.dumps([{"id": args.app_uuid}])
    api.post("/app/installed/", payload)

    logging.info(f"Completed installation of Matrix (Synapse) app {args.app_name}")


if __name__ == "__main__":
    main()
