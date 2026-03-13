"""
config.py
---------
Loads all environment variables from .env and exposes them as typed constants.

ALL configurable values live here. No other file should read from os.environ
directly — import from config instead. This makes it trivial to swap any
setting by editing .env alone.
"""

import os
from dotenv import load_dotenv

load_dotenv()


# ─── LLM ─────────────────────────────────────────────────────────────────────
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


# ─── Search APIs ─────────────────────────────────────────────────────────────
SERPER_API_KEY: str        = os.getenv("SERPER_API_KEY", "")
GOOGLE_PLACES_API_KEY: str = os.getenv("GOOGLE_PLACES_API_KEY", "")


# ─── Google Sheets ────────────────────────────────────────────────────────────
GOOGLE_SHEETS_CREDENTIALS_FILE: str = os.getenv(
    "GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json"
)


# ─── Runtime Settings ────────────────────────────────────────────────────────
MAX_LEADS_PER_RUN: int     = int(os.getenv("MAX_LEADS_PER_RUN", "100"))
VALIDATION_BATCH_SIZE: int = int(os.getenv("VALIDATION_BATCH_SIZE", "25"))
SEARCHES_PER_QUERY: int    = int(os.getenv("SEARCHES_PER_QUERY", "10"))


# ─── Internal File Paths ──────────────────────────────────────────────────────
CACHE_DIR: str         = "cache/research"
SPREADSHEETS_FILE: str = "data/spreadsheets.json"
TOKEN_FILE: str        = "token.json"


# ─── Validation Helper ────────────────────────────────────────────────────────
def require_key(env_var: str, value: str, setup_hint: str = "") -> str:
    """
    Validate that a required API key is set. Raises a clear, actionable error
    if the key is missing or still holds the placeholder value.

    Args:
        env_var:    The .env variable name (used in the error message).
        value:      The value loaded from the environment.
        setup_hint: Optional instructions on how to obtain the key.

    Returns:
        The value, if set and non-empty.

    Raises:
        ValueError: With a human-readable message explaining what to do.
    """
    if not value or value.startswith("your_"):
        msg = (
            f"\n[CONFIG ERROR] '{env_var}' is not set in your .env file.\n"
            f"Open .env and replace the placeholder with your actual key."
        )
        if setup_hint:
            msg += f"\n\nHow to get this key:\n  {setup_hint}"
        raise ValueError(msg)
    return value
