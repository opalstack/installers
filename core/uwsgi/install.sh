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
dXNlcn0vYXBwcy97bmFtZX0vc3RhcnQnCmtlZXBhbGl2ZSA9IGYnJycjIS9iaW4vYmFzaApta2Rp
ciAtcCAiJEhPTUUvYXBwcy97bmFtZX0vdG1wIgpQSURGSUxFPSIkSE9NRS9hcHBzL3tuYW1lfS90
bXAve25hbWV9LnBpZCIKaWYgWyAtZSAiJHt7UElERklMRX19IiBdICYmIChwcyAtdSAkKHdob2Ft
aSkgLW9waWQ9IHwKICAgICAgICAgICAgICAgICAgICAgICAgICAgZ3JlcCAtUCAiXlxzKiQoY2F0
ICR7e1BJREZJTEV9fSkkIiAmPiAvZGV2L251bGwpOyB0aGVuCiAgZWNobyAiQWxyZWFkeSBydW5u
aW5nLiIKICBleGl0IDk5CmZpCmVjaG8gLW4gJ1N0YXJ0ZWQgYXQgJwpkYXRlICIrJVktJW0tJWQg
JUg6JU06JVMiCi9ob21lL3t1c2VyfS9hcHBzL3tuYW1lfS9lbnYvYmluL3V3c2dpIC1NIC0taHR0
cCAxMjcuMC4wLjE6e3BvcnR9IC1IIC9ob21lL3t1c2VyfS9hcHBzL3tuYW1lfS9lbnYvIC0td3Nn
aS1maWxlIC9ob21lL3t1c2VyfS9hcHBzL3tuYW1lfS9teWFwcC53c2dpIC0tZGFlbW9uaXplIC9o
b21lL3t1c2VyfS9sb2dzL3tuYW1lfS91d3NnaS5sb2cgLS1wcm9jZXNzZXMgMiAtLXRocmVhZHMg
MiAtLXRvdWNoLXJlbG9hZCAvaG9tZS97dXNlcn0vYXBwcy97bmFtZX0vbXlhcHAud3NnaSAtLXBp
ZGZpbGUgJFBJREZJTEUKJycnCmYgPSBvcGVuKGtlZXBhbGl2ZV9wYXRoLCAndysnKQpmLndyaXRl
KGtlZXBhbGl2ZSkKZi5jbG9zZQpwcmludChmJ1dyb3RlIHtrZWVwYWxpdmVfcGF0aH0nKQoKa2ls
bF9wYXRoID0gZicvaG9tZS97dXNlcn0vYXBwcy97bmFtZX0va2lsbCcKa2lsbCA9IGYnJycjIS9i
aW4vYmFzaApraWxsIC05IGBjYXQgJEhPTUUvYXBwcy97bmFtZX0vdG1wL3tuYW1lfS5waWRgCicn
JwoKZiA9IG9wZW4oa2lsbF9wYXRoLCAndysnKQpmLndyaXRlKGtpbGwpCmYuY2xvc2UKcHJpbnQo
ZidXcm90ZSB7a2lsbF9wYXRofScpCgpzdG9wX3BhdGggPSBmJy9ob21lL3t1c2VyfS9hcHBzL3tu
YW1lfS9zdG9wJwpzdG9wID0gZicnJyMhL2Jpbi9iYXNoCi9ob21lL3t1c2VyfS9hcHBzL3tuYW1l
fS9lbnYvYmluL3V3c2dpIC0tc3RvcCAvaG9tZS97dXNlcn0vYXBwcy97bmFtZX0vdG1wL3tuYW1l
fS5waWQKcm0gIC9ob21lL3t1c2VyfS9hcHBzL3tuYW1lfS90bXAve25hbWV9LnBpZAonJycKCmYg
PSBvcGVuKHN0b3BfcGF0aCwgJ3crJykKZi53cml0ZShzdG9wKQpmLmNsb3NlCnByaW50KGYnV3Jv
dGUge3N0b3BfcGF0aH0nKQoKbXlhcHBfd3NnaV9wYXRoID0gZicvaG9tZS97dXNlcn0vYXBwcy97
bmFtZX0vbXlhcHAud3NnaScKbXlhcHBfd3NnaSA9IGYnJydkZWYgYXBwbGljYXRpb24oZW52LCBz
dGFydF9yZXNwb25zZSk6CiAgICBzdGFydF9yZXNwb25zZSgnMjAwIE9LJywgWygnQ29udGVudC1U
eXBlJywndGV4dC9odG1sJyldKQogICAgcmV0dXJuIFtiJ0hlbGxvIFdvcmxkISddCicnJwpmID0g
b3BlbihteWFwcF93c2dpX3BhdGgsICd3KycpCmYud3JpdGUobXlhcHBfd3NnaSkKZi5jbG9zZQpw
cmludChmJ1dyb3RlIHtteWFwcF93c2dpX3BhdGh9JykK" | base64 --decode > /home/$USER/apps/$APPNAME/tmp/$APPNAME-generator.py 
/usr/bin/python3.6 /home/$USER/apps/$APPNAME/tmp/$APPNAME-generator.py 

chmod +x /home/$USER/apps/$APPNAME/start
chmod +x /home/$USER/apps/$APPNAME/kill
chmod +x /home/$USER/apps/$APPNAME/stop

cline="*/10 * * * * /home/$USER/apps/$APPNAME/start"
(crontab -l; echo "$cline" ) | crontab -

# add installed OK
appok='{"id": "'"$UUID"'", "installed_ok":"True" }'
/usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d"$appok" https://my.opalstack.com/api/v0/app/installed_ok/
    
