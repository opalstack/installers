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
PATH=/usr/local/bin:/bin:/usr/bin:/usr/local/sbin:/usr/sbin:/opt/puppetlabs/bin:$HOME/.local/bin:$HOME/bin:
export PATH

export PATH=/opt/rh/rh-ruby30/root/usr/local/bin:/opt/rh/rh-ruby30/root/usr/bin:/opt/bin:$HOME/apps/$APPNAME/env/bin:$PATH
export GEM_PATH=/opt/rh/rh-ruby30/root/usr/share/gems:$HOME/apps/$APPNAME/env/gems
export LD_LIBRARY_PATH=/opt/rh/rh-ruby30/root/usr/local/lib64:/opt/rh/rh-ruby30/root/usr/lib64:/opt/lib
export GEM_HOME=$HOME/apps/$APPNAME/env

mkdir -p $HOME/apps/$APPNAME/tmp $HOME/apps/$APPNAME/src
export TMPDIR=$HOME/apps/$APPNAME/tmp
export LD_LIBRARY_PATH=$HOME/lib

# curl
cd $HOME/apps/$APPNAME/src
/bin/wget https://curl.se/download/curl-8.4.0.tar.bz2 -O $HOME/apps/$APPNAME/src/curl-8.4.0.tar.bz2
mkdir $HOME/apps/$APPNAME/src/curl-8.4.0
cd $HOME/apps/$APPNAME/src/curl-8.4.0
/bin/tar -xf $HOME/apps/$APPNAME/src/curl-8.4.0.tar.bz2 -C $HOME/apps/$APPNAME/src/curl-8.4.0 --strip-components=1
cd $HOME/apps/$APPNAME/src/curl-8.4.0
./configure --prefix=$HOME --with-openssl --with-nghttp2 --with-ngtcp2 --with-nghttp3 --with-quiche --with-msh3
make && make install

# php
cd $HOME/apps/$APPNAME/src
/bin/wget https://www.php.net/distributions/php-8.1.2.tar.xz -O $HOME/apps/$APPNAME/src/php-8.1.2.tar.xz
mkdir $HOME/apps/$APPNAME/src/php-8.1.2
cd $HOME/apps/$APPNAME/src/php-8.1.2
/bin/tar -xf $HOME/apps/$APPNAME/src/php-8.1.2.tar.xz -C $HOME/apps/$APPNAME/src/php-8.1.2 --strip-components=1
cd $HOME/apps/$APPNAME/src/php-8.1.2
./configure --prefix=$HOME --enable-gd --enable-opcache --with-pdo-mysql --with-pdo-pgsql=/usr/pgsql-11 --enable-bcmath --enable-calendar --enable-exif --enable-ftp --enable-mbstring --enable-soap --with-curl=$HOME --with-gettext --with-gmp --with-iconv --with-kerberos --with-mhash --with-mysqli --with-openssl --with-pgsql=/usr/pgsql-11 --with-xsl --with-zlib-dir --enable-sockets --enable-intl --with-mysql-sock=/var/lib/mysql/mysql.sock --enable-fpm --with-zlib --enable-embed
make && make install

# nginx unit
git clone https://github.com/nginx/unit $HOME/apps/$APPNAME/src/unit
cd $HOME/apps/$APPNAME/src/unit
./configure --prefix=$HOME --libdir=$HOME/lib --includedir=$HOME/include --datarootdir=$HOME/share --mandir=$HOME/share/man 
./configure php --config=$HOME/bin/php-config --lib-path=$HOME/lib --module=php_81
./configure perl
./configure python --config=/usr/bin/python3.6-config --module=python_36
./configure ruby  --ruby=/opt/rh/rh-ruby30/root/bin/ruby --module=ruby_30
make && make install

mkdir $HOME/apps/$APPNAME/www

echo -e 'extension=memcached.so
zend_extension=opcache.so
[opcache]
opcache.enable=1
opcache.memory_consumption=1024
opcache.interned_strings_buffer=8
opcache.max_accelerated_files=4000
opcache.validate_timestamps=1
opcache.revalidate_freq=2
opcache.save_comments=1
opcache.enable_cli=1' > $HOME/apps/$APPNAME/www/php.ini

echo -e '<?php
phpinfo();
phpinfo(INFO_MODULES);
?>' > $HOME/apps/$APPNAME/www/index.php

# Define the cron job command
CRON_JOB="$HOME/sbin/unitd --control unix:/$HOME/apps/$APPNAME/unit.sock --log $HOME/logs/apps/$APPNAME/unit.log"

# Add the cron job to crontab
(crontab -l 2>/dev/null; echo "@reboot $CRON_JOB") | crontab -

cat << EOF > $HOME/apps/$APPNAME/config.json
{
  "listeners": {
    "*:$PORT": {
      "pass": "routes"
    }
  },
  "routes": [
    {
      "match": {
        "uri": [
          "*.php",
          "*.php/*",
          "/wp-admin/"
        ]
      },
      "action": {
        "pass": "applications/www/direct"
      }
    },
    {
      "action": {
        "share": "$HOME/apps/$APPNAME/www\$uri",
        "fallback": {
          "pass": "applications/www/index"
        }
      }
    }
  ],
  "applications": {
    "www": {
      "type": "php",
      "processes": {
        "max": 4,
        "spare": 4,
        "idle_timeout": 20
      },
      "targets": {
        "direct": {
          "root": "$HOME/apps/$APPNAME/www/"
        },
        "index": {
          "root": "$HOME/apps/$APPNAME/www/",
          "script": "index.php"
        }
      },
      "options": {
        "file": "$HOME/apps/$APPNAME/www/php.ini",
        "admin": {
          "memory_limit": "1024M",
          "upload_max_filesize": "64M",
          "post_max_size": "64M"
        }
      }
    }
  }
}
EOF

$HOME/sbin/unitd --control unix:/$HOME/apps/$APPNAME/unit.sock --log $HOME/logs/apps/$APPNAME/unit.log
curl -X PUT --data-binary @$HOME/apps/$APPNAME/config.json --unix-socket /$HOME/apps/$APPNAME/unit.sock http://localhost/config

# add installed OK
/usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d'[{"id": "'$UUID'"}]' $API_URL/api/v1/app/installed/

# add php-memcached
# add start/stop
