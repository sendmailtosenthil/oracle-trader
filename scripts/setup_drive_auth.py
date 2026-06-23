"""One-time Google Drive OAuth setup — mints token.json from credentials.json.

Run locally (needs a browser-reachable machine):

    python scripts/setup_drive_auth.py

1. Download OAuth 2.0 Desktop credentials from Google Cloud Console.
2. Save them as ``credentials.json`` in the project root (or set
   DRIVE_CREDENTIALS_PATH).
3. Run this script, follow the URL, paste the code. It writes ``token.json``
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
    # Console flow works on headless machines; run_local_server() if a browser is available.
    creds = flow.run_console() if hasattr(flow, "run_console") else flow.run_local_server(port=0)

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
