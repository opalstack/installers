#! /usr/bin/python3

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
from urllib.parse import urlparse

API_HOST = 'my.opalstack.com'
API_BASE_URI = '/api/v0'
GITEA_URL = 'https://dl.gitea.io/gitea/1.8/gitea-1.8-linux-amd64'


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


def run_command(cmd):
    """runs a command, returns output"""
    logging.info(f'Running: {cmd}')
    return subprocess.check_output(cmd.split())

def add_cronjob(cronjob):
    """appends a cron job to the user's crontab"""
    homedir = os.path.expanduser('~')
    tmpname = f'{homedir}/.tmp{gen_password()}'
    tmp = open(tmpname, 'w')
    subprocess.run('crontab -l'.split(),stdout=tmp)
    tmp.write(f'{cronjob}\n')
    tmp.close()
    cmd = 'crontab {tmpname}'
    doit = run_command(cmd)



def main():
    """run it"""
    # grab args from cmd or env
    parser = argparse.ArgumentParser(
        description='Installs Gitea on Opalstack account')
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
    logging.info(f'Started installation of Gitea app {args.app_name}')
    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)
    appinfo = api.get(f'/app/read/{args.app_uuid}')
    appdir = f'/home/{appinfo["app_user"]}/apps/{appinfo["name"]}'
    os.mkdir(f'{appdir}/bin', 0o700)
    os.mkdir(f'{appdir}/custom', 0o700)
    os.mkdir(f'{appdir}/custom/conf', 0o700)
    os.mkdir(f'{appdir}/repos', 0o700)
    os.mkdir(f'{appdir}/tmp', 0o700)
    logging.info('Created initial gitea subdirectories')

    # download gitea
    download(GITEA_URL, f'{appdir}/gitea', perms=0o700)

    # config
    gitea_conf = textwrap.dedent(f'''\
            APP_NAME = {appinfo['name']}
            RUN_MODE = dev

            [repository]
            ROOT = {appdir}/repos
            DEFAULT_PRIVATE = private

            [server]
            HTTP_ADDR = 127.0.0.1
            HTTP_PORT = {appinfo['port']}
            ; change DOMAIN and ROOT_URL below to your domain and site
            DOMAIN = localhost
            ROOT_URL = http://localhost

            [database]
            DB_TYPE = sqlite3

            [service]
            DISABLE_REGISTRATION = true

            [security]
            INSTALL_LOCK   = true

            ''')
    create_file(f'{appdir}/custom/conf/app.ini', gitea_conf)

    # create the DB
    cmd = f'{appdir}/gitea migrate'
    createdb = run_command(cmd)
    logging.debug(createdb)

    # create initial user
    pw = gen_password()
    cmd = f'{appdir}/gitea admin create-user --name {appinfo["app_user"]} \
            --password {pw} --email {appinfo["app_user"]}@localhost --admin'
    createuser = run_command(cmd)
    logging.info(f'created initial gitea user {appinfo["app_user"]}')
    logging.debug(f'created initial gitea user {appinfo["app_user"]} with password {pw}')
    logging.debug(createuser)

    # start script
    start_script = textwrap.dedent(f'''\
                #!/bin/bash
                export TMPDIR={appdir}/tmp
                cd {appdir}
                mkdir -p {appdir}/var
                PIDFILE="{appdir}/var/gitea.pid"

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["app_user"]} | grep -x -f $PIDFILE &> /dev/null); then
                  echo "Gitea instance already running."
                  exit 99
                fi

                nohup "{appdir}/gitea" >> $HOME/logs/{appinfo["name"]}/gitea.log 2>&1 &

                echo $! > "$PIDFILE"
                chmod 600 "$PIDFILE"
                ''')
    create_file(f'{appdir}/start', start_script)
    cmd = f'chmod 700 {appdir}/start'
    createstart = run_command(cmd)

    # stop script
    stop_script = textwrap.dedent(f'''\
                #!/bin/bash
                cd {appdir}
                PIDFILE="{appdir}/var/gitea.pid"
                PID=$(cat $PIDFILE)

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["app_user"]} | grep -x -f $PIDFILE &> /dev/null); then
                  kill $PID
                  sleep 3
                fi

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["app_user"]} | grep -x -f $PIDFILE &> /dev/null); then
                  sleep 3
                  kill -9 $PID
                fi
                rm -f $PIDFILE
                echo "Stopped."
                ''')
    create_file(f'{appdir}/stop', stop_script)
    cmd = f'chmod 700 {appdir}/stop'
    createstop = run_command(cmd)

    # cron
    croncmd = f'*/10 * * * * {appdir}/start > /dev/null 2>&1'
    cronjob = add_cronjob(croncmd)

    # finished, push a notice with credentials
    msg = f'Initial user is {appinfo["app_user"]}, password: {pw}'
    payload = json.dumps({'id': args.app_uuid, 'installed_ok': True,
                          'note': msg})
    finished=api.post('/app/installed_ok/', payload)

    logging.info(f'Completed installation of Gitea app {args.app_name}')


if __name__ == '__main__':
    main()
