#! /usr/bin/python3

import argparse
import os
import http.client
import json
from urllib.parse import urlparse

API_HOST = 'my.opalstack.com'
API_BASE_URI = '/api/v0'
USER = os.environ['USER']
GITEA_URL = 'https://dl.gitea.io/gitea/1.8/gitea-1.8-linux-amd64'


class OpalstackAPI():
    """simple wrapper for http.client get and post"""
    def __init__(self, host, base_uri, authtoken):
        self.host = host
        self.headers = {
            'Content-type': 'application/json',
            'Authorization': f'Token {authtoken}'
        }
        self.base_uri = base_uri

    def get(self, endpoint):
        """GETs an API endpoint"""
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request('GET', endpoint, headers=self.headers)
        return json.loads(conn.getresponse().read())

    def post(self, endpoint, payload):
        """POSTs data to an API endpoint"""
        endpoint = self.base_uri + endpoint
        conn = http.client.HTTPSConnection(self.host)
        conn.request('POST', endpoint, payload, headers=self.headers)
        return json.loads(conn.getresponse().read())


def create_file(path, contents, writemode='w', perms=0o600):
    """make a file, perms are passed as octal"""
    with open(path, writemode) as f:
        f.write(contents)
    os.chmod(path, perms)


def download(url, localfile, writemode='wb', perms=0o600):
    """save a remote file, perms are passed as octal"""
    u = urlparse(url)
    if u.scheme == 'http':
        conn = http.client.HTTPConnection(u.netloc)
    else:
        conn = http.client.HTTPSConnection(u.netloc)
    conn.request('GET', u.path)
    r = conn.getresponse()
    with open(localfile, writemode) as f:
        while True:
            data = r.read(4096)
            if data:
                f.write(data)
            else:
                break
    os.chmod(localfile, perms)


def main():
    """run it"""
    # TODO logging
    # grab args from cmd or env
    parser = argparse.ArgumentParser(
        description='Installs Gitea on Opalstack account')
    parser.add_argument('-i', dest='app_uuid', help='UUID of the base app',
                        default=os.environ.get('UUID'))
    parser.add_argument('-n', dest='app_name', help='name of the base app',
                        default=os.environ.get('APPNAME'))
    parser.add_argument('-t', dest='opal_token', help='API auth token',
                        default=os.environ.get('OPAL_TOKEN'))
    args = parser.parse_args()

    # go!
    api = OpalstackAPI(API_HOST, API_BASE_URI, args.opal_token)
    appinfo = api.get(f'/app/read/{args.app_uuid}')
    appdir = f'/home/{appinfo["app_user"]}/apps/{appinfo["name"]}'
    os.mkdir(f'{appdir}/bin', 0o700)
    os.mkdir(f'{appdir}/conf', 0o700)
    os.mkdir(f'{appdir}/repos', 0o700)

    # download gitea
    download(GITEA_URL, f'{appdir}/gitea', perms=0o700)

    # config
    gitea_conf = f'[repository]\nROOT = {appdir}/repos/\n\n[server]\nHTTP_PORT = {appinfo["port"]}\n'
    create_file(f'{appdir}/conf/app.ini', gitea_conf)


if __name__ == '__main__':
    main()
