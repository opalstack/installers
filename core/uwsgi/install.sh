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
aW5nLiIKICBleGl0IDk5CmZpCnByaW50ZiAnU3RhcnRlZCBhdCAlKCVGICVUKVRcbicKL2hvbWUv
e3VzZXJ9L2FwcHMve25hbWV9L2Vudi9iaW4vdXdzZ2kgLS1odHRwIDEyNy4wLjAuMTp7cG9ydH0g
LUggL2hvbWUve3VzZXJ9L2FwcHMve25hbWV9L2Vudi8gLS13c2dpLWZpbGUgL2hvbWUve3VzZXJ9
L2FwcHMve25hbWV9L215YXBwLndzZ2kgLS1kYWVtb25pemUgL2hvbWUve3VzZXJ9L2xvZ3Mve25h
bWV9L3V3c2dpLmxvZyAtLXByb2Nlc3NlcyAyIC0tdGhyZWFkcyAyIC0tdG91Y2gtcmVsb2FkIC9o
b21lL3t1c2VyfS9hcHBzL3tuYW1lfS9teWFwcC53c2dpIC0tcGlkZmlsZSAkUElERklMRQplY2hv
ICQhID4gIiR7e1BJREZJTEV9fSIKY2htb2QgNjQ0ICIke3tQSURGSUxFfX0iCicnJwpmID0gb3Bl
bihrZWVwYWxpdmVfcGF0aCwgJ3crJykKZi53cml0ZShrZWVwYWxpdmUpCmYuY2xvc2UKcHJpbnQo
ZidXcm90ZSB7a2VlcGFsaXZlX3BhdGh9JykKCmtpbGxfcGF0aCA9IGYnL2hvbWUve3VzZXJ9L2Fw
cHMve25hbWV9L2tpbGwnCmtpbGwgPSBmJycnIyEvYmluL2Jhc2gKa2lsbCAtOSBgY2F0ICRIT01F
L2FwcHMve25hbWV9L3RtcC97bmFtZX0ucGlkYAonJycKCmYgPSBvcGVuKGtpbGxfcGF0aCwgJ3cr
JykKZi53cml0ZShraWxsKQpmLmNsb3NlCnByaW50KGYnV3JvdGUge2tpbGxfcGF0aH0nKQoKc3Rv
cF9wYXRoID0gZicvaG9tZS97dXNlcn0vYXBwcy97bmFtZX0vc3RvcCcKc3RvcCA9IGYnJycjIS9i
aW4vYmFzaAovaG9tZS97dXNlcn0vYXBwcy97bmFtZX0vZW52L2Jpbi91d3NnaSAtLXN0b3AgL2hv
bWUve3VzZXJ9L2FwcHMve25hbWV9L3RtcC97bmFtZX0ucGlkCnJtICAvaG9tZS97dXNlcn0vYXBw
cy97bmFtZX0vdG1wL3tuYW1lfS5waWQKJycnCgpmID0gb3BlbihzdG9wX3BhdGgsICd3KycpCmYu
d3JpdGUoc3RvcCkKZi5jbG9zZQpwcmludChmJ1dyb3RlIHtzdG9wX3BhdGh9JykKCm15YXBwX3dz
Z2lfcGF0aCA9IGYnL2hvbWUve3VzZXJ9L2FwcHMve25hbWV9L215YXBwLndzZ2knCm15YXBwX3dz
Z2kgPSBmJycnZGVmIGFwcGxpY2F0aW9uKGVudiwgc3RhcnRfcmVzcG9uc2UpOgogICAgc3RhcnRf
cmVzcG9uc2UoJzIwMCBPSycsIFsoJ0NvbnRlbnQtVHlwZScsJ3RleHQvaHRtbCcpXSkKICAgIHJl
dHVybiBbYidIZWxsbyBXb3JsZCEnXQonJycKZiA9IG9wZW4obXlhcHBfd3NnaV9wYXRoLCAndysn
KQpmLndyaXRlKG15YXBwX3dzZ2kpCmYuY2xvc2UKcHJpbnQoZidXcm90ZSB7bXlhcHBfd3NnaV9w
YXRofScpCg==" | base64 --decode > /home/$USER/apps/$APPNAME/tmp/$APPNAME-generator.py 
/usr/bin/python3.6 /home/$USER/apps/$APPNAME/tmp/$APPNAME-generator.py 


chmod +x /home/$USER/apps/$APPNAME/start
chmod +x /home/$USER/apps/$APPNAME/kill
chmod +x /home/$USER/apps/$APPNAME/stop

cline="*/10 * * * * /home/$USER/apps/$APPNAME/start"
(crontab -l; echo "$cline" ) | crontab -

# add installed OK
appok='{"id": "'"$UUID"'", "installed_ok":"True" }'
/usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d"$appok" https://my.opalstack.com/api/v0/app/installed_ok/
    
