#! /usr/bin/python3.6

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
        'PATH': '/usr/sqlite330/bin:/usr/local/bin:/usr/bin:/bin',
        'UMASK': '0002',
        'LD_LIBRARY_PATH': '/usr/sqlite330/lib',
}

class OpalstackAPITool():
    """simple wrapper for http.client get and post"""
    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host
        self.base_uri = base_uri

        # if there is no auth token, then try to log in with provided credentials
        if not authtoken:
            endpoint = self.base_uri + '/login/'
            payload = json.dumps({
                'username': user,
                'password': password
            })
            conn = http.client.HTTPSConnection(self.host)
            conn.request('POST', endpoint, payload,
                         headers={'Content-type': 'application/json'})
            result = json.loads(conn.getresponse().read())
            if not result.get('token'):
                logging.warn('Invalid username or password and no auth token provided, exiting.')
                sys.exit()
            else:
                authtoken = result['token']

        self.headers = {
            'Content-type': 'application/json',
            'Authorization': f'Token {authtoken}'
        }

    def get(self, endpoint):
        """GETs an API endpoint"""
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request('GET', endpoint, headers=self.headers)
        return json.loads(conn.getresponse().read())

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
    return ''.join(secrets.choice(chars) for i in range(length))

def run_command(cmd, env=CMD_ENV):
    """runs a command, returns output"""
    logging.info(f'Running: {cmd}')
    try:
        result = subprocess.check_output(shlex.split(cmd), env=env)
    except subprocess.CalledProcessError as e:
        logging.debug(e.output)
    return result

def add_cronjob(cronjob):
    """appends a cron job to the user's crontab"""
    homedir = os.path.expanduser('~')
    tmpname = f'{homedir}/.tmp{gen_password()}'
    tmp = open(tmpname, 'w')
    subprocess.run('crontab -l'.split(),stdout=tmp)
    tmp.write(f'{cronjob}\n')
    tmp.close()
    cmd = f'crontab {tmpname}'
    doit = run_command(cmd)
    cmd = run_command(f'rm -f {tmpname}')
    logging.info(f'Added cron job: {cronjob}')

def main():
    """run it"""
    # grab args from cmd or env
    parser = argparse.ArgumentParser(
        description='Installs FastAPI on Opalstack account')
    parser.add_argument('-i', dest='app_uuid', help='UUID of the base app',
                        default=os.environ.get('UUID'))
    parser.add_argument('-n', dest='app_name', help='name of the base app',
                        default=os.environ.get('APPNAME'))
    parser.add_argument('-t', dest='opal_token', help='API auth token',
                        default=os.environ.get('OPAL_TOKEN'))
    parser.add_argument('-u', dest='opal_user', help='Opalstack account name',
                        default=os.environ.get('OPAL_USER'))
    parser.add_argument('-p', dest='opal_password', help='Opalstack account password',
                        default=os.environ.get('OPAL_PASS'))
    args = parser.parse_args()

    # init logging
    logging.basicConfig(level=logging.INFO,
                        format='[%(asctime)s] %(levelname)s: %(message)s')
    # go!
    logging.info(f'Started installation of FastAPI app {args.app_name}')
    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)
    appinfo = api.get(f'/app/read/{args.app_uuid}')
    appdir = f'/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}'

    # create tmp dir
    os.mkdir(f'{appdir}/tmp', 0o700)
    logging.info(f'Created directory {appdir}/tmp')
    CMD_ENV['TMPDIR'] = f'{appdir}/tmp'

    # create virtualenv
    cmd = f'/usr/local/bin/python3.11 -m venv {appdir}/env'
    doit = run_command(cmd)
    logging.info(f'Created virtualenv at {appdir}/env')

    # install uvicorn
    cmd = f'{appdir}/env/bin/pip install uvicorn'
    doit = run_command(cmd)
    perms = run_command(f'chmod 700 {appdir}/env/bin/uvicorn')
    logging.info('Installed latest Uvicorn into virtualenv')

    # install gunicorn
    cmd = f'{appdir}/env/bin/pip install gunicorn'
    doit = run_command(cmd)
    perms = run_command(f'chmod 700 {appdir}/env/bin/gunicorn')
    logging.info('Installed latest Gunicorn into virtualenv')

    # install FastAPI
    cmd = f'{appdir}/env/bin/pip install fastapi'
    doit = run_command(cmd)
    logging.info('Installed latest FastAPI into virtualenv')

    # create project dir
    os.mkdir(f'{appdir}/main', 0o700)
    logging.info(f'Created FastAPI project directory {appdir}/main')

    # FastAPI example
    fastapi_example = textwrap.dedent('''\
                from typing import Union

                from fastapi import FastAPI

                app = FastAPI()


                @app.get("/")
                def read_root():
                    return {"Hello": "World"}


                @app.get("/items/{item_id}")
                def read_item(item_id: int, q: Union[str, None] = None):
                    return {"item_id": item_id, "q": q}
                ''')
    create_file(f'{appdir}/main/__init__.py', fastapi_example, perms=0o600)

    # start script
    start_script = textwrap.dedent(f'''\
                #!/bin/bash
                export TMPDIR={appdir}/tmp
                export LD_LIBRARY_PATH=/usr/sqlite330/lib
                mkdir -p {appdir}/tmp
                PIDFILE="{appdir}/tmp/gunicorn.pid"

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
                  echo "Gunicorn for {appinfo["name"]} already running."
                  exit 99
                fi

                {appdir}/env/bin/gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app -D -p $PIDFILE

                echo "Started Gunicorn for {appinfo["name"]}."
                ''')
    create_file(f'{appdir}/start', start_script, perms=0o700)

    # stop script
    stop_script = textwrap.dedent(f'''\
                #!/bin/bash
                PIDFILE="{appdir}/tmp/gunicorn.pid"

                if [ ! -e "$PIDFILE" ]; then
                    echo "$PIDFILE missing, maybe Gunicorn is already stopped?"
                    exit 99
                fi

                PID=$(cat $PIDFILE)

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
                  echo "Stopping Gunicorn."
                  sleep 3
                  kill -9 $PID
                fi
                rm -f $PIDFILE
                echo "Stopped."
                ''')
    create_file(f'{appdir}/stop', stop_script, perms=0o700)

    # cron
    m = random.randint(0,9)
    croncmd = f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1'
    cronjob = add_cronjob(croncmd)

    # make README
    readme = textwrap.dedent(f'''\
                # Opalstack FastAPI README

                ## Post-install steps

                Please take the following steps before you begin to use your FastAPI
                installation:

                1. Connect your FastAPI application to a site route in the control panel.

                2. Run the following commands to restart your FastAPI instance:

                   {appdir}/stop
                   {appdir}/start
                ''')
    create_file(f'{appdir}/README', readme)

    # start it
    cmd = f'{appdir}/start'
    startit = run_command(cmd)

    # finished, push a notice with credentials
    msg = f'See README in app directory for final steps.'
    payload = json.dumps([{'id': args.app_uuid }])
    finished=api.post('/app/installed/', payload)

    logging.info(f'Completed installation of FastAPI app {args.app_name}')

if __name__ == '__main__':
    main()
