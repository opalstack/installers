#!/bin/sh

EXPECTED_CHECKSUM="$(php -r 'copy("https://composer.github.io/installer.sig", "php://stdout");')"
php -r "copy('https://getcomposer.org/installer', 'composer-setup.php');"
ACTUAL_CHECKSUM="$(php -r "echo hash_file('sha384', 'composer-setup.php');")"

if [ "$EXPECTED_CHECKSUM" != "$ACTUAL_CHECKSUM" ]
then
    >&2 echo 'ERROR: Invalid installer checksum'
    rm composer-setup.php
    exit 1
fi

php composer-setup.php --quiet
RESULT=$?
rm composer-setup.php
#exit $RESULT

#!/bin/bash

mkdir -p ~/.local/
mkdir -p ~/.local/bin
cp ~/composer.phar ~/.local/bin/composer

git clone https://github.com/leonstafford/wp2static.git
cd wp2static
composer install
composer build wp2static
