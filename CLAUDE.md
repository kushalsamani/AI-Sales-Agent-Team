# Lead Research Agent — Project Memory

## Project Overview

A multi-agent lead generation system that takes a company name and target region as input, researches the industry and ideal customer profile (ICP), searches for matching companies using free APIs, deduplicates against existing data, and writes net-new leads to a Google Sheet. Built to be company-agnostic — it should work for any company in any industry.

**Current client using this system:** Advect Process Systems Canada Ltd. (manufacturers of PTFE Lined Pipes, Fittings, and Valves).

---

## Tech Stack

- **Language:** Python
- **LLM:** Gemini 2.5 Flash (via Google AI Studio, configured in `.env`)
- **Search APIs:** Serper.dev (primary), Google Places API (secondary)
- **Storage:** Google Sheets API
- **Config:** All API keys and model names stored in `.env` — no hardcoded values

---

## Folder Structure

```
lead-research-agent/
├── .env                          # API keys + model config (never commit this)
├── CLAUDE.md                     # This file
├── main.py                       # CLI entry point
├── config.py                     # Loads .env, shared constants
├── agents/
│   ├── research_agent.py         # ICP research, cached per company
│   └── search_agent.py           # Search, dedup, validate, collect leads
├── tools/
│   ├── serper_search.py          # Serper.dev API wrapper
│   ├── google_places.py          # Google Places API wrapper
│   └── sheets.py                 # Google Sheets read/write
├── cache/
│   └── research/                 # One JSON file per company (research cache)
└── requirements.txt
```

---

## Agent Architecture

### 1. Research Agent (`agents/research_agent.py`)
- Runs **once per company** and caches result to `cache/research/<company_slug>.json`
- On subsequent runs, loads from cache — no LLM call unless `--force-research` flag is passed
- Uses Gemini to produce a structured JSON output containing:
  - Company summary and product lines
  - ICP profiles: company types, sizes, sectors most likely to buy
  - Anti-ICP: who NOT to target (e.g., don't target manufacturers of the same product)
  - Search strategy: recommended query templates and sources
- **Must not hallucinate.** Research should be grounded in what Gemini knows about the company and its industry. The agent should reason like a senior B2B sales developer.

### 2. Search Agent (`agents/search_agent.py`)
- Reads existing leads from Google Sheets on startup → builds a **Python set of normalized domains** (zero LLM tokens for dedup)
- Uses research output to generate targeted search queries for the given region (one Gemini call, structured output)
- Executes searches via Serper.dev and Google Places API
- Normalizes all URLs to root domain before dedup check
- Validates results in **batches of 20–30** using one Gemini call per batch — structured output, no verbose reasoning
- Collects up to **100 unique, validated, net-new leads** per run
- If fewer companies exist in the market, it reports this honestly — no padding with low-quality leads
- Writes results to Google Sheets

---

## CLI Usage

```bash
# Standard run
python main.py --company "Advect Process Systems Canada Ltd." --region "Texas, USA"

# Force redo research (bypass cache)
python main.py --company "Advect Process Systems Canada Ltd." --region "Texas, USA" --force-research

# Broad region
python main.py --company "Advect Process Systems Canada Ltd." --region "Europe"
```

---

## Google Sheets Schema (v1)

One spreadsheet per company. One master sheet (tab). One row per company lead.

| Column | Description |
|---|---|
| `company_name` | Full legal or trading name of the lead company |
| `website` | Root domain (e.g., `abcdist.com`) |
| `country` | Country or countries where the company operates (comma-separated if multi-country) |
| `date_added` | ISO date when this lead was added (YYYY-MM-DD) |

**Multi-country companies:** List all countries in the `country` column, comma-separated (e.g., `USA, UK, India`).

---

## Token Efficiency Rules (CRITICAL)

This is a core constraint. Every architectural and coding decision must consider LLM token cost.

1. **Cache research results.** Never call the LLM for company/ICP research if a cache file exists (unless `--force-research` is passed).
2. **Deduplication is pure Python.** Never send a company to the LLM just to check if it already exists in the sheet. Use a normalized domain set loaded at startup.
3. **Batch LLM validation.** Send 20–30 candidates per Gemini call for ICP validation — not one at a time.
4. **Structured prompts.** All LLM prompts must request structured JSON output. No verbose explanations, no chain-of-thought in output — just the data.
5. **Minimize round-trips.** Each agent phase should aim for the minimum number of LLM calls needed. Plan prompts carefully before calling.
6. **Search execution is API-only.** Serper.dev and Google Places calls return raw data — no LLM involved until the validation step.
7. **One query-generation call per run.** Generate all search queries in a single Gemini call, not one per query.

---

## Code Quality Rules

1. **Modular.** Each agent and tool lives in its own file. No monolithic scripts.
2. **Simple and readable.** Code should be easy to understand for someone without deep Python experience. Prefer clarity over cleverness.
3. **Documented.** Every function must have a clear docstring explaining what it does, its parameters, and what it returns.
4. **No hardcoded values.** API keys, model names, max lead counts, batch sizes — all in `.env` or `config.py`.
5. **DRY but not over-abstracted.** Don't create abstractions for hypothetical future use. Only abstract when the same logic is used in 2+ places.
6. **Fail loudly.** If an API key is missing or a call fails, raise a clear error with a helpful message — don't silently skip.
7. **Time and space complexity.** Prefer O(1) or O(n) operations. The domain dedup set is O(1) lookup by design. Avoid nested loops over large datasets.

---

## Important Business Logic

- **The agent must reason about ICP.** For a PTFE lined pipe manufacturer like Advect, targeting other pipe manufacturers is wrong — they are competitors/manufacturers, not buyers. The correct ICPs are: industrial distributors, EPC (Engineering, Procurement & Construction) companies, maintenance and repair contractors, chemical plant operators, etc.
- **No hallucinated leads.** Every company returned must be a real, discoverable entity with a real website. No invented company names.
- **No garbage to fill the quota.** If only 15 high-quality leads exist in a market, return 15 and report that the market appears saturated for now. Do not return irrelevant companies to hit 100.
- **Market saturation reporting.** If the agent has exhausted all search strategies and found fewer than the maximum, it should clearly indicate this in the CLI output.

---

## Environment Variables (`.env`)

```
GEMINI_API_KEY=your_api_key_here
GEMINI_MODEL=gemini-2.5-flash

SERPER_API_KEY=your_api_key_here

GOOGLE_PLACES_API_KEY=your_api_key_here

GOOGLE_SHEETS_CREDENTIALS_FILE=credentials.json
```

---

## What is NOT in Scope for v1

- Email finding / contact enrichment (this is v2)
- Web UI (CLI only for v1; UI will be added later — keep this in mind when structuring code so the logic is easily callable from a future UI layer)
- Website reachability checks (HTTP ping validation deferred to later)
- Private company guidelines document (optional refinement feature, not required for the system to work)

---

## Future Scope (v2 and beyond)

- **Email enrichment:** For each company in the sheet, find general company email + priority contact emails (procurement, purchasing, directors). Public information only.
- **Web UI:** Wrap the CLI logic in a simple frontend. All agent logic must remain in `agents/` so the UI just calls the same functions.
- **Private company guidelines:** Optional JSON/text doc per company that the research agent can read for extra context (e.g., existing clients as ICP examples, confidential positioning notes).
- **Website validation:** HTTP ping to filter dead links before saving.
- **Force re-research control:** Already provisioned via `--force-research` flag.
