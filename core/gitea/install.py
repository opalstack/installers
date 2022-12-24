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
import shlex
import random
from urllib.parse import urlparse

API_HOST = os.environ.get('API_URL').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'
GITEA_URL = 'https://github.com/go-gitea/gitea/releases/download/v1.17.4/gitea-1.17.4-linux-amd64'
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


def download(url, appdir, localfile, writemode='wb', perms=0o600):
    """save a remote file, perms are passed as octal"""
    logging.info(f'Downloading {url} as {localfile} in {appdir} with permissions {oct(perms)}')
    subprocess.run(['/usr/bin/wget', url, '-P', appdir, '-o', '/dev/null', '-O', localfile ])
    os.chmod( localfile, perms)
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
        logging.debug(result)
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

    # turn on trailing slash
    payload = json.dumps({"id":args.app_uuid,"json":{"proxy_pass_trailing_slash":True}})
    slashit = api.post('/app/update/', payload)

    CMD_ENV['HOME'] = f'/home/{appinfo["osuser_name"]}'
    CMD_ENV['USER'] = appinfo['osuser_name']
    appdir = f'/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}'
    os.mkdir(f'{appdir}/bin', 0o700)
    os.mkdir(f'{appdir}/custom', 0o700)
    os.mkdir(f'{appdir}/custom/conf', 0o700)
    os.mkdir(f'{appdir}/data', 0o700)
    os.mkdir(f'{appdir}/log', 0o700)
    os.mkdir(f'{appdir}/repos', 0o700)
    os.mkdir(f'{appdir}/tmp', 0o700)
    os.mkdir(f'{appdir}/var', 0o700)
    logging.info('Created initial gitea subdirectories')

    # download gitea
    download(GITEA_URL, appdir, f'{appdir}/gitea', perms=0o700)

    # config
    gitea_conf = textwrap.dedent(f'''\
            APP_NAME = {appinfo['name']}
            RUN_MODE = prod

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
            INSTALL_LOCK = true

            [git]
            PATH = /opt/rh/sclo-git212/root/usr/bin/git
            ''')
    create_file(f'{appdir}/custom/conf/app.ini', gitea_conf)

    # create the DB
    cmd = f'{appdir}/gitea migrate'
    createdb = run_command(cmd)
    logging.debug(createdb)

    # create initial user
    pw = gen_password()
    cmd = f'{appdir}/gitea admin user create --name {appinfo["osuser_name"]} \
            --password {pw} --email {appinfo["osuser_name"]}@localhost --admin'
    createuser = run_command(cmd)
    logging.info(f'created initial gitea user {appinfo["osuser_name"]}')
    logging.debug(f'created initial gitea user {appinfo["osuser_name"]} with password {pw}')
    logging.debug(createuser)

    # start script
    start_script = textwrap.dedent(f'''\
                #!/bin/bash
                export TMPDIR={appdir}/tmp
                cd {appdir}
                mkdir -p {appdir}/var
                PIDFILE="{appdir}/var/gitea.pid"

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
                  echo "Gitea instance already running."
                  exit 99
                fi

                nohup "{appdir}/gitea" >> $HOME/logs/apps/{appinfo["name"]}/gitea.log 2>&1 &

                echo $! > "$PIDFILE"
                chmod 600 "$PIDFILE"
                echo "Started."
                ''')
    create_file(f'{appdir}/start', start_script, perms=0o700)

    # stop script
    stop_script = textwrap.dedent(f'''\
                #!/bin/bash
                cd {appdir}
                PIDFILE="{appdir}/var/gitea.pid"

                if [ ! -e "$PIDFILE" ]; then
                    echo "$PIDFILE missing, maybe Gitea is already stopped?"
                    exit 99
                fi

                PID=$(cat $PIDFILE)

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
                  kill $PID
                  sleep 3
                fi

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
                  echo "Gitea did not stop, killing it."
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
                # Opalstack Gitea README

                ## Post-install steps

                Please take the following steps before you begin to use your Gitea
                installation:

                1. Connect your Gitea application to a site route in the control panel.

                2. Edit {appdir}/custom/conf/app.ini to change DOMAIN
                   and ROOT_URL to use your Gitea site domain.

                3. Run the following commands to restart your Gitea instance:

                   {appdir}/stop
                   {appdir}/start

                4. Visit your Gitea site and log in.

                5. Click on the Profile menu in the top right corner and select
                   Settings.

                6. Set your email address to the address that you want to use with your
                   Gitea profile.

                7. If you plan to manage your repositories over SSH instead of HTTPS,
                   add your SSH key in your Gitea SSH/GPG Key settings.

                You're now ready to start using Gitea!
                ''')
    create_file(f'{appdir}/README', readme)

    # start it
    cmd = f'{appdir}/start'
    startit = run_command(cmd)

    # finished, push a notice with credentials
    msg = f'Initial user is {appinfo["osuser_name"]}, password: {pw} - see README in app directory for final steps.'
    payload = json.dumps([{'id': args.app_uuid}])
    finished=api.post('/app/installed/', payload)

    logging.info(f'Completed installation of Gitea app {args.app_name} - {msg}')


if __name__ == '__main__':
    main()
