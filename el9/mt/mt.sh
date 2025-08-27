#! /bin/bash
# Opalstack Movable Type installer.
# Adds: SITE app (STA static-only), README.md, and SLS symlink-static app that serves the SITE's mt-static.
# THIS LINE

CRED2='\033[1;91m'        # Red
CGREEN2='\033[1;92m'      # Green
CYELLOW2='\033[1;93m'     # Yellow
CBLUE2='\033[1;94m'       # Blue
CVIOLET2='\033[1;95m'     # Purple
CCYAN2='\033[1;96m'       # Cyan
CWHITE2='\033[1;97m'      # White
CEND='\033[0m'            # Text Reset

# --- App type codes (panel codes) ---
SITE_APP_TYPE="STA"     # Static Only (creates site dir)
SYMLINK_APP_TYPE="SLS"  # Symbolic link, Static only (nginx serves target symlink)

# --- noisy curl helpers (capture HTTP code + body) ---
curl_json_post() { # endpoint json_payload -> sets CURL_STATUS CURL_BODY
  local endpoint="$1"; shift
  local payload="$1"
  local tmp="$(mktemp)"
  CURL_STATUS=$(
    curl -sS -w '%{http_code}' -o "$tmp" \
      -H "Authorization: Token $OPAL_TOKEN" \
      -H "Content-Type: application/json" \
      -d "$payload" \
      "$API_URL$endpoint" || echo "000"
  )
  CURL_BODY="$(cat "$tmp")"
  rm -f "$tmp"
}

curl_json_get() { # endpoint -> sets CURL_STATUS CURL_BODY
  local endpoint="$1"
  local tmp="$(mktemp)"
  CURL_STATUS=$(
    curl -sS -w '%{http_code}' -o "$tmp" \
      -H "Authorization: Token $OPAL_TOKEN" \
      -H "Content-Type: application/json" \
      "$API_URL$endpoint" || echo "000"
  )
  CURL_BODY="$(cat "$tmp")"
  rm -f "$tmp"
}

fail_now() {
  printf "$CRED2"; echo "$1"; printf "$CEND"
  echo "[fail] $1" >> "$LOGFILE"
  echo "[fail] http=$CURL_STATUS body=$CURL_BODY" >> "$LOGFILE"
  exit 1
}

# i is for UUID, n is for app name
while getopts i:n: option
do
case "${option}" in
  i) UUID=${OPTARG};;
  n) APPNAME=$OPTARG;;
esac
done

LOGFILE="/home/$USER/logs/apps/$APPNAME/install.log"
printf 'Started at %(%F %T)T\n' >> "$LOGFILE"

if [ -z "$UUID" ] || [ -z "$OPAL_TOKEN" ] || [ -z "$APPNAME" ]; then
  printf $CRED2
  cat <<'ERRTXT'
This command requires the following parameters to function,
  -i App UUID, used to make API calls to control panel.
  -n Application NAME, must match the name in the control panel
  {$OPAL_TOKEN} Control panel token, used to authenticate to the API.
ERRTXT
  printf $CEND
  echo "Parameter check failed (UUID/OPAL_TOKEN/APPNAME missing)" >> "$LOGFILE"
  exit 1
fi

# === External download (MT tarball) ===
echo 'Preparing external download for Movable Type...'
echo "[step] preparing external download" >> "$LOGFILE"
MT_TARBALL_URL_DEFAULT="https://movabletype.org/downloads/stable/MTOS-5.2.13.tar.gz"
MT_TARBALL_URL="${MT_TARBALL_URL:-$MT_TARBALL_URL_DEFAULT}"
/bin/mkdir -p "/home/$USER/apps/$APPNAME" "/home/$USER/apps/$APPNAME/tmp" "/home/$USER/apps/$APPNAME/.cache"
if /usr/bin/curl -s --fail -L "$MT_TARBALL_URL" -o "/home/$USER/apps/$APPNAME/.cache/mt.tar.gz" ; then
  echo "[ok] downloaded MT tarball from $MT_TARBALL_URL" >> "$LOGFILE"
else
  printf $CRED2; echo 'Movable Type download failed.'; printf $CEND
  echo "[fail] download MT tarball" >> "$LOGFILE"
  exit 1
fi

# === API: validate UUID / get server + osuser ===
echo "[step] validate app UUID and fetch server/osuser id" >> "$LOGFILE"
curl_json_get "/api/v1/app/read/$UUID"
if [[ "$CURL_STATUS" != 2* ]]; then
  fail_now "app/read failed (uuid=$UUID)"
fi
serverid=$(echo "$CURL_BODY" | /usr/bin/jq -r .server)
osuser_id=$(echo "$CURL_BODY" | /usr/bin/jq -r .osuser)
echo "[ok] app/read; server=$serverid osuser=$osuser_id" >> "$LOGFILE"
printf $CGREEN2; echo 'UUID validation and server/osuser lookup OK.'; printf $CEND

# Get the the account email address (for notice text parity)
echo "[step] fetch account email" >> "$LOGFILE"
curl_json_get "/api/v1/account/info/"
if [[ "$CURL_STATUS" != 2* ]]; then
  fail_now "account/info failed"
fi
accountemail=$(echo "$CURL_BODY" | /usr/bin/jq -r .email)
echo "[ok] account/info; email=$accountemail" >> "$LOGFILE"
printf $CGREEN2; echo 'Admin email lookup OK.'; printf $CEND

# === create database user ===
APPDB="${APPNAME:0:8}_${UUID:0:8}"
echo "[step] create DB user for $APPDB" >> "$LOGFILE"
dbusend='[{"name": "'"$APPDB"'", "server": "'"$serverid"'"}]'
if dbjson=$(curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d"$dbusend"  "$API_URL/api/v1/mariauser/create/"); then
     export $(echo "$dbjson" | /usr/bin/jq -r '@sh "DBUSERID=\(.[0].id) DBUSER=\(.[0].name) DBPWD=\(.[0].default_password)"' )
     printf $CGREEN2; echo 'DB user creation OK.'; printf $CEND
     echo "[ok] mariauser/create; user=$DBUSER id=$DBUSERID" >> "$LOGFILE"
else
     printf $CRED2; echo 'DB user creation failed.'; printf $CEND
     echo "[fail] mariauser/create" >> "$LOGFILE"
     exit 1
fi
eval DBUSER=$DBUSER
eval DBUSERID=$DBUSERID
eval DBPWD=$DBPWD
echo "Database User Created"
echo "$DBUSER"
echo "$DBUSERID"

# === create database and grant RW to the user ===
echo "[step] create database $APPDB and grant RW to $DBUSERID" >> "$LOGFILE"
dbsend='[{ "name": "'"$APPDB"'", "server": "'"$serverid"'", "dbusers_readwrite": ["'"$DBUSERID"'"] }]'
echo "$dbsend"
if dbjson=$(curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d"$dbsend"  "$API_URL/api/v1/mariadb/create/"); then
     export $(echo "$dbjson" | /usr/bin/jq -r '@sh "DBNAME=\(.[0].name) DBID=\(.[0].id)"' )
     printf $CGREEN2; echo 'DB creation OK.'; printf $CEND
     echo "[ok] mariadb/create; name=$DBNAME id=$DBID" >> "$LOGFILE"
else
     printf $CRED2; echo 'DB creation failed.'; printf $CEND
     echo "[fail] mariadb/create" >> "$LOGFILE"
     exit 1
fi
eval DBNAME=$DBNAME
eval DBID=$DBID

echo "Database Created"
echo "$DBNAME"
echo "[info] waiting 10s for DB + user provisioning" >> "$LOGFILE"
/bin/sleep 10

# === poll DB readiness ===
echo "[step] poll DB readiness (id=$DBID)" >> "$LOGFILE"
if DBOKJSON=$(curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  "$API_URL/api/v1/mariadb/read/$DBID"); then
     printf $CYELLOW2; echo 'DB lookup.'; printf $CEND
     DBOK=$(echo "$DBOKJSON" | /usr/bin/jq -r .ready)
else
     printf $CRED2; echo 'DB lookup failed.'; printf $CEND
     echo "[fail] mariadb/read init" >> "$LOGFILE"
     exit 1
fi
while [ "$DBOK" = false ]; do
  echo "$DBOK"
  /bin/sleep 5
  if DBOKJSON=$(curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  "$API_URL/api/v1/mariadb/read/$DBID"); then
       printf $CYELLOW2; echo 'DB lookup.'; printf $CEND
       DBOK=$(echo "$DBOKJSON" | /usr/bin/jq -r .ready)
  else
       printf $CRED2; echo 'DB lookup failed.'; printf $CEND
       echo "[warn] mariadb/read retry failed" >> "$LOGFILE"
  fi
done
echo "[ok] DB ready" >> "$LOGFILE"
printf $CGREEN2; echo 'DB lookup OK.'; printf $CEND

# === poll DB USER readiness ===
echo "[step] poll DB user readiness (id=$DBUSERID)" >> "$LOGFILE"
if DBUOKJSON=$(curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  "$API_URL/api/v1/mariauser/read/$DBUSERID"); then
     printf $CYELLOW2; echo 'DB User lookup.'; printf $CEND
     DBUOK=$(echo "$DBUOKJSON" | /usr/bin/jq -r .ready)
else
     printf $CRED2; echo 'DB User lookup failed.'; printf $CEND
     echo "[fail] mariauser/read init" >> "$LOGFILE"
     exit 1
fi
while [ "$DBUOK" = false ]; do
  echo "$DBUOK"
  /bin/sleep 5
  if DBUOKJSON=$(curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  "$API_URL/api/v1/mariauser/read/$DBUSERID"); then
       printf $CYELLOW2; echo 'DB User lookup.'; printf $CEND
       DBUOK=$(echo "$DBUOKJSON" | /usr/bin/jq -r .ready)
  else
       printf $CRED2; echo 'DB User lookup failed.'; printf $CEND
       echo "[warn] mariauser/read retry failed" >> "$LOGFILE"
  fi
done
echo "[ok] DB user ready" >> "$LOGFILE"
printf $CGREEN2; echo 'DB User lookup OK.'; printf $CEND

# === Create the Movable Type application in docroot ===
APPDIR="/home/$USER/apps/$APPNAME"
echo 'Installing Movable Type into app docroot'
echo "[step] extract MT into $APPDIR" >> "$LOGFILE"
/bin/tar -xzf "$APPDIR/.cache/mt.tar.gz" -C "$APPDIR" --strip-components=1 && echo "[ok] extracted MT" >> "$LOGFILE"

# Disable bundled URI so EL9’s perl-URI is used
mkdir -p "$APPDIR/extlib-disabled"
mv "$APPDIR/extlib/URI.pm" "$APPDIR/extlib/URI" "$APPDIR/extlib-disabled/" 2>/dev/null || true

# Write mt-config.cgi (monolith at base domain; shared-host safeties)
echo "[step] write mt-config.cgi" >> "$LOGFILE"
cat > "$APPDIR/mt-config.cgi" <<CFGEOF
# ===== Movable Type configuration (generated) =====
CGIPath /
StaticWebPath /mt-static/
PublishCharset UTF-8
TimeZone UTC

# Database
ObjectDriver DBI::mysql
Database $DBNAME
DBUser $DBUSER
DBPassword $DBPWD
DBHost localhost
DBPort 3306

# Local paths (inside docroot)
TempDir ./tmp
SessionsPath ./tmp/sessions

# Safer defaults
NoLocking 1
AllowComments 0

# Mail transport
MailTransfer sendmail
SendMailPath /usr/sbin/sendmail
CFGEOF
/bin/mkdir -p "$APPDIR/tmp" "$APPDIR/tmp/sessions"
/usr/bin/chmod 600 "$APPDIR/mt-config.cgi" || true
echo "[ok] mt-config.cgi written" >> "$LOGFILE"

# Enable CGI via .htaccess (Apache/PHP-FPM app)
echo "[step] write .htaccess (enable CGI, protect config)" >> "$LOGFILE"
cat > "$APPDIR/.htaccess" <<'HTEOF'
# APA app — enable CGI for Movable Type at base domain
Options +ExecCGI -MultiViews +FollowSymLinks
AddHandler cgi-script .cgi
AddDefaultCharset UTF-8
DirectoryIndex index.html index.cgi

# Only MT scripts as CGI
<FilesMatch "^mt.*\.cgi$">
  SetHandler cgi-script
</FilesMatch>

# Protect sensitive files
<Files "mt-config.cgi">
  Require all denied
</Files>
<FilesMatch "\.(yml|yaml|json|lock|tmpl)$">
  Require all denied
</FilesMatch>

# QoL: /mt -> mt.cgi
RewriteEngine On
RewriteRule ^mt/?$ mt.cgi [L]

# No directory listing
Options -Indexes
HTEOF
echo "[ok] .htaccess written" >> "$LOGFILE"

# Ensure CGI bits
echo "[step] chmod +x mt*.cgi" >> "$LOGFILE"
/usr/bin/find "$APPDIR" -maxdepth 1 -type f -name '*.cgi' -exec chmod 755 {} + && echo "[ok] CGI perms set" >> "$LOGFILE"

# --------------------------------------------------------------------
# Create SITE app (via API) to hold the first published site (STA static-only)
# --------------------------------------------------------------------
SITE_APP_NAME="${APPNAME}_site"
echo "[step] app/create ${SITE_APP_NAME} type=${SITE_APP_TYPE}" >> "$LOGFILE"

site_payload=$(jq -n \
  --arg name "$SITE_APP_NAME" \
  --arg osuser "$osuser_id" \
  --arg type "$SITE_APP_TYPE" \
  '[{name: $name, osuser: $osuser, type: $type, json: {}}]')

curl_json_post "/api/v1/app/create/" "$site_payload"

if [[ "$CURL_STATUS" != 2* ]]; then
  printf "$CRED2"; echo "Site app creation failed ($SITE_APP_NAME) — http=$CURL_STATUS"; printf "$CEND"
  echo "$CURL_BODY"
  echo "[payload] $site_payload" >> "$LOGFILE"
  fail_now "site app create error"
fi

SITE_APP_ID=$(echo "$CURL_BODY" | jq -r '.[0].id')
echo "[ok] app/create; ${SITE_APP_NAME} id=$SITE_APP_ID body=$(echo "$CURL_BODY" | tr -d '\n' | cut -c1-400)" >> "$LOGFILE"

# poll readiness
echo "[step] poll site app readiness (id=$SITE_APP_ID)" >> "$LOGFILE"
while :; do
  curl_json_get "/api/v1/app/read/$SITE_APP_ID"
  if [[ "$CURL_STATUS" != 2* ]]; then
    echo "[warn] app/read site http=$CURL_STATUS body=$CURL_BODY" >> "$LOGFILE"
    sleep 3; continue
  fi
  ready=$(echo "$CURL_BODY" | jq -r '.ready')
  [[ "$ready" == "true" ]] && break
  sleep 2
done
echo "[ok] site app ready" >> "$LOGFILE"

# Panel has created the site directory. DO NOT manually create any symlink here.
SITEDIR="/home/$USER/apps/${SITE_APP_NAME}"
SYMLINK_TARGET="$SITEDIR/mt-static"   # final full path for SLS to serve

# --------------------------------------------------------------------
# Create SLS app (nginx symlink-static) pointing to the **symlink path**
# IMPORTANT: payload uses json: { sym_link_path: "<full path to site/mt-static" }
# DO NOT manually create ln -s; platform handles the target internals.
# --------------------------------------------------------------------
LINK_APP_NAME="${APPNAME}_site_static"
echo "[step] app/create ${LINK_APP_NAME} type=${SYMLINK_APP_TYPE} json.sym_link_path=$SYMLINK_TARGET" >> "$LOGFILE"

link_payload=$(jq -n \
  --arg name "$LINK_APP_NAME" \
  --arg osuser "$osuser_id" \
  --arg type "$SYMLINK_APP_TYPE" \
  --arg sym "$SYMLINK_TARGET" \
  '[{name: $name, osuser: $osuser, type: $type, json: {sym_link_path: $sym}}]')

curl_json_post "/api/v1/app/create/" "$link_payload"

if [[ "$CURL_STATUS" != 2* ]]; then
  printf "$CRED2"; echo "Symlink app creation failed ($LINK_APP_NAME) — http=$CURL_STATUS"; printf "$CEND"
  echo "$CURL_BODY"
  echo "[payload] $link_payload" >> "$LOGFILE"
  fail_now "symlink app create error"
fi

LINK_APP_ID=$(echo "$CURL_BODY" | jq -r '.[0].id')
echo "[ok] app/create; ${LINK_APP_NAME} id=$LINK_APP_ID body=$(echo "$CURL_BODY" | tr -d '\n' | cut -c1-400)" >> "$LOGFILE"

# poll readiness of SLS as well
echo "[step] poll symlink app readiness (id=$LINK_APP_ID)" >> "$LOGFILE"
while :; do
  curl_json_get "/api/v1/app/read/$LINK_APP_ID"
  if [[ "$CURL_STATUS" != 2* ]]; then
    echo "[warn] app/read symlink http=$CURL_STATUS body=$CURL_BODY" >> "$LOGFILE"
    sleep 3; continue
  fi
  ready=$(echo "$CURL_BODY" | jq -r '.ready')
  [[ "$ready" == "true" ]] && break
  sleep 2
done
echo "[ok] symlink app ready" >> "$LOGFILE"

# === README explaining structure and routing ===
README="$APPDIR/README.md"
echo "[step] write README.md" >> "$LOGFILE"
cat > "$README" <<MD
# Movable Type on Opalstack — App Layout

We created **three** app pieces:

1. **$APPNAME** (APA) — the Movable Type admin/CGI lives here  
   - Path: \`$APPDIR\`  
   - Bootstrap: \`/mt.cgi\`  
   - Static assets directory: \`$APPDIR/mt-static\`

2. **${APPNAME}_site** (**STA**) — the first **published site directory** (static HTML)  
   - Path: \`$SITEDIR\`

3. **${APPNAME}_site_static** (**SLS**) — nginx symlink-static app that **serves** the published site  
   - \`json.sym_link_path\`: \`$SYMLINK_TARGET\` (full path to the site's \`mt-static\` symlink)

## Routing (subdomains)

- Route a subdomain (e.g. \`mt.yourdomain.com\`) to **$APPNAME**.  
- Route another subdomain (e.g. \`blog.yourdomain.com\`) to **${APPNAME}_site_static**.

This mirrors the screenshot: the **site** references \`mt-static/\`; the platform-level **SLS** configuration serves that symlink path.

## Additional sites later

For more sites, repeat:

1. Create another app via API: type **STA** named like \`${APPNAME}_site2\`.  
2. Create another **SLS** app with \`json.sym_link_path\` set to \`/home/$USER/apps/${APPNAME}_site2/mt-static\`.  
3. Route a new subdomain to that new **SLS** app.

MD
echo "[ok] README.md written" >> "$LOGFILE"

# === POST app/installed (main app) ===
echo "[step] POST app/installed" >> "$LOGFILE"
/usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d'[{"id": "'$UUID'"}]' "$API_URL/api/v1/app/installed/" \
  && echo "[ok] app/installed" >> "$LOGFILE"

# === Notice ===
firstLine="Admin bootstrap: /mt.cgi — Site app: ${SITE_APP_NAME} (STA); Static link app: ${LINK_APP_NAME} (SLS, json.sym_link_path=$SYMLINK_TARGET). See $APPDIR/README.md."
echo "[step] POST notice/create" >> "$LOGFILE"
/usr/bin/curl -s -X POST \
  --header "Content-Type:application/json" \
  --header "Authorization: Token $OPAL_TOKEN" \
  -d'[{"type": "D", "content":"'"Created Movable Type app $APPNAME for $accountemail — $firstLine DB: $DBNAME / $DBUSER."'"}]' \
  "$API_URL/api/v1/notice/create/" && echo "[ok] notice/create" >> "$LOGFILE"

printf 'Completed at %(%F %T)T\n' >> "$LOGFILE"
