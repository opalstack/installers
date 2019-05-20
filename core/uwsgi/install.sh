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
echo "aW1wb3J0IG9zCnVzZXIgPSBvcy5nZXRlbnYoJ1VTRVInKQppbXBvcnQgYXJncGFyc2UKcGFyc2Vy
ID0gYXJncGFyc2UuQXJndW1lbnRQYXJzZXIoKQojLWRiIERBVEFCU0UgLXUgVVNFUk5BTUUgCnBh
cnNlci5hZGRfYXJndW1lbnQoIi1wIiwgaGVscD0iUG9ydCIpCnBhcnNlci5hZGRfYXJndW1lbnQo
Ii1uIiwgaGVscD0iQXBwIG5hbWUiKQphcmdzID0gcGFyc2VyLnBhcnNlX2FyZ3MoKQpwb3J0PWFy
Z3MucApuYW1lPWFyZ3MubgprZWVwYWxpdmVfcGF0aCA9IGYnL2hvbWUve3VzZXJ9L2FwcHMve25h
bWV9L2tlZXBhbGl2ZScKa2VlcGFsaXZlID0gZicnJyMhL2Jpbi9iYXNoCm1rZGlyIC1wICIkSE9N
RS90bXAiClBJREZJTEU9IiRIT01FL3RtcC97bmFtZX0ucGlkIgppZiBbIC1lICIke3tQSURGSUxF
fX0iIF0gJiYgKHBzIC11ICQod2hvYW1pKSAtb3BpZD0gfAogICAgICAgICAgICAgICAgICAgICAg
ICAgICBncmVwIC1QICJeXHMqJChjYXQgJHt7UElERklMRX19KSQiICY+IC9kZXYvbnVsbCk7IHRo
ZW4KICBlY2hvICJBbHJlYWR5IHJ1bm5pbmcuIgogIGV4aXQgOTkKZmkKcHJpbnRmICdTdGFydGVk
IGF0ICUoJUYgJVQpVFxuJwovaG9tZS97dXNlcn0vYXBwcy97bmFtZX0vZW52L2Jpbi91d3NnaSAt
LWh0dHAgMTI3LjAuMC4xOntwb3J0fSAtSCAvaG9tZS97dXNlcn0vYXBwcy97bmFtZX0vZW52LyAt
LXdzZ2ktZmlsZSAvaG9tZS97dXNlcn0vYXBwcy97bmFtZX0vbXlhcHAud3NnaSAtLWRhZW1vbml6
ZSAvaG9tZS97dXNlcn0vbG9ncy97bmFtZX0vdXdzZ2kubG9nIC0tcHJvY2Vzc2VzIDIgLS10aHJl
YWRzIDIgLS10b3VjaC1yZWxvYWQgL2hvbWUve3VzZXJ9L2FwcHMve25hbWV9L215YXBwLndzZ2kg
LS1waWRmaWxlICRQSURGSUxFCmVjaG8gJCEgPiAiJHt7UElERklMRX19IgpjaG1vZCA2NDQgIiR7
e1BJREZJTEV9fSIKJycnCmYgPSBvcGVuKGtlZXBhbGl2ZV9wYXRoLCAndysnKQpmLndyaXRlKGtl
ZXBhbGl2ZSkKZi5jbG9zZQpwcmludChmJ1dyb3RlIHtrZWVwYWxpdmVfcGF0aH0nKQoKa2lsbF9w
YXRoID0gZicvaG9tZS97dXNlcn0vYXBwcy97bmFtZX0va2lsbCcKa2lsbCA9ICcnJyMhL2Jpbi9i
YXNoCmtpbGwgLTkgYGNhdCAkSE9NRS90bXAvbXlwcm9ncmFtLnBpZGAKJycnCmYgPSBvcGVuKGtp
bGxfcGF0aCwgJ3crJykKZi53cml0ZShraWxsKQpmLmNsb3NlCnByaW50KGYnV3JvdGUge2tpbGxf
cGF0aH0nKQoKbXlhcHBfd3NnaV9wYXRoID0gZicvaG9tZS97dXNlcn0vYXBwcy97bmFtZX0vbXlh
cHAud3NnaScKbXlhcHBfd3NnaSA9IGYnJydkZWYgYXBwbGljYXRpb24oZW52LCBzdGFydF9yZXNw
b25zZSk6CiAgICBzdGFydF9yZXNwb25zZSgnMjAwIE9LJywgWygnQ29udGVudC1UeXBlJywndGV4
dC9odG1sJyldKQogICAgcmV0dXJuIFtiJ0hlbGxvIFdvcmxkISddCicnJwpmID0gb3BlbihteWFw
cF93c2dpX3BhdGgsICd3KycpCmYud3JpdGUobXlhcHBfd3NnaSkKZi5jbG9zZQpwcmludChmJ1dy
b3RlIHtteWFwcF93c2dpX3BhdGh9JykK" | base64 --decode > /home/$USER/ossrc/$APPNAME-generator.py
/usr/bin/python3.6 /home/$USER/ossrc/$APPNAME-generator.py -n $APPNAME -p $PORT

chmod +x /home/$USER/apps/$APPNAME/keepalive
chmod +x /home/$USER/apps/$APPNAME/kill

