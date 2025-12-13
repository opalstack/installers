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
from urllib.parse import urlparse

API_HOST = os.environ.get("API_URL").strip("https://").strip("http://")
API_BASE_URI = "/api/v1"

CMD_ENV = {
    "PATH": "/usr/sqlite330/bin:/usr/local/bin:/usr/bin:/bin",
    "UMASK": "0002",
    "LD_LIBRARY_PATH": "/usr/sqlite330/lib",
    # TMPDIR is set after we know appdir
}

DJANGO_VERSION = "5.2.5"
WAGTAIL_VERSION = "7.2.1"
PROJECT_NAME = "mysite"


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
            conn.request("POST", endpoint, payload, headers={"Content-type": "application/json"})
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
        return json.loads(conn.getresponse().read())

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
    conn = http.client.HTTPConnection(u.netloc) if u.scheme == "http" else http.client.HTTPSConnection(u.netloc)
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


def run_command(cmd, cwd=None, env=CMD_ENV):
    """runs a command, returns output (bytes). does NOT throw; caller should validate output if needed."""
    logging.info(f"Running: {cmd}")
    try:
        result = subprocess.check_output(
            shlex.split(cmd),
            cwd=cwd,
            env=env,
            stderr=subprocess.STDOUT,
        )
        return result
    except subprocess.CalledProcessError as e:
        out = e.output or b""
        logging.error(out.decode("utf-8", errors="replace"))
        return out


def run_command_or_die(cmd, cwd=None, env=CMD_ENV, err="Command failed"):
    out = run_command(cmd, cwd=cwd, env=env)
    # crude but reliable: if it printed typical CalledProcessError output, we already logged it;
    # still, we need a hard fail for critical steps. check return code by re-running via subprocess.run.
    r = subprocess.run(shlex.split(cmd), cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        logging.error(r.stdout.decode("utf-8", errors="replace"))
        logging.error(err)
        sys.exit(1)
    return out


def add_cronjob(cronjob):
    """appends a cron job to the user's crontab"""
    homedir = os.path.expanduser("~")
    tmpname = f"{homedir}/.tmp{gen_password()}"
    tmp = open(tmpname, "w")
    subprocess.run("crontab -l".split(), stdout=tmp)
    tmp.write(f"{cronjob}\n")
    tmp.close()
    run_command(f"crontab {tmpname}")
    run_command(f"rm -f {tmpname}")
    logging.info(f"Added cron job: {cronjob}")


def ensure_dir(path, perms=0o700):
    if not os.path.exists(path):
        os.makedirs(path, mode=perms, exist_ok=True)
        os.chmod(path, perms)
        logging.info(f"Created directory {path}")


def create_postgres_db(api, appinfo, args):
    """
    Follow the n8n installer pattern:
    - create psql user, capture default_password from create response
    - poll /psqluser/list until ready
    - create db with dbusers_readwrite
    - poll /psqldb/list until ready
    """
    db_name = f"{args.app_name[:8]}_{args.app_uuid[:8]}"
    db_pass = None
    psql_user_id = None

    # create database user
    payload = json.dumps([{"server": appinfo["server"], "name": db_name}])
    user_attempts = 0
    while True:
        logging.info(f"Trying to create database user {db_name}")
        psql_user = api.post("/psqluser/create/", payload)

        # Capture the auto-generated password from the API response
        if psql_user and len(psql_user) > 0 and "default_password" in psql_user[0]:
            db_pass = psql_user[0]["default_password"]
            logging.info("Received database password from API")

        time.sleep(5)
        existing_psql_users = api.get("/psqluser/list/")
        for check in json.loads(json.dumps(existing_psql_users)):
            if check.get("name") == db_name and check.get("ready"):
                psql_user_id = check.get("id")
                logging.info(f"Database user {db_name} created with ID {psql_user_id}")
                break

        if psql_user_id:
            break

        user_attempts += 1
        if user_attempts > 10:
            logging.error(f"Could not create database user {db_name}")
            sys.exit(1)

    if not db_pass:
        logging.error("Failed to retrieve database password from API")
        sys.exit(1)
    if not psql_user_id:
        logging.error("Failed to retrieve database user ID")
        sys.exit(1)

    # create database
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
            if check.get("name") == db_name and check.get("ready"):
                logging.info(f"Database {db_name} created and user permissions assigned")
                db_created = True
                break

        if db_created:
            break

        db_attempts += 1
        if db_attempts > 10:
            logging.error(f"Could not create database {db_name}")
            sys.exit(1)

    return db_name, db_pass


def patch_settings_for_postgres(settings_py, db_name, db_pass):
    """
    Wagtail start template typically has a DATABASES dict in settings/base.py.
    We avoid brittle regex replacement by appending a final DATABASES override
    (last assignment wins).
    """
    override = textwrap.dedent(
        f"""
        # --- Opalstack: force PostgreSQL (installer managed) ---
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
        """
    ).lstrip("\n")

    with open(settings_py, "r") as f:
        contents = f.read()

    if "Opalstack: force PostgreSQL" in contents:
        logging.info(f"PostgreSQL override already present in {settings_py}, skipping re-append")
        return

    with open(settings_py, "a") as f:
        f.write("\n" + override)

    logging.info(f"Appended PostgreSQL DATABASES override to {settings_py}")


def main():
    parser = argparse.ArgumentParser(description="Installs Wagtail (Django CMS) on an Opalstack account")
    parser.add_argument("-i", dest="app_uuid", help="UUID of the base app", default=os.environ.get("UUID"))
    parser.add_argument("-n", dest="app_name", help="name of the base app", default=os.environ.get("APPNAME"))
    parser.add_argument("-t", dest="opal_token", help="API auth token", default=os.environ.get("OPAL_TOKEN"))
    parser.add_argument("-u", dest="opal_user", help="Opalstack account name", default=os.environ.get("OPAL_USER"))
    parser.add_argument("-p", dest="opal_password", help="Opalstack account password", default=os.environ.get("OPAL_PASS"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

    logging.info(f"Started installation of Wagtail app {args.app_name}")
    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)

    appinfo = api.get(f"/app/read/{args.app_uuid}")
    appdir = f'/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}'
    project_root = f"{appdir}/{PROJECT_NAME}"

    # tmp dir
    ensure_dir(f"{appdir}/tmp", 0o700)
    CMD_ENV["TMPDIR"] = f"{appdir}/tmp"

    # create Postgres DB/user (n8n pattern)
    db_name, db_pass = create_postgres_db(api, appinfo, args)

    # create virtualenv
    python_executable_path = run_command("which python3.12").decode("utf-8", errors="replace").strip()
    if not python_executable_path:
        logging.error("python3.12 not found on PATH")
        sys.exit(1)

    run_command_or_die(f"{python_executable_path} -m venv {appdir}/env", err="Failed creating virtualenv")
    logging.info(f"Created virtualenv at {appdir}/env")

    # install uwsgi + deps
    run_command_or_die(f"{appdir}/env/bin/pip install --upgrade pip", err="Failed upgrading pip")
    run_command_or_die(f"{appdir}/env/bin/pip install uwsgi", err="Failed installing uwsgi")
    run_command(f"chmod 700 {appdir}/env/bin/uwsgi")
    logging.info("Installed latest uWSGI into virtualenv")

    # install django + wagtail + postgres driver
    run_command_or_die(f'{appdir}/env/bin/pip install "django=={DJANGO_VERSION}"', err="Failed installing Django")
    run_command_or_die(f'{appdir}/env/bin/pip install "wagtail=={WAGTAIL_VERSION}"', err="Failed installing Wagtail")
    # psycopg (modern) with binary wheels; fallback to psycopg2-binary if you prefer older
    run_command_or_die(f'{appdir}/env/bin/pip install "psycopg[binary]"', err="Failed installing psycopg")
    logging.info(f"Installed Django {DJANGO_VERSION}, Wagtail {WAGTAIL_VERSION}, psycopg into virtualenv")

    # create project dir + scaffold
    #ensure_dir(project_root, 0o700)
    run_command_or_die(f"{appdir}/env/bin/wagtail start {PROJECT_NAME} {project_root}", err="Failed running wagtail start")
    logging.info(f"Populated Wagtail project directory {project_root}")

    # locate settings
    settings_py = f"{project_root}/{PROJECT_NAME}/settings/base.py"
    if not os.path.exists(settings_py):
        settings_py = f"{project_root}/{PROJECT_NAME}/settings.py"

    # relaxed bootstrap host + static/media roots + postgres config
    if os.path.exists(settings_py):
        run_command(
            f'''sed -r -i "s/^ALLOWED_HOSTS\\s*=\\s*\\[[^\\]]*\\]/ALLOWED_HOSTS = \\['*'\\]/" {settings_py}'''
        )

        append_cfg = textwrap.dedent(
            f"""
            # --- Opalstack defaults ---
            STATIC_ROOT = os.path.join(BASE_DIR, 'static_collected')
            MEDIA_ROOT = os.path.join(BASE_DIR, 'media')
            """
        ).lstrip("\n")

        with open(settings_py, "r") as f:
            contents = f.read()
        if "STATIC_ROOT" not in contents or "MEDIA_ROOT" not in contents:
            with open(settings_py, "a") as f:
                f.write("\n" + append_cfg)
            logging.info(f"Appended STATIC_ROOT/MEDIA_ROOT to {settings_py}")

        # force postgres
        patch_settings_for_postgres(settings_py, db_name, db_pass)

    else:
        logging.error("Could not locate Wagtail settings file; cannot configure database")
        sys.exit(1)

    # uwsgi config
    wsgi_file = f"{project_root}/{PROJECT_NAME}/wsgi.py"
    if not os.path.exists(wsgi_file):
        alt = f"{project_root}/config/wsgi.py"
        if os.path.exists(alt):
            wsgi_file = alt

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

        # project
        chdir = {project_root}
        python-path = {project_root}
        wsgi-file = {wsgi_file}
        touch-reload = {wsgi_file}
        """
    )
    create_file(f"{appdir}/uwsgi.ini", uwsgi_conf, perms=0o600)

    # start script (shebang must be first byte)
    start_script = textwrap.dedent(
        f"""\
#!/bin/bash
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
    )
    create_file(f"{appdir}/start", start_script, perms=0o700)

    # stop script (shebang must be first byte)
    stop_script = textwrap.dedent(
        f"""\
#!/bin/bash
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
    )
    create_file(f"{appdir}/stop", stop_script, perms=0o700)

    # run migrations + collectstatic NOW that DB is configured
    run_command_or_die(f"{appdir}/env/bin/python manage.py migrate", cwd=project_root, err="Django migrate failed")
    run_command_or_die(
        f"{appdir}/env/bin/python manage.py collectstatic --noinput",
        cwd=project_root,
        err="collectstatic failed",
    )
    logging.info("Applied migrations and collected static files")

    # cron (keep it alive)
    m = random.randint(0, 9)
    croncmd = f"0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1"
    add_cronjob(croncmd)

    # README
    readme = textwrap.dedent(
        f"""\
        # Opalstack Wagtail README

        This installer created:
        - Python virtualenv: `{appdir}/env`
        - Wagtail project: `{project_root}`
        - uWSGI config: `{appdir}/uwsgi.ini`
        - PostgreSQL DB/user (installer managed):
          - DB name: `{db_name}`
          - DB user: `{db_name}`
          - DB password: `{db_pass}`

        ## Post-install steps

        1. Connect this app to a site route in the Opalstack control panel.

        2. Create an admin user:
           ```bash
           cd {project_root}
           source {appdir}/env/bin/activate
           python manage.py createsuperuser
           ```

        3. Restart your instance:
           ```bash
           {appdir}/stop
           {appdir}/start
           ```

        ## Files

        - Settings file updated for Postgres:
          - {settings_py}

        - Logs:
          - uWSGI: /home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/uwsgi.log
        """
    )
    create_file(f"{appdir}/README", readme, perms=0o600)

    # start it
    run_command_or_die(f"{appdir}/start", err="Failed starting uWSGI")

    # mark installed
    payload = json.dumps([{"id": args.app_uuid}])
    api.post("/app/installed/", payload)
    logging.info(f"Completed installation of Wagtail app {args.app_name}")


if __name__ == "__main__":
    main()
