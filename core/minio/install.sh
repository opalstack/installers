# Install minio
echo 'Minio server'
/bin/mkdir -p  $HOME/apps/$APPNAME/bin/minio
/bin/wget https://dl.min.io/server/minio/release/linux-amd64/minio -O  $HOME/apps/$APPNAME/bin/minio
/bin/chmod +x $HOME/apps/$APPNAME/bin/minio

MINIOPASS=`/usr/bin/openssl rand -base64 32`

MINIO_ACCESS_KEY=$USER MINIO_SECRET_KEY=$MINIOPASS $HOME/apps/$APPNAME/bin/minio server $HOME/apps/$APPNAME/mnt/data

wget https://dl.min.io/client/mc/release/linux-amd64/mc -O $HOME/apps/$APPNAME/bin/mc
/bin/chmod +x $HOME/apps/$APPNAME/bin/mc

/usr/bin/curl -s -X POST --header "Content-Type:application/json" --header "Authorization: Token $OPAL_TOKEN" -d'{"id": "'"$UUID"'", "init_created":true, "note":"'"Admin user: $USER / $MINIOPASS"'"}' $API_URL/api/v0/app/init_created/
