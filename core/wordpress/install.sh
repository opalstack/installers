#! /bin/bash
# Opalstack Wordpress installer.
# Takes token and app info, creates a MySQL DB and DBUSER and provies the info as vars.
# Order of operations best practice,
# First external downloads. Tarballs, zips, archives, external libraries.
# Second api calls to Opalstack control, DB creation, Port creation, etc.
# Last logic to create the application. Shell commands to build and install.

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

printf 'Started at %(%F %T)T\n' >> /home/$USER/logs/$APPNAME/install.log

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
    if serverjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  https://my.opalstack.com/api/v0/app/read/$UUID` ;then
         printf $CGREEN2
         echo 'UUID validation and server lookup OK.'
         printf $CEND
         serverid=`echo $serverjson | jq -r .server`
    else
         printf $CRED2
         echo 'UUID validation and server lookup failed.'
         exit 1
    fi;

    # Get the the account email address for wp install.
    if accountjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  https://my.opalstack.com/api/v0/account/info/` ;then
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
    dbsend='{"name": "'"$APPNAME"'", "server": "'"$serverid"'" }'
    if dbjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d"$dbsend"  https://my.opalstack.com/api/v0/mariadb/autoadd/` ;then
         export $(echo $dbjson| jq -r '@sh "DBNAME=\(.name) CHARSET=\(.charset) DBID=\(.id) DBUSERID=\(.dbuserid) DBUSER=\(.dbuser) DBPWD=\(.default_password) SERVER=\(.server)"' )
         printf $CGREEN2
         echo 'DB creation OK.'
         printf $CEND
    else
         printf $CRED2
         echo 'DB creation failed.'
         exit 1
    fi;
    eval DBNAME=$DBNAME
    eval DBUSERID=$DBUSERID
    eval DBID=$DBID
    eval DBUSER=$DBUSER
    eval DBPWD=$DBPWD
    echo "Database Created"
    echo $DBNAME
    echo $DBUSER

    echo "waiting for 10 seconds so the DB and DBUser can be created"
    sleep 10

    # check if the DB has been installed, initial request.
    if DBOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  https://my.opalstack.com/api/v0/mariadb/read/$DBID` ;then
         printf $CYELLOW2
         echo 'DB lookup.'
         printf $CEND
         DBOK=`echo $DBOKJSON | jq -r .installed_ok`
    else
         printf $CRED2
         echo 'DB lookup failed.'
         exit 1
    fi;

    # Iterate until DBOK True
    while [ $DBOK  == False ]
    do
    echo $DBOK

    sleep 5
    if DBOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  https://my.opalstack.com/api/v0/mariadb/read/$DBID` ;then
         printf $CYELLOW2
         echo 'DB lookup.'
         printf $CEND
         DBOK=`echo $DBOKJSON | jq -r .installed_ok`
    else
         printf $CRED2
         echo 'DB lookup failed.'
    fi;
    done

    printf $CGREEN2
    echo 'DB lookup OK.'
    printf $CEND

    # check if the DB USER has been installed, initial request.
    if DBUOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  https://my.opalstack.com/api/v0/mariauser/read/$DBUSERID` ;then
         printf $CYELLOW2
         echo 'DB User lookup.'
         printf $CEND
         DBUOK=`echo $DBUOKJSON | jq -r .installed_ok`
    else
         printf $CRED2
         echo 'DB User lookup failed.'
         exit 1
    fi;

    # Iterate until DBUOK True
    while [ $DBUOK  == False ]
    do
    echo $DBUOK

    sleep 5
    if DBUOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  https://my.opalstack.com/api/v0/mariauser/read/$DBUSERID` ;then
         printf $CYELLOW2
         echo 'DB User lookup.'
         printf $CEND
         DBUOK=`echo $DBUOKJSON | jq -r .installed_ok`
    else
         printf $CRED2
         echo 'DB User lookup failed.'
    fi;
    done

    printf $CGREEN2
    echo 'DB User lookup OK.'
    printf $CEND

    # have to do this to be sure the DBs have permissions, which can take 60 seconds after db creation.
    sleep 10

    # Install wp-cli
    echo 'WP CLI init'
    /bin/mkdir -p $HOME/bin/
    /bin/wget https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar -O $HOME/bin/wp
    /bin/chmod +x $HOME/bin/wp

    # use wp-cli to install wordpress,
    $HOME/bin/wp cli update
    $HOME/bin/wp core download --path=/home/$USER/apps/$APPNAME
    $HOME/bin/wp core config --dbhost=localhost --dbname=$DBNAME --dbuser=$DBUSER --dbpass=$DBPWD --path=/home/$USER/apps/$APPNAME
    /usr/bin/chmod 644 wp-config.php
    coreinstall=`$HOME/bin/wp core install --admin_name=$USER --admin_email=$accountemail --url="_" --title="Wordpress Blog" --path=/home/$USER/apps/$APPNAME`
    firstLine=`echo "${coreinstall}" | head -1`
    echo $firstLine
    # Send JSON installed OK.
    /usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d'{"id": "'"$UUID"'", "installed_ok":"True", "note":"'"Admin user: $USER / $firstLine"'"}' https://my.opalstack.com/api/v0/app/installed_ok/

fi;
