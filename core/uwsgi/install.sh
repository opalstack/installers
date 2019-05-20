#! /bin/bash
# Opalstack uwsgi installer.

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

printf 'Started at %(%F %T)T\n' >> /home/$USER/logs/$APPNAME/install.log

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
    if serverjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  https://my.opalstack.com/api/v0/app/read/$UUID` ;then
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

# installs a newer version of pip into .local/bin
/usr/bin/pip3.6 install --user --upgrade --force-reinstall pip
# Uses the upgraded pip to install/upgrade virtualenv
/home/$USER/.local/bin/pip3.6 install --user --force-reinstall virtualenv

/home/$USER/.local/bin/virtualenv /home/$USER/apps/$APPNAME/env
source /home/$USER/apps/$APPNAME/env/bin/activate

# install latest LTS release 
pip3.6 install https://projects.unbit.it/downloads/uwsgi-lts.tar.gz
chmod +x /home/$USER/apps/$APPNAME/env/bin/uwsgi

export PORT
export APPNAME
# generator.py - installs keepalive, kill, myapp.wsgi
echo "aW1wb3J0IG9zCnVzZXIgPSBvcy5nZXRlbnYoJ1VTRVInKQpuYW1lID0gb3MuZ2V0ZW52KCdBUFBO
QU1FJykKcG9ydCA9IG9zLmdldGVudignUE9SVCcpCmtlZXBhbGl2ZV9wYXRoID0gZicvaG9tZS97
dXNlcn0vYXBwcy97bmFtZX0va2VlcGFsaXZlJwprZWVwYWxpdmUgPSBmJycnIyEvYmluL2Jhc2gK
bWtkaXIgLXAgIiRIT01FL3RtcCIKUElERklMRT0iJEhPTUUvdG1wL3tuYW1lfS5waWQiCmlmIFsg
LWUgIiR7e1BJREZJTEV9fSIgXSAmJiAocHMgLXUgJCh3aG9hbWkpIC1vcGlkPSB8CiAgICAgICAg
ICAgICAgICAgICAgICAgICAgIGdyZXAgLVAgIl5ccyokKGNhdCAke3tQSURGSUxFfX0pJCIgJj4g
L2Rldi9udWxsKTsgdGhlbgogIGVjaG8gIkFscmVhZHkgcnVubmluZy4iCiAgZXhpdCA5OQpmaQpw
cmludGYgJ1N0YXJ0ZWQgYXQgJSglRiAlVClUXG4nCi9ob21lL3t1c2VyfS9hcHBzL3tuYW1lfS9l
bnYvYmluL3V3c2dpIC0taHR0cCAxMjcuMC4wLjE6e3BvcnR9IC1IIC9ob21lL3t1c2VyfS9hcHBz
L3tuYW1lfS9lbnYvIC0td3NnaS1maWxlIC9ob21lL3t1c2VyfS9hcHBzL3tuYW1lfS9teWFwcC53
c2dpIC0tZGFlbW9uaXplIC9ob21lL3t1c2VyfS9sb2dzL3tuYW1lfS91d3NnaS5sb2cgLS1wcm9j
ZXNzZXMgMiAtLXRocmVhZHMgMiAtLXRvdWNoLXJlbG9hZCAvaG9tZS97dXNlcn0vYXBwcy97bmFt
ZX0vbXlhcHAud3NnaSAtLXBpZGZpbGUgJFBJREZJTEUKZWNobyAkISA+ICIke3tQSURGSUxFfX0i
CmNobW9kIDY0NCAiJHt7UElERklMRX19IgonJycKZiA9IG9wZW4oa2VlcGFsaXZlX3BhdGgsICd3
KycpCmYud3JpdGUoa2VlcGFsaXZlKQpmLmNsb3NlCnByaW50KGYnV3JvdGUge2tlZXBhbGl2ZV9w
YXRofScpCgpraWxsX3BhdGggPSBmJy9ob21lL3t1c2VyfS9hcHBzL3tuYW1lfS9raWxsJwpraWxs
ID0gJycnIyEvYmluL2Jhc2gKa2lsbCAtOSBgY2F0ICRIT01FL3RtcC9teXByb2dyYW0ucGlkYAon
JycKZiA9IG9wZW4oa2lsbF9wYXRoLCAndysnKQpmLndyaXRlKGtpbGwpCmYuY2xvc2UKcHJpbnQo
ZidXcm90ZSB7a2lsbF9wYXRofScpCgpteWFwcF93c2dpX3BhdGggPSBmJy9ob21lL3t1c2VyfS9h
cHBzL3tuYW1lfS9teWFwcC53c2dpJwpteWFwcF93c2dpID0gZicnJ2RlZiBhcHBsaWNhdGlvbihl
bnYsIHN0YXJ0X3Jlc3BvbnNlKToKICAgIHN0YXJ0X3Jlc3BvbnNlKCcyMDAgT0snLCBbKCdDb250
ZW50LVR5cGUnLCd0ZXh0L2h0bWwnKV0pCiAgICByZXR1cm4gW2InSGVsbG8gV29ybGQhJ10KJycn
CmYgPSBvcGVuKG15YXBwX3dzZ2lfcGF0aCwgJ3crJykKZi53cml0ZShteWFwcF93c2dpKQpmLmNs
b3NlCnByaW50KGYnV3JvdGUge215YXBwX3dzZ2lfcGF0aH0nKQo=" | base64 --decode > /home/$USER/ossrc/$APPNAME-generator.py
/usr/bin/python3.6 /home/$USER/ossrc/$APPNAME-generator.py 

chmod +x /home/$USER/apps/$APPNAME/keepalive
chmod +x /home/$USER/apps/$APPNAME/kill

line="*/1 * * * * /home/$USER/apps/$APPNAME/keepalive"
(crontab -u $USER -l; echo "$line" ) | crontab -u $USER -

# add installed OK
