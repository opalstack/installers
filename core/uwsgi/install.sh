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

now=$(date)
echo "$now" >> /home/$USER/logs/$APPNAME/install.log

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

mkdir -p $HOME/apps/$APPNAME/tmp

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
dXNlcn0vYXBwcy97bmFtZX0vc3RhcnQnCmtlZXBhbGl2ZSA9IGYnJycjIS9iaW4vYmFzaApQSURG
SUxFPSIkSE9NRS9hcHBzL3tuYW1lfS90bXAve25hbWV9LnBpZCIKaWYgWyAtZSAiJHt7UElERklM
RX19IiBdICYmIChwcyAtdSAkKHdob2FtaSkgLW9waWQ9IHwKICAgICAgICAgICAgICAgICAgICAg
ICAgICAgZ3JlcCAtUCAiXlxzKiQoY2F0ICR7e1BJREZJTEV9fSkkIiAmPiAvZGV2L251bGwpOyB0
aGVuCiAgZWNobyAiQWxyZWFkeSBydW5uaW5nLiIKICBleGl0IDk5CmZpCmVjaG8gLW4gJ1N0YXJ0
ZWQgYXQgJwpkYXRlICIrJVktJW0tJWQgJUg6JU06JVMiCi9ob21lL3t1c2VyfS9hcHBzL3tuYW1l
fS9lbnYvYmluL3V3c2dpIC1NIC0taHR0cCAxMjcuMC4wLjE6e3BvcnR9IC1IIC9ob21lL3t1c2Vy
fS9hcHBzL3tuYW1lfS9lbnYvIC0td3NnaS1maWxlIC9ob21lL3t1c2VyfS9hcHBzL3tuYW1lfS9t
eWFwcC53c2dpIC0tZGFlbW9uaXplIC9ob21lL3t1c2VyfS9sb2dzL3tuYW1lfS91d3NnaS5sb2cg
LS1wcm9jZXNzZXMgMiAtLXRocmVhZHMgMiAtLXRvdWNoLXJlbG9hZCAvaG9tZS97dXNlcn0vYXBw
cy97bmFtZX0vbXlhcHAud3NnaSAtLXBpZGZpbGUgJFBJREZJTEUKJycnCmYgPSBvcGVuKGtlZXBh
bGl2ZV9wYXRoLCAndysnKQpmLndyaXRlKGtlZXBhbGl2ZSkKZi5jbG9zZQpwcmludChmJ1dyb3Rl
IHtrZWVwYWxpdmVfcGF0aH0nKQoKa2lsbF9wYXRoID0gZicvaG9tZS97dXNlcn0vYXBwcy97bmFt
ZX0va2lsbCcKa2lsbCA9IGYnJycjIS9iaW4vYmFzaApraWxsIC05IGBjYXQgJEhPTUUvYXBwcy97
bmFtZX0vdG1wL3tuYW1lfS5waWRgCicnJwoKZiA9IG9wZW4oa2lsbF9wYXRoLCAndysnKQpmLndy
aXRlKGtpbGwpCmYuY2xvc2UKcHJpbnQoZidXcm90ZSB7a2lsbF9wYXRofScpCgpzdG9wX3BhdGgg
PSBmJy9ob21lL3t1c2VyfS9hcHBzL3tuYW1lfS9zdG9wJwpzdG9wID0gZicnJyMhL2Jpbi9iYXNo
CgpQSURGSUxFPSIkSE9NRS9hcHBzL3tuYW1lfS90bXAve25hbWV9LnBpZCIKaWYgWyAtZSAiJHt7
UElERklMRX19IiBdICYmIChwcyAtdSAkKHdob2FtaSkgLW9waWQ9IHwKICAgICAgICAgICAgICAg
ICAgICAgICAgICAgZ3JlcCAtUCAiXlxzKiQoY2F0ICR7e1BJREZJTEV9fSkkIiAmPiAvZGV2L251
bGwpOyB0aGVuCi9ob21lL3t1c2VyfS9hcHBzL3tuYW1lfS9lbnYvYmluL3V3c2dpIC0tc3RvcCAv
aG9tZS97dXNlcn0vYXBwcy97bmFtZX0vdG1wL3tuYW1lfS5waWQKcm0gIC9ob21lL3t1c2VyfS9h
cHBzL3tuYW1lfS90bXAve25hbWV9LnBpZAogIGV4aXQgOTkKZmkKZWNobyAiTm8gUElEIGZpbGUi
CicnJwoKZiA9IG9wZW4oc3RvcF9wYXRoLCAndysnKQpmLndyaXRlKHN0b3ApCmYuY2xvc2UKcHJp
bnQoZidXcm90ZSB7c3RvcF9wYXRofScpCgpteWFwcF93c2dpX3BhdGggPSBmJy9ob21lL3t1c2Vy
fS9hcHBzL3tuYW1lfS9teWFwcC53c2dpJwpteWFwcF93c2dpID0gZicnJ2RlZiBhcHBsaWNhdGlv
bihlbnYsIHN0YXJ0X3Jlc3BvbnNlKToKICAgIHN0YXJ0X3Jlc3BvbnNlKCcyMDAgT0snLCBbKCdD
b250ZW50LVR5cGUnLCd0ZXh0L2h0bWwnKV0pCiAgICByZXR1cm4gW2InSGVsbG8gV29ybGQhJ10K
JycnCmYgPSBvcGVuKG15YXBwX3dzZ2lfcGF0aCwgJ3crJykKZi53cml0ZShteWFwcF93c2dpKQpm
LmNsb3NlCnByaW50KGYnV3JvdGUge215YXBwX3dzZ2lfcGF0aH0nKQo=" | base64 --decode > /home/$USER/apps/$APPNAME/tmp/$APPNAME-generator.py 
/usr/bin/python3.6 /home/$USER/apps/$APPNAME/tmp/$APPNAME-generator.py 

chmod +x /home/$USER/apps/$APPNAME/start
chmod +x /home/$USER/apps/$APPNAME/kill
chmod +x /home/$USER/apps/$APPNAME/stop

cline="*/10 * * * * /home/$USER/apps/$APPNAME/start"
(crontab -l; echo "$cline" ) | crontab -

# add installed OK
appok='{"id": "'"$UUID"'", "installed_ok":"True" }'
/usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d"$appok" https://my.opalstack.com/api/v0/app/installed_ok/
    
