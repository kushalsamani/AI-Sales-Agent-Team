"""
scripts/enrich_contacts.py
--------------------------
Scrape contact information (emails and phone numbers) from the websites of
Strong leads in the Leads tab, and write them back as two new columns.

Only processes leads classified as 'Strong'. Skips leads that already have
contact info unless --re-enrich is passed.

For each company:
  1. Fetches the homepage and scans for emails and phone numbers.
  2. Looks for a link to a contact page and fetches that too.
  3. Also tries common contact page paths (/contact, /contact-us, etc.)
  4. Writes all unique emails and phone numbers found, comma-separated.
  5. Writes 'N/A' if nothing is found.

No third-party APIs. Uses only the requests library and Python's built-in
html.parser. Results are saved to the sheet immediately for each company.

Usage:
  python scripts/enrich_contacts.py --company "Your Company Name"
  python scripts/enrich_contacts.py --company "Your Company Name" --re-enrich
"""

import argparse
import os
import re
import sys
from html.parser import HTMLParser

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import sheets
import config


# ─── Contact page paths to try (in order) ─────────────────────────────────────

_CONTACT_PATHS = [
    "/contact",
    "/contact-us",
    "/contactus",
    "/contact-us.html",
    "/contact.html",
    "/about/contact",
    "/about-us",
    "/about",
    "/reach-us",
    "/get-in-touch",
]

# Request timeout in seconds.
_TIMEOUT = 10

# Max characters of HTML to scan per page (avoids huge pages slowing things down).
_MAX_HTML_CHARS = 150_000


# ─── Regex Patterns ───────────────────────────────────────────────────────────

# Email pattern. Applied to raw HTML to catch mailto: links and plain text.
_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)

# Domains to exclude from email results (placeholder and system addresses).
_EMAIL_EXCLUDE_DOMAINS = {
    "example.com", "sentry.io", "wixpress.com", "squarespace.com",
    "domain.com", "email.com", "yourcompany.com", "company.com",
}

# File extensions that are never valid email TLDs.
# Catches image filenames like "banner@2x.jpg" that the regex can mistake for emails.
_INVALID_TLDS = {
    "jpg", "jpeg", "png", "gif", "svg", "webp", "ico", "bmp", "tiff", "tif",
    "pdf", "doc", "docx", "xls", "xlsx", "zip", "css", "js", "json", "xml",
    "mp4", "mp3", "mov", "avi", "woff", "woff2", "ttf", "eot",
}

# Phone number pattern. Matches common North American and international formats:
#   (123) 456-7890  |  123-456-7890  |  +1 800 555 1234  |  1.800.555.1234
_PHONE_RE = re.compile(
    r"(?:\+?1[-.\s]?)?"          # Optional country code +1
    r"(?:\(?\d{3}\)?[-.\s])"     # Area code
    r"\d{3}[-.\s]"               # First 3 digits
    r"\d{4}"                     # Last 4 digits
    r"(?!\d)",                   # Not followed by more digits
)


# ─── HTML Parsers ─────────────────────────────────────────────────────────────

class _LinkFinder(HTMLParser):
    """
    Minimal HTML parser that collects all href values from anchor tags.
    Used to find links to contact pages from the homepage.
    """

    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.links.append(value)


class _TextExtractor(HTMLParser):
    """
    Extracts only the visible text from HTML, skipping script, style, and
    other non-visible tags. Used for phone number extraction so that numbers
    embedded in JavaScript or tracking code are not picked up.
    """

    _SKIP_TAGS = {"script", "style", "noscript", "head", "nav", "footer", "header"}

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth: int = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._parts.append(text)

    def get_text(self) -> str:
        return " ".join(self._parts)


# ─── Scraping Helpers ─────────────────────────────────────────────────────────

def _fetch_html(url: str) -> str:
    """
    Fetch a page and return its raw HTML, capped at _MAX_HTML_CHARS.

    Returns empty string on any error (timeout, SSL, 404, etc.).

    Args:
        url: Full URL to fetch.

    Returns:
        Raw HTML string, or empty string on failure.
    """
    try:
        response = requests.get(
            url,
            timeout=_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; contact-finder/1.0)"},
            allow_redirects=True,
        )
        response.raise_for_status()
        return response.text[:_MAX_HTML_CHARS]
    except Exception:
        return ""


def _extract_emails(html: str) -> list[str]:
    """
    Extract unique email addresses from raw HTML.

    Applies regex to the full HTML (catches mailto: href attributes and
    plain text). Filters out known placeholder and system domains.

    Args:
        html: Raw HTML string.

    Returns:
        Sorted list of unique valid email addresses.
    """
    found = _EMAIL_RE.findall(html)
    result = []
    seen = set()
    for email in found:
        email = email.lower().strip(".")
        domain = email.split("@")[-1]
        tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
        if (
            email not in seen
            and domain not in _EMAIL_EXCLUDE_DOMAINS
            and tld not in _INVALID_TLDS
        ):
            seen.add(email)
            result.append(email)
    return sorted(result)


def _extract_phones(html: str) -> list[str]:
    """
    Extract unique phone numbers from visible page text only.

    Strips scripts, styles, and other non-visible tags before applying the
    regex, so numbers embedded in JavaScript or tracking code are ignored.

    Args:
        html: Raw HTML string.

    Returns:
        List of unique phone number strings as they appear on the page.
    """
    extractor = _TextExtractor()
    extractor.feed(html)
    visible_text = extractor.get_text()

    found = _PHONE_RE.findall(visible_text)
    seen = set()
    result = []
    for phone in found:
        key = re.sub(r"\s+", " ", phone).strip()
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _find_contact_link(html: str, base_url: str) -> str | None:
    """
    Scan a page's links for one that likely leads to a contact page.

    Args:
        html:     Raw HTML of the page to scan.
        base_url: Root URL of the site (used to resolve relative links).

    Returns:
        Full URL of the contact page if found, otherwise None.
    """
    parser = _LinkFinder()
    parser.feed(html)

    contact_keywords = ["contact", "reach", "get-in-touch", "getintouch"]

    for href in parser.links:
        href_lower = href.lower()
        if any(kw in href_lower for kw in contact_keywords):
            # Resolve relative URLs.
            if href.startswith("http"):
                return href
            if href.startswith("/"):
                return base_url.rstrip("/") + href
    return None


def _scrape_contact_info(website: str) -> tuple[str, str]:
    """
    Scrape emails and phone numbers from a company website.

    Strategy:
      1. Fetch homepage, scan for emails/phones.
      2. Look for a contact page link on the homepage and fetch it.
      3. If no contact link found, try common contact page paths.
      4. Merge all results and deduplicate.

    Args:
        website: Root domain or full URL of the company website.

    Returns:
        Tuple of (email_string, phone_string). Each is comma-separated if
        multiple found, or 'N/A' if nothing found.
    """
    base_url = website if website.startswith("http") else f"https://{website}"
    base_url = base_url.rstrip("/")

    all_emails: list[str] = []
    all_phones: list[str] = []

    # Step 1: Fetch and scan homepage.
    homepage_html = _fetch_html(base_url)
    if homepage_html:
        all_emails += _extract_emails(homepage_html)
        all_phones += _extract_phones(homepage_html)

        # Step 2: Look for a contact page link on the homepage.
        contact_url = _find_contact_link(homepage_html, base_url)
        if contact_url and contact_url != base_url:
            contact_html = _fetch_html(contact_url)
            if contact_html:
                all_emails += _extract_emails(contact_html)
                all_phones += _extract_phones(contact_html)

    # Step 3: Try common contact page paths if we have no emails yet.
    if not all_emails:
        for path in _CONTACT_PATHS:
            url = base_url + path
            html = _fetch_html(url)
            if html:
                emails = _extract_emails(html)
                phones = _extract_phones(html)
                if emails or phones:
                    all_emails += emails
                    all_phones += phones
                    break  # Stop at the first path that yields results.

    # Deduplicate while preserving order.
    seen_emails: set[str] = set()
    unique_emails = []
    for e in all_emails:
        if e not in seen_emails:
            seen_emails.add(e)
            unique_emails.append(e)

    seen_phones: set[str] = set()
    unique_phones = []
    for p in all_phones:
        key = re.sub(r"\s+", " ", p).strip()
        if key not in seen_phones:
            seen_phones.add(key)
            unique_phones.append(p)

    email_str = ", ".join(unique_emails) if unique_emails else "N/A"
    phone_str  = ", ".join(unique_phones) if unique_phones else "N/A"

    return email_str, phone_str


# ─── Main ─────────────────────────────────────────────────────────────────────

def enrich_contacts(company_name: str, re_enrich: bool = False) -> None:
    """
    Scrape and write contact info for all Strong leads in the Leads tab.

    Args:
        company_name: Seller company name. Used to find the correct spreadsheet.
        re_enrich:    If True, re-scrape leads that already have contact info.
    """
    spreadsheet_id = sheets.get_or_create_spreadsheet(company_name)

    # Ensure email and phone columns exist.
    email_col, phone_col = sheets.ensure_contact_columns(spreadsheet_id)

    # Read all leads.
    all_leads = sheets.read_leads_for_classification(spreadsheet_id)
    if not all_leads:
        print("[ENRICH] No leads found in sheet.")
        return

    # Filter to Strong leads only.
    strong_leads = [
        lead for lead in all_leads
        if lead.get("classification", "").strip() == "Strong"
    ]

    if not strong_leads:
        print("[ENRICH] No Strong leads found. Run classify_leads.py first.")
        return

    # Filter out already-enriched leads unless --re-enrich.
    to_enrich = [
        lead for lead in strong_leads
        if re_enrich or not lead.get("email", "").strip()
    ]

    print(f"[ENRICH] {len(strong_leads)} Strong leads, {len(to_enrich)} to enrich.")
    if not to_enrich:
        print("[ENRICH] All Strong leads already enriched. Use --re-enrich to redo.")
        return

    found_count = 0

    for i, lead in enumerate(to_enrich, start=1):
        company = lead.get("company_name", "Unknown")
        website = lead.get("website", "").strip()
        row     = lead["_row_number"]

        if not website:
            sheets.write_contact_info(
                spreadsheet_id, row, email_col, phone_col, "N/A", "N/A"
            )
            print(f"[ENRICH] ({i}/{len(to_enrich)}) {company} — no website, skipped.")
            continue

        email, phone = _scrape_contact_info(website)

        sheets.write_contact_info(
            spreadsheet_id, row, email_col, phone_col, email, phone
        )

        status = "found" if email != "N/A" or phone != "N/A" else "N/A"
        if status == "found":
            found_count += 1
        print(f"[ENRICH] ({i}/{len(to_enrich)}) {company} — {status}")

    print(f"\n[ENRICH] Done. Contact info found for {found_count}/{len(to_enrich)} companies.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scrape contact info for Strong leads and write to the Leads tab."
    )
    parser.add_argument(
        "--company",
        required=True,
        help='Seller company name. Example: "Your Company Name"',
    )
    parser.add_argument(
        "--re-enrich",
        action="store_true",
        help="Re-scrape leads that already have contact info (default: skip them).",
    )
    args = parser.parse_args()

    try:
        enrich_contacts(args.company, args.re_enrich)
    except KeyboardInterrupt:
        print("\n[ENRICH] Session interrupted by user. Progress already saved to sheet.")
