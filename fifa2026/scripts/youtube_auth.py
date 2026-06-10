"""YouTube OAuth helpers for upload_youtube.py."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from common import PROJECT_ROOT, SECRETS_DIR

load_dotenv(PROJECT_ROOT / ".env")

# Upload + set thumbnail + schedule publish
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]


def _secrets_path() -> Path:
    raw = os.getenv("YOUTUBE_CLIENT_SECRETS", "secrets/client_secret.json").strip()
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _token_path() -> Path:
    raw = os.getenv("YOUTUBE_TOKEN_PATH", "secrets/youtube_token.json").strip()
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def get_youtube_service():
    """Return an authenticated YouTube Data API service."""
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    token_file = _token_path()
    creds = None

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            secrets = _secrets_path()
            if not secrets.exists():
                raise FileNotFoundError(
                    f"Missing YouTube OAuth client secrets: {secrets}\n"
                    "Create credentials at https://console.cloud.google.com/apis/credentials "
                    "(Desktop app), download JSON, save as secrets/client_secret.json"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
            creds = flow.run_local_server(port=0)

        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds)
