#! /usr/bin/python36

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
    return subprocess.check_output(shlex.split(cmd), shell=True)

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
        description='Installs Django on Opalstack account')
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
    logging.info(f'Started installation of Django app {args.app_name}')
    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)
    appinfo = api.get(f'/app/read/{args.app_uuid}')
    appdir = f'/home/{appinfo["app_user"]}/apps/{appinfo["name"]}'

    # create tmp dir
    os.mkdir(f'{appdir}/tmp', 0o700)
    logging.info(f'Created directory {appdir}/tmp')

    # create virtualenv
    cmd = f'/bin/python36 -m venv {appdir}/env'
    doit = run_command(cmd)
    logging.info(f'Created virtualenv at {appdir}/env')

    # install uwsgi
    cmd = f'{appdir}/env/bin/pip install uwsgi'
    doit = run_command(cmd)
    logging.info('Installed latest uWSGI into virtualenv')

    # install django
    cmd = f'{appdir}/env/bin/pip install django'
    doit = run_command(cmd)
    logging.info('Installed latest Django into virtualenv')

    # create project dir
    os.mkdir(f'{appdir}/myproject', 0o700)
    logging.info(f'Created Django project directory {appdir}/myproject')

    # run startproject with dir option
    cmd = f'{appdir}/env/bin/django-admin startproject myproject {appdir}/myproject'
    doit = run_command(cmd)
    logging.info(f'Populated Django project directory {appdir}/myproject')

    # django config
    # set ALLOWED_HOSTS
    cmd = f'''sed -r -i "s/^ALLOWED_HOSTS = \[\]/ALLOWED_HOSTS = \['\*'\]/" {appdir}/myproject/myproject/settings.py'''
    doit = run_command(cmd)
    # comment out DATABASES
    cmd = f'''sed -r -i "/^DATABASES =/, /^}}$/ s/^/#/" {appdir}/myproject/myproject/settings.py'''
    doit = run_command(cmd)
    logging.info(f'Wrote initial Django config to {appdir}/myproject/myproject/settings.py')

    # uwsgi config
    uwsgi_conf = textwrap.dedent(f'''\
                [uwsgi]
                master = True
                http = 127.0.0.1:{appinfo["port"]}
                virtualenv = {appdir}/env/
                env = LD_LIBRARY_PATH={appdir}/env/lib
                daemonize = /home/{appinfo["app_user"]}/logs/{appinfo["name"]}/uwsgi.log
                pidfile = {appdir}/tmp/uwsgi.pid
                workers = 2
                threads = 2

                # adjust the following to point to your project
                python-path = {appdir}/myproject
                wsgi-file = {appdir}/myproject/myproject/wsgi.py
                touch-reload = {appdir}/myproject/myproject/wsgi.py
                ''')
    create_file(f'{appdir}/uwsgi.ini', uwsgi_conf, perms=0o600)

    # start script
    start_script = textwrap.dedent(f'''\
                #!/bin/bash
                export TMPDIR={appdir}/tmp
                mkdir -p {appdir}/tmp
                PIDFILE="{appdir}/tmp/uwsgi.pid"

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["app_user"]} | grep -x -f $PIDFILE &> /dev/null); then
                  echo "uWSGI for {appinfo["name"]} already running."
                  exit 99
                fi

                {appdir}/env/bin/uwsgi --ini {appdir}/uwsgi.ini

                echo "Started uWSGI for {appinfo["name"]}."
                ''')
    create_file(f'{appdir}/start', start_script, perms=0o700)

    # stop script
    stop_script = textwrap.dedent(f'''\
                #!/bin/bash
                PIDFILE="{appdir}/tmp/uwsgi.pid"

                if [ ! -e "$PIDFILE" ]; then
                    echo "$PIDFILE missing, maybe uWSGI is already stopped?"
                    exit 99
                fi

                PID=$(cat $PIDFILE)

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["app_user"]} | grep -x -f $PIDFILE &> /dev/null); then
                  {appdir}/env/bin/uwsgi --stop $PIDFILE
                  sleep 3
                fi

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["app_user"]} | grep -x -f $PIDFILE &> /dev/null); then
                  echo "uWSGI did not stop, killing it."
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
                # Opalstack Django README

                ## Post-install steps

                Please take the following steps before you begin to use your Django
                installation:

                1. Connect your Django application to a site route in the control panel.

                2. Edit {appdir}/myproject/myproject/settings.py to set ALLOWED_HOSTS
                   to include your site's domains. Example:

                       ALLOWED_HOSTS = ['domain.com', 'www.domain.com']

                3. Run the following commands to restart your Django instance:

                   {appdir}/stop
                   {appdir}/start

                ## Using your own project

                If you want to serve your own Django project from this instance:

                1. Upload your project directory to {appdir}

                2. Activate the app's environment:

                       source {appdir}/env/bin/activate

                3. Install your project's Python dependencies with pip.

                4. Edit {appdir}/uwsgi.ini to point `wsgi-file` and `touch-reload` at your project's WSGI handler

                5. Run the following commands to restart your Django instance:

                   {appdir}/stop
                   {appdir}/start
                ''')
    create_file(f'{appdir}/README', readme)

    # start it
    cmd = f'{appdir}/start'
    startit = run_command(cmd)

    # finished, push a notice with credentials
    msg = f'See README in app directory for final steps.'
    payload = json.dumps({'id': args.app_uuid, 'installed_ok': True,
                          'note': msg})
    finished=api.post('/app/installed_ok/', payload)

    logging.info(f'Completed installation of Django app {args.app_name}')


if __name__ == '__main__':
    main()
