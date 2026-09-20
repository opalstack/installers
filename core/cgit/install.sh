#!/bin/bash

set -Eeuo pipefail

echo "$( date +'%Y-%m-%d %H:%M:%S' ) started installation"

# args and env
while getopts "i:n:" opt; do
  case "${opt}" in
    i) UUID=${OPTARG} ;;
    n) APPNAME=$OPTARG ;;
  esac
done

if [ -z $UUID ]; then
  echo "missing UUID due to no -i arg, aborting."
  exit 1
fi
if [ -z $APPNAME ]; then
  echo "missing APPNAME due to no -n arg, aborting."
  exit 1
fi
if [ -z $OPAL_TOKEN ]; then
  echo "missing OPAL_TOKEN env var, aborting."
  exit 1
fi
if [ -z $API_URL ]; then
  echo "missing API_URL env var, aborting."
  exit 1
fi

export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

APPDIR=$PWD

CGIT_VERSION=$( grep -q CentOS /etc/redhat-release && echo 1.2.3 || echo 1.3.1 )

# dirs
mkdir -p \
  app/etc \
  app/lib \
  app/share/doc/cgit \
  app/share/man \
  app/var/cache/cgit \
  repos \
  src \
  www/static

# build
cd src
wget https://git.zx2c4.com/cgit/snapshot/cgit-$CGIT_VERSION.tar.xz
tar xf cgit-$CGIT_VERSION.tar.xz
cd cgit-$CGIT_VERSION

cat << EOF > cgit.conf
CGIT_SCRIPT_PATH = $APPDIR/www
CGIT_DATA_PATH = $APPDIR
prefix=$APPDIR/app
CGIT_CONFIG = $APPDIR/app/etc/cgitrc
CACHE_ROOT = $APPDIR/app/var/cache/cgit
libdir = $APPDIR/app/lib
filterdir = $APPDIR/app/lib/cgit/filters
docdir = $APPDIR/app/share/doc/cgit
htmldir = $APPDIR/app/share/doc/cgit
pdfdir = $APPDIR/app/share/doc/cgit
mandir = $APPDIR/app/share/man
EOF

grep -q CentOS /etc/redhat-release && CMD_PREFIX='scl enable devtoolset-11 -- ' || CMD_PREFIX=''
$CMD_PREFIX make get-git
$CMD_PREFIX make
$CMD_PREFIX make install
cp favicon.ico $APPDIR/www/
cp robots.txt $APPDIR/www/
cp cgit.css $APPDIR/www/static/
cp cgit.png $APPDIR/www/static/
[[ -e cgit.js ]] && cp cgit.js $APPDIR/www/static/
rm -f $APPDIR/cgit.css
rm -f $APPDIR/cgit.png
rm -f $APPDIR/favicon.ico
rm -f $APPDIR/robots.txt
rm -f $APPDIR/cgit.js
cd $APPDIR

# make cgitrc
cat << EOF > $APPDIR/app/etc/cgitrc
# site config
css=/static/cgit.css
logo=/static/cgit.png
root-title=$( basename $APPDIR )
root-desc=powered by cgit
root-readme=$APPDIR/www/about.md

# repo behavior
readme=:README.md
about-filter=$APPDIR/app/lib/cgit/filters/about-formatting.sh
source-filter=$APPDIR/app/lib/cgit/filters/syntax-highlighting.py

# auto repo config
scan-path=$APPDIR/repos
max-repo-count=10000

# # manual repo config
# repo.url=reponame
# repo.path=$APPDIR/repos/reponame
# repo.desc=Your repo description
# repo.owner=Repo Owner
EOF

# make .htaccess
cat << EOF > $APPDIR/www/.htaccess
Options +ExecCGI
AddHandler cgi-script .cgi
DirectoryIndex cgit.cgi
EOF

# make about page
cat << EOF > $APPDIR/www/about.md
My cgit site
EOF

# python deps
python3.13 -m venv $APPDIR/.venv
source $APPDIR/.venv/bin/activate
pip install markdown pygments
sed -i 's|#!/usr/bin/env python3|#!'$APPDIR'/.venv/bin/python3|' $APPDIR/app/lib/cgit/filters/html-converters/md2html
sed -i 's|#!/usr/bin/env python3|#!'$APPDIR'/.venv/bin/python3|' $APPDIR/app/lib/cgit/filters/syntax-highlighting.py
sed -i 's|#!/usr/bin/env python3|#!'$APPDIR'/.venv/bin/python3|' $APPDIR/app/lib/cgit/filters/email-gravatar.py

# make README
cat << EOF > $APPDIR/README
# Opalstack cgit README

## General

cgit is a fast read-only web frontend for git repositories. The cgit homepage
is: https://git.zx2c4.com/cgit/about/

Your cgit application was installed using version $CGIT_VERSION.

You must add the application to a site in order to access it via the web:
https://docs.opalstack.com/user-guide/sites/#adding-sites

## Configuration

Your cgit configuration is located at $APPDIR/app/etc/cgitrc. You can edit that
file to set the site name and description, logo and CSS path, repository
details, and more. The available settings are documented at:
https://git.zx2c4.com/cgit/tree/cgitrc.5.txt

## Repositories

The default repository location is $APPDIR/repos with cgit configured to
automatically list repositories in that directory.

To create a new repository, run the following commands replacing "my_new_repo"
with the name of your new repository:

  cd $APPDIR/repos
  git init --bare my_new_repo

You can add an existing repository to cgit like so, replacing REPO_URL with the
URL of the existing repository:

  cd $APPDIR/repos
  git clone --bare REPO_URL

You can clone your repostories over HTTP like so, replacing "repo_name" with
the repository name and "cgit.mydomain.com" with your cgit site URL:

  git clone https://cgit.mydomain.com/cgit.cgi/repo_name

Note that you cannot push changes back over HTTP with cgit. It is a read-only
interface. If you want to push changes back to a cgit-hosted repository, then
you must clone it via SSH like so, replacing "user@host" with your shell
username and Opalstack server name:

  git clone user@host:$APPDIR/repos/repo_name

You can then use that working copy to make and push changes. The pushed changes
will be then visible in cgit.
EOF

# finished
echo "$( date +'%Y-%m-%d %H:%M:%S' ) completed installation"
curl -s -o /dev/null -H "Content-Type: application/json" -H "Authorization: Token $OPAL_TOKEN" -d'[{"id": "'$UUID'"}]' $API_URL/api/v1/app/installed/
