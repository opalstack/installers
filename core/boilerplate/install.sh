#! /bin/bash
# Opalstack Boilerplate installer.
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
while getopts i:t:l:n: option
do
case "${option}"
in
i) UUID=${OPTARG};;
t) TOKEN=${OPTARG};;
n) APPNAME=$OPTARG;;
esac
done

if [ -z $UUID ] || [ -z $TOKEN ] || [ -z $APPNAME ]
then
     printf $CRED2
     echo 'This command requires the following parameters to function, 
     -i App UUID, used to make API calls to control panel.
     -t Control panel TOKEN, used to authenticate to the API. 
     -n Application NAME, must match the name in the control panel
     '
     exit 1
else    
    # Get the server's UUID and verify the app exists, and thus the file schema exists.
    if serverjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $TOKEN"  http://127.0.0.1:8000/api/v0/app/read/$UUID` ;then
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
    if dbjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $TOKEN" -d"$dbsend"  http://127.0.0.1:8000/api/v0/mariadb/autoadd/` ;then
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

    echo"waiting for 30 seconds so the DB and DBUser can be created"
    sleep 30
    
    # check if the DB has been installed, initial request. ------------------------------------------------------------------------------------------------------------
    if DBOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $TOKEN"  http://127.0.0.1:8000/api/v0/mariadb/read/$DBID` ;then
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
    if DBOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $TOKEN"  http://127.0.0.1:8000/api/v0/mariadb/read/$DBID` ;then
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
    if DBUOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $TOKEN"  http://127.0.0.1:8000/api/v0/mariauser/read/$DBUSERID` ;then
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
    if DBUOKJSON=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $TOKEN"  http://127.0.0.1:8000/api/v0/mariauser/read/$DBUSERID` ;then
         printf $CGREEN2
         echo 'DBUser OK lookup.'
         printf $CEND
         DBUOK=`echo $DBUOKJSON | jq -r .installed_ok`
    else
         printf $CRED2
         echo 'DBUser OK lookup.'
    fi;
    done

fi;
