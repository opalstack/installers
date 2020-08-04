# HOWTO install and run Discourse on Opalstack

1. **Discourse uses a lot of memory, so you'll need to have either a 2GB VPS plan or a Double Stack 2GB plan (2 plans on 1 box). [Contact support](https://help.opalstack.com/article/9/getting-help) to request the upgrade.**
2. [Create a new PostgreSQL database and user](https://help.opalstack.com/article/51/managing-databases#adding-databases), and make a note of database's name, username, and password.
3. [Contact support](https://help.opalstack.com/article/9/getting-help) to request that the `hstore` and `pg_trgm` extensions be enabled for on your new database for Discourse (be sure to include the DB name in the request).
4. [Create a new mail user](https://help.opalstack.com/article/98/managing-mail-users#adding-mail-users) and make a note of the mail user name and password. You can use one of your existing mail users if you like. The remainder of this example will use `your_mail_user` as the mail user name.
5. [Create a new shell user for your application](https://help.opalstack.com/article/45/managing-shell-users#adding-a-shell-user). You can use one of your existing shell users if you like. The remainder of this example will use `your_shell_user` as the shell user name.
6. [Create a new Proxy Port application](https://help.opalstack.com/article/47/managing-applications#adding-an-application) owned by the shell user from step 5. The remainder of this procedure will use `discourse_app` as the app name. Make a note of the port assigned to the application. The rest of this procedure assumes that the app is running on port 55551.
7. [Create a new Proxy Port application](https://help.opalstack.com/article/47/managing-applications#adding-an-application) owned by the shell user from step 5. The remainder of this procedure will use `discourse_redis` as the app name. Make a note of the port assigned to the application. The rest of this procedure assumes that the app is running on port 55552.
8. [Create a new "Nginx Static Only (Symbolic Link)" application](https://help.opalstack.com/article/47/managing-applications#adding-an-application) owned by the shell user from step 5. The remainder of this procedure will use `discourse_assets` as the app name. The symbolic link path should be `/home/your_shell_user/apps/discourse_app/discourse/public/assets/` but adjusted for your actual shell user and app names.
9. [Create a new "Nginx Static Only (Symbolic Link)" application](https://help.opalstack.com/article/47/managing-applications#adding-an-application) owned by the shell user from step 5. The remainder of this procedure will use `discourse_images` as the app name. The symbolic link path should be `/home/your_shell_user/apps/discourse_app/discourse/public/images/` but adjusted for your actual shell user and app names.
10. [Create a new site](https://help.opalstack.com/article/52/managing-sites#adding-sites) with the following routes:
    - discourse_app on /
    - discourse_assets on /assets
    - discourse_images on /images 
11. [SSH to your app's shell user account](https://help.opalstack.com/article/14/ssh-access) and download the installation script `install_discourse.sh`:

    ```
    wget https://raw.githubusercontent.com/opalstack/installers/master/community/discourse/install_discourse.sh
    ```
12. Edit the variables at the beginning of `install_discourse.sh` to match your shell user, app name, and the other user-/app-specific settings shown there.
13. Run the following commands to begin the installation:

    ```
    chmod 700 install_discourse.sh
    ./install_discourse.sh
    ```
14. Enter your initial Discourse admin user credentials when prompted at the command line.

When the installation is complete, Discourse (along with Redis and sidekiq) should be running and accessible on your site URL and you can complete the setup by using the setup wizard link which appears after you log in to Discourse with your admin credentials.

The commands to control everything are below - be sure to change `$APP_NAME` to your Discourse app's name:
```
# start redis
$HOME/apps/$APP_NAME/redis-stable/src/redis-server $HOME/apps/$APP_NAME/redis-stable/redis.conf

# stop redis
kill `cat $HOME/apps/$APP_NAME/tmp/redis.pid`

# start sidekiq
cd  $HOME/apps/$APP_NAME/discourse
RAILS_ENV=production RACK_ENV=production bundle exec sidekiq -d -L $HOME/logs/$APP_NAME/sidekiq.log -P $HOME/apps/$APP_NAME/discourse/tmp/pids/sidekiq.pid

# stop sidekiq
cd  $HOME/apps/$APP_NAME/discourse
RAILS_ENV=production RACK_ENV=production bundle exec sidekiqctl stop tmp/pids/sidekiq.pid

# start discourse
cd  $HOME/apps/$APP_NAME/discourse
RAILS_ENV=production RACK_ENV=production bundle exec pumactl start

# stop discourse
cd  $HOME/apps/$APP_NAME/discourse
RAILS_ENV=production RACK_ENV=production bundle exec pumactl stop
```
