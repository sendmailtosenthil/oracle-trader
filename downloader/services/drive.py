"""Google Drive upload via OAuth user credentials.

Ported from quant-downloader's ``GoogleDriveAdapter``. Uses an OAuth client
(``credentials.json``) plus a previously-minted user token (``token.json`` with
a refresh token) so uploads land in the user's personal Drive. Mint the token
once with ``scripts/setup_drive_auth.py``.

Paths are configurable via env so the secrets can be mounted into the container:
    DRIVE_CREDENTIALS_PATH (default: ./credentials.json)
    DRIVE_TOKEN_PATH       (default: ./token.json)
    DRIVE_ROOT_FOLDER      (default: QuantData)
"""
import json
import os

DEFAULT_CREDENTIALS_PATH = os.environ.get("DRIVE_CREDENTIALS_PATH", "credentials.json")
DEFAULT_TOKEN_PATH = os.environ.get("DRIVE_TOKEN_PATH", "token.json")
DEFAULT_ROOT_FOLDER = os.environ.get("DRIVE_ROOT_FOLDER", "QuantData")

SCOPES = ["https://www.googleapis.com/auth/drive"]

_FOLDER_MIME = "application/vnd.google-apps.folder"


class GoogleDriveUploader:
    """Find-or-create folders and create-or-overwrite files on Google Drive."""

    def __init__(self, credentials_path=None, token_path=None):
        self.credentials_path = credentials_path or DEFAULT_CREDENTIALS_PATH
        self.token_path = token_path or DEFAULT_TOKEN_PATH
        self._drive = self._build_service()

    def _build_service(self):
        # Imported lazily so the rest of the app works without google libs installed.
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        if not os.path.exists(self.credentials_path) or not os.path.exists(self.token_path):
            raise FileNotFoundError(
                f"Missing {self.credentials_path} or {self.token_path}. "
                "Run scripts/setup_drive_auth.py to authorize Google Drive."
            )

        with open(self.credentials_path) as f:
            client = json.load(f)
        client = client.get("installed") or client.get("web") or {}
        with open(self.token_path) as f:
            token = json.load(f)

        creds = Credentials(
            token=token.get("access_token") or token.get("token"),
            refresh_token=token.get("refresh_token"),
            token_uri=client.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=client.get("client_id"),
            client_secret=client.get("client_secret"),
            scopes=SCOPES,
        )
        if creds.refresh_token and (not creds.valid or creds.expired):
            creds.refresh(Request())
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def find_file(self, name, parent_id, mime_type=None):
        safe = name.replace("'", "\\'")
        query = f"name = '{safe}' and '{parent_id}' in parents and trashed = false"
        if mime_type:
            query += f" and mimeType = '{mime_type}'"
        res = self._drive.files().list(
            q=query, fields="files(id, name, mimeType)", spaces="drive"
        ).execute()
        files = res.get("files", [])
        return files[0] if files else None

    def list_files(self, parent_id, name_contains=None):
        """List non-trashed files in a folder, newest first (by createdTime)."""
        query = f"'{parent_id}' in parents and trashed = false"
        if name_contains:
            safe = name_contains.replace("'", "\\'")
            query += f" and name contains '{safe}'"
        res = self._drive.files().list(
            q=query, fields="files(id, name, createdTime)", spaces="drive",
            orderBy="createdTime desc",
        ).execute()
        return res.get("files", [])

    def delete_file(self, file_id):
        self._drive.files().delete(fileId=file_id).execute()

    def ensure_folder(self, name, parent_id="root"):
        existing = self.find_file(name, parent_id, _FOLDER_MIME)
        if existing:
            return existing["id"]
        metadata = {"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]}
        created = self._drive.files().create(body=metadata, fields="id").execute()
        return created["id"]

    def upload_file(self, file_path, file_name, parent_id, mime_type="application/zip"):
        """Create the file, or overwrite it if one of the same name exists."""
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        existing = self.find_file(file_name, parent_id)
        if existing:
            res = self._drive.files().update(
                fileId=existing["id"], media_body=media, fields="id"
            ).execute()
        else:
            metadata = {"name": file_name, "parents": [parent_id]}
            res = self._drive.files().create(
                body=metadata, media_body=media, fields="id"
            ).execute()
        return res["id"]
