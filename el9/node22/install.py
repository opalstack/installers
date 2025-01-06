#!/bin/python3

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
import time

UUID = os.environ.get('UUID')
OPAL_TOKEN = os.environ.get('OPAL_TOKEN')
APPNAME = os.environ.get('APPNAME')
CMD_ENV = None

def install_package(
    import_name,
    package_name=None,
    max_attempts=3,
    retry_interval=5
):
    """
    Attempts to import `import_name`. If not installed, installs `package_name` via pip,
    and retries until successful or until max_attempts is reached.

    Args:
        import_name (str): The name you use in `import <import_name>`.
        package_name (str): PyPI package name to install (defaults to import_name).
        max_attempts (int): How many times to attempt install + import.
        retry_interval (int): Seconds to wait between attempts.
    """
    if package_name is None:
        # If package_name not provided, use the same as import_name
        package_name = import_name

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"Attempt {attempt}/{max_attempts}: Trying to import '{import_name}'...")
            globals()[import_name] = __import__(import_name)
            print(f"Successfully imported '{import_name}'.")
            return  # Successfully imported, so we can return
        except ImportError:
            print(f"'{import_name}' not found. Attempting to install '{package_name}'...")
            try:
                # We’ll install to the user site by default in case system site-packages is not writable
                subprocess.check_call([
                    sys.executable, "-m", "pip", "install", "--user", package_name
                ])
            except subprocess.CalledProcessError:
                print(f"Failed to install '{package_name}'. Retrying in {retry_interval} seconds...")
                time.sleep(retry_interval)
                continue

            # If pip install succeeded, try the import again before the next loop iteration
            try:
                globals()[import_name] = __import__(import_name)
                print(f"Successfully installed and imported '{import_name}'.")
                return
            except ImportError:
                # Even though pip says it installed successfully, Python still can't see it
                print(
                    f"Installed '{package_name}', but still cannot import '{import_name}'. "
                    f"Will retry in {retry_interval} seconds..."
                )
                time.sleep(retry_interval)

    # If we exit the for-loop, it means we never successfully imported
    raise ImportError(
        f"Could not import '{import_name}' after {max_attempts} attempts. "
        f"Please check your environment or installation paths."
    )


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

def main(api):
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
    logging.info(f'Started installation of Node 22 {args.app_name}')
    appinfo = filt_one(api.apps.list_all(), {'id':UUID})
    appdir = f'/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}'
    os.mkdir(f'{appdir}/env')

    node22_url = "https://nodejs.org/dist/v22.12.0/node-v22.12.0-linux-x64.tar.xz"
    download(node22_url, f"{appdir}/node-v22.12.0-linux-x64.tar.xz")
    
    cmd = f"/usr/bin/tar -xf {appdir}/node-v22.12.0-linux-x64.tar.xz --directory {appdir}/env"
    run_command(cmd)

    activate = textwrap.dedent(f"""
    #!/bin/bash
    # activate - Environment activation script for {appinfo["name"]}

    # Ensure the script is being sourced
    if [[ "${{BASH_SOURCE[0]}}" == "${{0}}" ]]; then
        echo "Error: Please source this script instead of executing it."
        echo "Usage: source activate"
        exit 1
    fi

    # Determine the directory where this script is located
    APP_ROOT="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
    ENV_DIR="$APP_ROOT/env"

    # Ensure the env directory exists
    if [ ! -d "$ENV_DIR" ]; then
    echo "Error: Environment directory '$ENV_DIR' does not exist."
    return 1 2>/dev/null || exit 1
    fi

    # Check if bin directory exists and is not empty
    if [ ! -d "$ENV_DIR/bin" ] || [ -z "$(ls -A "$ENV_DIR/bin")" ]; then
    echo "Error: 'env/bin' directory is missing or empty."
    return 1 2>/dev/null || exit 1
    fi

    # Backup current PATH if not already backed up
    if [ -z "$APPNAME_ORIGINAL_PATH" ]; then
    export APPNAME_ORIGINAL_PATH="$PATH"
    fi

    # Prepend env/bin to PATH
    export PATH="$ENV_DIR/bin:$PATH"

    # Update LD_LIBRARY_PATH to include env/lib
    export LD_LIBRARY_PATH="$ENV_DIR/lib${{LD_LIBRARY_PATH:+":$LD_LIBRARY_PATH"}}"

    # Update CPATH to include env/include
    export CPATH="$ENV_DIR/include${{CPATH:+":$CPATH"}}"

    # Update LIBRARY_PATH to include env/lib
    export LIBRARY_PATH="$ENV_DIR/lib${{LIBRARY_PATH:+":$LIBRARY_PATH"}}"

    # Update PKG_CONFIG_PATH to include env/lib/pkgconfig
    export PKG_CONFIG_PATH="$ENV_DIR/lib/pkgconfig${{PKG_CONFIG_PATH:+":$PKG_CONFIG_PATH"}}"

    # Set a flag to indicate the environment is activated
    export APPNAME_ENV_ACTIVE=1

    echo "APPNAME environment activated."
    """)
    create_file(f'{appdir}/activate', activate, perms=0o700)

    deactivate = textwrap.dedent(f"""
    #!/bin/bash
    # deactivate - Environment deactivation script for {appinfo["name"]}

    # Ensure the script is being sourced
    if [[ "${{BASH_SOURCE[0]}}" == "${{0}}" ]]; then
        echo "Error: Please source this script instead of executing it."
        echo "Usage: source deactivate"
        exit 1
    fi

    # Restore the original PATH if it was backed up
    if [ -n "$APPNAME_ORIGINAL_PATH" ]; then
    export PATH="$APPNAME_ORIGINAL_PATH"
    unset APPNAME_ORIGINAL_PATH
    fi

    # Remove env/lib from LD_LIBRARY_PATH
    if [[ "$LD_LIBRARY_PATH" == *"$ENV_DIR/lib"* ]]; then
    export LD_LIBRARY_PATH=$(echo "$LD_LIBRARY_PATH" | sed -e "s|$ENV_DIR/lib:||" -e "s|:$ENV_DIR/lib||" -e "s|$ENV_DIR/lib||")
    fi

    # Remove env/include from CPATH
    if [[ "$CPATH" == *"$ENV_DIR/include"* ]]; then
    export CPATH=$(echo "$CPATH" | sed -e "s|$ENV_DIR/include:||" -e "s|:$ENV_DIR/include||" -e "s|$ENV_DIR/include||")
    fi

    # Remove env/lib from LIBRARY_PATH
    if [[ "$LIBRARY_PATH" == *"$ENV_DIR/lib"* ]]; then
    export LIBRARY_PATH=$(echo "$LIBRARY_PATH" | sed -e "s|$ENV_DIR/lib:||" -e "s|:$ENV_DIR/lib||" -e "s|$ENV_DIR/lib||")
    fi

    # Remove env/lib/pkgconfig from PKG_CONFIG_PATH
    if [[ "$PKG_CONFIG_PATH" == *"$ENV_DIR/lib/pkgconfig"* ]]; then
    export PKG_CONFIG_PATH=$(echo "$PKG_CONFIG_PATH" | sed -e "s|$ENV_DIR/lib/pkgconfig:||" -e "s|:$ENV_DIR/lib/pkgconfig||" -e "s|$ENV_DIR/lib/pkgconfig||")
    fi

    # Unset the environment active flag
    unset APPNAME_ENV_ACTIVE

    echo "APPNAME environment deactivated."
    """)
    create_file(f'{appdir}/deactivate', deactivate, perms=0o700)

    CMD_ENV['HOME'] = f'/home/{appinfo["osuser_name"]}/'  

    # make myproject/index.js
    cmd = f'mkdir -p {appdir}/myproject'
    doit = run_command(cmd)
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
    create_file(f'{appdir}/myproject/index.js', appjs, perms=0o600)

    # make myproject/index.js
    pkgjson = textwrap.dedent(f'''\
            {{
              "name": "myproject",
              "version": "1.0.0",
              "description": "Hello world",
              "main": "index.js",
              "scripts": {{
                "start": "node index.js"
              }}
            }}''')
    create_file(f'{appdir}/myproject/package.json', pkgjson, perms=0o600)

    # start script
    start_script = textwrap.dedent(f'''\
                #!/bin/bash

                APPNAME={appinfo["name"]}

                # set node version via scl
                source scl_source enable nodejs20
                NODE=$( which node )
                NPM=$( which npm )

                # set your project info here
                PROJECT=myproject
                STARTCMD="$NPM start"

                APPDIR=$HOME/apps/$APPNAME
                LOGDIR=$HOME/logs/apps/$APPNAME
                TMPDIR=$APPDIR/tmp
                PROJECTDIR=$APPDIR/$PROJECT
                PIDFILE=$TMPDIR/node.pid

                mkdir -p $APPDIR/tmp

                if [ -e "$PIDFILE" ] && (pgrep -F $PIDFILE &> /dev/null); then
                  echo "$APPNAME already running."
                  exit 99
                fi

                /usr/sbin/daemonize -c $PROJECTDIR -a -e $LOGDIR/error.log -o $LOGDIR/console.log -p $PIDFILE $STARTCMD

                echo "Started $APPNAME."
                ''')
    create_file(f'{appdir}/start', start_script, perms=0o700)

    # stop script
    stop_script = textwrap.dedent(f'''\
                #!/bin/bash

                APPNAME={appinfo["name"]}

                PIDFILE="$HOME/apps/$APPNAME/tmp/node.pid"

                if [ ! -e "$PIDFILE" ]; then
                    echo "$PIDFILE missing, maybe $APPNAME is already stopped?"
                    exit 99
                fi

                if [ -e "$PIDFILE" ] && (pgrep -F $PIDFILE &> /dev/null); then
                  pkill -g $(cat $PIDFILE)
                  sleep 3
                fi

                if [ -e "$PIDFILE" ] && (pgrep -F $PIDFILE &> /dev/null); then
                  echo "$APPNAME did not stop, killing it."
                  sleep 3
                  pkill -9 -g $(cat $PIDFILE)
                fi
                rm -f $PIDFILE
                echo "Stopped $APPNAME."
                ''')
    create_file(f'{appdir}/stop', stop_script, perms=0o700)

    # cron
    m = random.randint(0,9)
    croncmd = f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1'
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
    api.notices.create(msg)

    #mark installed
    api.apps.mark_installed([{'id': args.app_uuid}])

    logging.info(f'Completed installation of Node.js app {args.app_name}')



if __name__ == '__main__':
    install_package("opalstack")
    from opalstack.util import filt, filt_one
    api = opalstack.Api(token=OPAL_TOKEN)
    main(api)