#!/bin/bash

PYDIR=$1
export PATH=$PYDIR/bin:$PATH
export CPPFLAGS="-I$HOME/apps/testdjango/python/include $CPPFLAGS"

mkdir -p $PYDIR/src
cd $PYDIR/src

# openssl
echo "building openssl 1.1.1..."
wget https://www.openssl.org/source/openssl-1.1.1l.tar.gz
tar zxf openssl-1.1.1l.tar.gz
cd openssl-1.1.1l
./config --prefix=$PYDIR > /dev/null
make > /dev/null
make install > /dev/null
echo "building openssl 1.1.1..."

# python
wget https://www.python.org/ftp/python/3.10.1/Python-3.10.1.tar.xz
tar xf Python-3.10.1.tar.xz
cd Python-3.10.1
./configure --prefix=$PYDIR > /dev/null
make > /dev/null
make install > /dev/null