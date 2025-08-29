#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
Discourse on Opalstack (EL9 + Podman, rootless) — unified one-click installer

WHAT THIS DOES:
- Writes scripts, env, and a panel notice. No long-running tasks here.
- The generated **start** script:
  - (Always) recreates the pod bound to the app port and starts postgres/redis/web.
  - (First run only) performs install work, then writes ".installed".
  - (Subsequent runs) just starts cleanly without redoing install work.

EXTRAS:
- debug_on / debug_off scripts toggle a safe prepend-based CSS/hostname patch and clear caches.
- stop / update scripts included.
"""

import argparse, sys, os, json, logging, http.client, subprocess, shlex, secrets, string, textwrap, time

# ---------- Config ----------
API_URL = (os.environ.get('OPAL_API_URL') or os.environ.get('API_URL') or 'https://my.opalstack.com').rstrip('/')
API_HOST = API_URL.replace('https://', '').replace('http://', '')
API_BASE_URI = '/api/v1'

CMD_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'UMASK': '0002'}

IMG_WEB   = os.environ.get('DISCOURSE_IMAGE') or 'docker.io/tiredofit/discourse:latest'
IMG_REDIS = os.environ.get('REDIS_IMAGE')     or 'docker.io/library/redis:7-alpine'
IMG_PG    = os.environ.get('POSTGRES_IMAGE')  or 'docker.io/library/postgres:16-alpine'  # UID 999

# ---------- API ----------
class OpalAPI:
    def __init__(self, host, base_uri, token=None, user=None, password=None):
        self.host, self.base = host, base_uri
        if not token:
            conn = http.client.HTTPSConnection(self.host)
            payload = json.dumps({'username': user, 'password': password})
            conn.request('POST', f'{self.base}/login/', payload, headers={'Content-type':'application/json'})
            resp = conn.getresponse()
            data = json.loads(resp.read() or b'{}')
            if not data.get('token'):
                logging.error(f'Auth failed (HTTP {resp.status}).'); sys.exit(1)
            token = data['token']
        self.h = {'Content-type': 'application/json', 'Authorization': f'Token {token}'}

    def get(self, path):
        conn = http.client.HTTPSConnection(self.host)
        conn.request('GET', f'{self.base}{path}', headers=self.h)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b'{}')
        if resp.status >= 400:
            logging.error(f'GET {path} -> HTTP {resp.status}'); sys.exit(1)
        return data

    def post(self, path, payload):
        conn = http.client.HTTPSConnection(self.host)
        conn.request('POST', f'{self.base}{path}', payload, headers=self.h)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b'{}')
        if resp.status >= 400:
            logging.error(f'POST {path} -> HTTP {resp.status}: {data}')
        return data

# ---------- helpers ----------
def pw(n=24):
    import secrets, string
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(n))

def sh(cmd, cwd=None, env=CMD_ENV, check=True):
    logging.info(f'$ {cmd}')
    try:
        out = subprocess.check_output(shlex.split(cmd), cwd=cwd, env=env, stderr=subprocess.STDOUT)
        return out.decode('utf-8', 'ignore')
    except subprocess.CalledProcessError as e:
        logging.error(f'Command failed ({e.returncode}): {cmd}')
        logging.error(e.output.decode('utf-8', 'ignore'))
        if check: sys.exit(e.returncode)
        return e.output.decode('utf-8', 'ignore')

def write(path, content, mode=0o600):
    with open(path, 'w') as f: f.write(content)
    os.chmod(path, mode)
    logging.info(f'Created file {path} perms={oct(mode)}')

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description='Install Discourse (Podman) on Opalstack')
    ap.add_argument('-i', dest='uuid',       default=os.environ.get('UUID'))
    ap.add_argument('-n', dest='name',       default=os.environ.get('APPNAME'))
    ap.add_argument('-t', dest='token',      default=os.environ.get('OPAL_TOKEN'))
    ap.add_argument('-u', dest='user',       default=os.environ.get('OPAL_USER'))
    ap.add_argument('-p', dest='password',   default=os.environ.get('OPAL_PASS'))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
    if not args.uuid:
        logging.error('Missing app UUID (-i)'); sys.exit(1)

    api = OpalAPI(API_HOST, API_BASE_URI, args.token, args.user, args.password)
    app = api.get(f'/app/read/{args.uuid}')
    if not app.get('name'):
        logging.error('App not found.'); sys.exit(1)

    appname = app['name']
    osuser  = app['osuser_name']
    port    = int(app['port'])
    appdir  = f'/home/{osuser}/apps/{appname}'
    logdir  = f'/home/{osuser}/logs/apps/{appname}'

    logging.info(f'Preparing Discourse for app "{appname}" (port {port})')

    # dirs
    sh(f'mkdir -p {appdir}/data {appdir}/data/logs {appdir}/data/plugins {appdir}/data/cache')
    sh(f'mkdir -p {appdir}/pgdata')
    sh(f'mkdir -p {logdir}')

    # ---- creds/env (self-contained DB) ----
    db_user = f"disc_{args.uuid[:8]}".lower()
    db_name = db_user
    db_pass = pw(24)
    admin_user  = 'admin'
    admin_email = f'{admin_user}@example.com'
    admin_pass  = pw(16)

    env = textwrap.dedent(f"""\
    # ==== Discourse env ====
    # Postgres (container in pod)
    DB_HOST=127.0.0.1
    DB_PORT=5432
    DB_NAME={db_name}
    DB_USER={db_user}
    DB_PASS={db_pass}

    # Redis (container in pod)
    REDIS_HOST=127.0.0.1
    REDIS_PORT=6379

    # Admin bootstrap (change if desired — some images auto-use these)
    ADMIN_USER={admin_user}
    ADMIN_EMAIL={admin_email}
    ADMIN_PASS={admin_pass}

    # Logging
    LOG_PATH=/data/logs
    LOG_LEVEL=info

    # Optional: set your site hostname (recommended)
    DISCOURSE_HOSTNAME=wildcard.local
    """)
    write(f'{appdir}/.env', env, 0o600)

    # ---- scripts ----
    start = textwrap.dedent(f"""\
    #!/bin/bash
    # Unified start: always (re)creates pod + containers. On first run, performs install, writes ".installed".
    set -Eeuo pipefail
    export CONTAINERS_CGROUP_MANAGER=cgroupfs

    APP="{appname}"
    POD="$APP-pod"
    APPDIR="{appdir}"
    LOGDIR="{logdir}"
    PORT="{port}"
    IMG_WEB="{IMG_WEB}"
    IMG_REDIS="{IMG_REDIS}"
    IMG_PG="{IMG_PG}"
    INSTALLED_FILE="$APPDIR/.installed"

    echo "==> [{appname}] start: images:"
    echo "    web:   $IMG_WEB"
    echo "    redis: $IMG_REDIS"
    echo "    pg:    $IMG_PG"
    echo "==> port: $PORT"

    # Light touch maintenance
    podman system renumber || true

    # Recreate pod on the exact app port
    podman pod rm -f "$POD" >/dev/null 2>&1 || true
    if ss -ltn '( sport = :'$PORT' )' | grep -q LISTEN; then
      echo "ERROR: Port $PORT already in use on host."
      ss -ltnp '( sport = :'$PORT' )' || true
      exit 1
    fi
    podman pod create --name "$POD" -p 127.0.0.1:${{PORT}}:3000 >/dev/null

    # Ensure dirs and perms
    mkdir -p "$LOGDIR" "$APPDIR/pgdata" "$APPDIR/data" "$APPDIR/data/logs" "$APPDIR/data/plugins" "$APPDIR/data/cache"
    chmod 0777 "$LOGDIR" || true
    # Avoid duplicate Automation plugin (present in image)
    rm -rf "$APPDIR/data/plugins/automation" || true

    # Postgres storage perms within userns (UID 999 is postgres in the alpine image)
    podman unshare chown -R 999:999 "$APPDIR/pgdata" || true
    podman unshare chmod -R 0770 "$APPDIR/pgdata" || true

    # Export env (DB creds, hostname, etc)
    set -a; source "$APPDIR/.env"; set +a

    echo "==> start postgres"
    podman rm -f "$APP-postgres" >/dev/null 2>&1 || true
    podman run --cgroups=disabled -d --name "$APP-postgres" --pod "$POD" \\
      -e POSTGRES_USER="$DB_USER" \\
      -e POSTGRES_PASSWORD="$DB_PASS" \\
      -e POSTGRES_DB="$DB_NAME" \\
      -v "$APPDIR/pgdata:/var/lib/postgresql/data" \\
      "$IMG_PG" >/dev/null

    # Wait for PG readiness (max ~2min)
    for i in $(seq 1 120); do
      if podman exec "$APP-postgres" sh -lc 'pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1'; then
        break
      fi
      sleep 1
      [[ $i -eq 120 ]] && {{ echo "ERROR: postgres not ready"; exit 1; }}
    done

    echo "==> start redis"
    podman rm -f "$APP-redis" >/dev/null 2>&1 || true
    podman run --cgroups=disabled -d --name "$APP-redis" --pod "$POD" "$IMG_REDIS" >/dev/null

    echo "==> launch web"
    podman rm -f "$APP" >/dev/null 2>&1 || true
    podman run --cgroups=disabled -d --name "$APP" --pod "$POD" \\
      -v "$APPDIR/data:/data" \\
      -v "$LOGDIR:/data/logs" \\
      --env-file "$APPDIR/.env" \\
      -e XDG_CACHE_HOME=/data/cache \\
      --label io.containers.autoupdate=registry \\
      "$IMG_WEB" >/dev/null

    # -------- First-run install (one-time) --------
    if [[ ! -f "$INSTALLED_FILE" ]]; then
      echo "==> first run: installing (migrate, PG extensions, hostname)"

      echo "[install] create PG extensions (hstore, pg_trgm)"
      podman exec "$APP-postgres" sh -lc 'psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -Atc "CREATE EXTENSION IF NOT EXISTS hstore; CREATE EXTENSION IF NOT EXISTS pg_trgm;"'

      echo "[install] safe git ownership for /app"
      podman exec "$APP" git config --global --add safe.directory /app || true

      echo "[install] rails db:migrate"
      podman exec "$APP" bash -lc 'cd /app && RAILS_ENV=production bundle exec rake db:migrate'

      echo "[install] ensure hostname in config/discourse.conf"
      podman exec "$APP" bash -lc 'set -e
        cd /app
        test -f config/discourse.conf || cp config/discourse_defaults.conf config/discourse.conf
        H="${{DISCOURSE_HOSTNAME:-wildcard.local}}"
        if grep -q "^hostname" config/discourse.conf; then
          sed -i "s/^hostname.*/hostname = ${{H}}/" config/discourse.conf
        else
          printf "hostname = %s\\n" "$H" >> config/discourse.conf
        fi
        RAILS_ENV=production bin/rails r "puts \\"GlobalSetting.hostname => #{{GlobalSetting.hostname.inspect}}\\""
      '

      echo "[install] quick health probe"
      curl -sS -o /dev/null -w "HTTP %{{http_code}}\\n" "http://127.0.0.1:${{PORT}}/" || true

      date > "$INSTALLED_FILE"
      echo "==> install complete; wrote $INSTALLED_FILE"
    else
      echo "==> already installed; skipping install phase"
    fi

    echo "==> containers:"
    podman ps --format "table {{{{.Names}}}}\\t{{{{.Status}}}}\\t{{{{.Image}}}}\\t{{{{.Ports}}}}"

    echo "==> done. tip: podman logs -f {appname}"
    """)

    stop = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    export CONTAINERS_CGROUP_MANAGER=cgroupfs
    APP="{appname}"
    POD="$APP-pod"
    podman rm -f "$APP" "$APP-redis" "$APP-postgres" >/dev/null 2>&1 || true
    podman pod rm -f "$POD" >/dev/null 2>&1 || true
    echo "[stop] {appname} stopped"
    """)

    update = textwrap.dedent(f"""\
    #!/bin/bash
    set -Eeuo pipefail
    export CONTAINERS_CGROUP_MANAGER=cgroupfs
    "{appdir}/stop" || true
    "{appdir}/start"
    """)

    # debug_on: apply prepend-based patch + clear caches + restart
    debug_on = textwrap.dedent(f"""\
    #!/bin/bash
    # Applies safe prepend-based stylesheet/hostname patch, clears caches, restarts web.
    set -Eeuo pipefail
    export CONTAINERS_CGROUP_MANAGER=cgroupfs
    APP="{appname}"
    echo "[debug_on] applying prepend patch + cache clear"
    podman exec "$APP" bash -lc '
      set -e
      cd /app
      # remove any prior host/stylesheet patches to avoid class replacement
      rm -f config/initializers/010-wildcard_styles.rb \\
            config/initializers/zzz-hostname-hotfix.rb \\
            config/initializers/_hostname_guard.rb || true

      cat > config/initializers/010-wildcard_styles.rb << "RUBY"
      module WildcardStyles
        def current_hostname
          base = (defined?(super) ? super() : nil)
          (base.respond_to?(:presence) ? base.presence : base) ||
            (ENV["DISCOURSE_HOSTNAME"].to_s.strip.presence rescue nil) ||
            (ENV["HOSTNAME"].to_s.strip.presence rescue nil) ||
            "wildcard"
        end
        def theme_digest
          Digest::SHA1.hexdigest(
            scss_digest.to_s + color_scheme_digest.to_s + settings_digest + uploads_digest + current_hostname.to_s
          )
        end
      end
      Rails.configuration.to_prepare do
        if defined?(::Stylesheet::Manager::Builder)
          ::Stylesheet::Manager::Builder.prepend(WildcardStyles)
        end
      end
RUBY
      RAILS_ENV=production bin/rails r "Stylesheet::Manager.cache.clear; Rails.cache.clear"
    '
    podman restart "$APP" >/dev/null
    echo "[debug_on] done"
    """)

    # debug_off: remove the initializer(s) + clear caches + restart
    debug_off = textwrap.dedent(f"""\
    #!/bin/bash
    # Removes our debug/prepend patch, clears caches, restarts web.
    set -Eeuo pipefail
    export CONTAINERS_CGROUP_MANAGER=cgroupfs
    APP="{appname}"
    echo "[debug_off] removing patch + cache clear"
    podman exec "$APP" bash -lc '
      set -e
      cd /app
      rm -f config/initializers/010-wildcard_styles.rb \\
            config/initializers/zzz-hostname-hotfix.rb \\
            config/initializers/_hostname_guard.rb || true
      RAILS_ENV=production bin/rails r "Stylesheet::Manager.cache.clear; Rails.cache.clear"
    '
    podman restart "$APP" >/dev/null
    echo "[debug_off] done"
    """)

    readme = textwrap.dedent(f"""\
    # Discourse (Podman) on Opalstack

    **App:** {appname}  
    **Port:** {port} (host) → 3000 (container)  
    **Data:** {appdir}/data (Discourse) · {appdir}/pgdata (Postgres)  
    **Env:**  {appdir}/.env  
    **Logs:** {logdir}/ (discourse.log, unicorn*.log, sidekiq.log)

    ## Usage
    1) Start (first run performs one-time install, writes `.installed`):
       ```
       {appdir}/start
       ```

    2) Stop:
       ```
       {appdir}/stop
       ```

    3) Update (simple restart cycle):
       ```
       {appdir}/update
       ```

    4) Optional debug patch (stylesheet/hostname stable digest):
       ```
       {appdir}/debug_on   # apply patch + clear caches + restart
       {appdir}/debug_off  # remove patch + clear caches + restart
       ```

    ## Notes
    - Images override via env: DISCOURSE_IMAGE, REDIS_IMAGE, POSTGRES_IMAGE.
    - We remove duplicate `data/plugins/automation` to avoid redefinition spam.
    - Fontconfig cache is `/data/cache` via `XDG_CACHE_HOME` to stop noisy warnings.
    - Set `DISCOURSE_HOSTNAME` in `{appdir}/.env` for correct links and email.
    """)

    # write files
    write(f'{appdir}/start',      start,      0o700)
    write(f'{appdir}/stop',       stop,       0o700)
    write(f'{appdir}/update',     update,     0o700)
    write(f'{appdir}/debug_on',   debug_on,   0o700)
    write(f'{appdir}/debug_off',  debug_off,  0o700)
    write(f'{appdir}/README.md',  readme,     0o600)

    # Panel signals (best-effort)
    try:
        api.post('/app/installed/', json.dumps([{'id': args.uuid}]))
    except Exception as e:
        logging.warning(f'/app/installed/ failed once: {e}; retrying in 3s')
        time.sleep(3)
        try:
            api.post('/app/installed/', json.dumps([{'id': args.uuid}]))
        except Exception as e2:
            logging.error(f'/app/installed/ failed after retry: {e2}')

    try:
        api.post('/notice/create/', json.dumps([{
            'type':'M',
            'content': (
                f'Discourse prepared for app {appname}. SSH and run {appdir}/start. '
                f'First run performs install and writes .installed. '
                f'Initial admin vars in {appdir}/.env (ADMIN_*).'
            )
        }]))
    except Exception as e:
        logging.warning(f'/notice/create/ failed: {e}')

    logging.info('Install complete (scripts written).')

if __name__ == '__main__':
    main()
