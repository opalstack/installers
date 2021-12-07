#!/bin/bash

PYDIR=$1

mkdir -p $PYDIR/src

# openssl
cd $PYDIR/src
echo "building openssl..."
wget -q https://www.openssl.org/source/openssl-1.1.1l.tar.gz
tar zxf openssl-1.1.1l.tar.gz
cd openssl-1.1.1l
./config --prefix=$PYDIR > /dev/null
make > /dev/null
make install > /dev/null
echo "finished building openssl."

# python
echo "building python..."
export PATH=$PYDIR/bin:$PATH
export CPPFLAGS="-I$HOME/apps/testdjango/python/include $CPPFLAGS"
cd $PYDIR/src
wget -q https://www.python.org/ftp/python/3.10.1/Python-3.10.1.tar.xz
tar xf Python-3.10.1.tar.xz
cd Python-3.10.1
./configure --prefix=$PYDIR > /dev/null
make > /dev/null
make install > /dev/null
echo "finished building python."