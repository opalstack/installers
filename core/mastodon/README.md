# Opalstack Mastodon README

## Post-install steps

Please take the following steps before you begin to use your Mastodon instance:

1. Connect your Mastodon application to a site at https://my.opalstack.com/domains/.

2. Configure Mastodon to use the site domain as follows, replacing mydomain.com with your site domain from step 1:

        cd ~/apps/name_of_app
        ./change_domain.py -o localhost -n mydomain.com

3. Edit `~/apps/name_of_app//mastodon/.env.production` to configure the site's email settings:

        SMTP_SERVER=SMTP server
        SMTP_LOGIN=Mailbox name
        SMTP_PASSWORD=Mailbox password
        SMTP_FROM_ADDRESS=Email address

9. Run the following command to restart your app:

        ~/apps/name_of_app/restart

10. Create a Mastodon admin user as follows, replacing `username` and `username@example.com` with your own choices:

        cd ~/apps/name_of_app/mastodon
        source ../setenv
        RAILS_ENV=production bundle exec bin/tootctl accounts create username --email username@example.com --confirmed --role Owner

11. Visit the site you created in step 1 to log in.

12. Follow the steps at https://docs.joinmastodon.org/admin/setup/ to complete the setup.

For further info please see: https://docs.joinmastodon.org/
