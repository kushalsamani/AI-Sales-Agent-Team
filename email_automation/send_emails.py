"""
email_automation/send_emails.py
--------------------------------
Schedules personalised cold outreach emails via Zoho SMTP for Strong leads
that have not yet been contacted.

Pipeline:
  1. Read Strong leads with emails from the Leads sheet.
  2. Load Approached sheet — skip any company already contacted.
  3. For each new company, generate a 2-sentence opener using Gemini
     (company_name + classification_reason — no URL visit needed).
  4. Compose full email from fixed template.
  5. Schedule via Zoho SMTP with random delays starting 4+ hours from now.
  6. Write each sent company to the Approached sheet immediately.

Usage:
  # Test with 3 emails
  python email_automation/send_emails.py --company "Your Company" --count 3

  # Full daily run (uses EMAILS_PER_DAY from .env)
  python email_automation/send_emails.py --company "Your Company"

  # With follow-ups (sends new + follow-ups, combined up to EMAILS_PER_DAY*2)
  python email_automation/send_emails.py --company "Your Company" --follow-ups
"""

import argparse
import random
import smtplib
import sys
import os
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Allow imports from project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import sheets, llm
from tools.sheets import normalize_domain
import config


# ─── Constants ────────────────────────────────────────────────────────────────

ADVECT_WEBSITE   = "https://advectprocess.com/"
SENDER_NAME      = "Advect Process Systems"
FOLLOWUP_DAYS    = 5   # Business days before sending a follow-up.

# Keywords in classification_reason that indicate an EPC firm.
_EPC_KEYWORDS = ["epc", "engineering", "construction", "procurement", "contractor", "project"]


# ─── Email Template ───────────────────────────────────────────────────────────

# Plain text version (Zoho will send as plain text to avoid spam filters).
_EMAIL_BODY = """\
Hi {company_name} team,

{opener}

We are Advect, a manufacturer of PTFE/PFA Lined Pipes, Fittings, Valves, Dip Pipes and PTFE Expanded Sheets. Our focus is on high-performance, corrosion-resistant equipment for chemical, pharma, and industrial applications.

We are currently setting up our manufacturing facility in Texas, USA, and as we expand our North American presence, we're looking for {partner_word} who can represent our products.

Kindly visit our website at Advect Process Systems Ltd. ({website}) for our full product range, or you can simply reply to this email with the products of your interest.

If this is something that could support your projects, we would be glad to connect and explore how we can work together.

Best regards,
{sender_name}
{sender_email}
"""

_FOLLOWUP_BODY = """\
Hi {company_name} team,

Just wanted to bump this up in your inbox in case it got buried. Happy to answer any questions about our PTFE/PFA lined pipe, fitting, and valve range.

Best regards,
{sender_name}
{sender_email}
"""

# Gemini prompt — small input, no URL visit needed.
_OPENER_PROMPT = """
You are writing the opening 2 sentences of a cold B2B sales email.

Seller: Advect — manufacturer of PTFE/PFA lined pipes, fittings, valves, dip pipes.
Target company: {company_name}
What they do: {classification_reason}

Write exactly 2 sentences:
  1. "I came across {company_name} and noticed that [what they do, specific to their business]."
  2. "Given the overlap, I thought it made sense to introduce our company."

Rules:
  - Sentence 1 must mention something specific from "What they do" — not generic.
  - Do NOT mention price, discounts, or any specific product specs.
  - Do NOT use em dashes (—). Use commas or semicolons instead.
  - Return ONLY the 2 sentences, no extra text, no quotes.
"""


# ─── Subject Line ─────────────────────────────────────────────────────────────

def _get_subject(classification_reason: str) -> str:
    """
    Determine subject line based on company type.
    Checks classification_reason for EPC keywords — no LLM needed.

    Args:
        classification_reason: One-sentence description of the company.

    Returns:
        Subject line string.
    """
    reason_lower = classification_reason.lower()
    if any(kw in reason_lower for kw in _EPC_KEYWORDS):
        return "Lined Piping Systems — Project Inquiry"
    return "Lined Pipes, Fittings and Valves Inquiry"


# ─── Partner Word ─────────────────────────────────────────────────────────────

def _get_partner_word(classification_reason: str) -> str:
    """
    Return the right word for 'looking for ___' based on company type.

    Args:
        classification_reason: One-sentence description of the company.

    Returns:
        'distribution partners' for EPC/engineering firms,
        'distributors' for everyone else.
    """
    reason_lower = classification_reason.lower()
    if any(kw in reason_lower for kw in _EPC_KEYWORDS):
        return "procurement and project partners"
    return "distributors"


# ─── Opener Generation ────────────────────────────────────────────────────────

def _generate_opener(company_name: str, classification_reason: str) -> str:
    """
    Call Gemini to generate a 2-sentence personalised opener.

    Args:
        company_name:          Name of the target company.
        classification_reason: Why this company was classified as Strong.

    Returns:
        Two-sentence opener string. Falls back to a generic opener on error.
    """
    prompt = _OPENER_PROMPT.format(
        company_name=company_name,
        classification_reason=classification_reason,
    )
    try:
        result = llm.generate_text(prompt, temperature=0.4)
        return result.strip()
    except Exception as e:
        print(f"  [WARN] Gemini opener failed for {company_name}: {e}. Using fallback.")
        return (
            f"I came across {company_name} and noticed your work in the industrial "
            f"process space. Given the overlap, I thought it made sense to introduce our company."
        )


# ─── SMTP Scheduling ──────────────────────────────────────────────────────────

def _schedule_email(
    to_addresses: list[str],
    subject: str,
    body: str,
    send_at: datetime,
) -> bool:
    """
    Send an email via Zoho SMTP.

    Note: Zoho's SMTP API does not support server-side scheduling.
    We simulate scheduling by sleeping until send_at before connecting.
    For true background scheduling, run the script in the morning and
    let it sleep between sends — or use a task scheduler (Windows Task
    Scheduler / cron) to invoke it at a fixed time each day.

    Args:
        to_addresses: List of recipient email addresses.
        subject:      Email subject line.
        body:         Plain text email body.
        send_at:      Target datetime to send (script sleeps until then).

    Returns:
        True if sent successfully, False on error.
    """
    import time

    # Sleep until scheduled time.
    now = datetime.now()
    wait = (send_at - now).total_seconds()
    if wait > 0:
        print(f"  Waiting {int(wait // 60)}m {int(wait % 60)}s until scheduled time...")
        time.sleep(wait)

    msg = MIMEMultipart("alternative")
    msg["From"]    = f"{SENDER_NAME} <{config.ZOHO_EMAIL}>"
    msg["To"]      = ", ".join(to_addresses)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(config.ZOHO_SMTP_HOST, config.ZOHO_SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(config.ZOHO_EMAIL, config.ZOHO_APP_PASSWORD)
            server.sendmail(config.ZOHO_EMAIL, to_addresses, msg.as_string())
        return True
    except Exception as e:
        print(f"  [ERROR] Failed to send email: {e}")
        return False


# ─── Business Day Helper ──────────────────────────────────────────────────────

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
        if current.weekday() < 5:  # Monday=0, Friday=4
            added += 1
    return current


# ─── Main Pipeline ────────────────────────────────────────────────────────────

def run(company_name: str, count: int | None = None, send_followups: bool = False) -> None:
    """
    Run the email scheduling pipeline.

    Args:
        company_name:   Seller company name (must match spreadsheets.json).
        count:          If set, send only this many emails (for testing).
        send_followups: If True, also schedule follow-ups for companies
                        that haven't replied after FOLLOWUP_DAYS business days.
    """
    config.require_key("ZOHO_EMAIL", config.ZOHO_EMAIL)
    config.require_key("ZOHO_APP_PASSWORD", config.ZOHO_APP_PASSWORD)

    # ── Load spreadsheet ───────────────────────────────────────────────────────
    spreadsheet_id = sheets.get_or_create_spreadsheet(company_name)

    # ── Load Approached sheet (dedup source of truth) ──────────────────────────
    print("[APPROACHED] Loading already-contacted companies...")
    approached = sheets.get_approached_companies(spreadsheet_id)
    approached_domains: set[str] = {
        normalize_domain(r.get("website", "")) or ""
        for r in approached
    }
    print(f"[APPROACHED] {len(approached_domains)} companies already contacted.")

    # ── Load Strong leads with emails ──────────────────────────────────────────
    print("[LEADS] Reading Strong leads with emails...")
    all_leads = sheets.read_leads_for_classification(spreadsheet_id)
    strong_with_email = [
        lead for lead in all_leads
        if lead.get("classification", "").strip() == "Strong"
        and lead.get("email", "").strip()
        and lead.get("email", "").strip().upper() != "N/A"
    ]
    print(f"[LEADS] {len(strong_with_email)} Strong leads with emails found.")

    # ── Filter out already contacted ───────────────────────────────────────────
    new_leads = [
        lead for lead in strong_with_email
        if (normalize_domain(lead.get("website", "")) or "") not in approached_domains
    ]
    print(f"[LEADS] {len(new_leads)} not yet contacted.")

    # ── Handle follow-ups ──────────────────────────────────────────────────────
    followups: list[dict] = []
    if send_followups:
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
            due = _add_business_days(sent_date, FOLLOWUP_DAYS)
            if today >= due:
                followups.append(row)
        print(f"[FOLLOWUP] {len(followups)} follow-ups due.")

    # ── Apply count cap ────────────────────────────────────────────────────────
    daily_limit = count if count is not None else config.EMAILS_PER_DAY
    new_to_send  = new_leads[:daily_limit]
    fu_to_send   = followups[:daily_limit] if send_followups else []

    if not new_to_send and not fu_to_send:
        print("\n[RESULT] Nothing to send today.")
        return

    print(f"\n[EMAIL] Scheduling {len(new_to_send)} new + {len(fu_to_send)} follow-ups...")

    # ── Build send schedule ────────────────────────────────────────────────────
    # All emails (new + followups) are interleaved and assigned random slots.
    now = datetime.now()
    send_time = now + timedelta(seconds=config.EMAIL_FIRST_DELAY)
    today_str  = date.today().isoformat()

    def _next_send_time() -> datetime:
        nonlocal send_time
        delay = random.randint(config.EMAIL_DELAY_MIN, config.EMAIL_DELAY_MAX)
        send_time = send_time + timedelta(seconds=delay)
        return send_time

    # ── Send new emails ────────────────────────────────────────────────────────
    for lead in new_to_send:
        company_name_lead = lead.get("company_name", "")
        website           = lead.get("website", "")
        email_str         = lead.get("email", "")
        region            = lead.get("region", "")
        reason            = lead.get("classification_reason", "")

        to_addresses = [e.strip() for e in email_str.split(",") if e.strip()]
        if not to_addresses:
            continue

        subject      = _get_subject(reason)
        partner_word = _get_partner_word(reason)

        print(f"\n  Generating opener for: {company_name_lead}...")
        opener = _generate_opener(company_name_lead, reason)

        body = _EMAIL_BODY.format(
            company_name=company_name_lead,
            opener=opener,
            partner_word=partner_word,
            website=ADVECT_WEBSITE,
            sender_name=SENDER_NAME,
            sender_email=config.ZOHO_EMAIL,
        )

        scheduled = _next_send_time()
        print(f"  Sending to {', '.join(to_addresses)} at {scheduled.strftime('%H:%M:%S')}...")

        sent = _schedule_email(to_addresses, subject, body, scheduled)

        if sent:
            sheets.write_approached(spreadsheet_id, {
                "company_name":     company_name_lead,
                "website":          website,
                "region":           region,
                "email_sent_to":    ", ".join(to_addresses),
                "email_subject":    subject,
                "sent_on":          today_str,
                "follow_up_sent_on": "",
                "status":           "Approached",
                "reply_date":       "",
            })
            print(f"  [OK] Email sent and logged.")
        else:
            print(f"  [SKIP] Email failed — not logged to Approached sheet.")

    # ── Send follow-ups ────────────────────────────────────────────────────────
    for row in fu_to_send:
        company_name_lead = row.get("company_name", "")
        to_addresses      = [e.strip() for e in row.get("email_sent_to", "").split(",") if e.strip()]
        if not to_addresses:
            continue

        subject = "Re: " + row.get("email_subject", "Lined Pipes and Fittings Inquiry")
        body    = _FOLLOWUP_BODY.format(
            company_name=company_name_lead,
            sender_name=SENDER_NAME,
            sender_email=config.ZOHO_EMAIL,
        )

        scheduled = _next_send_time()
        print(f"\n  Follow-up to {company_name_lead} at {scheduled.strftime('%H:%M:%S')}...")

        sent = _schedule_email(to_addresses, subject, body, scheduled)

        if sent:
            sheets.update_approached_followup(spreadsheet_id, row.get("website", ""), today_str)
            print(f"  [OK] Follow-up sent and logged.")

    print(f"\n[RESULT] Done.")


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Schedule cold outreach emails for Strong leads via Zoho SMTP."
    )
    parser.add_argument(
        "--company", required=True,
        help='Seller company name. Example: "Your Company Name"',
    )
    parser.add_argument(
        "--count", type=int, default=None,
        help="Send only this many emails (for testing). Overrides EMAILS_PER_DAY.",
    )
    parser.add_argument(
        "--follow-ups", action="store_true",
        help="Also send follow-ups to companies that haven't replied after 5 business days.",
    )
    args = parser.parse_args()

    try:
        run(args.company, count=args.count, send_followups=args.follow_ups)
    except KeyboardInterrupt:
        print("\n[EMAIL] Session interrupted by user.")
