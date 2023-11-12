#! /bin/bash
# Opalstack Boilerplate installer.

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

printf 'Started at %(%F %T)T\n' >> /home/$USER/logs/apps/$APPNAME/install.log

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
    if serverjson=`curl -s --fail --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN"  $API_URL/api/v1/app/read/$UUID` ;then
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

PATH=/usr/local/bin:/bin:/usr/bin:/usr/local/sbin:/usr/sbin:/opt/puppetlabs/bin:$HOME/.local/bin:$HOME/bin:
export PATH



#!/bin/bash

# Function to compare version numbers
# Returns 0 if first version is greater than or equal to the second
compare_versions() {
    local IFS=.
    local i ver1=($1) ver2=($2)

    # Fill empty fields in ver1 with zeros
    for ((i=${#ver1[@]}; i<${#ver2[@]}; i++))
    do
        ver1[i]=0
    done

    for ((i=0; i<${#ver1[@]}; i++))
    do
        if [[ -z ${ver2[i]} ]]
        then
            # Fill empty fields in ver2 with zeros
            ver2[i]=0
        fi
        if ((10#${ver1[i]} > 10#${ver2[i]}))
        then
            return 0
        elif ((10#${ver1[i]} < 10#${ver2[i]}))
        then
            return 1
        fi
    done
    return 0
}

# Get the current memcached version
current_version=$($HOME/bin/memcached -V 2>/dev/null | awk '{print $2}')

# Target version
target_version="1.6.22"

# Check if current version is less than the target version or memcached is not installed
if [[ -z "$current_version" ]] || ! compare_versions $current_version $target_version; then
    echo "Current memcached version is lower than $target_version or not installed. Proceeding with installation."
    # Download and install memcached 1.6.22
    /bin/wget https://memcached.org/files/memcached-1.6.22.tar.gz -O $HOME/apps/$APPNAME/memcached-1.6.22.tar.gz
    mkdir -p $HOME/apps/$APPNAME/src
    /bin/tar -xf $HOME/apps/$APPNAME/memcached-1.6.22.tar.gz -C $HOME/apps/$APPNAME/src --strip-components=1
    cd $HOME/apps/$APPNAME/src/
    ./configure --prefix=$HOME
    /bin/make && /bin/make install
else
    echo "Current memcached version ($current_version) is $target_version or higher. No installation needed."
fi

# Start memcached
$HOME/bin/memcached -d -s $HOME/apps/$APPNAME/memcached.sock -P $HOME/apps/$APPNAME/memcached.pid -m 256

# Define the cron job command
CRON_JOB="$HOME/bin/memcached -d -s $HOME/apps/$APPNAME/memcached.sock -P $HOME/apps/$APPNAME/memcached.pid -m 256"

# Add the cron job to crontab
(crontab -l 2>/dev/null; echo "@reboot $CRON_JOB") | crontab -

# Create the start script
cat <<EOF >$HOME/apps/$APPNAME/start
#!/bin/bash

# Check if memcached is already running
if [ -f \$HOME/apps/$APPNAME/memcached.pid ]; then
    PID=\$(cat \$HOME/apps/$APPNAME/memcached.pid)
    if ps -p \$PID > /dev/null 2>&1; then
        echo "memcached is already running."
        exit 1
    fi
fi

# Start memcached command
\$HOME/bin/memcached -d -s \$HOME/apps/$APPNAME/memcached.sock -P \$HOME/apps/$APPNAME/memcached.pid -m 256
EOF

# Create the stop script
cat <<EOF >$HOME/apps/$APPNAME/stop
#!/bin/bash

# Check if the PID file exists
if [ -f \$HOME/apps/$APPNAME/memcached.pid ]; then
    # Read the PID from the file
    PID=\$(cat \$HOME/apps/$APPNAME/memcached.pid)

    # Kill the process
    kill \$PID

    # Optionally, you can also remove the PID file after stopping
    rm \$HOME/apps/$APPNAME/memcached.pid
else
    echo "PID file does not exist. Is memcached running?"
fi
EOF

# Make the scripts executable
chmod +x $HOME/apps/$APPNAME/start
chmod +x $HOME/apps/$APPNAME/stop

echo "Start and stop scripts for memcached have been created."

# add installed OK
/usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d'[{"id": "'$UUID'"}]' $API_URL/api/v1/app/installed/