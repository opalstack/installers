import os
user = os.getenv('USER')
name = os.getenv('APPNAME')
port = os.getenv('PORT')
keepalive_path = f'/home/{user}/apps/{name}/start'
keepalive = f'''#!/bin/bash
PIDFILE="$HOME/apps/{name}/tmp/{name}.pid"
if [ -e "${{PIDFILE}}" ] && (ps -u $(whoami) -opid= |
                           grep -P "^\s*$(cat ${{PIDFILE}})$" &> /dev/null); then
  echo "Already running."
  exit 99
fi
echo -n 'Started at '
date "+%Y-%m-%d %H:%M:%S"
/home/{user}/apps/{name}/env/bin/uwsgi --ini /home/{user}/apps/{name}/uwsgi.ini
'''
f = open(keepalive_path, 'w+')
f.write(keepalive)
f.close
print(f'Wrote {keepalive_path}')

kill_path = f'/home/{user}/apps/{name}/kill'
kill = f'''#!/bin/bash
kill -9 `cat $HOME/apps/{name}/tmp/{name}.pid`
'''

f = open(kill_path, 'w+')
f.write(kill)
f.close
print(f'Wrote {kill_path}')

stop_path = f'/home/{user}/apps/{name}/stop'
stop = f'''#!/bin/bash

PIDFILE="$HOME/apps/{name}/tmp/{name}.pid"
if [ -e "${{PIDFILE}}" ] && (ps -u $(whoami) -opid= |
                           grep -P "^\s*$(cat ${{PIDFILE}})$" &> /dev/null); then
/home/{user}/apps/{name}/env/bin/uwsgi --stop /home/{user}/apps/{name}/tmp/{name}.pid
rm  /home/{user}/apps/{name}/tmp/{name}.pid
  exit 99
fi
echo "No PID file"
'''

f = open(stop_path, 'w+')
f.write(stop)
f.close
print(f'Wrote {stop_path}')

uwsgi_ini_path = f'/home/{user}/apps/{name}/uwsgi.ini'
uwsgi_ini = f'''[uwsgi]
master = True
http = 127.0.0.1:{port}
virtualenv = /home/{user}/apps/{name}/env/
daemonize = /home/{user}/logs/{name}/uwsgi.log
pidfile = /home/{user}/apps/{name}/tmp/{name}.pid
workers = 2
threads = 2

# adjust the following to point to your project
wsgi-file = /home/{user}/apps/{name}/myapp/wsgi.py
touch-reload = /home/{user}/apps/{name}/myapp/wsgi.py
'''
f = open(uwsgi_ini_path, 'w+')
f.write(uwsgi_ini)
f.close
print(f'Wrote {uwsgi_ini_path}')

myapp_wsgi_path = f'/home/{user}/apps/{name}/myapp/wsgi.py'
myapp_wsgi = f'''def application(env, start_response):
    start_response('200 OK', [('Content-Type','text/html')])
    return [b'Hello World!']
'''
os.mkdir(f'/home/{user}/apps/{name}/myapp', mode=0o700)
f = open(myapp_wsgi_path, 'w+')
f.write(myapp_wsgi)
f.close
print(f'Wrote {myapp_wsgi_path}')
