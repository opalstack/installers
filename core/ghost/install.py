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
LTS_NODE_URL = 'https://nodejs.org/download/release/v14.17.0/node-v14.17.0-linux-x64.tar.xz'


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
    return ''.join(secrets.choice(chars) for i in range(length))


def run_command(cmd, cwd=None, env=CMD_ENV):
    """runs a command, returns output"""
    logging.info(f'Running: {cmd}')
    try:
        result = subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env)
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
        description='Installs Ghost web app on Opalstack account')
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
    logging.info(f'Started installation of Ghost app {args.app_name}')
    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)
    appinfo = api.get(f'/app/read/{args.app_uuid}')
    appdir = f'/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}'

    # get current LTS nodejs
    cmd = f'mkdir -p {appdir}/node'
    doit = run_command(cmd)
    download(LTS_NODE_URL, f'{appdir}/node.tar.xz')
    cmd = f'tar xf {appdir}/node.tar.xz --strip 1'
    doit = run_command(cmd, cwd=f'{appdir}/node')
    CMD_ENV['PATH'] = f'{appdir}/node/bin:{CMD_ENV["PATH"]}'

    # install ghostcli
    # TODO: remove sleep after race is figured out
    cmd = f'sleep 10'
    doit = run_command(cmd, cwd=appdir)
    cmd = f'npm install --prefix={appdir} ghost-cli@latest'
    doit = run_command(cmd, cwd=appdir)

    # install ghost instance
    cmd = f'mkdir {appdir}/ghost'
    doit = run_command(cmd)
    cmd = f'{appdir}/node_modules/.bin/ghost install local --port {appinfo["port"]} --log file'
    doit = run_command(cmd, cwd=f'{appdir}/ghost')

    # update ghost config to put logs in log dir
    cmd = f'{appdir}/node_modules/.bin/ghost config set logging[\'path\'] \'/home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/\''
    doit = run_command(cmd, cwd=f'{appdir}/ghost')

    # start script
    start_script = textwrap.dedent(f'''\
                #!/bin/bash
                PATH={appdir}/node/bin:$PATH
                {appdir}/node_modules/.bin/ghost start -d {appdir}/ghost
                echo "Started Ghost for {appinfo["name"]}."
                ''')
    create_file(f'{appdir}/start', start_script, perms=0o700)

    # stop script
    stop_script = textwrap.dedent(f'''\
                #!/bin/bash
                PATH={appdir}/node/bin:$PATH
                {appdir}/node_modules/.bin/ghost stop -d {appdir}/ghost
                echo "Stopped Ghost for {appinfo["name"]}."
                ''')
    create_file(f'{appdir}/stop', stop_script, perms=0o700)

    # cron
    m = random.randint(0,9)
    croncmd = f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1'
    cronjob = add_cronjob(croncmd)

    # make README
    readme = textwrap.dedent(f'''\
                # Opalstack Ghost README

                ## Post-Install Steps - IMPORTANT!

                1. Assign your {args.app_name} application to a Site Route in
                   your control panel and make a note of the site URL.

                2. SSH to the server as your app's shell user and run the
                   following commands to configure the site URL, for example
                   https://domain.com:

                    cd {appdir}/ghost
                    {appdir}/node_modules/.bin/ghost config url https://domain.com
                    {appdir}/node_modules/.bin/ghost restart

                3. Immediately visit your Ghost admin URL (for example
                   https://domain.com/ghost/) to set up your initial admin user.

                ## Production mode

                Your Ghost app is initially configured to run in development
                mode which uses more memory and is slower than production mode.
                To run in production mode please see:
                https://help.opalstack.com/article/122/running-ghost-in-production-mode


                ## Controlling your app

                Start your app by running:

                    {appdir}/start

                or

                    {appdir}/node_modules/.bin/ghost start -d {appdir}/ghost



                Stop your app by running:

                   {appdir}/stop

                or

                   {appdir}/node_modules/.bin/ghost stop -d {appdir}/ghost

                ## Installing modules

                If you want to install Node modules in your app directory:

                    cd {appdir}
                    npm install modulename

                ''')
    create_file(f'{appdir}/README', readme)

    # restart it
    cmd = f'{appdir}/node_modules/.bin/ghost restart'
    doit = run_command(cmd, cwd=f'{appdir}/ghost')

    # finished, push a notice
    msg = f'Post-install configuration is required, see README in app directory for more info.'
    payload = json.dumps([{'id': args.app_uuid}])
    finished=api.post('/app/installed/', payload)

    logging.info(f'Completed installation of Ghost app {args.app_name}')


if __name__ == '__main__':
    main()
