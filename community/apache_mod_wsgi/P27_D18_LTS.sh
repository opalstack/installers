#! /bin/bash

# python2.7 is end of life, and so is pip and many other tools which were available are no longer so.
# this will build a very basic Django install which will require packages from the projects being imported
# to be placed in the site-packages directory manually.
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

export APPROOT=$HOME/apps/$APPNAME
mkdir -p $APPROOT/src $APPROOT/tmp $APPROOT/lib/ $APPROOT/lib/python2.7 $APPROOT/lib/python2.7/site-packages
export TMPDIR=$APPROOT/tmp


echo "
/bin/wget https://github.com/opalstack/installers/raw/master/community/apache_mod_wsgi/httpd-2.4.41.tar.gz -O $APPROOT/src/httpd-2.4.41.tar.gz
/bin/tar zxf $APPROOT/src/httpd-2.4.41.tar.gz --directory=$APPROOT/src
cd $APPROOT/src/httpd-2.4.41 && ./configure --srcdir=$APPROOT/src/httpd-2.4.41 --prefix=$APPROOT/apache2 --enable-mods-shared=all --enable-mpms-shared=all --with-mpm=prefork
cd $APPROOT/src/httpd-2.4.41 && make --directory=$APPROOT/src/httpd-2.4.41
cd $APPROOT/src/httpd-2.4.41 && make --directory=$APPROOT/src/httpd-2.4.41 install
/bin/wget https://github.com/opalstack/installers/raw/master/community/apache_mod_wsgi/mod_wsgi-4.7.0.tar.gz -O $APPROOT/src/mod_wsgi-4.7.0.tar.gz
/bin/tar zxf $APPROOT/src/mod_wsgi-4.7.0.tar.gz --directory=$APPROOT/src
/bin/cd $APPROOT/src/mod_wsgi-4.7.0 && ./configure --srcdir=$APPROOT/src/mod_wsgi-4.7.0 --with-python=/usr/bin/python2.7 --with-apxs=$APPROOT/apache2/bin/apxs
/bin/cd $APPROOT/src/mod_wsgi-4.7.0 && make --directory=$APPROOT/src/mod_wsgi-4.7.0
/bin/cd $APPROOT/src/mod_wsgi-4.7.0 && make --directory=$APPROOT/src/mod_wsgi-4.7.0 install

export PYTHONPATH=$APPROOT/lib/python2.7/site-packages
/bin/easy_install-2.7 --prefix=$APPROOT https://github.com/opalstack/installers/raw/master/community/apache_mod_wsgi/Django-1.8.19.tar.gz
" > $APPROOT/build.sh


# add installed OK
/usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d'[{"id": "'$UUID'"}]' $API_URL/api/v1/app/installed/


#django-admin startproject myproject
#sed -r -i "s/^ALLOWED_HOSTS = \[\]/ALLOWED_HOSTS = \['\*'\]/" myproject/myproject/settings.py
#sed -r -i "/^DATABASES =/, /^}$/ s/^/#/" myproject/myproject/settings.py
