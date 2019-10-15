# HOWTO install and run Discourse on Opalstack

1. Discourse uses a lot of memory, so you'll need to have either a 2GB VPS plan or a double-stack Shared 1GB plan (2 plans on 1 box). Contact support to request the upgrade.
2. Create a new PostgreSQL database and user, and make a note of database's name, username, and password.
3. Open a support ticket to request that the `hstore` and `pg_trgm` extensions be enabled for on your new database for Discourse (be sure to include the DB name in the request). Do not skip this step!
4. Create a new mail user and make a note of the mail user name and password.
5. Create a new shell user if desired for your application. You can use one of your existing shell users if you like. The remainder of this example will use `your_shell_user` as the shell user name.
6. Create a new Proxy Port application owned by the shell user from step 3. The remainder of this procedure will use `discourse_app` as the app name. Make a note of the port assigned to the application. The rest of this procedure assumes that the app is running on port 55551.
7. Create a new Proxy Port application owned by the shell user from step 3. The remainder of this procedure will use `discourse_redis` as the app name. Make a note of the port assigned to the application. The rest of this procedure assumes that the app is running on port 55552.
8. Create a new "Nginx Static Only (Symbolic Link)" application owned by the shell user from step 3. The remainder of this procedure will use `discourse_assets` as the app name. The symbolic link path should be `/home/your_shell_user/apps/discourse_app/discourse/public/assets/` but adjusted for your actual shell user and app names.
9. Create a new "Nginx Static Only (Symbolic Link)" application owned by the shell user from step 3. The remainder of this procedure will use `discourse_images` as the app name. The symbolic link path should be `/home/your_shell_user/apps/discourse_app/discourse/public/images/` but adjusted for your actual shell user and app names.
10. Create a new site with the following routes:
    - discourse_app on /
    - discourse_assets on /assets
    - discourse_images on /images 
11. SSH to your app's shell user account and download the installation script `install_discourse.sh`:

    ```
    wget https://raw.githubusercontent.com/opalstack/installers/master/community/discourse/install_discourse.sh
    ```
12. Edit the variables at the beginning of `install_discourse.sh` to match your shell user, app name, and the other user-/app-specific settings shown there.
13. Run the following commands to begin the installation:

    ```
    chmod 700 install_discourse.sh
    ./install_discourse.sh
    ```
When the installation is complete, Discourse (along with Redis and sidekiq) should be running and accessible on your site URL.

The commands to control everything are below - be sure to change `$APP_NAME` to your Discourse app's name:
```
# start redis
$HOME/apps/$APP_NAME/redis-stable/src/redis-server $HOME/apps/$APP_NAME/redis-stable/redis.conf

# stop redis
kill `cat $HOME/apps/$APP_NAME/tmp/redis.pid`

# start sidekiq
cd  $HOME/apps/$APP_NAME/discourse
RAILS_ENV=production bundle exec sidekiq -d -L $HOME/logs/$APP_NAME/sidekiq.log -P $HOME/apps/$APP_NAME/discourse/tmp/pids/sidekiq.pid

# stop sidekiq
cd  $HOME/apps/$APP_NAME/discourse
RAILS_ENV=production bundle exec sidekiqctl stop tmp/pids/sidekiq.pid

# start discourse
cd  $HOME/apps/$APP_NAME/discourse
RAILS_ENV=production bundle exec pumactl start

# stop discourse
cd  $HOME/apps/$APP_NAME/discourse
RAILS_ENV=production bundle exec pumactl stop
```
