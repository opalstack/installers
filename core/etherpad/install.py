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
import secrets

API_HOST = os.environ.get('API_URL').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'
CMD_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin','UMASK': '0002',}
LTS_NODE_URL = 'https://nodejs.org/download/release/v14.17.0/node-v14.17.0-linux-x64.tar.xz'
ETHERPAD_URL = 'https://github.com/ether/etherpad-lite/archive/1.8.18.zip'

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
        connread = conn.getresponse().read()
        print(connread)
        return json.loads(connread)

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
    logging.info(f'Started installation of Etherpad app {args.app_name}')
    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)
    appinfo = api.get(f'/app/read/{args.app_uuid}')
    appdir = f'/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}'
    port = appinfo["port"]

    dbname = f'etherpad_{secrets.token_hex(4)}'

    # create database user
    mupayload =  json.dumps([{"name": dbname, "server": appinfo["server"] }])
    mariauser = api.post(f'/mariauser/create/', mupayload)[0]
    # create database
    mdbpayload = json.dumps([{ "name": dbname, "server": appinfo["server"], "charset":"utf8mb4", "dbusers_readwrite": [mariauser["id"]] }])
    mariadb = api.post(f'/mariadb/create/', mdbpayload)[0]
 
    # get current LTS nodejs
    cmd = f'mkdir -p {appdir}/node'
    doit = run_command(cmd)
    download(LTS_NODE_URL, f'{appdir}/node.tar.xz')
    cmd = f'tar xf {appdir}/node.tar.xz --strip 1'
    doit = run_command(cmd, cwd=f'{appdir}/node')
    CMD_ENV['PATH'] = f'{appdir}/node/bin:{CMD_ENV["PATH"]}'

    # cron
    m = random.randint(0,9)
    croncmd = f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1'
    cronjob = add_cronjob(croncmd)

    # make README
    readme = textwrap.dedent(f'''\
                # Opalstack Etherpad README

                ## Controlling your app

                Start your app by running:

                   {appdir}/start

                Stop your app by running:

                   {appdir}/stop

                To use Etherpad with Nextcloud using the owncloud plugin you must add some mimetypes to nextcloud. 
                See https://github.com/otetard/ownpad#mimetype-detection
                ''')
    create_file(f'{appdir}/README', readme)

    run_command(f'/bin/wget {ETHERPAD_URL} -O {appdir}/1.8.18.zip')
    run_command(f'/bin/unzip {appdir}/1.8.18.zip -d {appdir}/')
    run_command(f'/bin/rm {appdir}/1.8.18.zip')

    pw = secrets.token_hex(8)
    settings =  {
        "title": "Etherpad",
        "favicon": None,
        "skinName": "colibris",
        "skinVariants": "super-light-toolbar super-light-editor light-background",
        "ip": "0.0.0.0",
        "port": port,
        "showSettingsInAdminPage": True,
        "dbType" : "mysql",
        "dbSettings" : {
            "user":     dbname,
            "host":     "localhost",
            "port":     3306,
            "password": mariauser["default_password"],
            "database": dbname,
            "charset":  "utf8mb4"
        },
        "defaultPadText" : "Welcome to Etherpad!",
        "padOptions": {
            "noColors":         False,
            "showControls":     True,
            "showChat":         True,
            "showLineNumbers":  True,
            "useMonospaceFont": False,
            "userName":         False,
            "userColor":        False,
            "rtl":              False,
            "alwaysShowChat":   False,
            "chatAndUsers":     False,
            "lang":             "en-gb"
        },
        "padShortcutEnabled" : {
            "altF9":     True, 
            "altC":      True, 
            "cmdShift2": True, 
            "delete":    True,
            "return":    True,
            "esc":       True, 
            "cmdS":      True, 
            "tab":       True, 
            "cmdZ":      True, 
            "cmdY":      True, 
            "cmdI":      True, 
            "cmdB":      True, 
            "cmdU":      True, 
            "cmd5":      True, 
            "cmdShiftL": True, 
            "cmdShiftN": True, 
            "cmdShift1": True, 
            "cmdShiftC": True, 
            "cmdH":      True, 
            "ctrlHome":  True, 
            "pageUp":    True,
            "pageDown":  True
        },
        "suppressErrorsInPadText": False,
        "requireSession": True,
        "editOnly": False,
        "minify": True,
        "maxAge": 21600,
        "abiword": None,
        "soffice": None,
        "tidyHtml": None,
        "allowUnknownFileEnds": True,
        "requireAuthentication": True,
        "requireAuthorization": True,
        "trustProxy": True,
        "cookie": {
            "sameSite": "Lax"
        },
        "disableIPlogging": False,
        "automaticReconnectionTimeout": 0,
        "scrollWhenFocusLineIsOutOfViewport": {
            "percentage": {
            "editionAboveViewport": 0,
            "editionBelowViewport": 0
            },
            "duration": 0,
            "scrollWhenCaretIsInTheLastLineOfViewport": False,
            "percentageToScrollWhenUserPressesArrowUp": 0
        },
        "socketTransportProtocols" : ["xhr-polling", "jsonp-polling", "htmlfile"],
        "socketIo": {
            "maxHttpBufferSize": 10000
        },
        "loadTest": False,
        "dumpOnUncleanExit": False,
        "importExportRateLimiting": {
            "windowMs": 90000,
            "max": 10
        },
        "importMaxFileSize": 52428800,
        "commitRateLimiting": {
            "duration": 1,
            "points": 10
        },
        "exposeVersion": False,
        "loglevel": "INFO",
        "customLocaleStrings": {},
        "enableAdminUITests": False,
        "users": {
            appinfo["osuser_name"]: {
            "password": pw,
            "is_admin": True
            },
        },

    }

    create_file(f'{appdir}/etherpad-lite-1.8.18/settings.json', json.dumps(settings))

    # start script
    start_script = textwrap.dedent(f'''\
                #!/bin/sh
                export PATH=$PWD/node/bin:$PATH
                export TMPDIR={appdir}/tmp
                mkdir -p {appdir}/tmp
                PIDFILE="{appdir}/tmp/node.pid"

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
                echo "Etherpad already running."
                exit 99
                fi

                # Move to the Etherpad base directory.
                cd etherpad-lite-1.8.18 || exit 1

                # Source constants and useful functions
                . src/bin/functions.sh

                # Prepare the environment
                src/bin/installDeps.sh || exit 1

                # Move to the node folder and start
                log "Starting Etherpad..."

                /usr/sbin/daemonize -c {appdir} -a -e ~/logs/apps/{appinfo["name"]}/node_error.log -o ~/logs/apps/{appinfo["name"]}/node_output.log -p $PIDFILE {appdir}/node/bin/node {appdir}/etherpad-lite-1.8.18/src/node/server.js
                ''')
    create_file(f'{appdir}/start', start_script, perms=0o700)

    # stop script
    stop_script = textwrap.dedent(f'''\
                #!/bin/bash
                PIDFILE="{appdir}/tmp/node.pid"

                if [ ! -e "$PIDFILE" ]; then
                    echo "$PIDFILE missing, maybe Etherpad is already stopped?"
                    exit 99
                fi

                PID=$(cat $PIDFILE)

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
                  kill $PID
                  sleep 3
                fi

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
                  echo "Etherpad did not stop, killing it."
                  sleep 3
                  kill -9 $PID
                fi
                rm -f $PIDFILE
                echo "Stopped."
                ''')
    create_file(f'{appdir}/stop', stop_script, perms=0o700)

    run_command(f'{appdir}/start')

    # finished, push a notice
    msg = f'Initial user is {appinfo["osuser_name"]}, password: {pw} - see README in app directory.'
    payload = json.dumps([{'id': args.app_uuid}])
    finished=api.post('/app/installed/', payload)
    
    logging.info(f'Completed installation of Etherpad app {args.app_name}')

if __name__ == '__main__':
    main()
