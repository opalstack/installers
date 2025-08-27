#! /bin/bash
# Opalstack Movable Type installer.
# Takes token and app info, creates a MariaDB DB and DBUSER and provides the info as vars.
# Order of operations best practice,
# First external downloads. Tarballs, zips, archives, external libraries.
# Second api calls to Opalstack control, DB creation, Port creation, etc.
# Last logic to create the application. Shell commands to build and install.
# THIS LINE

CRED2='\033[1;91m'        # Red
CGREEN2='\033[1;92m'      # Green
CYELLOW2='\033[1;93m'     # Yellow
CBLUE2='\033[1;94m'       # Blue
CVIOLET2='\033[1;95m'     # Purple
CCYAN2='\033[1;96m'       # Cyan
CWHITE2='\033[1;97m'      # White
CEND='\033[0m'            # Text Reset

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

if [ -z $UUID ] || [ -z $OPAL_TOKEN ] || [ -z $APPNAME ]
then
  printf $CRED2
  echo 'This command requires the following parameters to function,
  -i App UUID, used to make API calls to control panel.
  -n Application NAME, must match the name in the control panel
   {$OPAL_TOKEN} Control panel token, used to authenticate to the API.
  '
  echo "Parameter check failed (UUID/OPAL_TOKEN/APPNAME missing)" >> "$LOGFILE"
  exit 1
else
  # === External download (MT tarball) ===
  echo 'Preparing external download for Movable Type...'
  echo "[step] preparing external download" >> "$LOGFILE"
  MT_TARBALL_URL_DEFAULT="https://movabletype.org/downloads/stable/MTOS-5.2.13.tar.gz"
  MT_TARBALL_URL="${MT_TARBALL_URL:-$MT_TARBALL_URL_DEFAULT}"
  /bin/mkdir -p /home/$USER/apps/$APPNAME /home/$USER/apps/$APPNAME/tmp /home/$USER/apps/$APPNAME/.cache
  if /usr/bin/curl -s --fail -L "$MT_TARBALL_URL" -o /home/$USER/apps/$APPNAME/.cache/mt.tar.gz ; then
    echo "[ok] downloaded MT tarball from $MT_TARBALL_URL" >> "$LOGFILE"
  else
    printf $CRED2; echo 'Movable Type download failed.'
    echo "[fail] download MT tarball" >> "$LOGFILE"
    exit 1
  fi

  # === API: validate UUID / get server id ===
  echo "[step] validate app UUID and fetch server id" >> "$LOGFILE"
  if serverjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  $API_URL/api/v1/app/read/$UUID` ;then
       printf $CGREEN2; echo 'UUID validation and server lookup OK.'; printf $CEND
       serverid=`echo $serverjson | /usr/bin/jq -r .server`
       echo "[ok] app/read; server=$serverid" >> "$LOGFILE"
  else
       printf $CRED2; echo 'UUID validation and server lookup failed.'
       echo "[fail] app/read" >> "$LOGFILE"
       exit 1
  fi;

  # Get the the account email address (for notice text parity)
  echo "[step] fetch account email" >> "$LOGFILE"
  if accountjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  $API_URL/api/v1/account/info/` ;then
       printf $CGREEN2; echo 'Admin email lookup OK.'; printf $CEND
       accountemail=`echo $accountjson | /usr/bin/jq -r .email`
       echo "[ok] account/info; email=$accountemail" >> "$LOGFILE"
  else
       printf $CRED2; echo 'Admin email lookup failed.'
       echo "[fail] account/info" >> "$LOGFILE"
       exit 1
  fi;

  # === create database user ===
  APPDB="${APPNAME:0:8}_${UUID:0:8}"
  echo "[step] create DB user for $APPDB" >> "$LOGFILE"
  dbusend='[{"name": "'"$APPDB"'", "server": "'"$serverid"'" }]'
  if dbjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d"$dbusend"  $API_URL/api/v1/mariauser/create/` ;then
       export $(echo $dbjson| /usr/bin/jq -r '@sh "DBUSERID=\(.[0].id) DBUSER=\(.[0].name) DBPWD=\(.[0].default_password)"' )
       printf $CGREEN2; echo 'DB user creation OK.'; printf $CEND
       echo "[ok] mariauser/create; user=$DBUSER id=$DBUSERID" >> "$LOGFILE"
  else
       printf $CRED2; echo 'DB user creation failed.'
       echo "[fail] mariauser/create" >> "$LOGFILE"
       exit 1
  fi;
  eval DBUSER=$DBUSER
  eval DBUSERID=$DBUSERID
  eval DBPWD=$DBPWD
  echo "Database User Created"
  echo $DBUSER
  echo $DBUSERID

  # === create database and grant RW to the user ===
  echo "[step] create database $APPDB and grant RW to $DBUSERID" >> "$LOGFILE"
  dbsend='[{ "name": '\"$APPDB\"', "server": '\"$serverid\"', "dbusers_readwrite": ['\"$DBUSERID\"'] }]'
  echo $dbsend
  if dbjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d"$dbsend"  $API_URL/api/v1/mariadb/create/` ;then
       export $(echo $dbjson| /usr/bin/jq -r '@sh "DBNAME=\(.[0].name) DBID=\(.[0].id) "' )
       printf $CGREEN2; echo 'DB creation OK.'; printf $CEND
       echo "[ok] mariadb/create; name=$DBNAME id=$DBID" >> "$LOGFILE"
  else
       printf $CRED2; echo 'DB creation failed.'
       echo "[fail] mariadb/create" >> "$LOGFILE"
       exit 1
  fi;
  eval DBNAME=$DBNAME
  eval DBID=$DBID

  echo "Database Created"
  echo $DBNAME
  echo "[info] waiting 10s for DB + user provisioning" >> "$LOGFILE"
  /bin/sleep 10

  # === poll DB readiness ===
  echo "[step] poll DB readiness (id=$DBID)" >> "$LOGFILE"
  if DBOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  $API_URL/api/v1/mariadb/read/$DBID` ;then
       printf $CYELLOW2; echo 'DB lookup.'; printf $CEND
       DBOK=`echo $DBOKJSON | /usr/bin/jq -r .ready`
  else
       printf $CRED2; echo 'DB lookup failed.'
       echo "[fail] mariadb/read init" >> "$LOGFILE"
       exit 1
  fi;

  while [ $DBOK == false ]
  do
    echo $DBOK
    /bin/sleep 5
    if DBOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  $API_URL/api/v1/mariadb/read/$DBID` ;then
         printf $CYELLOW2; echo 'DB lookup.'; printf $CEND
         DBOK=`echo $DBOKJSON | /usr/bin/jq -r .ready`
    else
         printf $CRED2; echo 'DB lookup failed.'
         echo "[warn] mariadb/read retry failed" >> "$LOGFILE"
    fi;
  done
  echo "[ok] DB ready" >> "$LOGFILE"
  printf $CGREEN2; echo 'DB lookup OK.'; printf $CEND

  # === poll DB USER readiness ===
  echo "[step] poll DB user readiness (id=$DBUSERID)" >> "$LOGFILE"
  if DBUOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  $API_URL/api/v1/mariauser/read/$DBUSERID` ;then
       printf $CYELLOW2; echo 'DB User lookup.'; printf $CEND
       DBUOK=`echo $DBUOKJSON | /usr/bin/jq -r .ready`
  else
       printf $CRED2; echo 'DB User lookup failed.'
       echo "[fail] mariauser/read init" >> "$LOGFILE"
       exit 1
  fi;

  while [ $DBUOK == false ]
  do
    echo $DBUOK
    /bin/sleep 5
    if DBUOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  $API_URL/api/v1/mariauser/read/$DBUSERID` ;then
         printf $CYELLOW2; echo 'DB User lookup.'; printf $CEND
         DBUOK=`echo $DBUOKJSON | /usr/bin/jq -r .ready`
    else
         printf $CRED2; echo 'DB User lookup failed.'
         echo "[warn] mariauser/read retry failed" >> "$LOGFILE"
    fi;
  done
  echo "[ok] DB user ready" >> "$LOGFILE"
  printf $CGREEN2; echo 'DB User lookup OK.'; printf $CEND

  # === Create the Movable Type application in docroot ===
  APPDIR="/home/$USER/apps/$APPNAME"
  echo 'Installing Movable Type into app docroot'
  echo "[step] extract MT into $APPDIR" >> "$LOGFILE"
  /bin/tar -xzf "$APPDIR/.cache/mt.tar.gz" -C "$APPDIR" --strip-components=1 && echo "[ok] extracted MT" >> "$LOGFILE"

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

  # Send JSON installed OK.
  echo "[step] POST app/installed" >> "$LOGFILE"
  /usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d'[{"id": "'$UUID'"}]' $API_URL/api/v1/app/installed/ \
    && echo "[ok] app/installed" >> "$LOGFILE"

  # Create notice
  firstLine="Admin bootstrap: /mt.cgi"
  echo "[step] POST notice/create" >> "$LOGFILE"
  /usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d'[{"type": "D", "content":"'"Created Movable Type app $APPNAME for $accountemail — $firstLine. DB: $DBNAME / $DBUSER"'"}]' $API_URL/api/v1/notice/create/ \
    && echo "[ok] notice/create" >> "$LOGFILE"

  printf 'Completed at %(%F %T)T\n' >> "$LOGFILE"
fi;
