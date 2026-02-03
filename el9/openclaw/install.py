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
from urllib.parse import urlparse

DEFAULT_API_HOST = "api.opalstack.com"
API_URL_ENV = os.environ.get("API_URL", f"https://{DEFAULT_API_HOST}")
API_HOST = API_URL_ENV.replace("https://", "").replace("http://", "").strip("/")
API_BASE_URI = "/api/v1"

CMD_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "UMASK": "0002",
}

# ---- Opalstack API wrapper ----
class OpalstackAPITool:
    """simple wrapper for http.client get and post"""

    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host
        self.base_uri = base_uri

        # if there is no auth token, then try to log in with provided credentials
        if not authtoken:
            endpoint = self.base_uri + "/login/"
            payload = json.dumps({"username": user, "password": password})
            conn = http.client.HTTPSConnection(self.host)
            conn.request(
                "POST",
                endpoint,
                payload,
                headers={"Content-type": "application/json"},
            )
            result = json.loads(conn.getresponse().read())
            if not result.get("token"):
                logging.warning("Invalid username or password and no auth token provided, exiting.")
                sys.exit(1)
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
        return json.loads(connread)

    def post(self, endpoint, payload):
        """POSTs data to an API endpoint"""
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request("POST", endpoint, payload, headers=self.headers)
        return json.loads(conn.getresponse().read())


# ---- helpers ----
def ensure_dir(path, perms=0o700):
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, perms)
    except PermissionError:
        pass


def create_file(path, contents, writemode="w", perms=0o600):
    with open(path, writemode) as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f"Created file {path} with permissions {oct(perms)}")


def gen_password(length=32):
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def run_command(cmd, cwd=None, env=None):
    logging.info(f"Running: {cmd}")
    if env is None:
        env = CMD_ENV
    try:
        result = subprocess.check_output(
            shlex.split(cmd),
            cwd=cwd,
            env=env,
            stderr=subprocess.STDOUT,
        )
        return result
    except subprocess.CalledProcessError as e:
        logging.error(e.output.decode("utf-8", errors="ignore"))
        return e.output


def add_cronjob(cronjob_line):
    """appends a cron job to the user's crontab"""
    homedir = os.path.expanduser("~")
    tmpname = f"{homedir}/.tmp{gen_password(12)}"
    with open(tmpname, "w") as tmp:
        subprocess.run(["crontab", "-l"], stdout=tmp, stderr=subprocess.DEVNULL)
        tmp.write(f"{cronjob_line}\n")
    run_command(f"crontab {tmpname}")
    run_command(f"rm -f {tmpname}")
    logging.info(f"Added cron job: {cronjob_line}")


def main():
    parser = argparse.ArgumentParser(
        description="Installs OpenClaw Gateway as an Opalstack userspace app (no systemctl)."
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
    parser.add_argument(
        "--openclaw-version",
        dest="openclaw_version",
        help='OpenClaw npm version spec (e.g. "latest" or "2026.2.1")',
        default=os.environ.get("OPENCLAW_VERSION", "latest"),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s",
    )

    if not args.app_uuid:
        logging.error("Missing app UUID (-i or UUID env).")
        sys.exit(1)

    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)
    appinfo = api.get(f"/app/read/{args.app_uuid}")

    osuser = appinfo["osuser_name"]
    appdir = f'/home/{osuser}/apps/{appinfo["name"]}'
    projectdir = f"{appdir}/oc"  # match your current debug layout
    bindir = f"{projectdir}/bin"
    logsdir = f"{projectdir}/logs"
    statedir = f"{projectdir}/state"
    cfg_path = f"{projectdir}/openclaw.json"
    env_path = f"{statedir}/.env"

    port = appinfo["port"]

    logging.info(f"Installing OpenClaw app '{appinfo['name']}' for user {osuser} on port {port}")

    ensure_dir(appdir, perms=0o700)
    ensure_dir(projectdir, perms=0o700)
    ensure_dir(bindir, perms=0o700)
    ensure_dir(logsdir, perms=0o700)
    ensure_dir(statedir, perms=0o700)

    # ---- secrets: gateway token in $OPENCLAW_STATE_DIR/.env ----
    gateway_token = gen_password(48)
    env_contents = textwrap.dedent(
        f"""\
        # OpenClaw per-app env (loaded by wrapper/start script)
        # You can add provider keys here later if you want, but OpenClaw typically stores secrets
        # in auth profiles under state/agents/... after you run `openclaw onboard` or `openclaw agents add`.
        OPENCLAW_GATEWAY_TOKEN="{gateway_token}"
        """
    )
    create_file(env_path, env_contents, perms=0o600)

    # ---- OpenClaw config (strict JSON) ----
    # We isolate state + config per app so multiple apps can run on one account cleanly.
    # Note: token is injected via ${OPENCLAW_GATEWAY_TOKEN} (env substitution).
    cfg = {
        "gateway": {
            "mode": "local",
            "port": port,
            "bind": "loopback",
            "trustedProxies": ["127.0.0.1"],
            "auth": {
                "mode": "token",
                "token": "${OPENCLAW_GATEWAY_TOKEN}",
            },
        },
        "logging": {
            "file": f"{logsdir}/openclaw.jsonl",
            "level": "info",
            "consoleLevel": "info",
        },
    }
    create_file(cfg_path, json.dumps(cfg, indent=2) + "\n", perms=0o600)

    # ---- npm userspace install into project-local prefix ----
    npm_prefix = f"{projectdir}/.npm-global"
    ensure_dir(npm_prefix, perms=0o700)

    install_env = dict(CMD_ENV)
    # npm reads lowercase env for config overrides
    install_env["npm_config_prefix"] = npm_prefix
    install_env["NPM_CONFIG_PREFIX"] = npm_prefix
    install_env["SHARP_IGNORE_GLOBAL_LIBVIPS"] = "1"
    # keep installs/cache local-ish
    install_env["npm_config_cache"] = f"{projectdir}/.npm-cache"
    ensure_dir(install_env["npm_config_cache"], perms=0o700)

    # Install OpenClaw using Node 22 SCL (required Node >=22)
    # NOTE: This depends on your host having nodejs22 SCL available, like your n8n installer.
    logging.info(f"Installing OpenClaw npm package: openclaw@{args.openclaw_version}")
    run_command(
        f"scl enable nodejs22 -- npm install -g openclaw@{args.openclaw_version}",
        cwd=projectdir,
        env=install_env,
    )

    # ---- wrapper: ./bin/openclaw ----
    wrapper = textwrap.dedent(
        f"""\
        #!/bin/bash
        set -euo pipefail

        APPDIR="{projectdir}"
        STATE_DIR="{statedir}"
        CONFIG_PATH="{cfg_path}"
        ENVFILE="{env_path}"
        NPM_PREFIX="{npm_prefix}"

        # Export env from STATE_DIR/.env (token, optional keys)
        if [ -f "$ENVFILE" ]; then
          set -a
          source "$ENVFILE"
          set +a
        fi

        export OPENCLAW_STATE_DIR="$STATE_DIR"
        export OPENCLAW_CONFIG_PATH="$CONFIG_PATH"

        # Ensure we find the per-app openclaw binary
        OC_BIN="$NPM_PREFIX/bin/openclaw"
        if [ ! -x "$OC_BIN" ]; then
          echo "OpenClaw binary not found at $OC_BIN"
          echo "Try re-running the installer or run: scl enable nodejs22 -- npm install -g openclaw@{args.openclaw_version}"
          exit 1
        fi

        exec scl enable nodejs22 -- "$OC_BIN" "$@"
        """
    )
    create_file(f"{bindir}/openclaw", wrapper, perms=0o700)

    # ---- start/stop/status/upgrade scripts in app root ----
    start_script = textwrap.dedent(
        f"""\
        #!/bin/bash
        set -euo pipefail

        APPDIR="{projectdir}"
        PIDFILE="$APPDIR/openclaw.pid"
        CONSOLE_LOG="$APPDIR/logs/openclaw.console.log"
        PORT="{port}"

        # If already running, exit cleanly
        if [ -f "$PIDFILE" ]; then
          PID="$(cat "$PIDFILE" || true)"
          if [ -n "$PID" ] && ps -p "$PID" >/dev/null 2>&1; then
            echo "OpenClaw already running (PID $PID) on port $PORT"
            exit 0
          fi
          rm -f "$PIDFILE"
        fi

        # Start in background
        nohup "$APPDIR/bin/openclaw" gateway --port "$PORT" --verbose --ws-log compact >>"$CONSOLE_LOG" 2>&1 &
        NEW_PID=$!
        echo "$NEW_PID" > "$PIDFILE"

        # Print a tokenized local dashboard URL (works over SSH tunnel too)
        # If you're accessing via your site/domain, the same ?token=... pattern applies.
        TOKEN_LINE=$("$APPDIR/bin/openclaw" --help >/dev/null 2>&1; echo "ok") || true
        if [ -f "$APPDIR/state/.env" ]; then
          # shellcheck disable=SC1091
          source "$APPDIR/state/.env"
        fi

        echo "Started OpenClaw (PID $NEW_PID) on port $PORT"
        if [ -n "${{OPENCLAW_GATEWAY_TOKEN:-}}" ]; then
          echo "Dashboard (local): http://127.0.0.1:$PORT/?token=${{OPENCLAW_GATEWAY_TOKEN}}"
        else
          echo "Dashboard (local): http://127.0.0.1:$PORT/"
          echo "Token missing in state/.env; Control UI will reject with token_missing."
        fi
        echo "Logs: tail -f $CONSOLE_LOG"
        """
    )
    create_file(f"{appdir}/start", start_script, perms=0o700)

    stop_script = textwrap.dedent(
        f"""\
        #!/bin/bash
        set -euo pipefail

        APPDIR="{projectdir}"
        PIDFILE="$APPDIR/openclaw.pid"

        if [ ! -f "$PIDFILE" ]; then
          echo "No PID file found, nothing to stop for {appinfo["name"]}."
          exit 0
        fi

        PID="$(cat "$PIDFILE" || true)"
        if [ -z "$PID" ]; then
          rm -f "$PIDFILE"
          echo "Empty PID file; cleaned up."
          exit 0
        fi

        if ps -p "$PID" >/dev/null 2>&1; then
          kill "$PID" 2>/dev/null || true
          # Wait up to ~10s for graceful shutdown
          for _ in $(seq 1 20); do
            if ps -p "$PID" >/dev/null 2>&1; then
              sleep 0.5
            else
              break
            fi
          done
          # If still up, hard kill
          if ps -p "$PID" >/dev/null 2>&1; then
            kill -9 "$PID" 2>/dev/null || true
          fi
          echo "Stopped OpenClaw for {appinfo["name"]} (PID $PID)."
        else
          echo "Process with PID $PID not running for {appinfo["name"]}."
        fi

        rm -f "$PIDFILE"
        """
    )
    create_file(f"{appdir}/stop", stop_script, perms=0o700)

    status_script = textwrap.dedent(
        f"""\
        #!/bin/bash
        set -euo pipefail

        APPDIR="{projectdir}"
        PIDFILE="$APPDIR/openclaw.pid"
        PORT="{port}"

        if [ -f "$PIDFILE" ]; then
          PID="$(cat "$PIDFILE" || true)"
          if [ -n "$PID" ] && ps -p "$PID" >/dev/null 2>&1; then
            echo "OpenClaw running (PID $PID) on port $PORT"
            # Health/status are safe even if no provider keys are configured yet.
            "$APPDIR/bin/openclaw" status || true
            exit 0
          fi
        fi

        echo "OpenClaw NOT running on port $PORT"
        exit 1
        """
    )
    create_file(f"{appdir}/status", status_script, perms=0o700)

    upgrade_script = textwrap.dedent(
        f"""\
        #!/bin/bash
        set -euo pipefail

        APPDIR="{projectdir}"
        NPM_PREFIX="{npm_prefix}"

        export npm_config_prefix="$NPM_PREFIX"
        export NPM_CONFIG_PREFIX="$NPM_PREFIX"
        export npm_config_cache="$APPDIR/.npm-cache"
        export SHARP_IGNORE_GLOBAL_LIBVIPS=1

        echo "Upgrading OpenClaw (userspace) ..."
        scl enable nodejs22 -- npm install -g openclaw@{args.openclaw_version}
        echo "Done."
        """
    )
    create_file(f"{appdir}/upgrade", upgrade_script, perms=0o700)

    # ---- cron watchdog (every 10 minutes) ----
    m = random.randint(0, 9)
    cron_line = f"0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1"
    add_cronjob(cron_line)

    # ---- README ----
    readme = textwrap.dedent(
        f"""\
        # Opalstack OpenClaw README (userspace, no systemctl)

        ## What you got
        - OpenClaw installed in userspace under:
            {projectdir}/.npm-global
        - Wrapper CLI (always uses Node 22 SCL + per-app state/config):
            {projectdir}/bin/openclaw
        - Per-app config:
            {cfg_path}
        - Per-app state dir (auth profiles, sessions, etc.):
            {statedir}
        - Logs:
            Console: {projectdir}/logs/openclaw.console.log
            JSONL:   {projectdir}/logs/openclaw.jsonl

        ## Control your app
        Start:
            {appdir}/start

        Stop:
            {appdir}/stop

        Status:
            {appdir}/status

        Upgrade (keeps your state/config):
            {appdir}/upgrade

        Tail logs:
            tail -f {projectdir}/logs/openclaw.console.log

        Follow gateway file logs (JSONL):
            {projectdir}/bin/openclaw logs --follow

        ## IMPORTANT: How to open the Control UI without "token_missing"
        This instance uses token auth. The dashboard must be opened with ?token=... or
        you must paste the same token into Control UI settings.

        Local URL (on the gateway host):
            http://127.0.0.1:{port}/?token=<token>

        Token is stored in:
            {env_path}

        ## Provider/auth setup (your current Anthropic "no API key" errors)
        The gateway can run without provider keys, but agents can't answer until you configure them.

        Recommended: run onboarding WITHOUT daemon install:
            {projectdir}/bin/openclaw onboard

        Or manually add auth for the main agent:
            {projectdir}/bin/openclaw agents add main

        Auth profiles live under (per-agent):
            {statedir}/agents/<agentId>/agent/auth-profiles.json

        ## Reverse proxy note (why trustedProxies is set)
        Opalstack's site proxy usually hits your app over loopback and sets X-Forwarded-* headers.
        We pre-set gateway.trustedProxies=["127.0.0.1"] in openclaw.json so those headers can be trusted.

        ## Security note
        The Control UI is an admin surface. Do not expose it publicly without auth.
        Token auth is enforced at the WebSocket handshake; use ?token=... on first load.
        """
    )
    create_file(f"{appdir}/README", readme, perms=0o600)

    # Start once (same pattern as your n8n installer)
    run_command(f"{appdir}/start")

    # Mark app installed
    payload = json.dumps([{"id": args.app_uuid}])
    api.post("/app/installed/", payload)

    logging.info(f"Completed installation of OpenClaw app {args.app_name}")


if __name__ == "__main__":
    main()
