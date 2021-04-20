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
CMD_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin','UMASK': '0002',}


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
        description='Installs Node.js web app on Opalstack account')
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
    logging.info(f'Started installation of Node.js app {args.app_name}')
    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)
    appinfo = api.get(f'/app/read/{args.app_uuid}')
    appdir = f'/home/{appinfo["app_user"]}/apps/{appinfo["name"]}'

    # make app.js
    NEWLINE = '\\n'
    appjs = textwrap.dedent(f'''\
            const http = require('http');

            const hostname = '127.0.0.1';
            const port = {appinfo["port"]};

            const server = http.createServer((req, res) => {{
              res.statusCode = 200;
              res.setHeader('Content-Type', 'text/plain');
              res.end('Hello World from Node.js{NEWLINE}');
            }});

            server.listen(port, hostname, () => {{
              console.log(`Server running at http://${{hostname}}:${{port}}/`);
            }});''')
    create_file(f'{appdir}/app.js', appjs, perms=0o600)

    # start script
    start_script = textwrap.dedent(f'''\
                #!/bin/bash
                export TMPDIR={appdir}/tmp
                mkdir -p {appdir}/tmp
                PIDFILE="{appdir}/tmp/node.pid"
                NODE=/bin/node

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["app_user"]} | grep -x -f $PIDFILE &> /dev/null); then
                  echo "Node.js for {appinfo["name"]} already running."
                  exit 99
                fi

                cd {appdir}
                /usr/sbin/daemonize -c {appdir} -e ~/logs/apps/{appinfo["name"]}/node_error.log -o ~/logs/apps/{appinfo["name"]}/node_output.log -p $PIDFILE $NODE app.js

                echo "Started Node.js for {appinfo["name"]}."
                ''')
    create_file(f'{appdir}/start', start_script, perms=0o700)

    # stop script
    stop_script = textwrap.dedent(f'''\
                #!/bin/bash
                PIDFILE="{appdir}/tmp/node.pid"

                if [ ! -e "$PIDFILE" ]; then
                    echo "$PIDFILE missing, maybe Node.js is already stopped?"
                    exit 99
                fi

                PID=$(cat $PIDFILE)

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["app_user"]} | grep -x -f $PIDFILE &> /dev/null); then
                  kill $PID
                  sleep 3
                fi

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["app_user"]} | grep -x -f $PIDFILE &> /dev/null); then
                  echo "Node.js did not stop, killing it."
                  sleep 3
                  kill -9 $PID
                fi
                rm -f $PIDFILE
                echo "Stopped."
                ''')
    create_file(f'{appdir}/stop', stop_script, perms=0o700)

    # cron
    croncmd = f'*/10 * * * * {appdir}/start > /dev/null 2>&1'
    cronjob = add_cronjob(croncmd)

    # make README
    readme = textwrap.dedent(f'''\
                # Opalstack Node.js README

                ## Controlling your app

                Start your app by running:

                   {appdir}/start

                Stop your app by running:

                   {appdir}/stop

                ## Installing modules

                If you want to install Node modules in your app directory:

                cd {appdir}
                npm install modulename

                ''')
    create_file(f'{appdir}/README', readme)

    # start it
    cmd = f'{appdir}/start'
    startit = run_command(cmd)

    # finished, push a notice
    msg = f'See README in app directory for more info.'
    payload = json.dumps({'id': args.app_uuid, 'init_created': True,
                          'note': msg})
    finished=api.post('/app/installed/', payload)

    logging.info(f'Completed installation of Node.js app {args.app_name}')


if __name__ == '__main__':
    main()
