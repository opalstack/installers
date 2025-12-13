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
}

DJANGO_VERSION = "5.2.5"
WAGTAIL_VERSION = "7.2.1"
PROJECT_NAME = "mysite"


class OpalstackAPITool:
    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host
        self.base_uri = base_uri

        if not authtoken:
            payload = json.dumps({"username": user, "password": password})
            conn = http.client.HTTPSConnection(self.host)
            conn.request("POST", self.base_uri + "/login/", payload, headers={"Content-type": "application/json"})
            result = json.loads(conn.getresponse().read())
            if not result.get("token"):
                sys.exit(1)
            authtoken = result["token"]

        self.headers = {
            "Content-type": "application/json",
            "Authorization": f"Token {authtoken}",
        }

    def get(self, endpoint):
        conn = http.client.HTTPSConnection(self.host)
        conn.request("GET", self.base_uri + endpoint, headers=self.headers)
        return json.loads(conn.getresponse().read())

    def post(self, endpoint, payload):
        conn = http.client.HTTPSConnection(self.host)
        conn.request("POST", self.base_uri + endpoint, payload, headers=self.headers)
        return json.loads(conn.getresponse().read())


def create_file(path, contents, perms=0o600):
    with open(path, "w") as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f"Created file {path}")


def gen_password(length=20):
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def run_command(cmd, cwd=None):
    logging.info(f"Running: {cmd}")
    r = subprocess.run(
        shlex.split(cmd),
        cwd=cwd,
        env=CMD_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if r.returncode != 0:
        logging.error(r.stdout.decode("utf-8", errors="replace"))
    return r.returncode, r.stdout


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


def create_postgres_db(api, appinfo, args):
    db_name = f"{args.app_name[:8]}_{args.app_uuid[:8]}"
    db_pass = None
    psql_user_id = None

    payload = json.dumps([{"server": appinfo["server"], "name": db_name}])

    for _ in range(10):
        logging.info(f"Trying to create database user {db_name}")
        resp = api.post("/psqluser/create/", payload)
        if resp and isinstance(resp, list) and "default_password" in resp[0]:
            db_pass = resp[0]["default_password"]

        time.sleep(5)
        for u in api.get("/psqluser/list/"):
            if u.get("name") == db_name and u.get("ready"):
                psql_user_id = u["id"]
                break
        if psql_user_id:
            break

    if not psql_user_id or not db_pass:
        sys.exit(1)

    payload = json.dumps([{
        "server": appinfo["server"],
        "name": db_name,
        "dbusers_readwrite": [psql_user_id],
    }])

    for _ in range(10):
        logging.info(f"Trying to create database {db_name}")
        api.post("/psqldb/create/", payload)
        time.sleep(5)
        for d in api.get("/psqldb/list/"):
            if d.get("name") == db_name and d.get("ready"):
                return db_name, db_pass

    sys.exit(1)


def patch_settings_for_postgres(settings_py, db_name, db_pass):
    override = f"""
# Opalstack PostgreSQL configuration
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
    with open(settings_py, "r") as f:
        if "Opalstack PostgreSQL configuration" in f.read():
            return

    with open(settings_py, "a") as f:
        f.write(override)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", dest="app_uuid", default=os.environ.get("UUID"))
    parser.add_argument("-n", dest="app_name", default=os.environ.get("APPNAME"))
    parser.add_argument("-t", dest="opal_token", default=os.environ.get("OPAL_TOKEN"))
    parser.add_argument("-u", dest="opal_user", default=os.environ.get("OPAL_USER"))
    parser.add_argument("-p", dest="opal_password", default=os.environ.get("OPAL_PASS"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)
    appinfo = api.get(f"/app/read/{args.app_uuid}")

    appdir = f"/home/{appinfo['osuser_name']}/apps/{appinfo['name']}"
    project_root = f"{appdir}/{PROJECT_NAME}"

    ensure_dir(f"{appdir}/tmp")
    CMD_ENV["TMPDIR"] = f"{appdir}/tmp"

    db_name, db_pass = create_postgres_db(api, appinfo, args)

    rc, out = run_command("which python3.12")
    if rc != 0:
        sys.exit(1)
    python = out.decode().strip()

    if run_command(f"{python} -m venv {appdir}/env")[0] != 0:
        sys.exit(1)

    for cmd in [
        f"{appdir}/env/bin/pip install --upgrade pip",
        f"{appdir}/env/bin/pip install uwsgi",
        f"{appdir}/env/bin/pip install django=={DJANGO_VERSION}",
        f"{appdir}/env/bin/pip install wagtail=={WAGTAIL_VERSION}",
        f"{appdir}/env/bin/pip install psycopg[binary]",
    ]:
        if run_command(cmd)[0] != 0:
            sys.exit(1)

    if os.path.exists(project_root) and not dir_is_empty(project_root):
        sys.exit(1)

    ensure_dir(project_root)
    if run_command(f"{appdir}/env/bin/wagtail start {PROJECT_NAME} {project_root}")[0] != 0:
        sys.exit(1)

    settings_py = f"{project_root}/{PROJECT_NAME}/settings/base.py"
    if not os.path.exists(settings_py):
        settings_py = f"{project_root}/{PROJECT_NAME}/settings.py"

    patch_settings_for_postgres(settings_py, db_name, db_pass)

    run_command(f"{appdir}/env/bin/python manage.py migrate", cwd=project_root)
    run_command(f"{appdir}/env/bin/python manage.py collectstatic --noinput", cwd=project_root)

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

Next steps:
- Connect this app to a domain in the control panel
- Create an admin user by running:
  cd {project_root}
  source {appdir}/env/bin/activate
  python manage.py createsuperuser
- Restart the app using the start and stop scripts
"""
    create_file(f"{appdir}/README", readme)

    run_command(f"{appdir}/start")
    api.post("/app/installed/", json.dumps([{"id": args.app_uuid}]))


if __name__ == "__main__":
    main()
