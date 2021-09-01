#! /bin/bash

# python2.7 is end of life, and so is pip and many other tools which were available.
# https://pip.pypa.io/en/latest/development/release-process/#python-2-support


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

export APPROOT=$HOME/apps/$APPNAME/
export TMPDIR=$APPROOT/tmp

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

mkdir -p $APPROOT/src $APPROOT/tmp


# cd src
wget http://archive.apache.org/dist/httpd/httpd-2.4.41.tar.gz -O $APPROOT/src/httpd-2.4.41.tar.gz
tar zxf httpd-2.4.41.tar.gz

# cd httpd-2.4.41
./configure --prefix=$HOME/apps/$APPNAME/apache2 --enable-mods-shared=all --enable-mpms-shared=all --with-mpm=prefork
make
make install

# cd ..
wget https://github.com/GrahamDumpleton/mod_wsgi/archive/4.7.0.tar.gz
tar zxf 4.7.0.tar.gz

# cd mod_wsgi-4.7.0
./configure --with-python=/usr/bin/python2.7 --with-apxs=$HOME/apps/$APPNAME/apache2/bin/apxs
make
make install

# cd $HOME/apps/$APP

pip2.7 install django==1.8.7 -t $HOME/

django-admin startproject myproject



sed -r -i "s/^ALLOWED_HOSTS = \[\]/ALLOWED_HOSTS = \['\*'\]/" myproject/myproject/settings.py
sed -r -i "/^DATABASES =/, /^}$/ s/^/#/" myproject/myproject/settings.py
