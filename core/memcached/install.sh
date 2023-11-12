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
    # Get the port and verify the app exists, and thus the file schema exists.
    if serverjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  $API_URL/api/v1/app/read/$UUID` ;then
         printf $CGREEN2
         echo 'UUID validation and server lookup OK.'
         printf $CEND
         PORT=`echo $serverjson | jq -r .port`
    else
         printf $CRED2
         echo 'UUID validation and server lookup failed.'
         exit 1
    fi;
fi;
echo $PORT

/bin/wget https://memcached.org/files/memcached-1.6.22.tar.gz -O $HOME/apps/$APPNAME/memcached-1.6.22.tar.gz
/bin/tar -xf $HOME/apps/$APPNAME/memcached-1.6.22.tar.gz memcached-1.6.22 -C $HOME/apps/$APPNAME/ --strip-components=1
cd $HOME/apps/$APPNAME/memcached-1.6.22/
$HOME/apps/$APPNAME/memcached-1.6.22//configure --prefix=$HOME
$HOME/apps/$APPNAME/memcached-1.6.22/make && $HOME/apps/$APPNAME/memcached-1.6.22/make install
$HOME/bin/memcached -d -u memcached -l 127.0.0.1 -p $PORT -m 256
