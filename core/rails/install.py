#!/bin/env python3.11

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



def run_command(cmd, env, cwd=None):
    """runs a command, returns output"""
    logging.info(f'Running: {cmd}')
    try:
        result = subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env)
    except subprocess.CalledProcessError as e:
        logging.debug(e.output)
    return result

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


def add_cronjob(cronjob, env):
    """appends a cron job to the user's crontab"""
    homedir = os.path.expanduser('~')
    tmpname = f'{homedir}/.tmp{gen_password()}'
    tmp = open(tmpname, 'w')
    subprocess.run('crontab -l'.split(),stdout=tmp)
    tmp.write(f'{cronjob}\n')
    tmp.close()
    cmd = f'crontab {tmpname}'
    doit = run_command(cmd, env)
    cmd = run_command(f'rm -f {tmpname}', env)
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
    CMD_ENV = {'PATH': f'{appdir}/myproject/bin:{appdir}/env/bin:/usr/local/bin:/usr/bin:/bin',
               'TMPDIR': f'{appdir}/tmp',
               'GEM_HOME': f'{appdir}/env',
               'UMASK': '0002',
               'HOME': f'/home/{appinfo["osuser_name"]}',}
    # make dirs env and tmp
    os.mkdir(f'{appdir}/env')
    os.mkdir(f'{appdir}/env/bin')
    os.mkdir(f'{appdir}/tmp')

    # set up yarn
    cmd = f'scl enable devtoolset-11 nodejs20 ruby32 -- corepack enable --install-directory={appdir}/env/bin'
    doit = run_command(cmd, CMD_ENV, cwd=f'{appdir}/env')

    # install rails and puma
    cmd = f'scl enable devtoolset-11 nodejs20 ruby32 -- gem install -N --no-user-install -n {appdir}/env/bin rails puma'
    doit = run_command(cmd, CMD_ENV, cwd=f'{appdir}')

    # make rails project
    cmd = f'scl enable devtoolset-11 nodejs20 ruby32 -- rails new myproject'
    doit = run_command(cmd, CMD_ENV, cwd=f'{appdir}')
    pid_dir = f'{appdir}/myproject/tmp/pids'
    if not os.path.isdir(pid_dir):
        os.mkdir(pid_dir)
    socket_dir = f'{appdir}/myproject/tmp/sockets'
    if not os.path.isdir(socket_dir):
        os.mkdir(socket_dir)

    # puma start script
    start_puma = textwrap.dedent(f'''\
                #!/bin/bash

                # name of your app, don't change this
                APPNAME={appinfo["name"]}

                # change the next line to your Rails project directory
                PROJECTDIR=$HOME/apps/$APPNAME/myproject

                # set your rails env, eg development or production
                RAILS_ENV=development

                # no need to edit below this line
                source scl_source enable devtoolset-11 nodejs20 ruby32
                export PATH=$PROJECTDIR/bin:$HOME/apps/$APPNAME/env/bin:$PATH
                export GEM_PATH=$HOME/apps/$APPNAME/env/gems:$GEM_PATH
                export GEM_HOME=$HOME/apps/$APPNAME/env

                PIDFILE="$PROJECTDIR/tmp/pids/server.pid"
                if [ -e "$PIDFILE" ] && (pgrep -u seantest | grep -x -f $PIDFILE &> /dev/null); then
                  echo "$APPNAME puma already running!"
                  exit 99
                fi

                cd $PROJECTDIR
                START="$PROJECTDIR/bin/bundle exec puma -b unix:///$PROJECTDIR/tmp/sockets/puma.sock --pidfile $PIDFILE"
                ( nohup $START > $PROJECTDIR/log/$RAILS_ENV.log 2>&1 & )

                echo "$APPNAME puma started"
                ''')
    create_file(f'{appdir}/start_puma', start_puma, perms=0o700)

    # puma stop script
    stop_puma = textwrap.dedent(f'''\
                #!/bin/bash

                PROJECTNAME=myproject

                # no need to edit below this line unless your project directory
                # is not in your app directory
                APPNAME={appinfo["name"]}
                APPDIR="$HOME/apps/$APPNAME"
                PROJECTDIR="$APPDIR/$PROJECTNAME"
                APP_PORT={appinfo["port"]}
                PIDFILE="$PROJECTDIR/tmp/pids/server.pid"

                PID=$( pgrep -a -f "$PROJECTDIR/tmp/sockets/puma.sock" | awk '{{print $1}}') || {{ echo "$APPNAME puma not running"; exit 99; }}
                echo $PID > $PIDFILE
                kill $PID
                sleep 3
                pgrep -o -f "$PROJECTDIR/tmp/sockets/puma.sock" && {{
                  echo "$APPNAME did not stop, killing it."
                  kill -9 $PID
                }}
                rm -f $PIDFILE
                echo "$APPNAME puma stopped"
                ''')
    create_file(f'{appdir}/stop_puma', stop_puma, perms=0o700)

    # nginx start script
    start_nginx = textwrap.dedent(f'''\
                #!/bin/bash

                APPNAME={appinfo["name"]}

                APPDIR=$HOME/apps/$APPNAME
                ERRLOG=$HOME/logs/apps/$APPNAME/nginx_error.log
                /usr/sbin/nginx -c $APPDIR/nginx/nginx.conf -p $APPDIR -e $ERRLOG
                echo "$APPNAME nginx started"

                ''')
    create_file(f'{appdir}/start_nginx', start_nginx, perms=0o700)

    # nginx stop script
    stop_nginx = textwrap.dedent(f'''\
                #!/bin/bash

                APPNAME={appinfo["name"]}

                APPDIR=$HOME/apps/$APPNAME
                ERRLOG=$HOME/logs/apps/$APPNAME/nginx_error.log
                /usr/sbin/nginx -c $APPDIR/nginx/nginx.conf -p $APPDIR -e $ERRLOG -s quit
                echo "$APPNAME nginx stopped"

                ''')
    create_file(f'{appdir}/stop_nginx', stop_nginx, perms=0o700)

    # nginx config
    nginx_conf = textwrap.dedent(f'''\
                pid /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/tmp/nginx.pid;

                events {{}}

                http {{
                    include /etc/nginx/mime.types;
                    default_type application/octet-stream;

                    client_body_temp_path /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/tmp/client_body;
                    fastcgi_temp_path     /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/tmp/fastcgi_temp;
                    proxy_temp_path       /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/tmp/proxy_temp;
                    scgi_temp_path        /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/tmp/scgi_temp;
                    uwsgi_temp_path       /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/tmp/uwsgi_temp;

                    log_format main '$http_x_forwarded_for - $remote_user [$time_local] "$request" $status $body_bytes_sent "$http_referer" "$http_user_agent"';
                    access_log /home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/nginx_access.log main;
                    error_log /home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/nginx_error.log;

                    upstream puma {{
                      server unix:/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/myproject/tmp/sockets/puma.sock fail_timeout=0;
                    }}


                    server {{
                        # change the next two lines to use your site domain
                        # and your project's public directory
                        server_name localhost;
                        root /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/myproject/public;

                        listen {appinfo["port"]};

                	    try_files $uri/index.html $uri @puma;

                        location @puma {{
                            proxy_pass http://puma;
                            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
                            proxy_set_header Host $http_host;
                            proxy_redirect off;
                        }}
                    }}
                }}
                ''')
    os.mkdir(f'{appdir}/nginx')
    create_file(f'{appdir}/nginx/nginx.conf', nginx_conf, perms=0o600)

    # main start script
    start_script = textwrap.dedent(f'''\
                #!/bin/bash

                # name of your app, don't change this
                APPNAME={appinfo["name"]}

                $HOME/apps/$APPNAME/start_puma
                $HOME/apps/$APPNAME/start_nginx
                                ''')
    create_file(f'{appdir}/start', start_script, perms=0o700)

    # main stop script
    stop_script = textwrap.dedent(f'''\
                #!/bin/bash

                # name of your app, don't change this
                APPNAME={appinfo["name"]}

                $HOME/apps/$APPNAME/stop_nginx
                $HOME/apps/$APPNAME/stop_puma
                ''')
    create_file(f'{appdir}/stop', stop_script, perms=0o700)

    # restart script
    restart_script = textwrap.dedent(f'''\
                #!/bin/bash

                # name of your app, don't change this
                APPNAME={appinfo["name"]}

                $HOME/apps/$APPNAME/stop
                $HOME/apps/$APPNAME/start
                ''')
    create_file(f'{appdir}/restart', restart_script, perms=0o700)

    # setenv script
    setenv = textwrap.dedent(f'''\
                #!/bin/bash

                # name of your app, don't change this
                APPNAME={appinfo["name"]}

                # change the next line to your Rails project directory
                PROJECTDIR=$HOME/apps/$APPNAME/myproject

                # set your rails env, eg development or production
                RAILS_ENV=development

                # no need to edit below this line
                PIDFILE="$PROJECTDIR/tmp/pids/server.pid"
                source scl_source enable devtoolset-11 nodejs20 ruby32
                export PATH=$PROJECTDIR/bin:$HOME/apps/$APPNAME/env/bin:$PATH
                export GEM_PATH=$HOME/apps/$APPNAME/env/gems:$GEM_PATH
                export GEM_HOME=$HOME/apps/$APPNAME/env
                export RAILS_ENV=$RAILS_ENV
                ''')
    create_file(f'{appdir}/setenv', setenv, perms=0o600)


    # cron
    m = random.randint(0,9)
    croncmd = f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1'
    cronjob = add_cronjob(croncmd, CMD_ENV)

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

                3. Edit {appdir}/nginx/nginx.conf to set server_name to include your
                   site's domains. Example:

                        server_name domain.com www.domain.com;

                4. Run the following command to restart your Rails instance:

                        {appdir}/restart

                ## Using your own project

                If you want to serve your own Rails project from this instance:

                1. Upload your project directory to {appdir}.

                2. SSH to your app's shell user account.

                3. Edit {appdir}/setenv to set PROJECTDIR to your project directory, eg:

                        PROJECTDIR=$HOME/app/$APPNAME/yourproject

                3. Set your environment for your application:

                        cd {appdir}
                        source setenv

                4. Install your project dependencies with bundle:

                        cd {appdir}/yourproject
                        bundle install

                5. Edit {appdir}/start_puma and {appdir}/stop_puma
                   to change the PROJECTDIR variable on line 4 to point to your
                   project directory, for example:

                        PROJECTDIR=$HOME/apps/$APPNAME/yourproject

                6. Edit {appdir}/nginx.conf to change the Puma socket path and server root to point to your project directory, for example:

                        ...
                        upstream puma {{
                          server unix:/home/shell_user_name/apps/app_name/yourproject/tmp/sockets/puma.sock fail_timeout=0;
                        }}


                        server {{
                            # change the next two lines to use your site domain
                            # and your project's public directory
                            server_name localhost;
                            root /home/shell_user_name/apps/app_name/yourproject;
                        ...


                7. Run the following commands to restart your Rails instance:

                        mkdir -p {appdir}/yourproject/tmp/pids
                        mkdir -p {appdir}/yourproject/tmp/sockets
                        cp {appdir}/oldproject/tmp/pids/* {appdir}/yourproject/tmp/pids/
                        {appdir}/restart

                For more information please refer to our Ruby on Rails topic guide at:

                https://docs.opalstack.com/topic-guides/rails/
                ''')
    create_file(f'{appdir}/README', readme)

    # start it
    cmd = f'{appdir}/start'
    startit = run_command(cmd, CMD_ENV)

    # finished, push a notice
    msg = f'See README in app directory for more info.'
    payload = json.dumps([{'id': args.app_uuid}])
    finished=api.post('/app/installed/', payload)

    logging.info(f'Completed installation of Rails app {args.app_name}')


if __name__ == '__main__':
    main()
