# AI Sales Agent — Lead Research Module

An AI-powered, multi-agent B2B lead generation system. Give it a company name and a target region — it researches the industry, identifies the ideal customer profile (ICP), searches for matching companies across multiple sources, deduplicates against your existing leads, and writes a clean, validated list directly to Google Sheets.

Designed to be **company-agnostic**: point it at any company in any industry and it figures out who to target.

---

## How It Works

```
Input: --company "Your Company" --region "Texas, USA"
             │
             ▼
   ┌─────────────────────┐
   │   Research Agent    │  Runs once per company, cached to disk.
   │                     │  Uses Gemini to understand the company,
   │                     │  its products, and who actually buys them.
   │                     │  Produces a structured ICP analysis.
   └─────────────────────┘
             │
             ▼
   ┌─────────────────────┐
   │    Search Agent     │  Orchestrates the full pipeline:
   │                     │
   │  1. Load Sheet      │  Reads existing leads from Google Sheets.
   │                     │  Builds a domain set for deduplication.
   │                     │  (Pure Python — zero LLM tokens.)
   │                     │
   │  2. Query Gen       │  One Gemini call generates 15–20 targeted
   │                     │  search queries based on ICP + region.
   │                     │
   │  3. Search          │  Executes queries via:
   │                     │    • Serper.dev  (Google search results)
   │                     │    • Google Places API  (local businesses,
   │                     │      catches low-SEO companies)
   │                     │
   │  4. Dedup           │  Filters out companies already in the sheet.
   │                     │  Domain normalisation + set lookup. No LLM.
   │                     │
   │  5. Validate        │  Sends candidates to Gemini in batches of 25.
   │                     │  Removes competitors, irrelevant companies,
   │                     │  and out-of-region results.
   │                     │
   │  6. Write           │  Appends validated leads to Google Sheets.
   └─────────────────────┘
             │
             ▼
   Google Sheets — one spreadsheet per company, one row per lead.
```

---

## Project Structure

```
AI-Sales-Agent/
├── main.py                     # CLI entry point
├── config.py                   # All settings loaded from .env
├── requirements.txt
├── .env                        # API keys (not committed)
│
├── agents/
│   ├── research_agent.py       # ICP research — runs once, cached per company
│   └── search_agent.py         # Full pipeline orchestration
│
├── tools/
│   ├── llm.py                  # Shared Gemini client (swap models via .env)
│   ├── serper_search.py        # Serper.dev Google Search API wrapper
│   ├── google_places.py        # Google Places Text Search API wrapper
│   └── sheets.py               # Google Sheets read/write + OAuth auth
│
├── cache/
│   └── research/               # Cached ICP research JSON, one file per company
│
└── data/
    └── spreadsheets.json       # Maps company names → Google Sheet IDs
```

---

## Token Efficiency

Minimising LLM API cost is a core design constraint.

| Operation | LLM Calls | How |
|---|---|---|
| ICP Research | **Once ever** per company | Cached to `cache/research/` |
| Query generation | **1** per run | Single structured prompt |
| Deduplication | **0** | Python set of normalised domains |
| Lead validation | **N / 25** per run | Batched, structured JSON output |
| Search execution | **0** | Direct API calls (Serper, Places) |
| Sheet read/write | **0** | Google Sheets API |

---

## Google Sheets Schema

One spreadsheet per company. One sheet (tab) named **Leads**. One row per company.

| company_name | website | country | date_added |
|---|---|---|---|
| ABC Industrial Supply | abcindustrial.com | USA | 2026-03-13 |
| XYZ Process Solutions | xyzprocess.de | Germany | 2026-03-13 |

- **country**: Comma-separated if the company operates in multiple countries (e.g. `USA, UK, India`).
- **date_added**: ISO date (YYYY-MM-DD), set automatically on write.
- The header row is frozen for easy filtering.

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure API keys

Copy `.env` and fill in your keys:

```
GEMINI_API_KEY=...         # Google AI Studio — free tier available
GEMINI_MODEL=gemini-2.5-flash

SERPER_API_KEY=...         # serper.dev — 2,500 free searches/month
GOOGLE_PLACES_API_KEY=...  # Google Cloud — $200 free credit/month

GOOGLE_SHEETS_CREDENTIALS_FILE=credentials.json
```

### 3. Google Sheets (one-time setup)

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project → enable **Google Sheets API** and **Google Drive API**
3. APIs & Services → Credentials → **Create OAuth 2.0 Client ID** (Desktop app)
4. Download the JSON → save as `credentials.json` in the project root
5. Run the program — a browser window opens once for authorisation
6. `token.json` is saved automatically — no browser needed after this

### 4. Run

```bash
# Standard run
python main.py --company "Your Company Name" --region "Texas, USA"

# Broad region
python main.py --company "Your Company Name" --region "Europe"

# Force re-run of ICP research (bypass cache)
python main.py --company "Your Company Name" --region "Germany" --force-research
```

---

## API Keys & Cost

| Service | Free Tier | Get Key |
|---|---|---|
| Gemini (LLM) | Free tier with rate limits | [aistudio.google.com](https://aistudio.google.com/app/apikey) |
| Serper.dev | 2,500 searches/month | [serper.dev](https://serper.dev) |
| Google Places | $200 credit/month | [Google Cloud Console](https://console.cloud.google.com) |
| Google Sheets | Free | Same Google Cloud project |

Typical cost per run (100 leads): well within free tier limits for moderate usage.

---

## Roadmap

- **v1 (current):** Lead discovery — company name, website, country written to Google Sheets.
- **v2:** Contact enrichment — find general company emails and priority decision-maker contacts (procurement, purchasing, directors) for each discovered company.
- **v3:** Web UI — browser-based interface wrapping the same agent pipeline.
