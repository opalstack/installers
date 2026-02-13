#!/usr/bin/env python3
"""
Opalstack Installer for MCP VibeSHEL

This installer sets up the MCP VibeSHEL PHP server, which enables AI assistants
to securely interact with files on your Opalstack account via the Model Context
Protocol (MCP).

Requirements:
- Opalstack account with an Nginx/PHP-FPM (NPF) application
- PHP 7.4 or higher

What this installer does:
1. Downloads index.php from GitHub to the app directory
2. Creates ~/.mcp_vibeshell.ini with a secure 256-bit token
3. Creates a README with usage and security information
4. Sends a notice with your Bearer token
"""

import argparse
import sys
import logging
import os
import http.client
import json
import secrets
import string
import textwrap
from urllib.parse import urlparse

# Opalstack API configuration
API_HOST = os.environ.get('API_URL', 'https://my.opalstack.com').strip('https://').strip('http://')
API_BASE_URI = '/api/v1'

# MCP VibeSHEL source - update this to your actual GitHub raw URL
VIBESHELL_PHP_URL = 'https://raw.githubusercontent.com/d3cline/VibeShell/refs/heads/main/index.php'


class OpalstackAPITool():
    """Simple wrapper for http.client get and post"""

    def __init__(self, host, base_uri, authtoken, user, password):
        self.host = host
        self.base_uri = base_uri

        # If there is no auth token, then try to log in with provided credentials
        if not authtoken:
            endpoint = self.base_uri + '/login/'
            payload = json.dumps({
                'username': user,
                'password': password
            })
            conn = http.client.HTTPSConnection(self.host)
            conn.request('POST', endpoint, payload,
                         headers={'Content-type': 'application/json'})
            result = json.loads(conn.getresponse().read())
            if not result.get('token'):
                logging.warning('Invalid username or password and no auth token provided, exiting.')
                sys.exit(1)
            else:
                authtoken = result['token']

        self.headers = {
            'Content-type': 'application/json',
            'Authorization': f'Token {authtoken}'
        }

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
        connread = conn.getresponse().read()
        return json.loads(connread)


def create_file(path, contents, writemode='w', perms=0o600):
    """Make a file, perms are passed as octal"""
    with open(path, writemode) as f:
        f.write(contents)
    os.chmod(path, perms)
    logging.info(f'Created file {path} with permissions {oct(perms)}')


def download(url, localfile, perms=0o644):
    """Download a remote file, perms are passed as octal"""
    logging.info(f'Downloading {url} as {localfile}')
    u = urlparse(url)

    if u.scheme == 'http':
        conn = http.client.HTTPConnection(u.netloc)
    else:
        conn = http.client.HTTPSConnection(u.netloc)

    conn.request('GET', u.path)
    r = conn.getresponse()

    if r.status != 200:
        logging.error(f'Failed to download {url}: HTTP {r.status}')
        sys.exit(1)

    with open(localfile, 'wb') as f:
        while True:
            data = r.read(4096)
            if data:
                f.write(data)
            else:
                break

    os.chmod(localfile, perms)
    logging.info(f'Downloaded {url} as {localfile} with permissions {oct(perms)}')


def gen_token(length=64):
    """
    Generate a cryptographically secure token.
    64 hex characters = 256 bits of entropy (maximum practical security)
    """
    return secrets.token_hex(length // 2)


def create_notice(api, app_uuid, token):
    """Send a notice to the user with their Bearer token"""
    notice_content = textwrap.dedent(f'''
        MCP VibeSHEL has been installed successfully!

        Your Bearer Token (save this securely):
        {token}

        Add this to your MCP client configuration:
        {{
          "mcpServers": {{
            "vibeshell": {{
              "type": "http",
              "url": "https://YOUR-DOMAIN.com/",
              "headers": {{
                "Authorization": "Bearer {token}"
              }}
            }}
          }}
        }}

        IMPORTANT: This token provides full file access to your home directory.
        See the README in your app directory for security information.
    ''').strip()

    payload = json.dumps([{
        'type': 'D',
        'content': notice_content
    }])

    result = api.post('/notice/create/', payload)
    logging.info('Sent notice with Bearer token')
    return result


def create_readme(appdir, appname):
    """Create a comprehensive README for the app"""
    readme_content = textwrap.dedent(f'''
        # MCP VibeSHEL - {appname}

        This application provides an MCP (Model Context Protocol) server that enables
        AI assistants to securely interact with files in your Opalstack home directory.

        ## What is MCP?

        The Model Context Protocol is an open standard that allows AI assistants (like
        Claude, Cursor, VS Code Copilot, etc.) to interact with external tools and data
        sources. This server implements MCP over HTTP with JSON-RPC 2.0.

        ## Security

        ### Permissions & Scope

        This MCP server has the following capabilities:

        | Tool | Permission | Description |
        |------|------------|-------------|
        | fs_info | READ | View directory structure info |
        | fs_list | READ | List files and directories |
        | fs_read | READ | Read file contents |
        | fs_write | WRITE | Create/modify files |
        | fs_tail | READ | Read end of files (logs) |
        | fs_search | READ | Search file contents |
        | fs_move | WRITE | Move/rename files |
        | fs_delete | DELETE | Remove files/directories |

        ### Security Boundaries

        - **Home Directory Jail**: All operations are restricted to your home directory
        - **Symlink Protection**: Symlinks are resolved to prevent escape attacks
        - **Protected Files**: Cannot modify ~/.bashrc, ~/.ssh/, ~/.gnupg/, etc.
        - **Rate Limiting**: 120 requests per minute per IP address
        - **Request Size Limit**: 2MB maximum request body

        ### !!! DANGERS & RISKS

        **This server provides powerful file access. Understand the risks:**

        1. **Full Home Access**: By default, AI can read/write ANY file in your home
        2. **Code Execution Risk**: AI could write malicious scripts to your apps
        3. **Data Exposure**: AI can read configuration files, databases, logs
        4. **Credential Theft**: If you store passwords in files, AI can read them
        5. **Token Compromise**: If your Bearer token leaks, anyone can access your files

        ### Mitigation Strategies

        1. **Restrict base_dir**: Edit ~/.mcp_vibeshell.ini to limit scope:
           ```ini
           [vibeshell]
           base_dir = "~/apps/{appname}"  # Only allow access to this app
           ```

        2. **Rotate Token**: If you suspect compromise, generate a new token:
           ```bash
           # Generate new token
           NEW_TOKEN=$(openssl rand -hex 32)
           # Update config
           sed -i "s/^token = .*/token = \\"$NEW_TOKEN\\"/" ~/.mcp_vibeshell.ini
           ```

        3. **Monitor Access**: Check your app's access logs regularly

        ## Configuration

        The configuration file is located at: `~/.mcp_vibeshell.ini`

        ```ini
        [vibeshell]
        ; Your Bearer token (keep this secret!)
        token = "your-token-here"

        ; Base directory for file operations
        ; "~" = full home access (default)
        ; "~/apps" = restrict to apps folder
        ; "~/apps/myapp" = restrict to single app
        base_dir = "~"
        ```

        ## How to Disable

        ### Option 1: Remove the token (disables auth, blocks all access)
        ```bash
        sed -i 's/^token = .*/token = ""/' ~/.mcp_vibeshell.ini
        ```

        ### Option 2: Delete the config file entirely
        ```bash
        rm ~/.mcp_vibeshell.ini
        ```

        ### Option 3: Delete the app via Opalstack dashboard
        1. Go to https://my.opalstack.com/
        2. Navigate to Applications
        3. Delete this application

        ## Connecting to a Domain

        To use this MCP server, you need to:

        1. **Create a Domain** in Opalstack dashboard (or use existing)
        2. **Create a Site** that maps your domain to this application
        3. **Enable HTTPS** (strongly recommended - use Let's Encrypt)

        Your MCP endpoint will be: `https://your-domain.com/`

        ## Testing

        Test with curl:

        ```bash
        # Replace with your actual domain and token
        curl -X POST https://your-domain.com/ \\
          -H "Authorization: Bearer YOUR_TOKEN" \\
          -H "Content-Type: application/json" \\
          -d '{{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{{}}}}'
        ```

        ## Files

        - `{appdir}/index.php` - The MCP server (do not modify)
        - `~/.mcp_vibeshell.ini` - Configuration file
        - `~/logs/apps/{appname}/` - Access and error logs

        ## Support

        - Opalstack Documentation: https://docs.opalstack.com/
        - MCP Specification: https://modelcontextprotocol.io/
        - Community Forum: https://community.opalstack.com/

        ## License

        MIT License
    ''').strip()

    create_file(f'{appdir}/README', readme_content, perms=0o644)


def main():
    """Run the installer"""
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description='Installs MCP VibeSHEL on Opalstack account')
    parser.add_argument('-i', dest='app_uuid', help='UUID of the base app',
                        default=os.environ.get('UUID'))
    parser.add_argument('-n', dest='app_name', help='Name of the base app',
                        default=os.environ.get('APPNAME'))
    parser.add_argument('-t', dest='opal_token', help='API auth token',
                        default=os.environ.get('OPAL_TOKEN'))
    parser.add_argument('-u', dest='opal_user', help='Opalstack account name',
                        default=os.environ.get('OPAL_USER'))
    parser.add_argument('-p', dest='opal_password', help='Opalstack account password',
                        default=os.environ.get('OPAL_PASS'))
    args = parser.parse_args()

    # Initialize logging
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s: %(message)s'
    )

    logging.info(f'Started installation of MCP VibeSHEL app {args.app_name}')

    # Initialize API connection
    api = OpalstackAPITool(
        API_HOST, API_BASE_URI,
        args.opal_token, args.opal_user, args.opal_password
    )

    # Get app info
    appinfo = api.get(f'/app/read/{args.app_uuid}')
    osuser_name = appinfo['osuser_name']
    appdir = f'/home/{osuser_name}/apps/{appinfo["name"]}'
    homedir = f'/home/{osuser_name}'

    logging.info(f'Installing to {appdir}')

    # Step 1: Download index.php from GitHub
    logging.info('Downloading MCP VibeSHEL PHP server...')
    download(VIBESHELL_PHP_URL, f'{appdir}/index.php', perms=0o644)

    # Step 2: Generate a secure 256-bit token
    token = gen_token(64)  # 64 hex chars = 256 bits
    logging.info('Generated secure 256-bit Bearer token')

    # Step 3: Create the config file in home directory
    config_content = textwrap.dedent(f'''
        ; MCP VibeSHEL Configuration
        ; Created by Opalstack installer
        ;
        ; SECURITY: This file contains your API token. Keep it secret!
        ; File permissions should be 600 (owner read/write only).

        [vibeshell]

        ; Bearer token for API authentication (256-bit)
        ; To regenerate: openssl rand -hex 32
        token = "{token}"

        ; Base directory for file operations
        ; "~" = full home directory access
        ; "~/apps" = restrict to apps folder only
        ; "~/apps/{appinfo["name"]}" = restrict to this app only
        base_dir = "~"
    ''').strip() + '\n'

    config_path = f'{homedir}/.mcp_vibeshell.ini'
    create_file(config_path, config_content, perms=0o600)
    logging.info(f'Created config file at {config_path}')

    # Step 4: Create the README
    create_readme(appdir, args.app_name)
    logging.info('Created README')

    # Step 5: Send notice with Bearer token
    create_notice(api, args.app_uuid, token)

    # Mark app as installed
    payload = json.dumps([{'id': args.app_uuid}])
    api.post('/app/installed/', payload)

    logging.info(f'Completed installation of MCP VibeSHEL app {args.app_name}')
    logging.info(f'Your Bearer token has been sent to your Opalstack notices.')
    logging.info(f'IMPORTANT: Save your token securely - it will not be shown again!')


if __name__ == '__main__':
    main()
