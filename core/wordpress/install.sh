#! /bin/bash
# Opalstack Wordpress installer.

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
    echo $DBPWD

    echo "waiting for 30 seconds so the DB and DBUser can be created"
    sleep 30
    
    # check if the DB has been installed, initial request. ------------------------------------------------------------------------------------------------------------
    if DBOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  https://my.opalstack.com/api/v0/mariadb/read/$DBID` ;then
         printf $CGREEN2
         echo 'DB OK lookup.'
         printf $CEND
         DBOK=`echo $DBOKJSON | jq -r .installed_ok`
    else
         printf $CRED2
         echo 'DB OK lookup.'
         exit 1
    fi;
    
    # Iterate until DBOK True
    while [ $DBOK  == "False" ]
    do
    echo $DBOK

    sleep 10
    if DBOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  https://my.opalstack.com/api/v0/mariadb/read/$DBID` ;then
         printf $CGREEN2
         echo 'DB OK lookup.'
         printf $CEND
         DBOK=`echo $DBOKJSON | jq -r .installed_ok`
    else
         printf $CRED2
         echo 'DB OK lookup.'
    fi;
    done
    
    # check if the DB USER has been installed, initial request. ------------------------------------------------------------------------------------------------------------
    if DBUOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  https://my.opalstack.com/api/v0/mariauser/read/$DBUSERID` ;then
         printf $CGREEN2
         echo 'DBUser OK lookup.'
         printf $CEND
         DBUOK=`echo $DBUOKJSON | jq -r .installed_ok`
    else
         printf $CRED2
         echo 'DBUser OK lookup.'
         exit 1
    fi;
    
    # Iterate until DBUOK True
    while [ $DBUOK  == "False" ]
    do
    echo $DBUOK

    sleep 10
    if DBUOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  https://my.opalstack.com/api/v0/mariauser/read/$DBUSERID` ;then
         printf $CGREEN2
         echo 'DBUser OK lookup.'
         printf $CEND
         DBUOK=`echo $DBUOKJSON | jq -r .installed_ok`
    else
         printf $CRED2
         echo 'DBUser OK lookup.'
    fi;
    done

    /bin/mkdir -p $HOME/bin/
    /bin/wget https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar -O $HOME/bin/wp
    /bin/chmod +x $HOME/bin/wp
    /$HOME/bin/wp cli update
    cd $HOME/apps/$APPNAME/
    $HOME/bin/wp core download
    $HOME/wp core config --dbhost=localhost --dbname=$DBNAME --dbuser=$DBUSER --dbpass=$DBPWD
    chmod 644 wp-config.php
    
    #$HOME/wp core install --url=yourwebsite.com --title="Your Blog Title" --admin_name=wordpress_admin --admin_password=4Long&Strong1 --admin_email=you@example.com

fi;


