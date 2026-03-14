"""
tools/sheets.py
---------------
Google Sheets API wrapper for reading existing leads and writing new ones.

Authentication uses OAuth2 (credentials.json from Google Cloud Console).
  - First run: opens a browser window for you to authorise access.
  - Token saved to token.json — all future runs are automatic (no browser).

One spreadsheet is created per company. The spreadsheet ID is stored in
data/spreadsheets.json so it is reused on subsequent runs.

Google Sheets setup (one-time):
  1. Go to https://console.cloud.google.com
  2. Create a project → enable "Google Sheets API" and "Google Drive API"
  3. APIs & Services → Credentials → Create OAuth 2.0 Client ID (Desktop app)
  4. Download JSON → save as 'credentials.json' in the project root
  5. Run the program — a browser window opens for one-time authorisation
"""

import json
import os
from datetime import date
from urllib.parse import urlparse

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

# Scopes required: read/write sheets + create new spreadsheets via Drive.
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Column order in the Leads sheet.
_HEADERS = ["company_name", "website", "country", "source", "search_query", "date_added"]

# Name and column order for the Rejected Companies tab.
_REJECTED_SHEET_NAME = "Rejected Companies"
_REJECTED_HEADERS = ["company_name", "website", "country", "source", "search_query", "date_added", "rejection_reason"]


# ─── Authentication ────────────────────────────────────────────────────────────

def _get_service():
    """
    Authenticate with Google and return a Sheets API service object.

    Handles token loading, refresh, and first-time browser authorisation
    transparently. Saves token to token.json after first authorisation.

    Returns:
        Authenticated Google Sheets API service object.

    Raises:
        FileNotFoundError: If credentials.json is missing, with setup instructions.
    """
    creds = None

    # Load saved token if it exists.
    if os.path.exists(config.TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(config.TOKEN_FILE, _SCOPES)

    # Refresh or re-authorise if needed.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(config.GOOGLE_SHEETS_CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"\n[AUTH ERROR] '{config.GOOGLE_SHEETS_CREDENTIALS_FILE}' not found.\n\n"
                    "One-time Google Sheets setup:\n"
                    "  1. Go to https://console.cloud.google.com\n"
                    "  2. Create a project → enable 'Google Sheets API' and 'Google Drive API'\n"
                    "  3. APIs & Services → Credentials → Create OAuth 2.0 Client ID\n"
                    "     (Application type: Desktop app)\n"
                    "  4. Download the JSON → save as 'credentials.json' in the project root\n"
                    "  5. Run the program again — a browser window will open for authorisation\n"
                    "  6. After authorising once, token.json is saved and no browser is needed again."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GOOGLE_SHEETS_CREDENTIALS_FILE, _SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(config.TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("sheets", "v4", credentials=creds)


# ─── Spreadsheet Management ───────────────────────────────────────────────────

def get_or_create_spreadsheet(company_name: str) -> str:
    """
    Get the spreadsheet ID for a company, creating a new one if needed.

    The mapping of company → spreadsheet ID is persisted in
    data/spreadsheets.json so the same sheet is reused across runs.

    Args:
        company_name: Full company name (used as spreadsheet title).

    Returns:
        Google Sheets spreadsheet ID string.
    """
    slug = _slugify(company_name)
    mapping = _load_mapping()

    if slug in mapping:
        return mapping[slug]

    # No sheet yet — create one.
    service = _get_service()
    spreadsheet = service.spreadsheets().create(body={
        "properties": {"title": f"{company_name} — Leads"},
        "sheets": [{
            "properties": {
                "title": "Leads",
                "gridProperties": {"frozenRowCount": 1},  # Freeze header row.
            }
        }],
    }).execute()

    spreadsheet_id = spreadsheet["spreadsheetId"]

    # Write header row.
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range="Leads!A1",
        valueInputOption="RAW",
        body={"values": [_HEADERS]},
    ).execute()

    # Persist the ID so future runs reuse this sheet.
    mapping[slug] = spreadsheet_id
    _save_mapping(mapping)

    print(f"[SHEETS] Created new spreadsheet: '{company_name} — Leads'")
    print(f"[SHEETS] View it at: https://docs.google.com/spreadsheets/d/{spreadsheet_id}")

    return spreadsheet_id


# ─── Reading Existing Leads ───────────────────────────────────────────────────

def get_existing_domains(spreadsheet_id: str) -> set[str]:
    """
    Load all existing lead websites from the sheet as a set of normalised domains.

    Used exclusively for deduplication — pure Python set lookup, zero LLM tokens.
    Reads only column B (website) to minimise data transfer.

    Args:
        spreadsheet_id: The Google Sheets spreadsheet ID.

    Returns:
        Set of normalised domain strings (e.g. {'abcdist.com', 'xyzsup.co.uk'}).
        Returns empty set if the sheet has no data or on read error.
    """
    service = _get_service()

    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="Leads!B2:B",  # Column B = website; skip row 1 (header).
        ).execute()
    except HttpError as e:
        print(f"[WARN] Could not read existing leads: {e}")
        return set()

    rows = result.get("values", [])
    domains = set()
    for row in rows:
        if row:
            domain = normalize_domain(row[0])
            if domain:
                domains.add(domain)

    return domains


# ─── Writing New Leads ────────────────────────────────────────────────────────

def append_leads(spreadsheet_id: str, leads: list[dict]) -> int:
    """
    Append validated lead rows to the Google Sheet.

    Each lead becomes one row: company_name | website | country | source | search_query | date_added.
    Uses INSERT_ROWS to append below existing data without overwriting.

    Args:
        spreadsheet_id: The Google Sheets spreadsheet ID.
        leads: List of lead dicts. Expected keys: company_name, website, country,
               source, search_query. date_added is added automatically (today's date).

    Returns:
        Number of rows successfully written. Returns 0 on error.
    """
    if not leads:
        return 0

    today = date.today().isoformat()
    rows = [
        [
            lead.get("company_name", ""),
            lead.get("website", ""),
            lead.get("country", ""),
            lead.get("source", ""),
            lead.get("search_query", ""),
            today,
        ]
        for lead in leads
    ]

    service = _get_service()
    try:
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range="Leads!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
        return len(rows)
    except HttpError as e:
        print(f"[ERROR] Failed to write leads to sheet: {e}")
        return 0


def append_rejected_leads(spreadsheet_id: str, rejected: list[dict]) -> int:
    """
    Append rejected candidate rows to the 'Rejected Companies' tab.

    Creates the tab with a header row automatically if it doesn't exist yet.
    Same column format as the Leads sheet so both tabs are easy to compare.

    Args:
        spreadsheet_id: The Google Sheets spreadsheet ID.
        rejected: List of candidate dicts that failed ICP validation.

    Returns:
        Number of rows written. Returns 0 if list is empty or on error.
    """
    if not rejected:
        return 0

    service = _get_service()
    _ensure_sheet_tab(service, spreadsheet_id, _REJECTED_SHEET_NAME, _REJECTED_HEADERS)

    today = date.today().isoformat()
    rows = [
        [
            r.get("company_name", ""),
            r.get("website", ""),
            r.get("country", ""),
            r.get("source", ""),
            r.get("search_query", ""),
            today,
            r.get("rejection_reason", ""),
        ]
        for r in rejected
    ]

    try:
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{_REJECTED_SHEET_NAME}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
        return len(rows)
    except HttpError as e:
        print(f"[ERROR] Failed to write rejected leads to sheet: {e}")
        return 0


def _ensure_sheet_tab(service, spreadsheet_id: str, tab_name: str, headers: list[str]) -> None:
    """
    Create a sheet tab with a frozen header row if it doesn't already exist.

    Args:
        service:        Authenticated Google Sheets API service object.
        spreadsheet_id: The spreadsheet to check/update.
        tab_name:       Name of the tab to create if missing.
        headers:        Column headers to write on row 1 of the new tab.
    """
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing_tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]

    if tab_name in existing_tabs:
        return

    # Create the tab.
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {
            "title": tab_name,
            "gridProperties": {"frozenRowCount": 1},
        }}}]},
    ).execute()

    # Write header row.
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_name}!A1",
        valueInputOption="RAW",
        body={"values": [headers]},
    ).execute()


# ─── URL Utilities ────────────────────────────────────────────────────────────

def normalize_domain(url: str) -> str | None:
    """
    Reduce a URL to its bare root domain for deduplication comparison.

    Examples:
      'https://www.abcdist.com/products/ptfe' → 'abcdist.com'
      'http://xyzsupply.co.uk'                → 'xyzsupply.co.uk'
      'abcdist.com'                           → 'abcdist.com'

    Args:
        url: Raw URL string (with or without scheme).

    Returns:
        Lowercase root domain string, or None if the URL is empty/unparseable.
    """
    if not url:
        return None
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        domain = urlparse(url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain or None
    except Exception:
        return None


# ─── Internal Helpers ─────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    """Convert a company name to a safe, consistent key for the mapping file."""
    import re
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)   # Remove special chars except hyphens.
    slug = re.sub(r"[\s_]+", "-", slug)    # Replace spaces/underscores with hyphens.
    slug = re.sub(r"-+", "-", slug)        # Collapse multiple hyphens.
    return slug.strip("-")


def _load_mapping() -> dict:
    """Load company → spreadsheet ID mapping from data/spreadsheets.json."""
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(config.SPREADSHEETS_FILE):
        return {}
    with open(config.SPREADSHEETS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_mapping(mapping: dict) -> None:
    """Persist company → spreadsheet ID mapping to data/spreadsheets.json."""
    os.makedirs("data", exist_ok=True)
    with open(config.SPREADSHEETS_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)
