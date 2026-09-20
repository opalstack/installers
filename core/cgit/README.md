# Opalstack cgit README

## General

cgit is a fast read-only web frontend for git repositories. The cgit homepage
is: https://git.zx2c4.com/cgit/about/

You must add the installed application to a site in order to access it via the
web: https://docs.opalstack.com/user-guide/sites/#adding-sites

## Configuration

Your cgit configuration is located at $APPDIR/app/etc/cgitrc. You can edit that
file to set the site name and description, logo and CSS path, repository
details, and more.

The available settings are documented at: 
https://git.zx2c4.com/cgit/tree/cgitrc.5.txt

## Repositories

The default repository location is $APPDIR/repos with cgit configured to
automatically list repositories in that directory.

To create a new repository, run the following commands replacing "my_new_repo"
with the name of your new repository:

  cd ~/apps/name_of_cgit_app/repos
  git init --bare my_new_repo

You can add an existing repository to cgit like so, replacing REPO_URL with the
URL of the existing repository:

  cd ~/apps/name_of_cgit_app/repos
  git clone --bare REPO_URL

You can clone your repostories over HTTP like so, replacing "repo_name" with
the repository name and "cgit.mydomain.com" with your cgit site URL:

  git clone https://cgit.mydomain.com/cgit.cgi/repo_name

Note that you cannot push changes back over HTTP with cgit. It is a read-only
interface. If you want to push changes back to a cgit-hosted repository, then
you must clone it via SSH like so, replacing "user@host" with your shell
username and Opalstack server name:

  git clone user@host:apps/name_of_cgit_app/repos/repo_name

You can then use that working copy to make and push changes. The pushed changes
will be then visible in cgit.
