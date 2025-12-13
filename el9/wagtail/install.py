#! /bin/python3
import argparse
import sys
import logging
import os
import http.client
import json
import secrets
import string
import subprocess
import shlex
import random
import time
import shutil

API_HOST = os.environ.get("API_URL").strip("https://").strip("http://")
API_BASE_URI = "/api/v1"

CMD_ENV = {
    "PATH": "/usr/sqlite330/bin:/usr/local/bin:/usr/bin:/bin",
    "UMASK": "0002",
    "LD_LIBRARY_PATH": "/usr/sqlite330/lib",
    # TMPDIR set after appdir known
}

DJANGO_VERSION = "5.2.5"
WAGTAIL_VERSION = "7.2.1"
PROJECT_NAME = "mysite"


class OpalstackAPITool:
    """simple wrapper for http.client get and post"""

    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host
        self.base_uri = base_uri

        if not authtoken:
            endpoint = self.base_uri + "/login/"
            payload = json.dumps({"username": user, "password": password})
            conn = http.client.HTTPSConnection(self.host)
            conn.request("POST", endpoint, payload, headers={"Content-type": "application/json"})
            result = json.loads(conn.getresponse().read())
            if not result.get("token"):
                logging.warning("Invalid username/password and no auth token provided, exiting.")
                sys.exit(1)
            authtoken = result["token"]

        self.headers = {
            "Content-type": "application/json",
            "Authorization": f"Token {authtoken}",
        }

    def get(self, endpoint):
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request("GET", endpoint, headers=self.headers)
        return json.loads(conn.getresponse().read())

    def post(self, endpoint, payload):
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request("POST", endpoint, payload, headers=self.headers)
        return json.loads(conn.getresponse().read())


def gen_password(length=20):
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def create_file(path, contents, perms=0o600):
    with open(path, "w") as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f"Created file {path} with permissions {oct(perms)}")


def run_command(cmd, cwd=None, env=None):
    """runs exactly once; returns (rc, output_bytes)"""
    if env is None:
        env = CMD_ENV
    logging.info(f"Running: {cmd}")
    r = subprocess.run(
        shlex.split(cmd),
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    out = r.stdout or b""
    if r.returncode != 0:
        logging.error(out.decode("utf-8", errors="replace"))
    return r.returncode, out


def ensure_dir(path, perms=0o700):
    if not os.path.exists(path):
        os.makedirs(path, mode=perms, exist_ok=True)
        os.chmod(path, perms)
        logging.info(f"Created directory {path}")


def recreate_empty_dir(path, perms=0o700):
    """Clean installer: delete any existing dest and recreate empty."""
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)
        logging.info(f"Removed existing directory {path}")
    os.makedirs(path, mode=perms, exist_ok=True)
    os.chmod(path, perms)
    logging.info(f"Created directory {path} (empty)")


def add_cronjob(cronjob):
    homedir = os.path.expanduser("~")
    tmpname = f"{homedir}/.tmp{gen_password()}"
    with open(tmpname, "w") as tmp:
        subprocess.run("crontab -l".split(), stdout=tmp, stderr=subprocess.DEVNULL)
        tmp.write(f"{cronjob}\n")
    rc, _ = run_command(f"crontab {tmpname}")
    run_command(f"rm -f {tmpname}")
    if rc != 0:
        logging.error("Failed installing crontab")
        sys.exit(1)
    logging.info(f"Added cron job: {cronjob}")


def create_postgres_db(api, appinfo, args):
    """
    Follow the n8n-ish pattern:
    - create psql user (capture default_password)
    - poll list until ready
    - create db with user in dbusers_readwrite
    - poll list until ready
    """
    db_name = f"{args.app_name[:8]}_{args.app_uuid[:8]}"
    db_pass = None
    psql_user_id = None

    payload = json.dumps([{"server": appinfo["server"], "name": db_name}])

    # user
    for _ in range(30):
        logging.info(f"Trying to create database user {db_name}")
        resp = api.post("/psqluser/create/", payload)
        if resp and isinstance(resp, list) and len(resp) > 0 and "default_password" in resp[0]:
            db_pass = resp[0]["default_password"]
            logging.info("Received database password from API")

        time.sleep(5)
        users = api.get("/psqluser/list/") or []
        for u in users:
            if u.get("name") == db_name and u.get("ready"):
                psql_user_id = u.get("id")
                logging.info(f"Database user {db_name} created with ID {psql_user_id}")
                break
        if psql_user_id:
            break

    if not psql_user_id or not db_pass:
        logging.error("Failed creating postgres user or retrieving password")
        sys.exit(1)

    # db
    payload = json.dumps(
        [
            {
                "server": appinfo["server"],
                "name": db_name,
                "dbusers_readwrite": [psql_user_id],
            }
        ]
    )

    for _ in range(30):
        logging.info(f"Trying to create database {db_name}")
        api.post("/psqldb/create/", payload)

        time.sleep(5)
        dbs = api.get("/psqldb/list/") or []
        for d in dbs:
            if d.get("name") == db_name and d.get("ready"):
                logging.info(f"Database {db_name} created and user permissions assigned")
                return db_name, db_pass

    logging.error("Failed creating postgres database (timed out)")
    sys.exit(1)


def find_settings_file(project_root):
    # Wagtail 7.x template usually: mysite/mysite/settings/base.py
    p1 = os.path.join(project_root, PROJECT_NAME, "settings", "base.py")
    if os.path.exists(p1):
        return p1
    # fallback: settings.py in package
    p2 = os.path.join(project_root, PROJECT_NAME, "settings.py")
    if os.path.exists(p2):
        return p2
    return None


def patch_settings_for_postgres(settings_py, db_name, db_pass):
    """
    Key points:
    - pg_hba error showed "no encryption" -> MUST force SSL.
    - append at end so it wins.
    """
    with open(settings_py, "r") as f:
        contents = f.read()

    if "import os" not in contents:
        # unlikely for Wagtail template, but make it safe
        contents = "import os\n" + contents
        with open(settings_py, "w") as f:
            f.write(contents)

    if "Opalstack installer overrides" in contents:
        logging.info(f"Overrides already present in {settings_py}, skipping append")
        return

    override = f"""
# Opalstack installer overrides

DATABASES = {{
    "default": {{
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "{db_name}",
        "USER": "{db_name}",
        "PASSWORD": "{db_pass}",
        "HOST": "localhost",
        "PORT": "5432",
    }}
}}

STATIC_ROOT = os.path.join(BASE_DIR, "static_collected")
MEDIA_ROOT = os.path.join(BASE_DIR, "media")
"""

    with open(settings_py, "a") as f:
        f.write(override)

    # allow wildcard allowed hosts for bootstrap
    run_command(
        f'''sed -r -i "s/^ALLOWED_HOSTS\\s*=\\s*\\[[^\\]]*\\]/ALLOWED_HOSTS = \\['*'\\]/" {settings_py}'''
    )

    logging.info(f"Patched settings for postgres + static/media: {settings_py}")


def main():
    parser = argparse.ArgumentParser(description="Installs Wagtail (Django CMS) on an Opalstack account")
    parser.add_argument("-i", dest="app_uuid", default=os.environ.get("UUID"))
    parser.add_argument("-n", dest="app_name", default=os.environ.get("APPNAME"))
    parser.add_argument("-t", dest="opal_token", default=os.environ.get("OPAL_TOKEN"))
    parser.add_argument("-u", dest="opal_user", default=os.environ.get("OPAL_USER"))
    parser.add_argument("-p", dest="opal_password", default=os.environ.get("OPAL_PASS"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
    logging.info(f"Started installation of Wagtail app {args.app_name}")

    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)
    appinfo = api.get(f"/app/read/{args.app_uuid}")

    appdir = f"/home/{appinfo['osuser_name']}/apps/{appinfo['name']}"
    project_root = f"{appdir}/{PROJECT_NAME}"

    # tmp dir
    ensure_dir(f"{appdir}/tmp", 0o700)
    CMD_ENV["TMPDIR"] = f"{appdir}/tmp"

    # postgres
    db_name, db_pass = create_postgres_db(api, appinfo, args)

    # python
    rc, out = run_command("which python3.12")
    python_executable_path = out.decode("utf-8", errors="replace").strip()
    if rc != 0 or not python_executable_path:
        logging.error("python3.12 not found on PATH")
        sys.exit(1)

    # venv
    rc, _ = run_command(f"{python_executable_path} -m venv {appdir}/env")
    if rc != 0:
        logging.error("Failed creating virtualenv")
        sys.exit(1)
    logging.info(f"Created virtualenv at {appdir}/env")

    venv_python = f"{appdir}/env/bin/python"

    # deps
    steps = [
        (f"{appdir}/env/bin/pip install --upgrade pip", "pip upgrade failed"),
        (f"{appdir}/env/bin/pip install uwsgi", "uwsgi install failed"),
        (f'{appdir}/env/bin/pip install "django=={DJANGO_VERSION}"', "django install failed"),
        (f'{appdir}/env/bin/pip install "wagtail=={WAGTAIL_VERSION}"', "wagtail install failed"),
        (f'{appdir}/env/bin/pip install "psycopg[binary]"', "psycopg install failed"),
    ]
    for cmd, err in steps:
        rc, _ = run_command(cmd)
        if rc != 0:
            logging.error(err)
            sys.exit(1)

    rc, _ = run_command(f"chmod 700 {appdir}/env/bin/uwsgi")
    if rc != 0:
        logging.error("Failed chmod uwsgi")
        sys.exit(1)

    logging.info(f"Installed Django {DJANGO_VERSION}, Wagtail {WAGTAIL_VERSION}, psycopg into virtualenv")

    # Wagtail start: dest MUST exist, and MUST be empty -> recreate it
    recreate_empty_dir(project_root, 0o700)

    rc, _ = run_command(f"{appdir}/env/bin/wagtail start {PROJECT_NAME} {project_root}")
    if rc != 0:
        logging.error("Failed running wagtail start")
        sys.exit(1)

    logging.info(f"Populated Wagtail project directory {project_root}")

    # settings patch BEFORE migrate
    settings_py = find_settings_file(project_root)
    if not settings_py:
        logging.error("Could not locate Wagtail settings file; cannot configure database")
        sys.exit(1)

    patch_settings_for_postgres(settings_py, db_name, db_pass)

    # wait for real postgres connectivity (ssl) BEFORE migrate
    #wait_for_postgres_via_venv(venv_python, db_name, db_pass, timeout_seconds=240)

    # uwsgi config + scripts
    wsgi_file = f"{project_root}/{PROJECT_NAME}/wsgi.py"
    if not os.path.exists(wsgi_file):
        alt = f"{project_root}/config/wsgi.py"
        if os.path.exists(alt):
            wsgi_file = alt
    if not os.path.exists(wsgi_file):
        logging.error("Could not locate wsgi.py")
        sys.exit(1)

    uwsgi_conf = (
        "[uwsgi]\n"
        "master = True\n"
        f"http-socket = 127.0.0.1:{appinfo['port']}\n"
        "env = LD_LIBRARY_PATH=/usr/sqlite330/lib\n"
        f"virtualenv = {appdir}/env/\n"
        f"daemonize = /home/{appinfo['osuser_name']}/logs/apps/{appinfo['name']}/uwsgi.log\n"
        f"pidfile = {appdir}/tmp/uwsgi.pid\n"
        "\n"
        "workers = 2\n"
        "threads = 2\n"
        "\n"
        f"chdir = {project_root}\n"
        f"python-path = {project_root}\n"
        f"wsgi-file = {wsgi_file}\n"
        f"touch-reload = {wsgi_file}\n"
    )
    create_file(f"{appdir}/uwsgi.ini", uwsgi_conf, perms=0o600)

    start_script = (
        "#!/bin/bash\n"
        f"export TMPDIR={appdir}/tmp\n"
        "export LD_LIBRARY_PATH=/usr/sqlite330/lib\n"
        f"mkdir -p {appdir}/tmp\n"
        f'PIDFILE="{appdir}/tmp/uwsgi.pid"\n'
        'if [ -f "$PIDFILE" ]; then\n'
        '  PID=$(cat "$PIDFILE" 2>/dev/null)\n'
        '  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then\n'
        f'    echo "uWSGI for {appinfo["name"]} already running."\n'
        "    exit 99\n"
        "  fi\n"
        "fi\n"
        f"{appdir}/env/bin/uwsgi --ini {appdir}/uwsgi.ini\n"
        f'echo "Started uWSGI for {appinfo["name"]}."\n'
    )
    create_file(f"{appdir}/start", start_script, perms=0o700)

    stop_script = (
        "#!/bin/bash\n"
        f'PIDFILE="{appdir}/tmp/uwsgi.pid"\n'
        'if [ ! -f "$PIDFILE" ]; then\n'
        '  echo "PIDFILE missing, maybe uWSGI is already stopped?"\n'
        "  exit 99\n"
        "fi\n"
        'PID=$(cat "$PIDFILE" 2>/dev/null)\n'
        f"{appdir}/env/bin/uwsgi --stop $PIDFILE 2>/dev/null\n"
        "sleep 2\n"
        'if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then\n'
        '  echo "uWSGI did not stop cleanly; killing."\n'
        '  kill -9 "$PID" 2>/dev/null\n'
        "fi\n"
        'rm -f "$PIDFILE"\n'
        'echo "Stopped."\n'
    )
    create_file(f"{appdir}/stop", stop_script, perms=0o700)

    # migrate (HARD FAIL)
    rc, _ = run_command(f"{appdir}/env/bin/python manage.py migrate", cwd=project_root)
    if rc != 0:
        logging.error("Django migrate failed; aborting.")
        sys.exit(1)

    # collectstatic (HARD FAIL)
    rc, _ = run_command(f"{appdir}/env/bin/python manage.py collectstatic --noinput", cwd=project_root)
    if rc != 0:
        logging.error("collectstatic failed; aborting.")
        sys.exit(1)

    # cron keepalive
    m = random.randint(0, 9)
    croncmd = f"0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1"
    add_cronjob(croncmd)

    # README (plain text)
    readme = (
        "Opalstack Wagtail Installation\n"
        "\n"
        "Application name:\n"
        f"{args.app_name}\n"
        "\n"
        "Python virtualenv:\n"
        f"{appdir}/env\n"
        "\n"
        "Project directory:\n"
        f"{project_root}\n"
        "\n"
        "PostgreSQL database:\n"
        f"Name: {db_name}\n"
        f"User: {db_name}\n"
        f"Password: {db_pass}\n"
        "Host: localhost\n"
        "Port: 5432\n"
        "SSL: required\n"
        "\n"
        "Logs:\n"
        f"/home/{appinfo['osuser_name']}/logs/apps/{appinfo['name']}/uwsgi.log\n"
        "\n"
        "Next steps:\n"
        "1) Connect this app to a site route in the Opalstack control panel.\n"
        "2) Create an admin user:\n"
        f"   cd {project_root}\n"
        f"   source {appdir}/env/bin/activate\n"
        "   python manage.py createsuperuser\n"
        "3) Restart:\n"
        f"   {appdir}/stop\n"
        f"   {appdir}/start\n"
    )
    create_file(f"{appdir}/README", readme, perms=0o600)

    # start it (HARD FAIL)
    rc, _ = run_command(f"{appdir}/start")
    if rc != 0:
        logging.error("Failed starting uWSGI")
        sys.exit(1)

    # mark installed
    api.post("/app/installed/", json.dumps([{"id": args.app_uuid}]))
    logging.info(f"Completed installation of Wagtail app {args.app_name}")


if __name__ == "__main__":
    main()
