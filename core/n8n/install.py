#!/usr/local/bin/python3.11
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
from urllib.parse import urlparse

API_HOST = os.environ.get('API_URL').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'

CMD_ENV = {
    'PATH': '/usr/local/bin:/usr/bin:/bin',
    'UMASK': '0002',
}


class OpalstackAPITool:
    """simple wrapper for http.client get and post"""

    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host
        self.base_uri = base_uri

        # if there is no auth token, then try to log in with provided credentials
        if not authtoken:
            endpoint = self.base_uri + '/login/'
            payload = json.dumps({
                'username': user,
                'password': password,
            })
            conn = http.client.HTTPSConnection(self.host)
            conn.request('POST', endpoint, payload,
                         headers={'Content-type': 'application/json'})
            result = json.loads(conn.getresponse().read())
            if not result.get('token'):
                logging.warn(
                    'Invalid username or password and no auth token provided, exiting.'
                )
                sys.exit()
            else:
                authtoken = result['token']

        self.headers = {
            'Content-type': 'application/json',
            'Authorization': f'Token {authtoken}',
        }

    def get(self, endpoint):
        """GETs an API endpoint"""
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request('GET', endpoint, headers=self.headers)
        connread = conn.getresponse().read()
        logging.info(connread)
        return json.loads(connread)

    def post(self, endpoint, payload):
        """POSTs data to an API endpoint"""
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request('POST', endpoint, payload, headers=self.headers)
        return json.loads(conn.getresponse().read())


def create_file(path, contents, writemode='w', perms=0o600):
    """make a file, perms are passed as octal"""
    with open(path, writemode) as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f'Created file {path} with permissions {oct(perms)}')


def download(url, localfile, writemode='wb', perms=0o600):
    """save a remote file, perms are passed as octal"""
    logging.info(f'Downloading {url} as {localfile} with permissions {oct(perms)}')
    u = urlparse(url)
    if u.scheme == 'http':
        conn = http.client.HTTPConnection(u.netloc)
    else:
        conn = http.client.HTTPSConnection(u.netloc)
    conn.request('GET', u.path)
    r = conn.getresponse()
    with open(localfile, writemode) as f:
        while True:
            data = r.read(4096)
            if data:
                f.write(data)
            else:
                break
    os.chmod(localfile, perms)
    logging.info(f'Downloaded {url} as {localfile} with permissions {oct(perms)}')


def gen_password(length=20):
    """makes a random password"""
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))


def run_command(cmd, cwd=None, env=CMD_ENV):
    """runs a command, returns output (logs errors but does not abort)"""
    logging.info(f'Running: {cmd}')
    try:
        result = subprocess.check_output(
            shlex.split(cmd),
            cwd=cwd,
            env=env,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as e:
        logging.error(f'Command failed: {cmd}')
        logging.error(e.output)
        result = e.output
    return result


def add_cronjob(cronjob):
    """appends a cron job to the user's crontab"""
    homedir = os.path.expanduser('~')
    tmpname = f'{homedir}/.tmp{gen_password()}'
    tmp = open(tmpname, 'w')
    subprocess.run('crontab -l'.split(), stdout=tmp)
    tmp.write(f'{cronjob}\n')
    tmp.close()
    cmd = f'crontab {tmpname}'
    _ = run_command(cmd)
    _ = run_command(f'rm -f {tmpname}')
    logging.info(f'Added cron job: {cronjob}')


def main():
    """run it"""
    parser = argparse.ArgumentParser(
        description='Installs n8n web app on Opalstack EL7 account'
    )
    parser.add_argument(
        '-i',
        dest='app_uuid',
        help='UUID of the base app',
        default=os.environ.get('UUID'),
    )
    parser.add_argument(
        '-n',
        dest='app_name',
        help='name of the base app',
        default=os.environ.get('APPNAME'),
    )
    parser.add_argument(
        '-t',
        dest='opal_token',
        help='API auth token',
        default=os.environ.get('OPAL_TOKEN'),
    )
    parser.add_argument(
        '-u',
        dest='opal_user',
        help='Opalstack account name',
        default=os.environ.get('OPAL_USER'),
    )
    parser.add_argument(
        '-p',
        dest='opal_password',
        help='Opalstack account password',
        default=os.environ.get('OPAL_PASS'),
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s: %(message)s',
    )

    logging.info(f'Started installation of n8n app {args.app_name}')

    api = OpalstackAPITool(
        API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password
    )
    appinfo = api.get(f'/app/read/{args.app_uuid}')
    appdir = f'/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}'

    # ------------------------------------------------------------------
    # Create n8n project and package.json (forum recipe + app port)
    # ------------------------------------------------------------------
    cmd = f'mkdir -p {appdir}/n8n'
    _ = run_command(cmd)

    pkgjson = textwrap.dedent(
        f'''\
        {{
          "name": "my-n8n",
          "version": "1.0.0",
          "description": "My n8n site",
          "scripts": {{
            "start": "PORT={appinfo["port"]} n8n start",
            "stop": "PORT={appinfo["port"]} n8n stop"
          }},
          "dependencies": {{
            "n8n": "^1.106.3",
            "sqlite3": "^5.1.7"
          }}
        }}
        '''
    )

    create_file(f'{appdir}/n8n/package.json', pkgjson, perms=0o600)

    # ------------------------------------------------------------------
    # Install n8n + deps with Node 20 + devtoolset-11 + distutils Python
    # ------------------------------------------------------------------
    distutils_python = '/usr/local/bin/python3.11'

    CMD_ENV['NPM_CONFIG_BUILD_FROM_SOURCE'] = 'true'
    CMD_ENV['NODE_GYP_FORCE_PYTHON'] = distutils_python
    CMD_ENV['PYTHON'] = distutils_python
    CMD_ENV['npm_config_python'] = distutils_python

    # Optional sanity check: does this Python have distutils?
    _ = run_command(
        f'{distutils_python} -c "import distutils; print(\'distutils-ok\')"'
    )

    # npm install --build-from-source under devtoolset-11 + nodejs20
    cmd = 'scl enable devtoolset-11 nodejs20 -- npm install --build-from-source'
    _ = run_command(cmd, cwd=f'{appdir}/n8n')

    # ------------------------------------------------------------------
    # start / stop scripts (daemonize, like core Node installer)
    # ------------------------------------------------------------------
    start_script = textwrap.dedent(
        f'''\
        #!/bin/bash

        APPNAME={appinfo["name"]}

        # set node version via scl
        source scl_source enable nodejs20
        NODE=$( which node )
        NPM=$( which npm )

        # n8n project info
        PROJECT=n8n
        APPDIR=$HOME/apps/$APPNAME
        LOGDIR=$HOME/logs/apps/$APPNAME
        TMPDIR=$APPDIR/tmp
        PROJECTDIR=$APPDIR/$PROJECT
        PIDFILE=$TMPDIR/node.pid

        mkdir -p "$TMPDIR"
        mkdir -p "$LOGDIR"

        if [ -e "$PIDFILE" ] && (pgrep -F "$PIDFILE" &> /dev/null); then
          echo "$APPNAME already running."
          exit 99
        fi

        # n8n listens on the app port; set it explicitly
        export N8N_PORT={appinfo["port"]}

        # IMPORTANT: set this to the public URL you will use for this app
        # e.g. https://n8n.example.com/
        # export WEBHOOK_URL=https://n8n.example.com/

        STARTCMD="$NPM start"

        /usr/sbin/daemonize \\
          -c "$PROJECTDIR" \\
          -a \\
          -e "$LOGDIR/error.log" \\
          -o "$LOGDIR/console.log" \\
          -p "$PIDFILE" \\
          $STARTCMD

        echo "Started n8n for $APPNAME."
        '''
    )
    create_file(f'{appdir}/start', start_script, perms=0o700)

    stop_script = textwrap.dedent(
        f'''\
        #!/bin/bash

        APPNAME={appinfo["name"]}
        PIDFILE="$HOME/apps/$APPNAME/tmp/node.pid"

        if [ ! -e "$PIDFILE" ]; then
          echo "$PIDFILE missing, maybe $APPNAME is already stopped?"
          exit 99
        fi

        if [ -e "$PIDFILE" ] && (pgrep -F "$PIDFILE" &> /dev/null); then
          pkill -g "$(cat "$PIDFILE")"
          sleep 3
        fi

        if [ -e "$PIDFILE" ] && (pgrep -F "$PIDFILE" &> /dev/null); then
          echo "$APPNAME did not stop, killing it."
          sleep 3
          pkill -9 -g "$(cat "$PIDFILE")"
        fi

        rm -f "$PIDFILE"
        echo "Stopped n8n for $APPNAME."
        '''
    )
    create_file(f'{appdir}/stop', stop_script, perms=0o700)

    # ------------------------------------------------------------------
    # Cron to keep it running (same pattern as other installers)
    # ------------------------------------------------------------------
    m = random.randint(0, 9)
    croncmd = (
        f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * '
        f'{appdir}/start > /dev/null 2>&1'
    )
    _ = add_cronjob(croncmd)

    # ------------------------------------------------------------------
    # README
    # ------------------------------------------------------------------
    readme = textwrap.dedent(
        f'''\
        # Opalstack n8n README (EL7, nodejs20 + devtoolset-11)

        n8n is installed into:

          {appdir}/n8n

        It runs as a Node.js app on port {appinfo["port"]} using nodejs20
        (with devtoolset-11 for native builds and Python 3.11 with distutils).

        ## Controlling your app

        Start your app by running:

          {appdir}/start

        Stop your app by running:

          {appdir}/stop

        Logs are written to:

          /home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/console.log
          /home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/error.log

        ## WEBHOOK_URL

        The start script automatically sets:

          N8N_PORT={appinfo["port"]}

        You **must** edit the start script and set WEBHOOK_URL to the public
        URL you will use for this app, for example:

          export WEBHOOK_URL=https://n8n.example.com/

        After updating WEBHOOK_URL run:

          {appdir}/stop
          {appdir}/start

        to apply the change.
        '''
    )
    create_file(f'{appdir}/README', readme, perms=0o600)

    # Start it once
    cmd = f'{appdir}/start'
    _ = run_command(cmd)

    # Notify API that install is complete
    payload = json.dumps([{'id': args.app_uuid}])
    _ = api.post('/app/installed/', payload)

    logging.info(f'Completed installation of n8n app {args.app_name}')


if __name__ == '__main__':
    main()
