#! /bin/python3
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

DJANGO_VERSION = '5.2.5'
WAGTAIL_VERSION = '7.2.1'
PROJECT_NAME = 'mysite'


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
            conn.request('POST', endpoint, payload, headers={'Content-type': 'application/json'})
            result = json.loads(conn.getresponse().read())

            if not result.get('token'):
                logging.warning('Invalid username or password and no auth token provided, exiting.')
                sys.exit(1)
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
    return ''.join(secrets.choice(chars) for _ in range(length))


def run_command(cmd, env=CMD_ENV):
    """runs a command, returns output (or b'' on failure)"""
    logging.info(f'Running: {cmd}')
    try:
        result = subprocess.check_output(shlex.split(cmd), env=env, stderr=subprocess.STDOUT)
        return result
    except subprocess.CalledProcessError as e:
        logging.error(e.output.decode('utf-8', errors='replace'))
        return b''


def add_cronjob(cronjob):
    """appends a cron job to the user's crontab"""
    homedir = os.path.expanduser('~')
    tmpname = f'{homedir}/.tmp{gen_password()}'
    tmp = open(tmpname, 'w')
    subprocess.run('crontab -l'.split(), stdout=tmp)
    tmp.write(f'{cronjob}\n')
    tmp.close()
    run_command(f'crontab {tmpname}')
    run_command(f'rm -f {tmpname}')
    logging.info(f'Added cron job: {cronjob}')


def main():
    """run it"""
    parser = argparse.ArgumentParser(
        description='Installs Wagtail (Django CMS) on an Opalstack account'
    )
    parser.add_argument('-i', dest='app_uuid', help='UUID of the base app', default=os.environ.get('UUID'))
    parser.add_argument('-n', dest='app_name', help='name of the base app', default=os.environ.get('APPNAME'))
    parser.add_argument('-t', dest='opal_token', help='API auth token', default=os.environ.get('OPAL_TOKEN'))
    parser.add_argument('-u', dest='opal_user', help='Opalstack account name', default=os.environ.get('OPAL_USER'))
    parser.add_argument('-p', dest='opal_password', help='Opalstack account password', default=os.environ.get('OPAL_PASS'))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')

    logging.info(f'Started installation of Wagtail app {args.app_name}')
    api = OpalstackAPITool(API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password)

    appinfo = api.get(f'/app/read/{args.app_uuid}')
    appdir = f'/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}'

    # create tmp dir
    os.mkdir(f'{appdir}/tmp', 0o700)
    logging.info(f'Created directory {appdir}/tmp')
    CMD_ENV['TMPDIR'] = f'{appdir}/tmp'

    # create virtualenv
    python_executable_path = run_command('which python3.12').decode('utf-8').strip()
    if not python_executable_path:
        logging.error('python3.12 not found on PATH')
        sys.exit(1)

    run_command(f'{python_executable_path} -m venv {appdir}/env')
    logging.info(f'Created virtualenv at {appdir}/env')

    # install uwsgi
    run_command(f'{appdir}/env/bin/pip install uwsgi')
    run_command(f'chmod 700 {appdir}/env/bin/uwsgi')
    logging.info('Installed latest uWSGI into virtualenv')

    # install django + wagtail
    run_command(f'{appdir}/env/bin/pip install django=={DJANGO_VERSION}')
    run_command(f'{appdir}/env/bin/pip install wagtail=={WAGTAIL_VERSION}')
    logging.info(f'Installed Django {DJANGO_VERSION} and Wagtail {WAGTAIL_VERSION} into virtualenv')

    # create wagtail project dir
    os.mkdir(f'{appdir}/{PROJECT_NAME}', 0o700)
    logging.info(f'Created Wagtail project directory {appdir}/{PROJECT_NAME}')

    # wagtail start <project> <dest>
    run_command(f'{appdir}/env/bin/wagtail start {PROJECT_NAME} {appdir}/{PROJECT_NAME}')
    logging.info(f'Populated Wagtail project directory {appdir}/{PROJECT_NAME}')

    settings_py = f'{appdir}/{PROJECT_NAME}/{PROJECT_NAME}/settings/base.py'
    if not os.path.exists(settings_py):
        # fallback for template variations
        settings_py = f'{appdir}/{PROJECT_NAME}/{PROJECT_NAME}/settings.py'

    # relaxed default config: ALLOWED_HOSTS + basic static/media roots
    if os.path.exists(settings_py):
        run_command(
            f'''sed -r -i "s/^ALLOWED_HOSTS\\s*=\\s*\\[[^\\]]*\\]/ALLOWED_HOSTS = \\['*'\\]/" {settings_py}'''
        )
        # Add STATIC_ROOT / MEDIA_ROOT if not already present
        append_cfg = textwrap.dedent(f"""
        # --- Opalstack defaults ---
        STATIC_ROOT = os.path.join(BASE_DIR, 'static_collected')
        MEDIA_ROOT = os.path.join(BASE_DIR, 'media')
        """).lstrip("\n")
        with open(settings_py, 'r') as f:
            contents = f.read()
        if 'STATIC_ROOT' not in contents or 'MEDIA_ROOT' not in contents:
            with open(settings_py, 'a') as f:
                f.write('\n' + append_cfg)
            logging.info(f'Appended STATIC_ROOT/MEDIA_ROOT to {settings_py}')
    else:
        logging.warning('Could not locate Wagtail settings file; skipping ALLOWED_HOSTS/static/media tweaks')

    # uwsgi config
    wsgi_file = f'{appdir}/{PROJECT_NAME}/{PROJECT_NAME}/wsgi.py'
    # (newer wagtail templates use config/ structure; try that too)
    if not os.path.exists(wsgi_file):
        alt = f'{appdir}/{PROJECT_NAME}/config/wsgi.py'
        if os.path.exists(alt):
            wsgi_file = alt

    python_path = f'{appdir}/{PROJECT_NAME}'

    uwsgi_conf = textwrap.dedent(f'''
    [uwsgi]
    master = True
    http-socket = 127.0.0.1:{appinfo["port"]}
    env = LD_LIBRARY_PATH=/usr/sqlite330/lib
    virtualenv = {appdir}/env/
    daemonize = /home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/uwsgi.log
    pidfile = {appdir}/tmp/uwsgi.pid

    workers = 2
    threads = 2

    # adjust the following to point to your project
    python-path = {python_path}
    wsgi-file = {wsgi_file}
    touch-reload = {wsgi_file}
    ''')
    create_file(f'{appdir}/uwsgi.ini', uwsgi_conf, perms=0o600)

    # start script (MUST start with shebang, no leading newline)
    start_script = textwrap.dedent(f"""\
#!/bin/bash
export TMPDIR={appdir}/tmp
export LD_LIBRARY_PATH=/usr/sqlite330/lib
mkdir -p {appdir}/tmp

PIDFILE="{appdir}/tmp/uwsgi.pid"
if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
  echo "uWSGI for {appinfo["name"]} already running."
  exit 99
fi

{appdir}/env/bin/uwsgi --ini {appdir}/uwsgi.ini
echo "Started uWSGI for {appinfo["name"]}."
""")
    create_file(f'{appdir}/start', start_script, perms=0o700)

    # stop script (MUST start with shebang, no leading newline)
    stop_script = textwrap.dedent(f"""\
#!/bin/bash
PIDFILE="{appdir}/tmp/uwsgi.pid"
if [ ! -e "$PIDFILE" ]; then
  echo "$PIDFILE missing, maybe uWSGI is already stopped?"
  exit 99
fi

PID=$(cat $PIDFILE)
if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
  {appdir}/env/bin/uwsgi --stop $PIDFILE
  sleep 3
fi

if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
  echo "uWSGI did not stop, killing it."
  sleep 3
  kill -9 $PID
fi

rm -f $PIDFILE
echo "Stopped."
""")
    create_file(f'{appdir}/stop', stop_script, perms=0o700)

    # cron (keep it alive like other installers)
    m = random.randint(0, 9)
    croncmd = f'0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1'
    add_cronjob(croncmd)

    # README
    readme = textwrap.dedent(f'''
    # Opalstack Wagtail README

    This installer created:
    - Python virtualenv: `{appdir}/env`
    - Wagtail project: `{appdir}/{PROJECT_NAME}`
    - uWSGI config: `{appdir}/uwsgi.ini`

    ## Post-install steps (required)

    1. Connect this app to a site route in the Opalstack control panel.

    2. Configure `ALLOWED_HOSTS` in your settings file (we defaulted to `['*']` for bootstrapping):
       - {appdir}/{PROJECT_NAME}/{PROJECT_NAME}/settings/base.py (common)
       - or {appdir}/{PROJECT_NAME}/{PROJECT_NAME}/settings.py (template fallback)

    3. Initialize the database, create an admin user, and collect static files:

       ```bash
       cd {appdir}/{PROJECT_NAME}
       source {appdir}/env/bin/activate
       python manage.py migrate
       python manage.py createsuperuser
       python manage.py collectstatic
       ```

       If you want SQLite for quick start, the default Wagtail template is fine.
       For production, use Postgres/MySQL and update `DATABASES` in settings.

    4. Restart your instance:

       ```bash
       {appdir}/stop
       {appdir}/start
       ```

    ## Notes

    - Wagtail uses Pillow for image handling. If image processing fails, it usually means
      system image libs are missing; contact support if you hit that.

    - Wagtail’s starter template was generated with:
      `wagtail start {PROJECT_NAME} {appdir}/{PROJECT_NAME}`

    ## More info

    - Wagtail docs: https://docs.wagtail.org/
    ''').lstrip('\n')
    create_file(f'{appdir}/README', readme)

    # start it
    run_command(f'{appdir}/start')

    # finished
    payload = json.dumps([{'id': args.app_uuid}])
    api.post('/app/installed/', payload)
    logging.info(f'Completed installation of Wagtail app {args.app_name}')


if __name__ == '__main__':
    main()
