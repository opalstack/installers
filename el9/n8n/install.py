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
from urllib.parse import urlparse

API_HOST = os.environ.get("API_URL").strip("https://").strip("http://")
API_BASE_URI = "/api/v1"

CMD_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "UMASK": "0002",
}


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
                logging.warn(
                    "Invalid username or password and no auth token provided, exiting."
                )
                sys.exit()
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
    logging.info(
        f"Downloading {url} as {localfile} with permissions {oct(perms)}"
    )
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
    logging.info(
        f"Downloaded {url} as {localfile} with permissions {oct(perms)}"
    )


def gen_password(length=20):
    """makes a random password"""
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def run_command(cmd, cwd=None, env=CMD_ENV):
    """runs a command, returns output"""
    logging.info(f"Running: {cmd}")
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


def main():
    """run it"""
    # grab args from cmd or env
    parser = argparse.ArgumentParser(
        description="Installs n8n automation app on Opalstack account"
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

    # init logging
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s",
    )

    # go!
    logging.info(f"Started installation of n8n app {args.app_name}")

    api = OpalstackAPITool(
        API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password
    )
    appinfo = api.get(f"/app/read/{args.app_uuid}")
    appdir = f'/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}'
    projectdir = f"{appdir}/n8n"

    # Make project dir
    cmd = f"mkdir -p {projectdir}"
    run_command(cmd)

    # package.json from community thread, but port from API
    package_data = {
        "name": "my-n8n",
        "version": "1.0.0",
        "description": "My n8n site",
        "scripts": {
            "start": f'PORT={appinfo["port"]} n8n start',
            "stop": f'PORT={appinfo["port"]} n8n stop',
        },
        "dependencies": {
            "n8n": "^1.106.3",
            "sqlite3": "^5.1.7",
        },
    }
    package_json = json.dumps(package_data, indent=2)
    create_file(f"{projectdir}/package.json", package_json, perms=0o600)

    # Build env: always build from source, and force node-gyp to use system Python 3
    # (RHEL 9 default /usr/bin/python3 -> Python 3.9 with distutils)
    CMD_ENV["NPM_CONFIG_BUILD_FROM_SOURCE"] = "true"
    CMD_ENV["NODE_GYP_FORCE_PYTHON"] = "/usr/bin/python3"
    CMD_ENV["PYTHON"] = "/usr/bin/python3"

    # Optional: mirror the forum step, but with the correct Python for EL9
    cmd = (
        "scl enable nodejs22 -- "
        "npm config set python /usr/bin/python3"
    )
    run_command(cmd, cwd=projectdir)

    # Install n8n and deps using NodeJS 22 SCL
    cmd = "scl enable nodejs22 -- npm install --build-from-source"
    run_command(cmd, cwd=projectdir)

    # start script: N8N_PORT + WEBHOOK_URL, NodeJS 22, npm start
    start_script = textwrap.dedent(
        f"""\
        #!/bin/bash
        cd "{projectdir}"

        # n8n port must match the app port assigned by Opalstack
        export N8N_PORT={appinfo["port"]}

        # IMPORTANT: set this to the actual URL of the site attached to this app, trailing slash
        # example: https://n8n.example.com/
        export WEBHOOK_URL="https://example.com/"

        scl enable nodejs22 -- npm start

        echo "Started n8n for {appinfo["name"]}."
        """
    )
    create_file(f"{appdir}/start", start_script, perms=0o700)

    # stop script using NodeJS 22 SCL
    stop_script = textwrap.dedent(
        f"""\
        #!/bin/bash
        cd "{projectdir}"

        scl enable nodejs22 -- npm stop

        echo "Stopped n8n for {appinfo["name"]}."
        """
    )
    create_file(f"{appdir}/stop", stop_script, perms=0o700)

    # README with post-install instructions
    readme = textwrap.dedent(
        f"""\
        # Opalstack n8n README (EL9)

        ## Post-install steps (IMPORTANT)

        1. Assign your `{args.app_name}` application to a site in your Opalstack control panel
           and make a note of that site's URL.

        2. Edit the `start` script in `{appdir}` and update the `WEBHOOK_URL` value so that
           it matches the URL you configured in step 1, including the trailing slash.

        3. SSH to the server as your app's shell user and run:

               {appdir}/stop   # stop the app if it's running
               {appdir}/start  # start n8n

        After the app has restarted, you should be able to access the n8n UI at the URL
        you configured for the site.

        ## Controlling your app

        Start your app:

            {appdir}/start

        Stop your app:

            {appdir}/stop
        """
    )
    create_file(f"{appdir}/README", readme, perms=0o600)

    # Mark app as installed (same pattern as Ghost installer)
    payload = json.dumps([{"id": args.app_uuid}])
    api.post("/app/installed/", payload)

    logging.info(f"Completed installation of n8n app {args.app_name}")


if __name__ == "__main__":
    main()
