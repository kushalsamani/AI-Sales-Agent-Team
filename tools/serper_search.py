"""
tools/serper_search.py
-----------------------
Wrapper around the Serper.dev Google Search API.

Free tier: 2,500 searches/month. No credit card required.
Sign up and get your key at: https://serper.dev

This module is intentionally simple — it just executes a query and returns
raw organic results. All filtering and validation happens in the search agent.
"""

import requests
import config

_ENDPOINT = "https://google.serper.dev/search"


def search(query: str, num_results: int | None = None) -> list[dict]:
    """
    Run a Google search via Serper.dev and return organic results.

    If SERPER_API_KEY is not configured, logs a warning and returns an empty
    list so the pipeline can still continue with other sources (e.g. Places).

    Args:
        query:       The search query string.
        num_results: Number of results to request. Defaults to SEARCHES_PER_QUERY
                     from config.

    Returns:
        List of result dicts. Each dict contains at minimum:
          - 'title' (str): Page/company title.
          - 'link'  (str): URL of the result.
          - 'snippet' (str): Short description snippet.
        Returns [] on API error or missing key.
    """
    if not config.SERPER_API_KEY or config.SERPER_API_KEY.startswith("your_"):
        print("[WARN] SERPER_API_KEY not configured — skipping Serper search.")
        return []

    num = num_results or config.SEARCHES_PER_QUERY

    try:
        response = requests.post(
            _ENDPOINT,
            headers={
                "X-API-KEY": config.SERPER_API_KEY,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": num},
            timeout=15,
        )
        response.raise_for_status()
        return response.json().get("organic", [])

    except requests.exceptions.Timeout:
        print(f"[WARN] Serper timed out for query: '{query}'")
        return []
    except requests.exceptions.RequestException as e:
        print(f"[WARN] Serper request failed for query '{query}': {e}")
        return []
