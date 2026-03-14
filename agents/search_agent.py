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
import re

from agents import research_agent
from tools import llm, serper_search, google_places, sheets
import config


# ─── Garbage Domain Blocklist ─────────────────────────────────────────────────
# These domains should NEVER appear as leads regardless of what search returns.
# Checked before LLM validation — saves tokens and prevents garbage in sheet.
_GARBAGE_DOMAINS = {
    # Reference / encyclopaedia
    "wikipedia.org", "wikimedia.org",
    # Maps / navigation
    "mapquest.com", "maps.google.com", "openstreetmap.org",
    # Social media
    "linkedin.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "tiktok.com",
    # Job / review sites
    "glassdoor.com", "indeed.com", "ziprecruiter.com",
    # Business directories
    "yellowpages.com", "yelp.com", "manta.com", "bbb.org",
    "dnb.com", "zoominfo.com", "crunchbase.com", "bloomberg.com",
    "hoovers.com", "bizapedia.com", "corporationwiki.com",
    # Generic web
    "google.com", "bing.com", "yahoo.com", "reddit.com",
    "quora.com", "medium.com", "wordpress.com", "blogspot.com",
    "amazon.com", "ebay.com", "alibaba.com",
}


# ─── Prompts ──────────────────────────────────────────────────────────────────

_QUERY_GEN_PROMPT = """
Generate specific Google search queries to find B2B companies that would purchase
products from this seller.

SELLER: {company_name}

TARGET REGION: {region}

ICP TYPES TO FIND (generate queries for EACH type below):
{icp_types}

PRODUCT GROUPS (generate one "distributor" and one "supplier" query for EACH group
below — these are the core queries and must all be covered):
{product_groups}

BUYER VOCABULARY (generate additional queries using these terms — they match how
buyers describe their own needs on their websites, capturing companies that
product-name queries would miss):
{buyer_vocabulary}

INSTRUCTIONS:
  - Use the region EXACTLY as given: "{region}". Do NOT substitute, rotate, or
    replace it with city names, abbreviations, or subregions.
  - PRODUCT GROUP queries: for each group, generate one query in the format
    "[region] [product group] distributor". Every group must appear in at least one query.
  - BUYER VOCABULARY queries: for each vocabulary term, generate one query in the format
    "[region] [vocab term] distributor".
  - ICP TYPE queries: for each non-distributor ICP type, generate additional
    queries using that ICP type's keywords and industry context.
  - Queries must return company websites, not articles, news, or directories.
  - Make each query distinct — no near-duplicates.

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

ALSO EXCLUDE — STRICTLY:
  - Companies that clearly manufacture or produce the same products as the seller.
  - Companies obviously outside the target region: {region}
  - Companies with no clear industry relevance.
  - Duplicate company names (same company, different URL).
  - Government bodies, municipalities, city departments, public utilities.
  - Reference websites (wikipedia.org, mapquest.com, city hall sites, etc.).
  - Any website that is not the company's own official website.
  - Very large Fortune 100 multinationals with rigid vendor approval processes
    and established global supplier programs — cold outreach is rarely effective
    with these companies. Focus on mid-size companies instead.

CANDIDATES ({n} companies):
{candidates_json}

Each candidate has: company_name, website, country, and snippet (Google's description
of the page — may be empty for some candidates). Use the snippet as your primary signal
for judging relevance. If snippet is empty, use the company name and domain.

For each qualifying company:
  - Use the snippet, name, and domain together to determine relevance.
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
    validated, rejected = _validate_in_batches(new_candidates, research, region)
    print(f"[VALIDATE] {len(validated)} leads passed, {len(rejected)} rejected.")

    # ── Step 7: Cap at MAX_LEADS_PER_RUN ──────────────────────────────────────
    final_leads = validated[: config.MAX_LEADS_PER_RUN]

    # ── Step 8: Write validated leads to Google Sheets ────────────────────────
    print(f"\n[SHEETS] Writing {len(final_leads)} leads to Google Sheets...")
    written = sheets.append_leads(spreadsheet_id, final_leads)
    print(f"[SHEETS] {written} leads written successfully.")

    # ── Step 9: Write rejected companies to Rejected Companies tab ────────────
    if rejected:
        print(f"[SHEETS] Writing {len(rejected)} rejected companies to 'Rejected Companies' tab...")
        sheets.append_rejected_leads(spreadsheet_id, rejected)

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

    # Product groups: grouped families of products (e.g. "industrial valves and fittings").
    # Falls back to the individual products list if product_groups is not in the cache.
    _raw_groups = research.get("product_groups") or research.get("products") or []
    all_product_groups: list[str] = [str(g) for g in _raw_groups if g]
    product_groups = "\n".join(f"  - {g}" for g in all_product_groups)

    # Buyer vocabulary: how target buyers describe their own needs on their websites.
    # Drives SEO-matched queries that surface companies not found by product name alone.
    _raw_vocab = research.get("buyer_vocabulary") or []
    all_buyer_vocab: list[str] = [str(v) for v in _raw_vocab if v]
    buyer_vocabulary = "\n".join(f"  - {v}" for v in all_buyer_vocab)

    # Use a custom prompt template from the cache if one exists, otherwise
    # fall back to the built-in default. This allows per-company query strategies
    # without touching any code — just add "query_prompt_template" to the cache JSON.
    prompt_template = str(research.get("query_prompt_template") or _QUERY_GEN_PROMPT)
    prompt = prompt_template.format(
        company_name=research.get("company_summary", "")[:200],
        region=region,
        icp_types=icp_types,
        product_groups=product_groups,
        buyer_vocabulary=buyer_vocabulary,
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

    Each candidate is tagged with 'source' ("Google Search" or "Google Places")
    and 'search_query' (the query that produced it) for sheet tracking.

    Args:
        queries: List of search query strings.

    Returns:
        List of raw candidate dicts with keys: company_name, website, country,
        source, search_query. All domains are normalised. No duplicates within this list.
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
                if domain and domain not in seen_domains and not _is_garbage_domain(domain):
                    seen_domains.add(domain)
                    candidate["website"] = domain
                    candidate["source"] = "Google Search"
                    candidate["search_query"] = query
                    candidates.append(candidate)

        # Google Places (catches businesses with poor/no SEO).
        for place in google_places.search_places(query):
            domain = sheets.normalize_domain(place.get("website", ""))
            if domain and domain not in seen_domains and not _is_garbage_domain(domain):
                seen_domains.add(domain)
                place["website"] = domain
                place["source"] = "Google Places"
                place["search_query"] = query
                candidates.append(place)

    return candidates


def _deduplicate(candidates: list[dict], existing_domains: set[str]) -> list[dict]:
    """
    Remove candidates already in the sheet and deduplicate by company name.

    Two checks — both pure Python, zero LLM tokens:
      1. Domain check: skip if domain already in the Google Sheet.
      2. Name check: skip if a company with the same normalised name already
         passed (catches same company appearing with two different URLs,
         e.g. exxonmobil.com and corporate.exxonmobil.com).

    Args:
        candidates:       Raw candidates from _execute_searches.
        existing_domains: Normalised domains already in the sheet.

    Returns:
        Filtered list containing only net-new, name-unique candidates.
    """
    seen_names: set[str] = set()
    result: list[dict] = []

    for c in candidates:
        domain = sheets.normalize_domain(c.get("website", ""))
        if domain in existing_domains:
            continue
        name_key = _normalize_name(c.get("company_name", ""))
        if name_key and name_key in seen_names:
            continue
        if name_key:
            seen_names.add(name_key)
        result.append(c)

    return result


def _validate_in_batches(
    candidates: list[dict],
    research: dict,
    region: str,
) -> tuple[list[dict], list[dict]]:
    """
    Validate candidates against ICP criteria using Gemini in batches.

    Each batch is one LLM call. Stops early once MAX_LEADS_PER_RUN is reached
    to avoid unnecessary API calls.

    After LLM validation, source and search_query are re-attached from the
    original candidates using domain lookup (zero extra LLM tokens).

    Only candidates that were actually sent to the LLM are counted as rejected.
    Candidates skipped due to early stopping are not logged as rejected.

    Args:
        candidates: New candidates from _deduplicate (include source, search_query).
        research:   ICP research dict from research_agent.
        region:     Target region string.

    Returns:
        Tuple of:
          - List of validated lead dicts (company_name, website, country, source, search_query).
          - List of rejected candidate dicts (same format — sent to LLM but did not pass).
    """
    # Build domain → candidate lookup for re-attaching metadata and building rejected list.
    candidate_by_domain: dict[str, dict] = {}
    for c in candidates:
        domain = sheets.normalize_domain(c.get("website", ""))
        if domain:
            candidate_by_domain[domain] = c

    icp_summary = "\n".join(
        f"  - {p['type']}: {p['why_they_buy']}"
        for p in research.get("icp_profiles", [])
    )
    anti_icp_summary = "\n".join(
        f"  - {a['type']}: {a['reason']}"
        for a in research.get("anti_icp", [])
    )

    validated: list[dict] = []
    processed_domains: set[str] = set()   # domains actually sent to the LLM
    validated_domains: set[str] = set()   # domains that passed validation
    batch_size = config.VALIDATION_BATCH_SIZE
    total_batches = math.ceil(len(candidates) / batch_size)

    for batch_num, i in enumerate(range(0, len(candidates), batch_size), 1):

        # Stop early if we already have enough leads.
        if len(validated) >= config.MAX_LEADS_PER_RUN:
            print(f"  [VALIDATE] Max leads reached ({config.MAX_LEADS_PER_RUN}). Stopping early.")
            break

        batch = candidates[i : i + batch_size]
        print(f"  Batch {batch_num}/{total_batches} — {len(batch)} candidates...")

        # Track every domain sent to the LLM so we can identify rejections later.
        for c in batch:
            domain = sheets.normalize_domain(c.get("website", ""))
            if domain:
                processed_domains.add(domain)

        # Send only the fields the LLM needs — reduces input tokens.
        # snippet is included for Serper results; empty string for Places results.
        slim_batch = [
            {
                "company_name": c.get("company_name", ""),
                "website": c.get("website", ""),
                "country": c.get("country", ""),
                "snippet": c.get("snippet", ""),
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
                # Re-attach source and search_query from pre-validation metadata.
                for lead in result:
                    domain = sheets.normalize_domain(lead.get("website", ""))
                    if domain:
                        validated_domains.add(domain)
                        meta = candidate_by_domain.get(domain, {})
                        lead["source"] = meta.get("source", "")
                        lead["search_query"] = meta.get("search_query", "")
                validated.extend(result)
        except ValueError as e:
            print(f"  [WARN] Batch {batch_num} validation failed: {e}. Skipping batch.")

    # Rejected = processed by LLM but not in the validated set.
    rejected: list[dict] = [
        candidate_by_domain[d]
        for d in processed_domains
        if d not in validated_domains and d in candidate_by_domain
    ]

    return validated, rejected


# ─── Filter Helpers ───────────────────────────────────────────────────────────

def _is_garbage_domain(domain: str) -> bool:
    """
    Return True if the domain should never be a lead.

    Checks against the blocklist and also flags government/municipal domains
    (.gov, city hall patterns) and domains clearly not company websites.

    Args:
        domain: Normalised root domain string (e.g. 'wikipedia.org').

    Returns:
        True if the domain should be discarded before LLM validation.
    """
    if not domain:
        return True
    # Exact blocklist match.
    if domain in _GARBAGE_DOMAINS:
        return True
    # Any subdomain of a blocklisted domain.
    if any(domain.endswith("." + g) for g in _GARBAGE_DOMAINS):
        return True
    # Government / public sector domains.
    if domain.endswith(".gov") or domain.endswith(".gov.uk") or domain.endswith(".gc.ca"):
        return True
    return False


def _normalize_name(name: str) -> str:
    """
    Normalise a company name for duplicate detection.

    Lowercases, strips legal suffixes (LLC, Inc, Ltd, Corp, Co),
    and collapses whitespace so 'ExxonMobil Corp' and 'ExxonMobil LLC'
    are treated as the same company.

    Args:
        name: Raw company name string.

    Returns:
        Normalised string key, or empty string if name is blank.
    """
    if not name:
        return ""
    n = name.lower().strip()
    # Remove common legal suffixes.
    n = re.sub(r"\b(llc|inc|ltd|corp|co|gmbh|plc|ag|bv|sas|srl|pty|limited|incorporated|corporation|company)\b\.?", "", n)
    # Collapse whitespace.
    n = re.sub(r"\s+", " ", n).strip()
    return n


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
        "snippet": result.get("snippet", "")[:200],  # Google's description of the page.
    }


# ─── Display Helpers ──────────────────────────────────────────────────────────

def _print_research_summary(research: dict) -> None:
    """Print a concise research summary to the console for user visibility."""
    icp_types  = ", ".join(p["type"] for p in research.get("icp_profiles", []))
    anti_types = ", ".join(a["type"] for a in research.get("anti_icp", []))

    products = research.get("products", [])
    # Group products 4 per line for compact display.
    product_lines = [
        ", ".join(products[i : i + 4])
        for i in range(0, len(products), 4)
    ]
    print(f"\n{'─' * 60}")
    print("  ICP RESEARCH SUMMARY")
    print(f"{'─' * 60}")
    print(f"  Industry : {research.get('industry', 'N/A')}")
    print(f"  Products :")
    for line in product_lines:
        print(f"    {line}")
    print(f"  Target   : {icp_types}")
    print(f"  Exclude  : {anti_types}")
    print(f"{'─' * 60}\n")
