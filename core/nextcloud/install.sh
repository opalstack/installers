#! /bin/bash
# Opalstack Nextcloud Installer
# only to be used with apache cgi app type APA

CRED2='\033[1;91m'        # Red
CGREEN2='\033[1;92m'      # Green
CYELLOW2='\033[1;93m'     # Yellow
CBLUE2='\033[1;94m'       # Blue
CVIOLET2='\033[1;95m'     # Purple
CCYAN2='\033[1;96m'       # Cyan
CWHITE2='\033[1;97m'      # White
CEND='\033[0m'       # Text Reset

# i is for UUID, t is for user token, n is for app name
while getopts i:n: option
do
case "${option}"
in
i) UUID=${OPTARG};;
n) APPNAME=$OPTARG;;
esac
done

printf 'Started at %(%F %T)T\n' >> /home/$USER/logs/apps/$APPNAME/install.log

if [ -z $UUID ] || [ -z $OPAL_TOKEN ] || [ -z $APPNAME ]
then
     printf $CRED2
     echo 'This command requires the following parameters to function,
     -i App UUID, used to make API calls to control panel.
     -n Application NAME, must match the name in the control panel
      {$OPAL_TOKEN} Control panel token, used to authenticate to the API.
     '
     exit 1
else
    # Get the server's UUID and verify the app exists, and thus the file schema exists.
    if serverjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  $API_URL/api/v1/app/read/$UUID` ;then
         printf $CGREEN2
         echo 'UUID validation and server lookup OK.'
         printf $CEND
         serverid=`echo $serverjson | jq -r .server`
    else
         printf $CRED2
         echo 'UUID validation and server lookup failed.'
         exit 1
    fi;

    # Get the the account email address for install.
    if accountjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  $API_URL/api/v1/account/info/` ;then
         printf $CGREEN2
         echo 'Admin email lookup OK.'
         printf $CEND
         accountemail=`echo $accountjson | jq -r .email`
    else
         printf $CRED2
         echo 'Admin email lookup failed.'
         exit 1
    fi;

    # create database
    # unique DB name
    APPDB="${APPNAME:0:8}_${UUID:0:8}"
    dbusend='[{"name": "'"$APPDB"'", "server": "'"$serverid"'" }]'
    # create database user
    if dbjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d"$dbusend"  $API_URL/api/v1/psqluser/create/` ;then
         export $(echo $dbjson| jq -r '@sh "DBUSERID=\(.[0].id) DBUSER=\(.[0].name) DBPWD=\(.[0].default_password)"' )
         printf $CGREEN2
         echo 'DB user creation OK.'
         printf $CEND
    else
         printf $CRED2
         echo 'DB user creation failed.'
         exit 1
    fi;
    eval DBUSER=$DBUSER
    eval DBUSERID=$DBUSERID
    eval DBPWD=$DBPWD
    echo "Database User Created"
    echo $DBUSER
    echo $DBUSERID

    dbsend='[{ "name": '\"$APPDB\"', "server": '\"$serverid\"', "dbusers_readwrite": ['\"$DBUSERID\"'] }]'
    echo $dbsend
    if dbjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d"$dbsend"  $API_URL/api/v1/psqldb/create/` ;then
         export $(echo $dbjson| jq -r '@sh "DBNAME=\(.[0].name) DBID=\(.[0].id) "' )
         printf $CGREEN2
         echo 'DB creation OK.'
         printf $CEND
    else
         printf $CRED2
         echo 'DB creation failed.'
         exit 1
    fi;
    eval DBNAME=$DBNAME
    eval DBID=$DBID

    echo "Database Created"
    echo $DBNAME

    echo "waiting for 10 seconds so the DB and DBUser can be created"
    sleep 10

    # check if the DB has been installed, initial request.
    if DBOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  $API_URL/api/v1/psqldb/read/$DBID` ;then
         printf $CYELLOW2
         echo 'DB lookup.'
         printf $CEND
         DBOK=`echo $DBOKJSON | jq -r .ready`
    else
         printf $CRED2
         echo 'DB lookup failed.'
         exit 1
    fi;

    # Iterate until DBOK True
    while [ $DBOK  == false ]
    do
    echo $DBOK

    sleep 5
    if DBOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  $API_URL/api/v1/psqldb/read/$DBID` ;then
         printf $CYELLOW2
         echo 'DB lookup.'
         printf $CEND
         DBOK=`echo $DBOKJSON | jq -r .ready`
    else
         printf $CRED2
         echo 'DB lookup failed.'
    fi;
    done

    printf $CGREEN2
    echo 'DB lookup OK.'
    printf $CEND

    # check if the DB USER has been installed, initial request.
    if DBUOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  $API_URL/api/v1/psqluser/read/$DBUSERID` ;then
         printf $CYELLOW2
         echo 'DB User lookup.'
         printf $CEND
         DBUOK=`echo $DBUOKJSON | jq -r .ready`
    else
         printf $CRED2
         echo 'DB User lookup failed.'
         exit 1
    fi;

    # Iterate until DBUOK True
    while [ $DBUOK  == false ]
    do
    echo $DBUOK

    sleep 5
    if DBUOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  $API_URL/api/v1/psqluser/read/$DBUSERID` ;then
         printf $CYELLOW2
         echo 'DB User lookup.'
         printf $CEND
         DBUOK=`echo $DBUOKJSON | jq -r .ready`
    else
         printf $CRED2
         echo 'DB User lookup failed.'
    fi;
    done

    printf $CGREEN2
    echo 'DB User lookup OK.'
    printf $CEND

    # wget and untar
    /bin/wget https://download.nextcloud.com/server/releases/latest.tar.bz2 -O $HOME/apps/$APPNAME/latest.tar.bz2
    /bin/tar -xf $HOME/apps/$APPNAME/latest.tar.bz2 nextcloud -C $HOME/apps/$APPNAME/ --strip-components=1
    /bin/rm $HOME/apps/$APPNAME/latest.tar.bz2
    /bin/rm $HOME/apps/$APPNAME/index.html

    # generate password
    app_pass=`date +%s | sha256sum | base64 | head -c 20`
    echo $app_pass

    # install with occ
    /bin/php83 -d memory_limit=512M $HOME/apps/$APPNAME/occ maintenance:install --database pgsql --database-name $DBNAME --database-user $DBUSER --database-pass $DBPWD --admin-user $USER --admin-pass $app_pass --admin-email $accountemail --data-dir $HOME/apps/$APPNAME/data

    # set crontab
    (crontab -l ; echo "*/5  *  *  *  * /bin/php83 $HOME/apps/$APPNAME/cron.php")| crontab -

    # Send JSON installed OK.
    /usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d'[{"id": "'$UUID'"}]' $API_URL/api/v1/app/installed/

    # Create notice
    /usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d'[{"type": "D", "content":"'"Created Nextcloud app $APPNAME with Admin user: $USER and password: $app_pass"'"}]' $API_URL/api/v1/notice/create/

fi;
