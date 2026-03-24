"""
email_automation/send_emails.py
--------------------------------
Email outreach engine. Saves personalised HTML drafts to Zoho via IMAP for
Strong leads that have not yet been contacted. Follow-ups are checked and
drafted automatically on every run.

Company-specific content lives in two separate gitignored files:
  - sender.py   : sender identity, URLs, subject/partner logic, follow-up rules
  - templates.py : HTML email bodies and Gemini opener prompt

Pipeline:
  1. Read Strong leads with emails from the Leads sheet.
  2. Load Approached sheet -- skip any company already contacted.
  3. For each new company, generate a 2-sentence opener using Gemini.
  4. Compose full HTML email from templates.py.
  5. Check Approached sheet for overdue follow-ups (based on FOLLOWUP_DAYS).
  6. Save all drafts to Zoho Drafts folder via IMAP (terminal exits immediately).
  7. Log drafted companies to the Approached sheet.

Usage:
  python email_automation/send_emails.py --company "Your Company Name"
"""

import argparse
import imaplib
import time
import sys
import os
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.utils import formatdate, make_msgid

# Allow imports from project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import sheets, llm
from tools.sheets import normalize_domain
import config
import sender
import templates

# --- Constants ----------------------------------------------------------------

# Path to the sender logo image.
_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "logo.jpg")


# --- Opener Generation --------------------------------------------------------

def _generate_opener(company_name: str, classification_reason: str) -> str:
    """
    Call Gemini to generate a 2-sentence personalised opener.

    Args:
        company_name:          Name of the target company.
        classification_reason: Why this company was classified as Strong.

    Returns:
        Two-sentence opener string. Falls back to a generic opener on error.
    """
    prompt = templates.OPENER_PROMPT.format(
        company_name=company_name,
        classification_reason=classification_reason,
    )
    try:
        result = llm.generate_text(prompt, temperature=0.4)
        return result.strip()
    except Exception as e:
        print(f"  [WARN] Gemini opener failed for {company_name}: {e}. Using fallback.")
        return (
            "I came across your website and noticed your work in the industrial "
            "process space. Given the overlap, I thought it made sense to introduce our company."
        )


# --- Logo Loading -------------------------------------------------------------

def _load_logo() -> bytes:
    """
    Load the sender logo image from disk.

    Returns:
        Raw bytes of the logo image.

    Raises:
        FileNotFoundError: If the logo file is missing.
    """
    if not os.path.exists(_LOGO_PATH):
        raise FileNotFoundError(
            f"[EMAIL] Logo not found at: {_LOGO_PATH}\n"
            f"Place your logo as email_automation/assets/logo.jpg"
        )
    with open(_LOGO_PATH, "rb") as f:
        return f.read()


# --- Build MIME Message -------------------------------------------------------

def _build_message(
    to_addresses: list[str],
    subject: str,
    html_body: str,
    logo_bytes: bytes,
    logo_cid: str,
) -> MIMEMultipart:
    """
    Build a MIME multipart/related message with an inline logo image.

    Args:
        to_addresses: List of recipient email addresses.
        subject:      Email subject line.
        html_body:    HTML body string (already formatted).
        logo_bytes:   Raw bytes of the logo image.
        logo_cid:     Content-ID for the inline image (without angle brackets).

    Returns:
        Fully constructed MIMEMultipart message.
    """
    msg = MIMEMultipart("related")
    msg["From"]       = f"{sender.SENDER_DISPLAY} <{config.ZOHO_EMAIL}>"
    msg["To"]         = ", ".join(to_addresses)
    msg["Subject"]    = subject
    msg["Date"]       = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)

    logo = MIMEImage(logo_bytes, "jpeg")
    logo.add_header("Content-ID", f"<{logo_cid}>")
    logo.add_header("Content-Disposition", "inline", filename="logo.jpg")
    msg.attach(logo)

    return msg


# --- IMAP Draft Saving --------------------------------------------------------

def _save_drafts(messages: list[MIMEMultipart]) -> int:
    """
    Connect to the IMAP server and append all messages to the Drafts folder.

    Args:
        messages: List of fully built MIME messages to save as drafts.

    Returns:
        Number of drafts saved successfully.
    """
    saved = 0
    try:
        with imaplib.IMAP4_SSL(sender.IMAP_HOST, sender.IMAP_PORT) as imap:
            imap.login(config.ZOHO_EMAIL, config.ZOHO_APP_PASSWORD)
            drafts_folder = _find_drafts_folder(imap)
            print(f"[IMAP] Saving to folder: {drafts_folder}")

            for msg in messages:
                try:
                    imap.append(
                        drafts_folder,
                        "\\Draft",
                        imaplib.Time2Internaldate(time.time()),
                        msg.as_bytes(),
                    )
                    saved += 1
                except Exception as e:
                    to = msg.get("To", "unknown")
                    print(f"  [ERROR] Failed to save draft for {to}: {e}")

    except imaplib.IMAP4.error as e:
        raise RuntimeError(
            f"[IMAP] Login failed: {e}\n"
            f"Check ZOHO_EMAIL and ZOHO_APP_PASSWORD in .env"
        )

    return saved


def _find_drafts_folder(imap: imaplib.IMAP4_SSL) -> str:
    """
    Find the Drafts folder name from the IMAP folder list.

    Args:
        imap: Authenticated IMAP4_SSL connection.

    Returns:
        Folder name string (quoted if it contains spaces).
    """
    _, folders = imap.list()
    if not folders:
        return "Drafts"
    for folder_info in folders:
        if not folder_info:
            continue
        if isinstance(folder_info, bytes):
            raw = folder_info.decode("utf-8", errors="replace")
        elif isinstance(folder_info, str):
            raw = folder_info
        else:
            raw = repr(folder_info)
        lower = raw.lower()  # type: ignore[assignment]
        tokens = lower.split()  # type: ignore[assignment]
        if "\\drafts" in lower or '"drafts"' in lower or (tokens and "drafts" in tokens[-1]):  # type: ignore[operator]
            parts = raw.split('"')  # type: ignore[assignment]
            if len(parts) >= 2:
                folder_name: str = str(parts[-2])
                if " " in folder_name:
                    return f'"{folder_name}"'
                return folder_name
    return "Drafts"


# --- Business Day Helper ------------------------------------------------------

def _add_business_days(start: date, days: int) -> date:
    """
    Add N business days to a date, skipping weekends.

    Args:
        start: Starting date.
        days:  Number of business days to add.

    Returns:
        Resulting date after adding business days.
    """
    current = start
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


# --- Main Pipeline ------------------------------------------------------------

def run(company_name: str) -> None:
    """
    Run the email draft pipeline.

    Drafts up to EMAILS_PER_DAY new emails and up to EMAILS_PER_DAY follow-ups
    (for companies overdue based on sender.FOLLOWUP_DAYS). Saves all drafts to
    Zoho via IMAP and logs each to the Approached sheet. Terminal exits
    immediately after saving.

    Args:
        company_name: Seller company name (must match spreadsheets.json).
    """
    config.require_key("ZOHO_EMAIL", config.ZOHO_EMAIL)
    config.require_key("ZOHO_APP_PASSWORD", config.ZOHO_APP_PASSWORD)

    logo_bytes = _load_logo()
    print(f"[EMAIL] Logo loaded ({len(logo_bytes)} bytes).")

    spreadsheet_id = sheets.get_or_create_spreadsheet(company_name)

    # -- Load Approached sheet -------------------------------------------------
    print("[APPROACHED] Loading already-contacted companies...")
    approached = sheets.get_approached_companies(spreadsheet_id)
    approached_domains: set[str] = {
        normalize_domain(r.get("website", "")) or ""
        for r in approached
    }
    print(f"[APPROACHED] {len(approached_domains)} companies already contacted.")

    # -- Load Strong leads with emails -----------------------------------------
    print("[LEADS] Reading Strong leads with emails...")
    all_leads = sheets.read_leads_for_classification(spreadsheet_id)
    strong_with_email = [
        lead for lead in all_leads
        if lead.get("classification", "").strip() == "Strong"
        and lead.get("email", "").strip()
        and lead.get("email", "").strip().upper() != "N/A"
    ]
    print(f"[LEADS] {len(strong_with_email)} Strong leads with emails found.")

    new_leads = [
        lead for lead in strong_with_email
        if (normalize_domain(lead.get("website", "")) or "") not in approached_domains
    ]
    print(f"[LEADS] {len(new_leads)} not yet contacted.")

    # -- Check for due follow-ups ----------------------------------------------
    followups: list[dict] = []
    today = date.today()
    for row in approached:
        if row.get("status", "") != "Approached":
            continue
        if not row.get("sent_on"):
            continue
        try:
            sent_date = date.fromisoformat(row["sent_on"])
        except ValueError:
            continue
        if today >= _add_business_days(sent_date, sender.FOLLOWUP_DAYS):
            followups.append(row)
    print(f"[FOLLOWUP] {len(followups)} follow-ups due.")

    new_to_send = new_leads[:config.EMAILS_PER_DAY]
    fu_to_send  = followups[:config.EMAILS_PER_DAY]

    if not new_to_send and not fu_to_send:
        print("\n[RESULT] Nothing to draft today.")
        return

    print(f"\n[EMAIL] Building {len(new_to_send)} new + {len(fu_to_send)} follow-up drafts...")

    today_str = date.today().isoformat()
    messages_to_save: list[tuple[MIMEMultipart, dict | None, bool]] = []

    # -- Build new email drafts ------------------------------------------------
    for lead in new_to_send:
        company_name_lead = lead.get("company_name", "")
        website           = lead.get("website", "")
        email_str         = lead.get("email", "")
        region            = lead.get("region", "")
        reason            = lead.get("classification_reason", "")

        to_addresses = [e.strip() for e in email_str.split(",") if e.strip()]
        if not to_addresses:
            continue

        subject      = sender.get_subject(reason)
        partner_word = sender.get_partner_word(reason)

        print(f"\n  Generating opener for: {company_name_lead}...")
        opener = _generate_opener(company_name_lead, reason)

        logo_cid = f"logo_{website.replace('.', '_')}"
        html = templates.HTML_BODY.format(
            company_name=company_name_lead,
            opener=opener,
            partner_word=partner_word,
            advect_website=sender.ADVECT_WEBSITE,
            advecton_website=sender.ADVECTON_WEBSITE,
            sender_name=sender.SENDER_NAME,
            logo_cid=logo_cid,
        )

        msg = _build_message(to_addresses, subject, html, logo_bytes, logo_cid)
        messages_to_save.append((msg, lead, False))
        print(f"  Draft ready: {company_name_lead} -> {', '.join(to_addresses)}")

    # -- Build follow-up drafts ------------------------------------------------
    for row in fu_to_send:
        company_name_lead = row.get("company_name", "")
        to_addresses      = [e.strip() for e in row.get("email_sent_to", "").split(",") if e.strip()]
        if not to_addresses:
            continue

        subject  = "Re: " + row.get("email_subject", "Inquiry")
        logo_cid = f"logo_fu_{company_name_lead.replace(' ', '_')}"
        html = templates.HTML_FOLLOWUP.format(
            company_name=company_name_lead,
            advect_website=sender.ADVECT_WEBSITE,
            advecton_website=sender.ADVECTON_WEBSITE,
            sender_name=sender.SENDER_NAME,
            logo_cid=logo_cid,
        )

        msg = _build_message(to_addresses, subject, html, logo_bytes, logo_cid)
        messages_to_save.append((msg, row, True))
        print(f"  Follow-up draft ready: {company_name_lead}")

    if not messages_to_save:
        print("\n[RESULT] No drafts to save.")
        return

    # -- Save all drafts to Zoho -----------------------------------------------
    print(f"\n[IMAP] Connecting to {sender.IMAP_HOST}...")
    mime_messages = [m for m, _, _ in messages_to_save]
    saved = _save_drafts(mime_messages)
    print(f"[IMAP] {saved}/{len(mime_messages)} drafts saved to Zoho.")

    # -- Log to Approached sheet -----------------------------------------------
    for i, (_, data, is_followup) in enumerate(messages_to_save):
        if i >= saved:
            break
        if is_followup:
            website = data.get("website", "")
            sheets.update_approached_followup(spreadsheet_id, website, today_str)
        else:
            lead              = data
            company_name_lead = lead.get("company_name", "")
            website           = lead.get("website", "")
            region            = lead.get("region", "")
            email_str         = lead.get("email", "")
            to_addresses      = [e.strip() for e in email_str.split(",") if e.strip()]
            subject           = sender.get_subject(lead.get("classification_reason", ""))
            sheets.write_approached(spreadsheet_id, {
                "company_name":      company_name_lead,
                "website":           website,
                "region":            region,
                "email_sent_to":     ", ".join(to_addresses),
                "email_subject":     subject,
                "sent_on":           today_str,
                "follow_up_sent_on": "",
                "status":            "Approached",
                "reply_date":        "",
            })

    print(f"\n[RESULT] Done. {saved} drafts saved. Open Zoho Mail to review and send.")


# --- CLI ----------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Save personalised cold outreach HTML drafts to Zoho via IMAP."
    )
    parser.add_argument(
        "--company", required=True,
        help='Seller company name. Example: "Your Company Name"',
    )
    args = parser.parse_args()

    try:
        run(args.company)
    except KeyboardInterrupt:
        print("\n[EMAIL] Interrupted by user.")
