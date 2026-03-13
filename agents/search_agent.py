"""
agents/search_agent.py
----------------------
Main orchestration agent. Runs the full lead generation pipeline:

  1. Research   — Gets ICP analysis (cached per company, one LLM call ever).
  2. Dedup prep — Loads existing sheet leads as a domain set (pure Python, 0 tokens).
  3. Query gen  — Generates targeted search queries (1 LLM call per run).
  4. Search     — Executes queries via Serper + Google Places (API calls, 0 LLM tokens).
  5. Dedup      — Filters out already-known companies (Python set lookup, 0 tokens).
  6. Validate   — Sends batches of candidates to Gemini for ICP match check.
  7. Write      — Appends validated leads to Google Sheets.

TOKEN EFFICIENCY SUMMARY (per run):
  - 1 LLM call for query generation
  - ceil(new_candidates / VALIDATION_BATCH_SIZE) LLM calls for validation
  - 0 LLM calls for deduplication (Python set)
  - 0 LLM calls for research (cached)
"""

import json
import math

from agents import research_agent
from tools import llm, serper_search, google_places, sheets
import config


# ─── Prompts ──────────────────────────────────────────────────────────────────

_QUERY_GEN_PROMPT = """
Generate specific Google search queries to find B2B companies that would purchase
industrial products in a target region.

SELLER:
  Company: {company_name}
  Products: {products}

TARGET REGION: {region}

ICP TYPES TO FIND:
{icp_types}

SUGGESTED QUERY TEMPLATES (replace {{region}} with actual region):
{templates}

INSTRUCTIONS:
  - Replace {{region}} in each template with "{region}" and its regional variations
    (e.g. for "Texas, USA" also try "Houston", "Dallas", "TX").
  - Generate 15 to 20 diverse queries spanning all ICP types.
  - Queries must return company websites, not articles, news, or directories.
  - Mix product-specific, industry-specific, and role-specific queries.
  - Make each query distinct — avoid near-duplicates.

Return ONLY a JSON array of query strings, no other text:
["query 1", "query 2", ...]
"""

_VALIDATION_PROMPT = """
You are a B2B sales qualification expert. Review the candidate companies below
and return ONLY those that are genuine potential buyers for this seller.

SELLER:
  {company_summary}
  Products: {products}

TARGET REGION: {region}

WHO TO INCLUDE (ICP — Ideal Customer Profile):
{icp_summary}

WHO TO EXCLUDE (Anti-ICP):
{anti_icp_summary}

ALSO EXCLUDE:
  - Companies that clearly manufacture or produce the same products as the seller.
  - Companies obviously outside the target region: {region}
  - Companies with no clear industry relevance.
  - Any duplicate entries.

CANDIDATES ({n} companies):
{candidates_json}

For each qualifying company:
  - Use context clues (website, name, snippet) to determine the country.
  - If country cannot be determined, default to the primary country in: "{region}".
  - Clean up the company name if it contains junk (e.g. " | LinkedIn", " - Home").

Return ONLY a JSON array. Return [] if no candidates qualify. No explanation:
[{{"company_name": "...", "website": "...", "country": "..."}}]
"""


# ─── Public Interface ─────────────────────────────────────────────────────────

def run(
    company_name: str,
    region: str,
    force_research: bool = False,
) -> tuple[list[dict], int]:
    """
    Run the full lead generation pipeline for a company and region.

    Args:
        company_name:    The company to generate leads for.
        region:          Target region (e.g. "Texas, USA", "Germany", "South America").
        force_research:  If True, bypass cached ICP research and re-run it.

    Returns:
        Tuple of:
          - List of validated lead dicts (company_name, website, country, date_added).
          - Count of rows written to Google Sheets.
    """

    # ── Step 1: ICP Research (cached) ─────────────────────────────────────────
    research = research_agent.get_research(company_name, force=force_research)
    _print_research_summary(research)

    # ── Step 2: Load existing leads for deduplication (0 LLM tokens) ──────────
    print("[SHEETS] Connecting to Google Sheets...")
    spreadsheet_id = sheets.get_or_create_spreadsheet(company_name)
    existing_domains = sheets.get_existing_domains(spreadsheet_id)
    print(f"[SHEETS] {len(existing_domains)} existing leads loaded for deduplication.")

    # ── Step 3: Generate search queries (1 LLM call) ──────────────────────────
    print(f"\n[SEARCH] Generating search queries for region: '{region}'...")
    queries = _generate_queries(research, region)
    print(f"[SEARCH] {len(queries)} queries generated.")

    # ── Step 4: Execute searches (API calls only, 0 LLM tokens) ───────────────
    print(f"\n[SEARCH] Executing searches...")
    raw_candidates = _execute_searches(queries)
    print(f"[SEARCH] {len(raw_candidates)} unique raw candidates collected.")

    # ── Step 5: Deduplicate against existing sheet (pure Python, 0 tokens) ────
    new_candidates = _deduplicate(raw_candidates, existing_domains)
    print(f"[SEARCH] {len(new_candidates)} new candidates after deduplication.")

    if not new_candidates:
        print(
            "\n[RESULT] No new candidates found for this region.\n"
            "         The market may be fully covered, or try different search terms."
        )
        return [], 0

    # ── Step 6: Batch validate with LLM ───────────────────────────────────────
    total_batches = math.ceil(len(new_candidates) / config.VALIDATION_BATCH_SIZE)
    print(
        f"\n[VALIDATE] Validating {len(new_candidates)} candidates "
        f"in {total_batches} batch(es) of up to {config.VALIDATION_BATCH_SIZE}..."
    )
    validated = _validate_in_batches(new_candidates, research, region)
    print(f"[VALIDATE] {len(validated)} leads passed ICP validation.")

    # ── Step 7: Cap at MAX_LEADS_PER_RUN ──────────────────────────────────────
    final_leads = validated[: config.MAX_LEADS_PER_RUN]

    # ── Step 8: Write to Google Sheets ────────────────────────────────────────
    print(f"\n[SHEETS] Writing {len(final_leads)} leads to Google Sheets...")
    written = sheets.append_leads(spreadsheet_id, final_leads)
    print(f"[SHEETS] {written} leads written successfully.")

    return final_leads, written


# ─── Pipeline Steps ───────────────────────────────────────────────────────────

def _generate_queries(research: dict, region: str) -> list[str]:
    """
    Use Gemini to generate targeted search queries from the ICP research.

    One LLM call per run. Returns a flat list of query strings.

    Args:
        research: Research dict from research_agent.
        region:   Target region string from CLI.

    Returns:
        List of search query strings. Falls back to filled-in templates on error.
    """
    icp_types = "\n".join(
        f"  - {p['type']}: {p['description']}"
        for p in research.get("icp_profiles", [])
    )
    templates = "\n".join(
        f"  - {t}" for t in research.get("search_query_templates", [])
    )

    prompt = _QUERY_GEN_PROMPT.format(
        company_name=research.get("company_summary", "")[:200],
        products=", ".join(research.get("products", [])),
        region=region,
        icp_types=icp_types,
        templates=templates,
    )

    try:
        result = llm.generate_json(prompt, temperature=0.3)
        if isinstance(result, list):
            return [str(q) for q in result if q]
        print("[WARN] Query generation returned unexpected format. Using template fallback.")
    except ValueError as e:
        print(f"[WARN] Query generation failed: {e}. Using template fallback.")

    # Fallback: fill region into the templates from research.
    return [
        t.replace("{region}", region)
        for t in research.get("search_query_templates", [])
    ]


def _execute_searches(queries: list[str]) -> list[dict]:
    """
    Execute all queries via Serper and Google Places. Returns deduplicated raw candidates.

    Deduplication here is intra-run only (same domain appearing in multiple queries).
    Cross-run deduplication (against the sheet) happens in _deduplicate().

    Args:
        queries: List of search query strings.

    Returns:
        List of raw candidate dicts with keys: company_name, website, country.
        All domains are normalised. No duplicates within this list.
    """
    seen_domains: set[str] = set()
    candidates: list[dict] = []

    for i, query in enumerate(queries, 1):
        print(f"  [{i:02d}/{len(queries):02d}] {query}")

        # Serper (Google search results).
        for result in serper_search.search(query):
            candidate = _parse_serper_result(result)
            if candidate:
                domain = sheets.normalize_domain(candidate["website"])
                if domain and domain not in seen_domains:
                    seen_domains.add(domain)
                    candidate["website"] = domain
                    candidates.append(candidate)

        # Google Places (catches businesses with poor/no SEO).
        for place in google_places.search_places(query):
            domain = sheets.normalize_domain(place.get("website", ""))
            if domain and domain not in seen_domains:
                seen_domains.add(domain)
                place["website"] = domain
                candidates.append(place)

    return candidates


def _deduplicate(candidates: list[dict], existing_domains: set[str]) -> list[dict]:
    """
    Remove candidates whose domains already exist in the Google Sheet.

    Pure Python — no LLM tokens. O(1) per lookup due to set structure.

    Args:
        candidates:       Raw candidates from _execute_searches.
        existing_domains: Normalised domains already in the sheet.

    Returns:
        Filtered list containing only net-new candidates.
    """
    return [
        c for c in candidates
        if sheets.normalize_domain(c.get("website", "")) not in existing_domains
    ]


def _validate_in_batches(
    candidates: list[dict],
    research: dict,
    region: str,
) -> list[dict]:
    """
    Validate candidates against ICP criteria using Gemini in batches.

    Each batch is one LLM call. Stops early once MAX_LEADS_PER_RUN is reached
    to avoid unnecessary API calls.

    Args:
        candidates: New candidates from _deduplicate.
        research:   ICP research dict from research_agent.
        region:     Target region string.

    Returns:
        List of validated lead dicts (company_name, website, country).
    """
    icp_summary = "\n".join(
        f"  - {p['type']}: {p['why_they_buy']}"
        for p in research.get("icp_profiles", [])
    )
    anti_icp_summary = "\n".join(
        f"  - {a['type']}: {a['reason']}"
        for a in research.get("anti_icp", [])
    )

    validated: list[dict] = []
    batch_size = config.VALIDATION_BATCH_SIZE
    total_batches = math.ceil(len(candidates) / batch_size)

    for batch_num, i in enumerate(range(0, len(candidates), batch_size), 1):

        # Stop early if we already have enough leads.
        if len(validated) >= config.MAX_LEADS_PER_RUN:
            print(f"  [VALIDATE] Max leads reached ({config.MAX_LEADS_PER_RUN}). Stopping early.")
            break

        batch = candidates[i : i + batch_size]
        print(f"  Batch {batch_num}/{total_batches} — {len(batch)} candidates...")

        # Send only the fields the LLM needs — reduces input tokens.
        slim_batch = [
            {
                "company_name": c.get("company_name", ""),
                "website": c.get("website", ""),
                "country": c.get("country", ""),
            }
            for c in batch
        ]

        prompt = _VALIDATION_PROMPT.format(
            company_summary=research.get("company_summary", "")[:300],
            products=", ".join(research.get("products", [])),
            region=region,
            icp_summary=icp_summary,
            anti_icp_summary=anti_icp_summary,
            n=len(slim_batch),
            candidates_json=json.dumps(slim_batch, ensure_ascii=False),
        )

        try:
            # Low temperature — we want precise, consistent filtering, not creativity.
            result = llm.generate_json(prompt, temperature=0.1)
            if isinstance(result, list):
                validated.extend(result)
        except ValueError as e:
            print(f"  [WARN] Batch {batch_num} validation failed: {e}. Skipping batch.")

    return validated


# ─── Parsing Helpers ──────────────────────────────────────────────────────────

def _parse_serper_result(result: dict) -> dict | None:
    """
    Parse a raw Serper organic search result into a candidate dict.

    Cleans the title to extract a usable company name by stripping common
    suffixes like '| LinkedIn', '- Home', '| Company Name', etc.

    Args:
        result: Raw result dict from Serper (keys: title, link, snippet).

    Returns:
        Candidate dict with keys: company_name, website, country.
        Returns None if essential fields are missing.
    """
    link = result.get("link", "").strip()
    title = result.get("title", "").strip()

    if not link or not title:
        return None

    # Strip common noise from page titles.
    name = title
    for separator in [" | ", " - ", " — ", " · "]:
        if separator in name:
            name = name.split(separator)[0].strip()
            break

    if not name:
        name = title

    return {
        "company_name": name,
        "website": link,   # Will be normalised by caller.
        "country": "",     # Determined by LLM during validation.
    }


# ─── Display Helpers ──────────────────────────────────────────────────────────

def _print_research_summary(research: dict) -> None:
    """Print a concise research summary to the console for user visibility."""
    icp_types  = ", ".join(p["type"] for p in research.get("icp_profiles", []))
    anti_types = ", ".join(a["type"] for a in research.get("anti_icp", []))

    print(f"\n{'─' * 60}")
    print("  ICP RESEARCH SUMMARY")
    print(f"{'─' * 60}")
    print(f"  Industry : {research.get('industry', 'N/A')}")
    print(f"  Products : {', '.join(research.get('products', []))}")
    print(f"  Target   : {icp_types}")
    print(f"  Exclude  : {anti_types}")
    print(f"{'─' * 60}\n")
