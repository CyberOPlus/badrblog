"""Blogger authentication and API service for the Jobs publisher."""
import json
import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from config import (
    BLOGGER_CLIENT_ID,
    BLOGGER_CLIENT_SECRET,
    CREDENTIALS_FILE,
    SCOPES,
    TOKEN_FILE,
)
from state_io import atomic_write_json

def is_local_publisher(service):
    return getattr(service, "is_local_publisher", False) is True


def _save_credentials(creds):
    atomic_write_json(TOKEN_FILE, json.loads(creds.to_json()))


def get_credentials():
    """
    Handle Blogger OAuth when credentials are available.
    Returns None when authentication is unavailable; no local file counts as a post.
    """
    creds = None
    has_env_oauth = bool(BLOGGER_CLIENT_ID and BLOGGER_CLIENT_SECRET)
    has_blogger_auth = CREDENTIALS_FILE.exists() or has_env_oauth

    if TOKEN_FILE.exists():
        print("Found saved login tokens. Loading...")
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
            if creds and creds.expired and creds.refresh_token:
                print("  Token expired. Refreshing...")
                creds.refresh(Request())
                _save_credentials(creds)
                print("  Token refreshed successfully!")
            elif creds:
                print("  Token is still valid!")
        except Exception as e:
            print(f"  Error loading saved token: {e}")
            creds = None

    if creds and creds.valid:
        return creds

    if not has_blogger_auth or os.getenv("GITHUB_ACTIONS", "").lower() == "true":
        print("Blogger authentication unavailable; job stays queued for retry.")
        return None

    print("\nFirst-time setup: opening browser for Google login...")

    try:
        if CREDENTIALS_FILE.exists():
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        else:
            flow = InstalledAppFlow.from_client_config(
                {
                    "installed": {
                        "client_id": BLOGGER_CLIENT_ID,
                        "client_secret": BLOGGER_CLIENT_SECRET or "",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["http://localhost"],
                    }
                },
                SCOPES,
            )

        creds = flow.run_local_server(
            port=0,
            prompt="consent",
            authorization_prompt_message="",
        )
        _save_credentials(creds)
        print("  Authentication successful!")
        print("  Login tokens saved.")
        return creds

    except Exception as e:
        error_text = str(e)
        print(f"  Authentication failed: {error_text}")
        normalized_error = error_text.lower()
        if (
            "deleted_client" in normalized_error
            or "client_secret is missing" in normalized_error
            or "invalid_client" in normalized_error
        ):
            print("  The configured Blogger OAuth client is no longer usable.")
            print("  Add a fresh client_secret.json file or set new BLOGGER_CLIENT_ID and BLOGGER_CLIENT_SECRET values in .env.")
        return None


def create_blogger_service(creds):
    """
    Create the real Blogger API service for the guarded Jobs publisher.
    """
    if not creds:
        return None

    print("Connecting to Blogger API...")
    service = build("blogger", "v3", credentials=creds)
    print("Blogger API connected!")
    return service
