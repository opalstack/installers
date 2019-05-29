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
ciAtcCAiJEhPTUUvdG1wIgpQSURGSUxFPSIkSE9NRS90bXAve25hbWV9LnBpZCIKaWYgWyAtZSAi
JHt7UElERklMRX19IiBdICYmIChwcyAtdSAkKHdob2FtaSkgLW9waWQ9IHwKICAgICAgICAgICAg
ICAgICAgICAgICAgICAgZ3JlcCAtUCAiXlxzKiQoY2F0ICR7e1BJREZJTEV9fSkkIiAmPiAvZGV2
L251bGwpOyB0aGVuCiAgZWNobyAiQWxyZWFkeSBydW5uaW5nLiIKICBleGl0IDk5CmZpCnByaW50
ZiAnU3RhcnRlZCBhdCAlKCVGICVUKVRcbicKL2hvbWUve3VzZXJ9L2FwcHMve25hbWV9L2Vudi9i
aW4vdXdzZ2kgLS1odHRwIDEyNy4wLjAuMTp7cG9ydH0gLUggL2hvbWUve3VzZXJ9L2FwcHMve25h
bWV9L2Vudi8gLS13c2dpLWZpbGUgL2hvbWUve3VzZXJ9L2FwcHMve25hbWV9L215YXBwLndzZ2kg
LS1kYWVtb25pemUgL2hvbWUve3VzZXJ9L2xvZ3Mve25hbWV9L3V3c2dpLmxvZyAtLXByb2Nlc3Nl
cyAyIC0tdGhyZWFkcyAyIC0tdG91Y2gtcmVsb2FkIC9ob21lL3t1c2VyfS9hcHBzL3tuYW1lfS9t
eWFwcC53c2dpIC0tcGlkZmlsZSAkUElERklMRQplY2hvICQhID4gIiR7e1BJREZJTEV9fSIKY2ht
b2QgNjQ0ICIke3tQSURGSUxFfX0iCicnJwpmID0gb3BlbihrZWVwYWxpdmVfcGF0aCwgJ3crJykK
Zi53cml0ZShrZWVwYWxpdmUpCmYuY2xvc2UKcHJpbnQoZidXcm90ZSB7a2VlcGFsaXZlX3BhdGh9
JykKCmtpbGxfcGF0aCA9IGYnL2hvbWUve3VzZXJ9L2FwcHMve25hbWV9L2tpbGwnCmtpbGwgPSBm
JycnIyEvYmluL2Jhc2gKa2lsbCAtOSBgY2F0ICRIT01FL3RtcC97bmFtZX0ucGlkYAonJycKCmYg
PSBvcGVuKGtpbGxfcGF0aCwgJ3crJykKZi53cml0ZShraWxsKQpmLmNsb3NlCnByaW50KGYnV3Jv
dGUge2tpbGxfcGF0aH0nKQoKc3RvcF9wYXRoID0gZicvaG9tZS97dXNlcn0vYXBwcy97bmFtZX0v
c3RvcCcKc3RvcCA9IGYnJycjIS9iaW4vYmFzaAovaG9tZS97dXNlcn0vYXBwcy97bmFtZX0vZW52
L2Jpbi91d3NnaSAtLXN0b3AgL2hvbWUve3VzZXJ9L3RtcC97bmFtZX0ucGlkCnJtICAvaG9tZS97
dXNlcn0vdG1wL3tuYW1lfS5waWQKJycnCgpmID0gb3BlbihzdG9wX3BhdGgsICd3KycpCmYud3Jp
dGUoc3RvcCkKZi5jbG9zZQpwcmludChmJ1dyb3RlIHtzdG9wX3BhdGh9JykKCm15YXBwX3dzZ2lf
cGF0aCA9IGYnL2hvbWUve3VzZXJ9L2FwcHMve25hbWV9L215YXBwLndzZ2knCm15YXBwX3dzZ2kg
PSBmJycnZGVmIGFwcGxpY2F0aW9uKGVudiwgc3RhcnRfcmVzcG9uc2UpOgogICAgc3RhcnRfcmVz
cG9uc2UoJzIwMCBPSycsIFsoJ0NvbnRlbnQtVHlwZScsJ3RleHQvaHRtbCcpXSkKICAgIHJldHVy
biBbYidIZWxsbyBXb3JsZCEnXQonJycKZiA9IG9wZW4obXlhcHBfd3NnaV9wYXRoLCAndysnKQpm
LndyaXRlKG15YXBwX3dzZ2kpCmYuY2xvc2UKcHJpbnQoZidXcm90ZSB7bXlhcHBfd3NnaV9wYXRo
fScpCg==" | base64 --decode > /home/$USER/ossrc/$APPNAME-generator.py
/usr/bin/python3.6 /home/$USER/ossrc/$APPNAME-generator.py 

chmod +x /home/$USER/apps/$APPNAME/start
chmod +x /home/$USER/apps/$APPNAME/kill
chmod +x /home/$USER/apps/$APPNAME/stop

cline="*/10 * * * * /home/$USER/apps/$APPNAME/start"
(crontab -l; echo "$cline" ) | crontab -

# add installed OK
appok='{"id": "'"$UUID"'", "installed_ok":"True" }'
/usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d"$appok" https://my.opalstack.com/api/v0/app/installed_ok/
    
