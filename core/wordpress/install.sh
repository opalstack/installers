#! /bin/bash
# Opalstack WordPress installer.
# i is for UUID, t is for user token, n is for app name
CRED2='\033[1;91m'        # Red
CGREEN2='\033[1;92m'      # Green
CYELLOW2='\033[1;93m'     # Yellow
CBLUE2='\033[1;94m'       # Blue
CVIOLET2='\033[1;95m'     # Purple
CCYAN2='\033[1;96m'       # Cyan
CWHITE2='\033[1;97m'      # White
CEND='\033[0m'       # Text Reset

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
    # Best practice is to first make any external downloads, 
    # next api calls to control, 
    # last is the logic to create the application. 
    # autoadd DB, return its JSON user/pass as vars 

    # Get the server's UUID and verify the app exists, and thus the file schema exists.
    if serverjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $TOKEN"  http://127.0.0.1:8000/api/v0/app/read/$UUID` ;then
         printf $CGREEN2
         echo 'UUID validation, server lookup OK.'
         printf $CEND
         serverid=`echo $serverjson | jq .server`
    else
         printf $CRED2
         echo 'UUID validation, server lookup failed.'
         sexit 1
    fi;
    
    # create database
    dbsend='{"name": "'"$APPNAME"'", "server": '$serverid' }'
    if dbjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $TOKEN" -d"$dbsend"  http://127.0.0.1:8000/api/v0/mariadb/autoadd/` ;then
         export $(echo $dbjson| jq -r '@sh "DBNAME=\(.name) CHARSET=\(.charset) DBUSER=\(.dbuser) DBPWD=\(.default_password) SERVER=\(.server)"')
         printf $CGREEN2
         echo 'DB creation OK.'
         printf $CEND
    else
         printf $CRED2
         echo 'DB creation failed.'
         exit 1
    fi;
    echo $DBNAME


fi;



# curl -O https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar
# Path to your WordPress installs
# SITE_PATH="/home/$USER/apps/"  # need to add app path TODO
