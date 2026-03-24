# AI Sales Agent Team

An AI-powered, multi-agent B2B lead generation and outreach system. Give it a company name and a target region, and it researches the industry, identifies the ideal customer profile (ICP), searches for matching companies, deduplicates against your existing leads, classifies them by fit, enriches contact details, and drafts personalised cold outreach emails -- all written directly to Google Sheets and your email Drafts folder.

Designed to be **company-agnostic**: point it at any company in any industry and it figures out who to target.

---

## How It Works

```
Step 1: Lead Discovery

Input: --company "Your Company" --region "Your Region"  (e.g. "Texas, USA", "Germany", "Ontario, Canada")
             |
             v
   +---------------------+
   |   Research Agent    |  Runs once per company, cached to disk.
   |                     |  Uses Gemini to understand the company,
   |                     |  its products, and who actually buys them.
   |                     |  Produces a structured ICP analysis.
   +---------------------+
             |
             v
   +---------------------+
   |    Search Agent     |  Orchestrates the full pipeline:
   |                     |
   |  1. Load Sheet      |  Reads existing leads from Google Sheets.
   |                     |  Builds a domain set for deduplication.
   |                     |  (Pure Python, zero LLM tokens.)
   |                     |
   |  2. Query Gen       |  One Gemini call generates ~22 targeted
   |                     |  search queries driven by product groups,
   |                     |  buyer vocabulary, and ICP types, all from
   |                     |  the research cache.
   |                     |
   |  3. Search          |  Executes queries via:
   |                     |    - Serper.dev  (Google search results)
   |                     |    - Serper Maps  (local businesses,
   |                     |      catches low-SEO companies)
   |                     |
   |  4. Dedup           |  Filters out companies already in the sheet.
   |                     |  Domain normalisation + set lookup. No LLM.
   |                     |
   |  5. Validate        |  Sends candidates to Gemini in batches of 30.
   |                     |  Removes competitors, irrelevant companies,
   |                     |  and out-of-region results.
   |                     |
   |  6. Write           |  Validated leads go to the Leads tab.
   |                     |  Rejected companies go to Rejected Companies tab.
   +---------------------+
             |
             v
   Google Sheets: one spreadsheet per company, two tabs:
     - Leads              - companies that passed ICP validation
     - Rejected Companies - companies that were processed but did not pass


Step 2: Classify Leads (run separately, on-demand)

Input: --company "Your Company"
             |
             v
   +---------------------+
   |  Classify Script    |  Reads the Leads tab, visits each company
   |                     |  website using Gemini URL Context (Gemini
   |                     |  fetches the page itself), and classifies
   |                     |  each lead as Strong, Weak, or Not a Lead,
   |                     |  with a one-sentence reason written back
   |                     |  to the Leads tab.
   +---------------------+


Step 3: Enrich Contacts (run separately, on-demand)

Input: --company "Your Company"
             |
             v
   +---------------------+
   |   Enrich Script     |  Reads Strong leads from the Leads tab,
   |                     |  visits each website, and scrapes emails
   |                     |  and phone numbers. Writes results back
   |                     |  to the Leads tab. No third-party APIs.
   +---------------------+


Step 4: Send Outreach Emails (run separately, daily)

Input: --company "Your Company"
             |
             v
   +---------------------+
   |  Email Outreach     |  Reads Strong leads with emails from the
   |                     |  Leads tab. Generates a personalised opener
   |                     |  for each using Gemini. Builds an HTML email
   |                     |  and saves it as a Draft in your email client
   |                     |  via IMAP. Also checks for due follow-ups
   |                     |  (5 business days after first contact) and
   |                     |  drafts those automatically.
   |                     |
   |                     |  Terminal exits immediately. Review drafts
   |                     |  and send from your email client.
   +---------------------+
             |
             v
   Google Sheets: Approached tab tracks every company emailed,
   date sent, subject line, and follow-up status.
```

---

## Project Structure

```
AI-Sales-Agent/
├── main.py                     # CLI entry point for lead discovery
├── config.py                   # All settings loaded from .env
├── requirements.txt
├── .env                        # API keys, not committed, never share this
|
├── agents/
|   ├── research_agent.py       # ICP research, runs once, cached per company
|   └── search_agent.py         # Full pipeline orchestration
|
├── tools/
|   ├── llm.py                  # Shared Gemini client (swap models via .env)
|   ├── serper_search.py        # Serper.dev Google Search API wrapper
|   ├── google_places.py        # Google Places Text Search API wrapper
|   └── sheets.py               # Google Sheets read/write + OAuth auth
|
├── scripts/
|   ├── classify_leads.py       # On-demand classifier: reads Leads tab, visits
|   |                           # each website via Gemini URL Context, writes
|   |                           # classification and reason back to the sheet.
|   └── enrich_contacts.py      # On-demand contact scraper: reads Strong leads,
|                               # scrapes emails and phone numbers from company
|                               # websites, writes back to the Leads tab.
|
├── email_automation/
|   ├── send_emails.py          # Email outreach engine (this file is tracked)
|   ├── sender.py               # NOT in repo: your sender identity, URLs, IMAP
|   |                           # settings, subject line logic. See setup below.
|   ├── templates.py            # NOT in repo: HTML email bodies and Gemini
|   |                           # opener prompt. See setup below.
|   └── assets/
|       └── your_logo.jpg       # NOT in repo: your company logo for the signature
|
├── cache/
|   └── research/               # Cached ICP research JSON, one file per company.
|                               # Not committed, generated automatically on first run.
|
└── data/
    └── spreadsheets.json       # Maps company names to Google Sheet IDs.
                                # Not committed, generated automatically on first run.
```

---

## Token Efficiency

Minimising LLM API cost is a core design constraint.

| Operation | LLM Calls | How |
|---|---|---|
| ICP Research | **Once ever** per company | Cached to `cache/research/` |
| Query generation | **1** per run | Single structured prompt |
| Deduplication | **0** | Python set of normalised domains |
| Lead validation | **N / 30** per run | Batched, structured JSON output |
| Search execution | **0** | Direct API calls (Serper web + Serper Maps) |
| Sheet read/write | **0** | Google Sheets API |
| Lead classification | **1 per lead** | Gemini URL Context fetches website, classifies Strong/Weak/Not a Lead |
| Email opener | **1 per new email** | Gemini generates 2-sentence personalised opener from classification_reason |

---

## Google Sheets Schema

One spreadsheet per company with three tabs.

**Leads tab**: companies that passed ICP validation. Classification and contact columns are added by their respective scripts:

| company_name | website | region | source | search_query | date_added | classification | classification_reason | email | phone |
|---|---|---|---|---|---|---|---|---|---|
| ABC Dist. | abcdist.com | Texas, USA | Google Search | industrial valve distributor Texas | 2026-03-13 | Strong | Distributor of industrial valves and piping. | info@abcdist.com | +1 555 000 1234 |

**Rejected Companies tab**: companies that did not pass ICP validation. Same columns as Leads (without classification). Useful for auditing what was filtered and why.

**Approached tab**: one row per company that has been emailed. Tracked by the email outreach engine.

| company_name | website | region | email_sent_to | email_subject | sent_on | follow_up_sent_on | status | reply_date |
|---|---|---|---|---|---|---|---|---|

- **classification**: `Strong`, `Weak`, or `Not a Lead`. Added by `classify_leads.py`.
- **classification_reason**: One sentence explaining the classification.
- **source**: `Google Search` or `Google Maps`, indicating which API found this company.
- **search_query**: The exact query that surfaced this company.
- **region**: The region passed via `--region` when the run was executed (e.g. `Texas, USA`).
- **date_added**: ISO date (YYYY-MM-DD), set automatically.
- Header row is frozen on all tabs for easy filtering.

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure API keys

Copy `.env` and fill in your keys:

```
GEMINI_API_KEY=...                      # Google AI Studio, free tier available
GEMINI_MODEL=gemini-2.5-flash           # Swap model here without touching code

SERPER_API_KEY=...                      # serper.dev, used for both web search and Maps search

GOOGLE_SHEETS_CREDENTIALS_FILE=credentials.json

# Email outreach (only needed for send_emails.py):
ZOHO_EMAIL=...                          # Your sending email address
ZOHO_APP_PASSWORD=...                   # App-specific password from your email provider

# Optional, defaults shown:
MAX_LEADS_PER_RUN=500                   # Hard cap on leads written per run
VALIDATION_BATCH_SIZE=30                # Candidates per LLM validation call
SEARCHES_PER_QUERY=20                   # Results fetched per search query
EMAILS_PER_DAY=10                       # Max new emails drafted per run
```

### 3. Google Sheets (one-time setup)

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project and enable **Google Sheets API** and **Google Drive API**
3. APIs & Services > Credentials > **Create OAuth 2.0 Client ID** (Desktop app)
4. Download the JSON and save as `credentials.json` in the project root
5. Run the program; a browser window opens once for authorisation
6. `token.json` is saved automatically, no browser needed after this

### 4. Run lead discovery

```bash
# Standard run
python main.py --company "Your Company Name" --region "Texas, USA"

# Broad region
python main.py --company "Your Company Name" --region "Europe"

# Force re-run of ICP research (bypass cache)
python main.py --company "Your Company Name" --region "Germany" --force-research
```

### 5. Classify leads (run after discovery)

```bash
# Classify all unclassified leads in the Leads tab
python scripts/classify_leads.py --company "Your Company Name"

# Re-classify leads that already have a classification
python scripts/classify_leads.py --company "Your Company Name" --reclassify
```

Each lead's website is read by Gemini (via URL Context) and classified as `Strong`, `Weak`, or `Not a Lead`, with a one-sentence reason written back to the sheet. Results are saved immediately, so you can safely stop and resume at any time.

### 6. Enrich contacts (run after classification)

```bash
# Scrape emails and phone numbers for Strong leads only
python scripts/enrich_contacts.py --company "Your Company Name"

# Re-scrape companies that already have contact info
python scripts/enrich_contacts.py --company "Your Company Name" --re-enrich
```

Visits each Strong lead's website, checks the homepage and common contact pages (`/contact`, `/contact-us`, `/about-us`, etc.), and writes all found emails and phone numbers back to the Leads tab. No third-party APIs used.

### 7. Send outreach emails (run daily)

The email engine (`email_automation/send_emails.py`) is company-agnostic. Before running it, you need to create two files that are **not included in this repo** because they contain your company's branding and copy:

**`email_automation/sender.py`** -- your sender identity and settings:

```python
SENDER_NAME    = "Your Name"
SENDER_DISPLAY = "Your Company Name"

ADVECT_WEBSITE   = "https://yourwebsite.com/"     # primary website URL
ADVECTON_WEBSITE = "https://yourwebsite2.com/"    # optional second website, or same as above

IMAP_HOST = "imap.yourprovider.com"   # e.g. imappro.zoho.in, imap.gmail.com
IMAP_PORT = 993

FOLLOWUP_DAYS = 5   # business days before a follow-up is drafted

_EPC_KEYWORDS = ["engineering", "construction", "procurement", "contractor", "project"]

def get_subject(classification_reason: str) -> str:
    if any(kw in classification_reason.lower() for kw in _EPC_KEYWORDS):
        return "Your EPC Subject Line"
    return "Your Default Subject Line"

def get_partner_word(classification_reason: str) -> str:
    if any(kw in classification_reason.lower() for kw in _EPC_KEYWORDS):
        return "project partners"
    return "distributors"
```

**`email_automation/templates.py`** -- your HTML email bodies and Gemini opener prompt:

```python
HTML_BODY = """
<html>
<body style="font-family: Calibri, sans-serif; font-size: 11pt;">
<p>Hi {company_name} team,</p>
<p>{opener}</p>
<p>Your email body here...</p>
<p style="margin: 0;">Best,</p>
<p style="margin: 0;">{sender_name}</p>
<img src="cid:{logo_cid}" style="height: 50px;" />
</body>
</html>
"""

HTML_FOLLOWUP = """..."""   # follow-up email body, same variables minus {opener} and {partner_word}

OPENER_PROMPT = """
You are writing the opening 2 sentences of a cold B2B email.
Seller: Your Company -- describe what you sell.
Target company: {company_name}
What they do: {classification_reason}
Write exactly 2 sentences...
"""
```

**`email_automation/assets/your_logo.jpg`** -- your company logo. Referenced as `assets/Advect Logo.jpg` in the engine by default; update `_LOGO_PATH` in `send_emails.py` to match your filename.

Once those files are in place:

```bash
python email_automation/send_emails.py --company "Your Company Name"
```

This drafts up to `EMAILS_PER_DAY` new emails and any due follow-ups, saves them to your IMAP Drafts folder, and logs each company to the **Approached** tab in Google Sheets. Open your email client to review and send.

---

## API Keys & Cost

| Service | Get Key |
|---|---|
| Gemini (LLM) | [aistudio.google.com](https://aistudio.google.com/app/apikey) |
| Serper.dev | [serper.dev](https://serper.dev) -- used for both web search and Maps search |
| Google Sheets | [console.cloud.google.com](https://console.cloud.google.com) |

Check each provider's current pricing page; plans and free tiers change over time. LLM costs per run are very low (typically under $0.05 for Gemini 2.5 Flash at standard usage).

---

## Roadmap

- **v1:** Lead discovery and ICP validation, written to Google Sheets with source and search query tracking.
- **v1.5:** Lead classification using Gemini URL Context: Strong, Weak, or Not a Lead with a reason, written back to the Leads tab.
- **v2 (current):** Contact enrichment: scrapes emails and phone numbers from company websites for Strong leads, written back to the Leads tab.
- **v3:** Outreach automation: personalised HTML email drafts saved to IMAP, follow-ups tracked automatically via Google Sheets.
- **v4:** Web UI, browser-based interface wrapping the same agent pipeline.
