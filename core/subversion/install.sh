#! /bin/bash
# Opalstack Boilerplate installer.

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
     -t Control panel TOKEN, used to authenticate to the API.
     -n Application NAME, must match the name in the control panel
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
    /bin/svnadmin create /home/$USER/apps/$APPNAME/repo
    /usr/bin/touch /home/$USER/apps/$APPNAME/passwd
    PASSWORD=$(date +%s | sha256sum | base64 | head -c 16 ; echo)
    /usr/bin/htpasswd -b -c /home/$USER/apps/$APPNAME/passwd $USER $PASSWORD
    /bin/setfacl -m u:apache:r-- /home/$USER/logs/apps/$APPNAME/passwd
    /usr/bin/touch /home/$USER/apps/$APPNAME/authz
    /bin/setfacl -m u:apache:r-- /home/$USER/apps/$APPNAME/authz
    /usr/bin/echo -e "[/]\n~\* = rw" > /home/$USER/apps/$APPNAME/authz
    /usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d'[{"id": "'$UUID'"}]' $API_URL/api/v1/app/installed/
    /usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d'[{"type": "D", "content":"'"Created Subversion app $APPNAME with login $USER / $PASSWORD"'"}]' $API_URL/api/v1/notice/create/

fi;
