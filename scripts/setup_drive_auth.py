"""One-time Google Drive OAuth setup — mints token.json from credentials.json.

On the headless VPS, tunnel the callback port from your laptop:

    ssh -L 8765:localhost:8765 -i ~/.ssh/oracle-vps-private.key ubuntu@<vps>
    cd ~/oracle-trader && venv/bin/python scripts/setup_drive_auth.py

then open the printed URL in your laptop's browser. Google redirects to
localhost:8765, which the tunnel carries back to the script.

1. Download OAuth 2.0 Desktop credentials from Google Cloud Console.
2. Save them as ``credentials.json`` in the project root (or set
   DRIVE_CREDENTIALS_PATH).
3. Run this script and approve access in the browser. It writes ``token.json``
   (with a refresh_token) which the app reuses for uploads.
"""
import json
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]
CREDENTIALS_PATH = os.environ.get("DRIVE_CREDENTIALS_PATH", "credentials.json")
TOKEN_PATH = os.environ.get("DRIVE_TOKEN_PATH", "token.json")


def main():
    if not os.path.exists(CREDENTIALS_PATH):
        print(f"Error: {CREDENTIALS_PATH} not found.")
        print("Download OAuth 2.0 Desktop credentials from Google Cloud Console "
              "and save them there.")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    # google-auth-oauthlib >= 1.0 dropped run_console(); a fixed port lets the
    # redirect ride an SSH tunnel. prompt=consent forces a fresh refresh_token.
    port = int(os.environ.get("DRIVE_AUTH_PORT", "8765"))
    creds = flow.run_local_server(
        port=port, open_browser=False, access_type="offline", prompt="consent",
        authorization_prompt_message="Open this URL in your browser:\n{url}\n",
    )

    token = {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    with open(TOKEN_PATH, "w") as f:
        json.dump(token, f)
    print(f"Token stored to {TOKEN_PATH}. Drive uploads are now authorized.")


if __name__ == "__main__":
    main()
