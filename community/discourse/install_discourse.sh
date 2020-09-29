#!/bin/bash

# change the following to your values
SHELL_USER=your_shell_user
DISCOURSE_APP_NAME=discourse_app
REDIS_APP_NAME=discourse_redis
SITE_DOMAIN=domain.com
DB_NAME=your_db
DB_USER=your_db_user
DB_PASS=your_db_password
DISCOURSE_PORT=55551
REDIS_PORT=55552
SMTP_HOST=smtp.us.opalstack.com
SMTP_USER=your_mail_user
SMTP_PASS=your_mail_password


# STOP
# you should not need to edit below this line
# env
export PATH=/usr/pgsql-11/bin/:$PATH
export RAILS_ENV=production

# install brotli in home dir
mkdir -p ~/src ~/bin
cd ~/src
git clone https://github.com/google/brotli.git
cd brotli
make
cp bin/brotli ~/bin/

# install redis
cd ~/apps/$REDIS_APP_NAME
mkdir tmp
wget http://download.redis.io/redis-stable.tar.gz
tar zxf redis-stable.tar.gz
rm redis-stable.tar.gz
cd redis-stable
make

#  discourse_redis port
sed -i -e 's/port 6379/port '"$REDIS_PORT"'/' redis.conf

# change username and app name in next 4 lines to your shell user and discourse app name
sed -i -e 's/^pidfile \/var\/run\/redis_6379.pid/pidfile \/home\/'"$SHELL_USER"'\/apps\/'"$REDIS_APP_NAME"'\/tmp\/redis.pid/' redis.conf
sed -i -e 's/^logfile ""/logfile \/home\/'"$SHELL_USER"'\/logs\/apps\/'"$REDIS_APP_NAME"'\/redis.log/' redis.conf
sed -i -e 's/daemonize no/daemonize yes/' redis.conf

# start redis
/home/$SHELL_USER/apps/$REDIS_APP_NAME/redis-stable/src/redis-server /home/$SHELL_USER/apps/$REDIS_APP_NAME/redis-stable/redis.conf

# install discourse
cd /home/$SHELL_USER/apps/$DISCOURSE_APP_NAME
git clone https://github.com/discourse/discourse.git
cd discourse
mkdir -p tmp/pids
bundle install --path vendor/bundle
# configure config/discourse.conf
cat > config/discourse.conf << EOF
hostname = "$SITE_DOMAIN"
db_name = $DB_NAME
db_username = $DB_USER
db_password = $DB_PASS
redis_port = $REDIS_PORT
smtp_address = "$SMTP_HOST"
smtp_port = 587
smtp_domain = "$SITE_DOMAIN"
smtp_user_name = "$SMTP_USER"
smtp_password = "$SMTP_PASS"
EOF

# configure config/puma.rb
sed -ie 's/unix.*sock/tcp:\/\/127.0.0.1:'"$DISCOURSE_PORT"'/' config/puma.rb
sed -ie 's/discourse\/discourse/'"$SHELL_USER"'\/apps\/'"$DISCOURSE_APP_NAME"'\/discourse/' config/puma.rb

# prep the instance
RAILS_ENV=production bundle exec rake db:migrate
RAILS_ENV=production bundle exec rake assets:precompile

# create initial user
echo "Almost done! Please follow the prompts to create your initial admin user:"
RAILS_ENV=production bundle exec rake admin:create

# start sidekiq
RAILS_ENV=production  RACK_ENV=production bundle exec sidekiq -d -L $HOME/logs/apps/$DISCOURSE_APP_NAME/sidekiq.log -P $HOME/apps/$DISCOURSE_APP_NAME/discourse/tmp/pids/sidekiq.pid
# start puma & discourse
RAILS_ENV=production RACK_ENV=production bundle exec pumactl start

# finished!
echo "Installation complete - Discourse should now be installed and running!"
echo "Now complete the setup at $SITE_DOMAIN/wizard/"
