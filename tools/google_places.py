"""
tools/google_places.py
-----------------------
Places-style business search using Serper.dev's Google Maps endpoint.

Uses the same SERPER_API_KEY already configured for web search — no
additional API key or Google Cloud billing required.

Serper's /maps endpoint hits Google Maps directly and returns local
business listings: name, website, address, phone. Cost is the same
flat rate as a regular Serper search query (~$0.001/query), compared
to ~$0.37/query for the Google Places API.

Why use this alongside Serper web search?
  Many industrial businesses have minimal web presence but are registered
  on Google Maps. This catches companies that organic search would miss
  due to poor SEO or no indexed website.

  Companies with no website in the response are skipped — a website is
  required for deduplication and as a lead data point.
"""

import requests
import config

_MAPS_ENDPOINT = "https://google.serper.dev/maps"


def search_places(query: str) -> list[dict]:
    """
    Search for businesses using Serper's Google Maps endpoint.

    Results without a website are excluded to maintain data quality.
    Uses the same SERPER_API_KEY as the web search tool.

    Args:
        query: Natural language query (e.g. "industrial equipment suppliers in Chicago").

    Returns:
        List of dicts with keys:
          - 'company_name' (str): Business name from Google Maps.
          - 'website'      (str): Company website URL.
          - 'country'      (str): Country extracted from the address.
        Returns [] if API key is not configured or the call fails.
    """
    if not config.SERPER_API_KEY or config.SERPER_API_KEY.startswith("your_"):
        print("[WARN] SERPER_API_KEY not configured — skipping Places search.")
        return []

    try:
        response = requests.post(
            _MAPS_ENDPOINT,
            headers={
                "X-API-KEY": config.SERPER_API_KEY,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": config.SEARCHES_PER_QUERY},
            timeout=15,
        )
        response.raise_for_status()
        raw_results = response.json().get("places", [])

    except requests.exceptions.Timeout:
        print(f"[WARN] Serper Maps timed out for query: '{query}'")
        return []
    except requests.exceptions.RequestException as e:
        print(f"[WARN] Serper Maps request failed for query '{query}': {e}")
        return []

    places = []
    for place in raw_results:
        website = place.get("website", "").strip()

        if not website:
            continue  # Skip — website required for dedup and data quality.

        places.append({
            "company_name": place.get("title", ""),
            "website": website,
            "country": _extract_country(place.get("address", "")),
        })

    return places


def _extract_country(address: str) -> str:
    """
    Extract the country from an address string.

    Serper Maps returns addresses in the format:
      "123 Main St, Houston, TX 77001, United States"
    The country is consistently the last comma-separated component.

    Args:
        address: Full address string from Serper Maps result.

    Returns:
        Country string, or empty string if not determinable.
    """
    if not address:
        return ""
    parts = [p.strip() for p in address.split(",")]
    return parts[-1] if parts else ""
