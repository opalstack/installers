#! /usr/bin/python3.6

import argparse
import sys
import logging
import os
import os.path
import http.client
import json
import textwrap
import secrets
import string
import subprocess
import shlex
import random
from urllib.parse import urlparse
import urllib.request

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
    urllib.request.urlretrieve(url,filename=localfile)
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
        description='Installs Rails web app on Opalstack account')
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
    logging.info(f'Started installation of Rails app {args.app_name}')
    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)
    appinfo = api.get(f'/app/read/{args.app_uuid}')
    appdir = f'/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}'
    CMD_ENV = {'PATH': f'/opt/bin:{appdir}/myproject/bin:{appdir}/env/bin:/usr/local/bin:/usr/bin:/bin',
               'LD_LIBRARY_PATH': '/opt/lib',
               'TMPDIR': f'{appdir}/tmp',
               'GEM_HOME': f'{appdir}/env',
               'UMASK': '0002',
               'HOME': f'/home/{appinfo["osuser_name"]}',}

    # make dirs env and tmp
    os.mkdir(f'{appdir}/env')
    os.mkdir(f'{appdir}/tmp')

    # install yarn into env
    download('https://yarnpkg.com/latest.tar.gz', f'{appdir}/tmp/yarn.tar.gz', perms=0o700)
    cmd = f'tar zxf {appdir}/tmp/yarn.tar.gz --strip 1'
    doit = run_command(cmd, cwd=f'{appdir}/env')

    # install rails and puma
    cmd = f'gem install -N rails:6.1.4.4 puma rake:12.3.3'
    doit = run_command(cmd, cwd=f'{appdir}', env=CMD_ENV)

    # make rails project
    cmd = f'rails new myproject'
    doit = run_command(cmd, cwd=f'{appdir}', env=CMD_ENV)
    pid_dir = f'{appdir}/myproject/tmp/pids'
    if not os.path.isdir(pid_dir):
        os.mkdir(pid_dir)

    # DELETEME when rails no longer ships broken webpack
    cmd = f'''/bin/sed -i -e 's/check_yarn_integrity: true/check_yarn_integrity: false/' {appdir}/myproject/config/webpacker.yml'''
    doit = run_command(cmd, cwd=f'{appdir}', env=CMD_ENV)

    # start script
    start_script = textwrap.dedent(f'''\
                #!/bin/bash

                # change the next line to your Rails project directory
                PROJECTDIR='{appdir}/myproject'

                # set your rails env, eg development or production
                RAILS_ENV=development

                # no need to edit below this line
                APP_PORT={appinfo["port"]}
                PATH=/opt/bin:$PROJECTDIR/bin:{appdir}/env/bin:$PATH
                PIDFILE="$PROJECTDIR/tmp/pids/server.pid"

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
                  echo "Rails for {appinfo["name"]} already running."
                  exit 99
                fi

                cd $PROJECTDIR
                LD_LIBRARY_PATH=/opt/lib GEM_HOME={appdir}/env $PROJECTDIR/bin/bundle exec rails s -e $RAILS_ENV -p $APP_PORT -d -P $PIDFILE

                echo "Started Rails for {appinfo["name"]}."
                ''')
    create_file(f'{appdir}/start', start_script, perms=0o700)

    # stop script
    stop_script = textwrap.dedent(f'''\
                #!/bin/bash

                # change the next line to your Rails project directory
                PROJECTDIR='{appdir}/myproject'

                # no need to edit below this line
                PIDFILE="$PROJECTDIR/tmp/pids/server.pid"

                if [ ! -e "$PIDFILE" ]; then
                    echo "$PIDFILE missing, maybe Rails is already stopped?"
                    exit 99
                fi

                PID=$(cat $PIDFILE)

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
                  kill $PID
                  sleep 3
                fi

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
                  echo "Rails did not stop, killing it."
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
                # Opalstack Rails README

                ## Post-install steps

                Please take the following steps before you begin to use your Rails
                installation:

                1. Connect your Rails application to a site route in the control panel.

                2. Edit {appdir}/myproject/config/environments/development.rb to set
                   config.hosts to include your site's domains. Example:

                        config.hosts << "domain.com"
                        config.hosts << "www.domain.com"

                3. Run the following commands to restart your Rails instance:

                        {appdir}/stop
                        {appdir}/start

                ## Using your own project

                If you want to serve your own Rails project from this instance:

                1. Upload your project directory to: {appdir}

                2. SSH to your app's shell user account.

                3. Set your GEM_HOME and PATH environment to your application and then install
                   your project dependencies with bundle:

                        export GEM_HOME={appdir}/env
                        export PATH={appdir}/env/bin:{appdir}/yourproject/bin:$PATH
                        cd {appdir}/yourproject
                        bundle install

                4. Edit {appdir}/start and {appdir}/stop
                   to change the PROJECTDIR variable on line 4 to point to your
                   project directory.

                5. Run the following commands to restart your Rails instance:

                        mkdir -p {appdir}/yourproject/tmp/pids
                        cp {appdir}/oldproject/tmp/pids/* {appdir}/yourproject/tmp/pids/
                        {appdir}/stop
                        {appdir}/start

                For more information please refer to our Ruby on Rails topic guide at:

                https://help.opalstack.com/section/112/ruby-on-rails
                ''')
    create_file(f'{appdir}/README', readme)

    # start it
    cmd = f'{appdir}/start'
    startit = run_command(cmd)

    # finished, push a notice
    msg = f'See README in app directory for more info.'
    payload = json.dumps([{'id': args.app_uuid}])
    finished=api.post('/app/installed/', payload)

    logging.info(f'Completed installation of Rails app {args.app_name}')


if __name__ == '__main__':
    main()
