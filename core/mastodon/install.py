#!/usr/local/bin/python3.11

import argparse
import http.client
import json
import logging
import os
import os.path
import random
import secrets
import shlex
import string
import subprocess
import sys
import textwrap
import time
import urllib.request
from urllib.parse import urlparse

API_HOST = os.environ.get("API_URL").strip("https://").strip("http://")
API_BASE_URI = "/api/v1"
CMD_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "UMASK": "0002",
}
CMD_PREFIX = '/bin/scl enable devtoolset-11 nodejs20 ruby32 rh-redis5 -- '
MASTODON_VERSION = "4.2.7"


class OpalstackAPITool:
    """simple wrapper for http.client get and post"""

    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host
        self.base_uri = base_uri

        # if there is no auth token, then try to log in with provided credentials
        if not authtoken:
            endpoint = self.base_uri + "/login/"
            payload = json.dumps({"username": user, "password": password})
            conn = http.client.HTTPSConnection(self.host)
            conn.request(
                "POST", endpoint, payload, headers={"Content-type": "application/json"}
            )
            result = json.loads(conn.getresponse().read())
            if not result.get("token"):
                logging.warn(
                    "Invalid username or password and no auth token provided, exiting."
                )
                sys.exit()
            else:
                authtoken = result["token"]

        self.headers = {
            "Content-type": "application/json",
            "Authorization": f"Token {authtoken}",
        }

    def get(self, endpoint):
        """GETs an API endpoint"""
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request("GET", endpoint, headers=self.headers)
        return json.loads(conn.getresponse().read())

    def post(self, endpoint, payload):
        """POSTs data to an API endpoint"""
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request("POST", endpoint, payload, headers=self.headers)
        return json.loads(conn.getresponse().read())


def run_command(cmd, env, cwd=None, use_shlex=True):
    """runs a command, returns output"""
    logging.info(f"Running: {cmd}")
    # add scl env to commands
    cmd = CMD_PREFIX + cmd
    try:
        if use_shlex:
            cmd = shlex.split(cmd)
        result = subprocess.check_output(cmd, cwd=cwd, env=env)
        return result
    except subprocess.CalledProcessError as e:
        logging.debug(e.output)


def create_file(path, contents, writemode="w", perms=0o600):
    """make a file, perms are passed as octal"""
    with open(path, writemode) as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f"Created file {path} with permissions {oct(perms)}")


def append_file(path, contents, writemode="a"):
    """append a file"""
    with open(path, writemode) as f:
        f.write(contents)
    logging.info(f"Appended file {path}")


def download(url, localfile, writemode="wb", perms=0o600):
    """save a remote file, perms are passed as octal"""
    logging.info(f"Downloading {url} as {localfile} with permissions {oct(perms)}")
    urllib.request.urlretrieve(url, filename=localfile)
    os.chmod(localfile, perms)
    logging.info(f"Downloaded {url} as {localfile} with permissions {oct(perms)}")


def gen_password(length=20):
    """makes a random password"""
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for i in range(length))


def add_cronjob(cronjob, env):
    """appends a cron job to the user's crontab"""
    homedir = os.path.expanduser("~")
    tmpname = f"{homedir}/.tmp{gen_password()}"
    tmp = open(tmpname, "w")
    subprocess.run("crontab -l".split(), stdout=tmp)
    tmp.write(f"{cronjob}\n")
    tmp.close()
    cmd = f"crontab {tmpname}"
    doit = run_command(cmd, env)
    cmd = run_command(f"rm -f {tmpname}", env)
    logging.info(f"Added cron job: {cronjob}")


def main():
    """run it"""
    # grab args from cmd or env
    parser = argparse.ArgumentParser(
        description="Installs Mastodon web app on Opalstack account"
    )
    parser.add_argument(
        "-i",
        dest="app_uuid",
        help="UUID of the base app",
        default=os.environ.get("UUID"),
    )
    parser.add_argument(
        "-n",
        dest="app_name",
        help="name of the base app",
        default=os.environ.get("APPNAME"),
    )
    parser.add_argument(
        "-t",
        dest="opal_token",
        help="API auth token",
        default=os.environ.get("OPAL_TOKEN"),
    )
    parser.add_argument(
        "-u",
        dest="opal_user",
        help="Opalstack account name",
        default=os.environ.get("OPAL_USER"),
    )
    parser.add_argument(
        "-p",
        dest="opal_password",
        help="Opalstack account password",
        default=os.environ.get("OPAL_PASS"),
    )
    args = parser.parse_args()

    # init logging
    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s"
    )
    # go!
    logging.info(f"Started installation of Mastodon app {args.app_name}")
    api = OpalstackAPITool(
        API_HOST, API_BASE_URI, args.opal_token, args.opal_user, args.opal_password
    )
    appinfo = api.get(f"/app/read/{args.app_uuid}")
    appdir = f'/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}'
    CMD_ENV = {
        "RAILS_ENV": "production",
        "PATH": f"{appdir}/node/bin:{appdir}/mastodon/bin:/usr/local/bin:/usr/bin:/bin:/usr/pgsql-11/bin/",
        "LD_LIBRARY_PATH": f"{appdir}/mastodon/lib",
        "TMPDIR": f"{appdir}/tmp",
        "GEM_HOME": f"{appdir}/mastodon",
        "UMASK": "0002",
        "HOME": f'/home/{appinfo["osuser_name"]}',
    }
    sh = "/bin/sh -c"

    # create database and database user
    db_name = f"{args.app_name[:8]}_{args.app_uuid[:8]}"
    db_pass = gen_password()

    # # create database user
    payload = json.dumps(
        [
            {
                "server": appinfo["server"],
                "name": db_name,
                "password": db_pass,
                "external": "false",
            }
        ]
    )
    user_attempts = 0
    while True:
        logging.info(f"Trying to create database user {db_name}")
        psql_user = api.post(f"/psqluser/create/", payload)
        time.sleep(5)

        existing_psql_users = api.get(f"/psqluser/list/")
        check_existing = json.loads(json.dumps(existing_psql_users))

        for check in check_existing:
            if check["name"] == db_name:
                logging.info(f"Database user {db_name} created")
                payload = json.dumps(
                    [
                        {
                            "server": appinfo["server"],
                            "name": db_name,
                            "dbusers_readwrite": [check["id"]],
                        }
                    ]
                )
                user_created = True
        if user_created:
            break
        else:
            user_attempts += 1
            if user_attempts > 10:
                logging.info(f"Could not create database user {db_name}")
                sys.exit()

    # create database
    db_attempts = 0
    while True:
        db_created = False
        logging.info(f"Trying to create database {db_name}")
        psql_user = api.post(f"/psqldb/create/", payload)
        time.sleep(5)

        existing_psql_db = api.get(f"/psqldb/list/")
        check_existing = json.loads(json.dumps(existing_psql_db))

        for check in check_existing:
            if check["name"] == db_name:
                logging.info(f"Database {db_name} created")
                payload = json.dumps(
                    [{"id": [check["id"]], "password": db_pass, "external": "false"}]
                )
                psql_password = api.post(f"/psqluser/update/", payload)
                db_created = True
        if db_created:
            break
        else:
            db_attempts += 1
            if db_attempts > 10:
                logging.info(f"Could not create database {db_name}")
                sys.exit()

    # install mastodon
    cmd = f"git clone https://github.com/mastodon/mastodon.git mastodon"
    doit = run_command(cmd, CMD_ENV, cwd=f"{appdir}")
    cmd = f"git checkout -f v{MASTODON_VERSION}"
    doit = run_command(cmd, CMD_ENV, cwd=f"{appdir}/mastodon")
    tmp_dir = f"{appdir}/mastodon/tmp"
    if not os.path.isdir(tmp_dir):
        os.mkdir(tmp_dir)
    pid_dir = f"{appdir}/mastodon/tmp/pids"
    if not os.path.isdir(pid_dir):
        os.mkdir(pid_dir)
    socket_dir = f"{appdir}/mastodon/tmp/sockets"
    if not os.path.isdir(socket_dir):
        os.mkdir(socket_dir)
    tmp_dir = f"{appdir}/tmp"
    if not os.path.isdir(tmp_dir):
        os.mkdir(tmp_dir)
    cache_dir = f"{appdir}/tmp/cache"
    if not os.path.isdir(cache_dir):
        os.mkdir(cache_dir)
    nginx_dir = f"{appdir}/tmp/cache/nginx"
    if not os.path.isdir(nginx_dir):
        os.mkdir(nginx_dir)

    # set up yarn
    cmd = f'mkdir -p {appdir}/node/bin'
    doit = run_command(cmd, CMD_ENV)
    cmd = f'corepack enable --install-directory={appdir}/node/bin'
    doit = run_command(cmd, CMD_ENV, cwd=f'{appdir}/node')
    cmd = "yarn set version classic"
    doit = run_command(cmd, CMD_ENV)

    # install dependencies
    doit = run_command(cmd, CMD_ENV, cwd=f"{appdir}/mastodon/")
    cmd = "bundle config deployment 'true'"
    doit = run_command(cmd, CMD_ENV, cwd=f"{appdir}/mastodon/")
    cmd = "bundle config without 'development test'"
    doit = run_command(cmd, CMD_ENV, cwd=f"{appdir}/mastodon/")
    cmd = "bundle config set jobs 4"
    doit = run_command(cmd, CMD_ENV, cwd=f"{appdir}/mastodon/")
    cmd = "bundle install"
    doit = run_command(cmd, CMD_ENV, cwd=f"{appdir}/mastodon/")
    cmd = "yarn install --pure-lockfile"
    doit = run_command(cmd, CMD_ENV, cwd=f"{appdir}/mastodon/")

    # redis config
    redis_conf = textwrap.dedent(
        f"""\
                # create a unix domain socket to listen on
                port 0
                unixsocket /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/tmp/sockets/redis.sock
                unixsocketperm 700
                daemonize no
                """
    )
    create_file(f"{appdir}/mastodon/redis.conf", redis_conf, perms=0o664)

    # nginx config
    nginx_conf = textwrap.dedent(
        f"""\
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

                    proxy_cache_path /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/tmp/cache/nginx levels=1:2 keys_zone=CACHE:10m inactive=7d max_size=1g;

                    map $http_upgrade $connection_upgrade {{
                        default upgrade;
                        ''      close;
                    }}

                    upstream puma {{
                        server unix://home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/tmp/sockets/puma.sock fail_timeout=0;
                    }}

                    upstream streaming {{
                        server unix://home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/tmp/sockets/streaming.sock fail_timeout=0;

                    }}

                    server {{
                        # change the next two lines to use your site domain
                        # and your project's public directory
                        server_name localhost;
                        root /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/public;

                        listen {appinfo["port"]};
                        keepalive_timeout 70;
                        sendfile on;
                        client_max_body_size 80m;

                        try_files $uri/index.html $uri @puma;

                        location @puma {{
                            proxy_pass http://puma;
                            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
                            proxy_set_header Host $http_host;
                            proxy_redirect off;
                        }}

                        location / {{
                            try_files $uri @puma;
                        }}

                        location = /sw.js {{
                            add_header Cache-Control "public, max-age=604800, must-revalidate";
                            add_header Strict-Transport-Security "max-age=63072000; includeSubDomains";
                            try_files $uri =404;
                        }}

                        location ~ ^/assets/ {{
                            add_header Cache-Control "public, max-age=2419200, must-revalidate";
                            add_header Strict-Transport-Security "max-age=63072000; includeSubDomains";
                            try_files $uri =404;
                        }}

                        location ~ ^/avatars/ {{
                            add_header Cache-Control "public, max-age=2419200, must-revalidate";
                            add_header Strict-Transport-Security "max-age=63072000; includeSubDomains";
                            try_files $uri =404;
                        }}

                        location ~ ^/emoji/ {{
                            add_header Cache-Control "public, max-age=2419200, must-revalidate";
                            add_header Strict-Transport-Security "max-age=63072000; includeSubDomains";
                            try_files $uri =404;
                        }}

                        location ~ ^/headers/ {{
                            add_header Cache-Control "public, max-age=2419200, must-revalidate";
                            add_header Strict-Transport-Security "max-age=63072000; includeSubDomains";
                            try_files $uri =404;
                        }}

                        location ~ ^/packs/ {{
                            add_header Cache-Control "public, max-age=2419200, must-revalidate";
                            add_header Strict-Transport-Security "max-age=63072000; includeSubDomains";
                            try_files $uri =404;
                        }}

                        location ~ ^/shortcuts/ {{
                            add_header Cache-Control "public, max-age=2419200, must-revalidate";
                            add_header Strict-Transport-Security "max-age=63072000; includeSubDomains";
                            try_files $uri =404;
                        }}

                        location ~ ^/sounds/ {{
                            add_header Cache-Control "public, max-age=2419200, must-revalidate";
                            add_header Strict-Transport-Security "max-age=63072000; includeSubDomains";
                            try_files $uri =404;
                        }}

                        location ~ ^/system/ {{
                            add_header Cache-Control "public, max-age=2419200, immutable";
                            add_header Strict-Transport-Security "max-age=63072000; includeSubDomains";
                            try_files $uri =404;
                        }}

                        location ^~ /api/v1/streaming {{
                            proxy_set_header Host $host;
                            proxy_set_header X-Real-IP $remote_addr;
                            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
                            proxy_set_header X-Forwarded-Proto $scheme;
                            proxy_set_header Proxy "";

                            proxy_pass http://streaming;
                            proxy_buffering off;
                            proxy_redirect off;
                            proxy_http_version 1.1;
                            proxy_set_header Upgrade $http_upgrade;
                            proxy_set_header Connection $connection_upgrade;

                            add_header Strict-Transport-Security "max-age=63072000; includeSubDomains";

                            tcp_nodelay on;
                        }}

                        error_page 404 500 501 502 503 504 /500.html;
                    }}
                }}
                """
    )
    os.mkdir(f"{appdir}/nginx")
    create_file(f"{appdir}/nginx/nginx.conf", nginx_conf, perms=0o600)

    # supervisord config
    supervisord_conf = textwrap.dedent(
        f"""\
                [unix_http_server]
                file=/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/tmp/sockets/supervisor.sock

                [supervisord]
                logfile=/home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/supervisord.log
                logfile_maxbytes=50MB
                logfile_backups=10
                loglevel=info
                pidfile=/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/tmp/pids/supervisord.pid

                [rpcinterface:supervisor]
                supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

                [supervisorctl]
                serverurl=unix:///home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/tmp/sockets/supervisor.sock

                [program:redis]
                directory=/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon
                command=redis-server /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/redis.conf
                stdout_logfile=/home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/redis.log
                stderr_logfile=/home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/redis.log

                [program:puma]
                directory=/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon
                environment=
                    LD_PRELOAD=/usr/lib/libjemalloc.so,
                    RAILS_ENV=production,
                    SOCKET=/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/tmp/sockets/puma.sock
                command=bundle exec puma -C /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/config/puma.rb --pidfile /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/tmp/pids/puma.pid
                stdout_logfile=/home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/puma.log
                stderr_logfile=/home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/puma.log

                [program:sidekiq]
                directory=/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon
                environment=
                    LD_PRELOAD=/usr/lib/libjemalloc.so
                command=bundle exec sidekiq -e production -C config/sidekiq.yml
                stdout_logfile=/home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/sidekiq.log
                stderr_logfile=/home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/sidekiq.log

                [program:streaming]
                directory=/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon
                environment=
                    SOCKET="/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/tmp/sockets/streaming.sock",
                    REDIS_URL="unix:///home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/tmp/sockets/redis.sock",
                    NODE_ENV="production",
                    STREAMING_CLUSTER_NUM="1"
                command=node ./streaming
                stdout_logfile=/home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/streaming.log
                stderr_logfile=/home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/streaming.log

                [program:nginx]
                directory=/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/
                command=/usr/sbin/nginx -c /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/nginx/nginx.conf -p /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]} -e /home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/nginx_error.log -g "daemon off;"
                stdout_logfile=/home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/nginx.log
                stderr_logfile=/home/{appinfo["osuser_name"]}/logs/apps/{appinfo["name"]}/nginx.log
                """
    )
    create_file(f"{appdir}/supervisord.conf", supervisord_conf, perms=0o600)

    # start script
    start_script = textwrap.dedent(
        f"""\
                #!/bin/bash

                # name of your app, don't change this
                APPNAME={appinfo["name"]}

                # change the next line to your Mastodon project directory
                PROJECTDIR=$HOME/apps/$APPNAME/mastodon

                # set the rails env
                RAILS_ENV=production

                # no need to edit below this line
                export PATH=$HOME/apps/$APPNAME/node/bin:$PROJECTDIR/bin:$PATH
                source scl_source enable devtoolset-11 nodejs20 ruby32 rh-redis5
                PIDFILE="$PROJECTDIR/tmp/pids/supervisord.pid"

                # clean up streaming socket if node isn't running
                pgrep -f "node ./streaming" > /dev/null || (test -S $PROJECTDIR/tmp/sockets/streaming.sock &&  rm -f $PROJECTDIR/tmp/sockets/streaming.sock)

                if [ -e "$PIDFILE" ] && (pgrep -u {appinfo["osuser_name"]} | grep -x -f $PIDFILE &> /dev/null); then
                  echo "$APPNAME supervisord agent already running!"
                  PYTHONPATH=$PROJECTDIR/bin/ $PROJECTDIR/bin/supervisorctl -c /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/supervisord.conf start all
                else
                  echo "$APPNAME supervisord agent already not running, starting!"
                  PYTHONPATH=$PROJECTDIR/bin/ $PROJECTDIR/bin/supervisord -c /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/supervisord.conf
                fi

                """
    )
    create_file(f"{appdir}/start", start_script, perms=0o700)

    # stop script
    stop_script = textwrap.dedent(
        f"""\
                #!/bin/bash

                PYTHONPATH=/home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/bin/ /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/bin/supervisorctl -c /home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/supervisord.conf stop all
                """
    )
    create_file(f"{appdir}/stop", stop_script, perms=0o700)

    # restart script
    restart_script = textwrap.dedent(
        f"""\
                #!/bin/bash

                # name of your app, don't change this
                APPNAME={appinfo["name"]}

                $HOME/apps/$APPNAME/stop
                sleep 5
                $HOME/apps/$APPNAME/start
                """
    )
    create_file(f"{appdir}/restart", restart_script, perms=0o700)

    # setenv script
    setenv = textwrap.dedent(
        f"""\
                #!/bin/bash

                # name of your app, don't change this
                APPNAME={appinfo["name"]}

                # change the next line to your Mastodon checkout  directory
                PROJECTDIR=$HOME/apps/$APPNAME/mastodon

                # set your rails env, eg development or production
                RAILS_ENV=production

                # no need to edit below this line
                export LD_PRELOAD=/usr/lib64/libjemalloc.so
                export PATH=$HOME/apps/$APPNAME/node/bin:$HOME/apps/$APPNAME/mastodon/bin:/usr/local/bin:/bin:/usr/bin:/usr/local/sbin:/usr/sbin:/opt/puppetlabs/bin:/usr/pgsql-11/bin/:$HOME/.local/bin:$HOME/bin:$PATH
                export GEM_PATH=$HOME/apps/$APPNAME/mastodon/vendor/bundle/ruby/gems
                export GEM_HOME=$HOME/apps/$APPNAME/mastodon/
                export RAILS_ENV=$RAILS_ENV
                source scl_source enable devtoolset-11 nodejs20 ruby32 rh-redis5
                """
    )
    create_file(f"{appdir}/setenv", setenv, perms=0o600)

    # .env.production config
    env_production = textwrap.dedent(
        f"""\
                # See https://docs.joinmastodon.org/admin/config/ for all options available.

                RAILS_ENV=production

                # Federation
                # ----------
                # This identifies your server and cannot be changed safely later
                # ----------
                LOCAL_DOMAIN=localhost

                # Redis
                # # -----
                REDIS_URL=unix:///home/{appinfo["osuser_name"]}/apps/{appinfo["name"]}/mastodon/tmp/sockets/redis.sock

                # PostgreSQL
                # ----------
                DB_HOST=localhost
                DB_USER={db_name}
                DB_NAME={db_name}
                DB_PASS={db_pass}
                DB_PORT=5432

                # Secrets
                # -------
                SECRET_KEY_BASE={gen_password(128)}
                OTP_SECRET={gen_password(128)}

                # Sending mail
                # ------------
                SMTP_SERVER=smtp.us.opalstack.com
                SMTP_PORT=465
                SMTP_LOGIN=
                SMTP_PASSWORD=
                SMTP_FROM_ADDRESS=
                SMTP_SSL=true
                SMTP_ENABLE_STARTTLS_AUTO=false
                SMTP_AUTH_METHOD=plain
                SMTP_OPENSSL_VERIFY_MODE=none
                SMTP_DELIVERY_METHOD=smtp

                # Web Push
                # --------
                """
    )
    create_file(f"{appdir}/mastodon/.env.production", env_production, perms=0o664)

    # change_domain.py script
    change_domain = textwrap.dedent(
        '''\
                #!/usr/bin/env python3.10

                import argparse
                import logging
                import sys
                import textwrap

                import psycopg2


                def replace_text(input_file, search_text, replace_text):
                    """Replace text in a file"""
                    with open(input_file, "r") as file:
                        filedata = file.read()

                    filedata = filedata.replace(search_text, replace_text)
                    filedata = filedata.replace(f"DB_HOST={replace_text}", f"DB_HOST={search_text}")

                    with open(input_file, "w") as file:
                        file.write(filedata)


                def find_in_file(input_file, find_line):
                    with open(input_file) as f:
                        lines = f.readlines()
                        for line in lines:
                            if line.startswith(find_line):
                                value = line.split("=")[1].strip()
                    return value


                def execute_sql(database, database_user, database_password, domain):
                    """Execute sql command"""
                    connection = psycopg2.connect(
                        database=database,
                        user=database_user,
                        password=database_password,
                        host="localhost",
                        port="5432",
                    )

                    connection.autocommit = True
                    cursor = connection.cursor()
                    statement = f"UPDATE accounts SET username='{domain}' WHERE id='-99'"
                    cursor.execute(statement)
                    connection.commit()
                    connection.close()


                def main():
                    """run it"""
                    # grab args from cmd
                    parser = argparse.ArgumentParser(
                        description="Changes the password of your Mastodon app"
                    )
                    parser.add_argument(
                        "-b",
                        dest="bypass",
                        help="Bypass warning acknowledgement",
                        action="store_true",
                    )
                    parser.add_argument(
                        "-n",
                        dest="new_domain",
                        help="New domain of Mastdon app",
                        required=True,
                    )
                    parser.add_argument(
                        "-o",
                        dest="old_domain",
                        help="New domain of Mastdon app",
                        required=True,
                    )

                    args = parser.parse_args()

                    if not args.bypass:
                        warning = textwrap.dedent(
                            """
                            ##############################  WARNING  ##############################

                            Only change your domain at initial set up. Changing the domain is not
                            recommended after server is set up as it will will cause remote servers
                            to confuse your existing accounts with entirely new ones.

                            See more in the Federation section of
                            https://docs.joinmastodon.org/admin/config/

                            #######################################################################

                            Type "yes" to continue or "no" to exit.

                            #######################################################################
                            """
                        )

                        answer = input(warning)
                        if answer == "yes":
                            pass
                        elif answer == "no":
                            sys.exit()
                        else:
                            print("Please enter yes or no.")

                    # init logging
                    logging.basicConfig(
                        level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s"
                    )

                    # set variables
                    old_domain = args.old_domain
                    new_domain = args.new_domain
                    config_file = "mastodon/.env.production"
                    nginx_file = "nginx/nginx.conf"

                    # get database infomation from config file
                    logging.info(f"Finding the database infomation from {config_file}")
                    database = find_in_file(config_file, "DB_NAME")
                    database_user = find_in_file(config_file, "DB_USER")
                    database_password = find_in_file(config_file, "DB_PASS")

                    # go!
                    logging.info(f"Replacing domain in {config_file}")
                    replace_text(config_file, old_domain, new_domain)

                    logging.info(f"Replacing domain in {nginx_file}")
                    replace_text(nginx_file, old_domain, new_domain)

                    logging.info(f"Replacing domain in database {database}")
                    execute_sql(database, database_user, database_password, new_domain)

                    logging.info(f"Completed changing domain of Mastodon app")


                if __name__ == "__main__":
                    main()

                '''
    )
    create_file(f"{appdir}/change_domain.py", change_domain, perms=0o775)

    # populate database
    cmd = f"bundle exec rails db:schema:load"
    doit = run_command(cmd, CMD_ENV, cwd=f"{appdir}/mastodon")
    cmd = f"bundle exec rails db:seed"
    doit = run_command(cmd, CMD_ENV, cwd=f"{appdir}/mastodon")

    # precomile assets
    cmd = f"bundle exec rails assets:precompile"
    doit = run_command(cmd, CMD_ENV, cwd=f"{appdir}/mastodon")

    # generate_vapid_key
    cmd = 'bundle exec rake mastodon:webpush:generate_vapid_key'
    vapid_keys = run_command(cmd, CMD_ENV, cwd=f"{appdir}/mastodon/")
    conf = open(f'{appdir}/mastodon/.env.production', 'a')
    conf.write(vapid_keys.decode())
    conf.close()

    # install supervisord
    cmd = f"pip3.11 install --target={appdir}/mastodon/bin/ supervisor"
    doit = run_command(cmd, CMD_ENV, cwd=f"{appdir}")
    cmd = f"rsync -r bin/bin/ bin/"
    doit = run_command(cmd, CMD_ENV, cwd=f"{appdir}/mastodon")
    cmd = f"rm -rf bin"
    doit = run_command(cmd, CMD_ENV, cwd=f"{appdir}/mastodon/bin")

    # cron
    m = random.randint(0, 9)
    croncmd = f"0{m},1{m},2{m},3{m},4{m},5{m} * * * * {appdir}/start > /dev/null 2>&1"
    cronjob = add_cronjob(croncmd, CMD_ENV)

    # make README
    readme = textwrap.dedent(
        f"""\
                # Opalstack Mastodon README

                ## Post-install steps

                Please take the following steps before you begin to use your Mastodon instance:

                1. Connect your Mastodon application to a site at https://my.opalstack.com/domains/.

                2. Configure Mastodon to use the site domain as follows, replacing mydomain.com with your site domain from step 1:

                        cd {appdir}
                        ./change_domain.py -o localhost -n mydomain.com

                3. Edit {appdir}/mastodon/.env.production to configure the site's email settings:

                        SMTP_SERVER=SMTP server
                        SMTP_LOGIN=Mailbox name
                        SMTP_PASSWORD=Mailbox password (enclose in 'single quotes' if it contains any $ characters)
                        SMTP_FROM_ADDRESS=Email address

                4. Run the following command to restart your app:

                        {appdir}/restart

                5. Create a Mastodon admin user as follows, replacing "username" and "username@example.com" with your own choices:

                        cd {appdir}/mastodon
                        source ../setenv
                        RAILS_ENV=production bundle exec bin/tootctl accounts create username --email username@example.com --confirmed --role Owner

                6. Visit the site you created in step 1 to log in.

                7. Follow the steps at https://docs.joinmastodon.org/admin/setup/ to complete the setup.

                For further info please see: https://docs.joinmastodon.org/
                """
    )
    create_file(f"{appdir}/README", readme)

    # finished, push a notice
    payload = json.dumps([{"id": args.app_uuid}])
    finished = api.post("/app/installed/", payload)
    msg = f'Installation of Mastodon app {appinfo["name"]} is complete. See README in the app directory on your server for mandatory configuration steps.'
    payload = json.dumps([{"type": "M", "content": msg}])
    notice = api.post("/notice/create/", payload)
    logging.info(msg)


if __name__ == "__main__":
    main()
