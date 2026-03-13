"""
agents/research_agent.py
------------------------
Researches a company and its industry to produce a structured ICP
(Ideal Customer Profile) analysis used by the search agent.

Web-grounded research flow (runs once per company, then cached):
  1. Serper search  — finds the company's official website + supporting results.
  2. Website fetch  — scrapes homepage and one product/about page.
  3. Gemini analysis — ICP analysis based ONLY on real gathered information.
                       Never guesses from company name alone.

The agent reasons like a senior B2B sales developer:
  - It identifies who BUYS the company's products (not who manufactures them).
  - It flags anti-ICPs (competitors, same-product manufacturers) to exclude.
  - It produces search query templates and recommends search sources.

TOKEN EFFICIENCY:
  Research runs once per company and is cached to disk.
  The LLM is called ONCE per company. All subsequent runs load from cache.
  Use force=True (or --force-research CLI flag) to re-run when needed.
"""

import json
import os
import re
import requests
from html.parser import HTMLParser

from tools import llm, serper_search
import config


# ─── Domains to skip when identifying company website ─────────────────────────
_SKIP_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "instagram.com",
    "youtube.com", "wikipedia.org", "bloomberg.com", "crunchbase.com",
    "glassdoor.com", "indeed.com", "yellowpages.com", "yelp.com",
    "dnb.com", "zoominfo.com", "manta.com", "bizapedia.com",
}

# Pages to try after homepage for richer product/service info.
_PRODUCT_PATHS = ["/products", "/services", "/solutions", "/about", "/what-we-do", "/about-us"]

# Max characters of website text to include in the research prompt.
_MAX_HOMEPAGE_CHARS  = 3000
_MAX_SUBPAGE_CHARS   = 2000
_MAX_SNIPPET_CHARS   = 1500


# ─── Prompt ───────────────────────────────────────────────────────────────────

_RESEARCH_PROMPT = """
You are a senior B2B sales strategist with deep expertise across industrial,
chemical, manufacturing, process engineering, and distribution sectors.

You have been given REAL, VERIFIED information about a company gathered directly
from their website and search results. Use ONLY this information for your analysis.
Do NOT rely on general assumptions or guess from the company name.

Company: {company_name}

━━━ GATHERED COMPANY INFORMATION ━━━
{company_info}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Based strictly on the information above, produce a structured ICP analysis.
Your goal: identify which types of companies are most likely to PURCHASE from
this company — not companies that manufacture the same products.

Think through:
1. What does this company ACTUALLY sell (based on the info above)?
2. Who are their real BUYERS? (distributors, EPCs, plant operators, contractors...)
3. Who should be EXCLUDED? (manufacturers of the same product, raw material suppliers)
4. What search terms will find real buyers in any given region?

CRITICAL — DISTRIBUTOR DISTINCTION:
  There are two types of distributors. Get this right:
  - INCLUDE as ICP: Specialized distributors focused on industrial piping,
    process equipment, fluid handling, valves, or corrosion-resistant materials.
    These companies RESELL specialized products to end-users in their region
    and are key buyers.
  - EXCLUDE as anti-ICP: General-purpose industrial distributors that carry
    thousands of unrelated products (like Grainger or general hardware suppliers).
    They are unlikely to stock niche specialized products.
  If this company sells specialized industrial products, specialized distributors
  must appear as an ICP profile.

Return a single valid JSON object — no markdown, no extra text:

{{
  "company_summary": "One concise paragraph: what they make, who uses it, what industry.",
  "products": ["exact product or service from their website"],
  "industry": "Primary industry name",
  "icp_profiles": [
    {{
      "type": "Company type (e.g. Industrial Distributor)",
      "description": "Who these companies are in plain language.",
      "why_they_buy": "Specific reason they would purchase from this company.",
      "company_size": "SME / Large / Any",
      "example_keywords": ["keyword1", "keyword2", "keyword3"]
    }}
  ],
  "anti_icp": [
    {{
      "type": "Company type to avoid targeting",
      "reason": "Why targeting them would be wasteful (e.g. they manufacture the same product)"
    }}
  ],
  "search_query_templates": [
    "query using {{region}} as placeholder for the target region"
  ],
  "recommended_search_sources": ["serper", "google_places"]
}}

Quality rules:
  - 3 to 6 ICP profiles covering distinct buyer types.
  - 2 to 4 anti-ICP entries. Always include manufacturers of the same product.
  - 12 to 16 search query templates using {{region}} as placeholder.
  - Templates must find company websites, not articles or news.
  - Products list must reflect ACTUAL products found in the gathered info above.
  - Do not invent anything not supported by the gathered information.
"""


# ─── Public Interface ─────────────────────────────────────────────────────────

def get_research(company_name: str, force: bool = False) -> dict:
    """
    Return ICP research for a company. Loads from cache when available.

    On first call (or when force=True), gathers real web data then calls Gemini.
    All subsequent calls load from the JSON cache — zero LLM or web tokens spent.

    Args:
        company_name: Full company name to research.
        force:        If True, bypass cache and re-run research from scratch.

    Returns:
        Dict with keys:
          company_summary, products, industry,
          icp_profiles, anti_icp,
          search_query_templates, recommended_search_sources

    Raises:
        ValueError: If Gemini returns malformed JSON.
    """
    cache_path = _cache_path(company_name)

    if not force and os.path.exists(cache_path):
        print(f"[RESEARCH] Loading cached research for '{company_name}'...")
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)

    print(f"[RESEARCH] Running web-grounded research for '{company_name}'...")
    print(f"[RESEARCH] This runs once and is cached — future runs are instant.")

    company_info = _gather_company_info(company_name)
    research = _run_research(company_name, company_info)
    _save_cache(cache_path, research)
    print(f"[RESEARCH] Done. Cached to: {cache_path}")

    return research


# ─── Web Gathering ────────────────────────────────────────────────────────────

def _gather_company_info(company_name: str) -> str:
    """
    Gather real company information from the web.

    Step 1: Search for the company on Serper (2 queries).
    Step 2: Identify the official company website from results.
    Step 3: Fetch homepage text.
    Step 4: Fetch one product/about subpage for richer detail.
    Step 5: Compile everything into a single text block for the LLM.

    Args:
        company_name: Full company name.

    Returns:
        A text block containing website content + search snippets.
        Falls back to search snippets only if the website cannot be fetched.
    """
    # Two targeted searches: brand name + products/services.
    print(f"[RESEARCH]   Searching web for company info...")
    results_brand    = serper_search.search(f'"{company_name}"', num_results=5)
    results_products = serper_search.search(f'{company_name} products services', num_results=5)
    all_results = results_brand + results_products

    # Find the official website.
    website_url = _find_company_website(all_results)

    sections = []

    # Fetch and include website content.
    if website_url:
        print(f"[RESEARCH]   Found website: {website_url}")
        homepage_text = _fetch_page_text(website_url)
        if homepage_text:
            sections.append(f"HOMEPAGE ({website_url}):\n{homepage_text[:_MAX_HOMEPAGE_CHARS]}")

        # Try subpages for product/service details.
        base = website_url.rstrip("/")
        for path in _PRODUCT_PATHS:
            subpage_text = _fetch_page_text(base + path)
            if subpage_text and len(subpage_text) > 200:
                sections.append(f"SUBPAGE ({base + path}):\n{subpage_text[:_MAX_SUBPAGE_CHARS]}")
                print(f"[RESEARCH]   Also fetched: {base + path}")
                break  # One subpage is enough.
    else:
        print(f"[RESEARCH]   Could not identify official website. Using search snippets only.")

    # Always include search snippets as supporting context.
    snippets = "\n".join(
        f"- {r.get('title', '')}: {r.get('snippet', '')}"
        for r in all_results[:8]
        if r.get("snippet")
    )
    if snippets:
        sections.append(f"SEARCH RESULT SNIPPETS:\n{snippets[:_MAX_SNIPPET_CHARS]}")

    if not sections:
        return f"No web information could be gathered for '{company_name}'. Use general industry knowledge carefully."

    return "\n\n".join(sections)


def _find_company_website(results: list[dict]) -> str | None:
    """
    Identify the company's official website from a list of search results.

    Skips social media, directories, and aggregator sites.
    Returns the first result URL that looks like a real company website.

    Args:
        results: List of Serper result dicts (keys: title, link, snippet).

    Returns:
        URL string of the official website, or None if not found.
    """
    for result in results:
        link = result.get("link", "")
        if not link:
            continue
        domain = _extract_domain(link)
        if domain and not any(skip in domain for skip in _SKIP_DOMAINS):
            # Return the root URL, not a deep page link.
            parsed = link.split("/")
            root = "/".join(parsed[:3])  # e.g. "https://advect.ca"
            return root
    return None


def _fetch_page_text(url: str) -> str:
    """
    Fetch a webpage and extract readable text content.

    Uses Python's built-in html.parser — no extra dependencies needed.
    Strips scripts, styles, nav, and footer elements to keep content clean.

    Args:
        url: Full URL to fetch.

    Returns:
        Extracted plain text string, or empty string on failure.
    """
    try:
        response = requests.get(
            url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"},
        )
        response.raise_for_status()
        extractor = _HTMLTextExtractor()
        extractor.feed(response.text)
        # Collapse whitespace and return clean text.
        text = re.sub(r"\s+", " ", extractor.get_text()).strip()
        return text
    except Exception:
        return ""


# ─── HTML Text Extraction ─────────────────────────────────────────────────────

class _HTMLTextExtractor(HTMLParser):
    """
    Minimal HTML parser that extracts readable text.
    Skips script, style, nav, header, and footer tags.
    No external dependencies — uses Python's built-in html.parser.
    """

    _SKIP_TAGS = {"script", "style", "nav", "header", "footer", "noscript", "head"}

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth: int  = 0

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


# ─── LLM Research ─────────────────────────────────────────────────────────────

def _run_research(company_name: str, company_info: str) -> dict:
    """
    Call Gemini to produce structured ICP research, grounded in real company data.

    Args:
        company_name: Company name (for context in the prompt).
        company_info: Real information gathered from the web.

    Returns:
        Parsed research dict.

    Raises:
        ValueError: If the LLM response is not valid JSON.
    """
    prompt = _RESEARCH_PROMPT.format(
        company_name=company_name,
        company_info=company_info,
    )
    result = llm.generate_json(prompt, temperature=0.2)

    if not isinstance(result, dict):
        raise ValueError(
            f"[RESEARCH ERROR] Expected a JSON object from Gemini, got: {type(result)}"
        )

    return result


# ─── Cache Helpers ────────────────────────────────────────────────────────────

def _cache_path(company_name: str) -> str:
    """
    Build the file path for a company's research cache.

    Converts the company name to a safe slug (lowercase, hyphens only).
    Example: 'Advect Process Systems Canada Ltd.' → 'advect-process-systems-canada-ltd'
    """
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    slug = re.sub(r"[^\w\s-]", "", company_name.lower().strip())
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return os.path.join(config.CACHE_DIR, f"{slug}.json")


def _save_cache(path: str, data: dict) -> None:
    """Write research data to the cache file as formatted JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _extract_domain(url: str) -> str:
    """Extract the root domain from a URL for skip-list checking."""
    try:
        # Remove scheme, get netloc, strip www.
        domain = url.split("//")[-1].split("/")[0].lower()
        return domain.replace("www.", "")
    except Exception:
        return ""
