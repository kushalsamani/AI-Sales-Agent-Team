"""
agents/research_agent.py
------------------------
Researches a company and its industry to produce a structured ICP
(Ideal Customer Profile) analysis used by the search agent.

The agent reasons like a senior B2B sales developer:
  - It identifies who BUYS the company's products (not who manufactures them).
  - It flags anti-ICPs (competitors, same-product manufacturers) to exclude.
  - It produces search query templates and recommends search sources.

TOKEN EFFICIENCY:
  Research is cached to disk (cache/research/<company-slug>.json).
  The LLM is called ONCE per company. All subsequent runs load from cache.
  Use force=True (or --force-research CLI flag) to re-run when needed.
"""

import json
import os
import re

from tools import llm
import config


# ─── Prompt ───────────────────────────────────────────────────────────────────

_RESEARCH_PROMPT = """
You are a senior B2B sales strategist with deep expertise across industrial,
chemical, manufacturing, and process engineering sectors.

Research the company below and produce a structured ICP (Ideal Customer Profile)
analysis. Your goal: identify which types of companies are most likely to PURCHASE
from this company — not companies that make the same products.

Company: {company_name}

Think through:
1. What does this company manufacture or sell? What industry?
2. Who are their actual BUYERS? (distributors, EPCs, plant operators, contractors, etc.)
3. Who should be EXCLUDED? (manufacturers of the same product, raw material suppliers)
4. What search terms will find real buyers in any given region?

Return a single valid JSON object — no markdown, no extra text:

{{
  "company_summary": "One concise paragraph: what they make, who uses it, what industry.",
  "products": ["product or service 1", "product or service 2"],
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
      "reason": "Why targeting them would be a waste (e.g. they manufacture the same product)"
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
  - 12 to 16 search query templates. Use {{region}} as the placeholder.
  - Templates must be specific enough to return company websites, not articles.
  - Do not invent facts. Reason from real industry knowledge.
  - Think about which company SIZES and TYPES actually buy industrial products.
"""


# ─── Public Interface ─────────────────────────────────────────────────────────

def get_research(company_name: str, force: bool = False) -> dict:
    """
    Return ICP research for a company. Loads from cache when available.

    On first call (or when force=True), queries Gemini and caches the result.
    All subsequent calls load from the JSON cache — zero LLM tokens spent.

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

    print(f"[RESEARCH] Running ICP research for '{company_name}' (this runs once and is cached)...")
    research = _run_research(company_name)
    _save_cache(cache_path, research)
    print(f"[RESEARCH] Done. Cached to: {cache_path}")

    return research


# ─── Internal ─────────────────────────────────────────────────────────────────

def _run_research(company_name: str) -> dict:
    """
    Call Gemini to produce structured ICP research for the given company.

    Uses a low temperature (0.2) for factual, consistent output.

    Args:
        company_name: Company name to research.

    Returns:
        Parsed research dict.

    Raises:
        ValueError: If the LLM response is not valid JSON.
    """
    prompt = _RESEARCH_PROMPT.format(company_name=company_name)
    result = llm.generate_json(prompt, temperature=0.2)

    if not isinstance(result, dict):
        raise ValueError(
            f"[RESEARCH ERROR] Expected a JSON object from Gemini, got: {type(result)}"
        )

    return result


def _cache_path(company_name: str) -> str:
    """
    Build the file path for a company's research cache.

    Converts the company name to a safe slug (lowercase, hyphens only).
    Example: 'Advect Process Systems Canada Ltd.' → 'advect-process-systems-canada-ltd'

    Args:
        company_name: Full company name.

    Returns:
        Absolute path string to the .json cache file.
    """
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    slug = re.sub(r"[^\w\s-]", "", company_name.lower().strip())
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return os.path.join(config.CACHE_DIR, f"{slug}.json")


def _save_cache(path: str, data: dict) -> None:
    """
    Write research data to the cache file as formatted JSON.

    Args:
        path: Full file path to write.
        data: Research dict to serialise.
    """
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
