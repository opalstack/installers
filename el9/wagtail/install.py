#! /bin/python3
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

API_HOST = os.environ.get("API_URL").strip("https://").strip("http://")
API_BASE_URI = "/api/v1"

CMD_ENV = {
    "PATH": "/usr/sqlite330/bin:/usr/local/bin:/usr/bin:/bin",
    "UMASK": "0002",
    "LD_LIBRARY_PATH": "/usr/sqlite330/lib",
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


def create_file(path, contents, perms=0o600):
    with open(path, "w") as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f"Created file {path} with permissions {oct(perms)}")


def gen_password(length=20):
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


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


def dir_is_empty(path):
    try:
        return len(os.listdir(path)) == 0
    except FileNotFoundError:
        return True


def rmdir_if_empty(path):
    if os.path.exists(path) and dir_is_empty(path):
        try:
            os.rmdir(path)
            logging.info(f"Removed empty directory {path}")
        except OSError:
            # if something created a file between checks
            pass


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
    Create psql user + db and wait until API reports both ready.
    Still must do a real connection wait afterward (pg_hba/ssl propagation).
    """
    db_name = f"{args.app_name[:8]}_{args.app_uuid[:8]}"
    db_pass = None
    psql_user_id = None

    # Create DB user (capture default_password)
    payload = json.dumps([{"server": appinfo["server"], "name": db_name}])
    for attempt in range(1, 31):
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
        logging.error("Could not create database user or retrieve password")
        sys.exit(1)

    # Create DB and grant rw
    payload = json.dumps(
        [
            {
                "server": appinfo["server"],
                "name": db_name,
                "dbusers_readwrite": [psql_user_id],
            }
        ]
    )
    for attempt in range(1, 31):
        logging.info(f"Trying to create database {db_name}")
        api.post("/psqldb/create/", payload)

        time.sleep(5)
        dbs = api.get("/psqldb/list/") or []
        for d in dbs:
            if d.get("name") == db_name and d.get("ready"):
                logging.info(f"Database {db_name} created and user permissions assigned")
                return db_name, db_pass

    logging.error("Could not create database (timed out waiting for ready)")
    sys.exit(1)


def wait_for_postgres(db_name, db_pass, host="localhost", port="5432", timeout_seconds=180):
    """
    Real connectivity wait (NOT just API-ready).
    Your error shows pg_hba requires encryption, so we force sslmode=require.
    """
    try:
        import psycopg
    except Exception:
        logging.error("psycopg is not available to installer runtime; cannot perform db connectivity wait")
        return

    deadline = time.time() + timeout_seconds
    last_err = None
    while time.time() < deadline:
        try:
            conn = psycopg.connect(
                dbname=db_name,
                user=db_name,
                password=db_pass,
                host=host,
                port=port,
                sslmode="require",
                connect_timeout=5,
            )
            conn.close()
            logging.info("PostgreSQL connection verified (sslmode=require)")
            return
        except Exception as e:
            last_err = str(e)
            time.sleep(3)

    logging.error("Timed out waiting for PostgreSQL connectivity. Last error:")
    logging.error(last_err or "unknown")
    sys.exit(1)


def patch_settings_for_postgres(settings_py, db_name, db_pass):
    """
    Append override so it wins; include sslmode=require to satisfy pg_hba.
    """
    override = textwrap.dedent(
        f"""\
        # Opalstack PostgreSQL configuration
        DATABASES = {{
            "default": {{
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "{db_name}",
                "USER": "{db_name}",
                "PASSWORD": "{db_pass}",
                "HOST": "localhost",
                "PORT": "5432",
                "OPTIONS": {{"sslmode": "require"}},
            }}
        }}
        """
    )

    with open(settings_py, "r") as f:
        contents = f.read()

    if "Opalstack PostgreSQL configuration" in contents:
        logging.info(f"PostgreSQL override already present in {settings_py}, skipping")
        return

    with open(settings_py, "a") as f:
        f.write("\n" + override)

    logging.info(f"Appended PostgreSQL DATABASES override to {settings_py}")


def main():
    parser = argparse.ArgumentParser(description="Installs Wagtail on an Opalstack account")
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

    # Postgres (API-ready)
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

    # pip deps
    steps = [
        (f"{appdir}/env/bin/pip install --upgrade pip", "Failed upgrading pip"),
        (f"{appdir}/env/bin/pip install uwsgi", "Failed installing uwsgi"),
        (f'{appdir}/env/bin/pip install "django=={DJANGO_VERSION}"', "Failed installing Django"),
        (f'{appdir}/env/bin/pip install "wagtail=={WAGTAIL_VERSION}"', "Failed installing Wagtail"),
        (f'{appdir}/env/bin/pip install "psycopg[binary]"', "Failed installing psycopg"),
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

    # scaffold (bulletproof): do NOT precreate non-empty dir
    if os.path.exists(project_root) and not dir_is_empty(project_root):
        logging.error(f"{project_root} exists and is not empty; refusing to overlay. Delete it and rerun.")
        sys.exit(1)
    rmdir_if_empty(project_root)

    rc, _ = run_command(f"{appdir}/env/bin/wagtail start {PROJECT_NAME} {project_root}")
    if rc != 0:
        logging.error("Failed running wagtail start")
        sys.exit(1)
    logging.info(f"Populated Wagtail project directory {project_root}")

    # settings path
    settings_py = f"{project_root}/{PROJECT_NAME}/settings/base.py"
    if not os.path.exists(settings_py):
        settings_py = f"{project_root}/{PROJECT_NAME}/settings.py"
    if not os.path.exists(settings_py):
        logging.error("Could not locate Wagtail settings file; cannot configure database")
        sys.exit(1)

    # ALLOWED_HOSTS
    run_command(
        f'''sed -r -i "s/^ALLOWED_HOSTS\\s*=\\s*\\[[^\\]]*\\]/ALLOWED_HOSTS = \\['*'\\]/" {settings_py}'''
    )

    # STATIC_ROOT / MEDIA_ROOT
    with open(settings_py, "r") as f:
        contents = f.read()
    if "STATIC_ROOT" not in contents or "MEDIA_ROOT" not in contents:
        append_cfg = textwrap.dedent(
            """\
            # Opalstack defaults
            STATIC_ROOT = os.path.join(BASE_DIR, 'static_collected')
            MEDIA_ROOT = os.path.join(BASE_DIR, 'media')
            """
        )
        with open(settings_py, "a") as f:
            f.write("\n" + append_cfg)
        logging.info(f"Appended STATIC_ROOT/MEDIA_ROOT to {settings_py}")

    # DB config (sslmode=require)
    patch_settings_for_postgres(settings_py, db_name, db_pass)

    # Real DB connectivity wait (this is what you’re yelling about — correctly)
    wait_for_postgres(db_name, db_pass, host="localhost", port="5432", timeout_seconds=240)

    # uwsgi.ini + start/stop
    wsgi_file = f"{project_root}/{PROJECT_NAME}/wsgi.py"
    if not os.path.exists(wsgi_file):
        alt = f"{project_root}/config/wsgi.py"
        if os.path.exists(alt):
            wsgi_file = alt
    if not os.path.exists(wsgi_file):
        logging.error("Could not locate wsgi.py")
        sys.exit(1)

    uwsgi_conf = textwrap.dedent(
        f"""\
        [uwsgi]
        master = True
        http-socket = 127.0.0.1:{appinfo["port"]}
        env = LD_LIBRARY_PATH=/usr/sqlite330/lib
        virtualenv = {appdir}/env/
        daemonize = /home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/uwsgi.log
        pidfile = {appdir}/tmp/uwsgi.pid

        workers = 2
        threads = 2

        chdir = {project_root}
        python-path = {project_root}
        wsgi-file = {wsgi_file}
        touch-reload = {wsgi_file}
        """
    )
    create_file(f"{appdir}/uwsgi.ini", uwsgi_conf, perms=0o600)

    start_script = f"""#!/bin/bash
export TMPDIR={appdir}/tmp
export LD_LIBRARY_PATH=/usr/sqlite330/lib
mkdir -p {appdir}/tmp

PIDFILE="{appdir}/tmp/uwsgi.pid"
if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
  echo "uWSGI for {appinfo["name"]} already running."
  exit 99
fi

{appdir}/env/bin/uwsgi --ini {appdir}/uwsgi.ini
echo "Started uWSGI for {appinfo["name"]}."
"""
    create_file(f"{appdir}/start", start_script, perms=0o700)

    stop_script = f"""#!/bin/bash
PIDFILE="{appdir}/tmp/uwsgi.pid"
if [ ! -e "$PIDFILE" ]; then
  echo "$PIDFILE missing, maybe uWSGI is already stopped?"
  exit 99
fi

PID=$(cat $PIDFILE)
if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
  {appdir}/env/bin/uwsgi --stop $PIDFILE
  sleep 3
fi

if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
  echo "uWSGI did not stop, killing it."
  sleep 3
  kill -9 $PID
fi

rm -f $PIDFILE
echo "Stopped."
"""
    create_file(f"{appdir}/stop", stop_script, perms=0o700)

    # migrate (HARD FAIL if rc != 0)
    rc, _ = run_command(f"{appdir}/env/bin/python manage.py migrate", cwd=project_root)
    if rc != 0:
        logging.error("Django migrate failed; aborting.")
        sys.exit(1)

    # collectstatic (HARD FAIL)
    rc, _ = run_command(f"{appdir}/env/bin/python manage.py collectstatic --noinput", cwd=project_root)
    if rc != 0:
        logging.error("collectstatic failed; aborting.")
        sys.exit(1)

    logging.info("Applied migrations and collected static files")

    # cron keepalive
    m = random.randint(0, 9)
    croncmd = f"0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1"
    add_cronjob(croncmd)

    # README (plain text)
    readme = f"""Opalstack Wagtail Installation

Application name:
{args.app_name}

Python virtualenv:
{appdir}/env

Project directory:
{project_root}

PostgreSQL database:
Name: {db_name}
User: {db_name}
Password: {db_pass}
Host: localhost
Port: 5432
SSL: required

Logs:
uWSGI log:
home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/uwsgi.log

Next steps:
1) Connect this app to a site route in the Opalstack control panel.
2) Create an admin user:
   cd {project_root}
   source {appdir}/env/bin/activate
   python manage.py createsuperuser
3) Restart:
   {appdir}/stop
   {appdir}/start
"""
    create_file(f"{appdir}/README", readme, perms=0o600)

    # start it (HARD FAIL if rc != 0)
    rc, _ = run_command(f"{appdir}/start")
    if rc != 0:
        logging.error("Failed starting uWSGI")
        sys.exit(1)

    # mark installed
    api.post("/app/installed/", json.dumps([{"id": args.app_uuid}]))
    logging.info(f"Completed installation of Wagtail app {args.app_name}")


if __name__ == "__main__":
    main()
