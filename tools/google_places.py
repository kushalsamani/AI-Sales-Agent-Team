"""
tools/google_places.py
-----------------------
Wrapper around the Google Places Text Search API.

Monthly credit: $200 (resets each month). Requires a Google Cloud project
with billing enabled — but you won't be charged unless you exceed $200/month.

Enable the Places API at:
  https://console.cloud.google.com → APIs & Services → Places API

Why use this alongside Serper?
  Many industrial businesses have minimal web presence but are registered on
  Google Maps / Places. This catches companies that Serper would miss due to
  poor SEO or no website at all.

  Note: Companies with no website are skipped — a website is required for
  deduplication and as a lead data point.
"""

import requests
import config

_TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
_DETAILS_URL     = "https://maps.googleapis.com/maps/api/place/details/json"


def search_places(query: str) -> list[dict]:
    """
    Search for businesses using Google Places Text Search.

    For each result, attempts to retrieve the company website. Results without
    a discoverable website are excluded to maintain data quality.

    Args:
        query: Natural language query (e.g., "industrial pipe distributors in Texas").

    Returns:
        List of dicts with keys:
          - 'company_name' (str): Business name from Google Places.
          - 'website'      (str): Root domain of the company website.
          - 'country'      (str): Country extracted from the formatted address.
        Returns [] if API key is not configured or the call fails.
    """
    if not config.GOOGLE_PLACES_API_KEY or config.GOOGLE_PLACES_API_KEY.startswith("your_"):
        print("[WARN] GOOGLE_PLACES_API_KEY not configured — skipping Places search.")
        return []

    try:
        response = requests.get(
            _TEXT_SEARCH_URL,
            params={"query": query, "key": config.GOOGLE_PLACES_API_KEY},
            timeout=15,
        )
        response.raise_for_status()
        raw_results = response.json().get("results", [])

    except requests.exceptions.Timeout:
        print(f"[WARN] Google Places timed out for query: '{query}'")
        return []
    except requests.exceptions.RequestException as e:
        print(f"[WARN] Google Places request failed for query '{query}': {e}")
        return []

    places = []
    for place in raw_results:
        website = place.get("website")
        place_id = place.get("place_id")

        # Text Search doesn't always return website — fetch from Details if missing.
        if not website and place_id:
            website = _fetch_website(place_id)

        if not website:
            continue  # Skip — website required for dedup and data quality.

        places.append({
            "company_name": place.get("name", ""),
            "website": website,
            "country": _extract_country(place.get("formatted_address", "")),
        })

    return places


def _fetch_website(place_id: str) -> str | None:
    """
    Fetch the website field for a place using its place_id via the Details API.

    This is a targeted call requesting only the 'website' field to minimise
    API cost (billed per field mask in the new Places API).

    Args:
        place_id: Google Places place_id string.

    Returns:
        Website URL string, or None if unavailable.
    """
    try:
        response = requests.get(
            _DETAILS_URL,
            params={
                "place_id": place_id,
                "fields": "website",
                "key": config.GOOGLE_PLACES_API_KEY,
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get("result", {}).get("website")
    except requests.exceptions.RequestException:
        return None


def _extract_country(formatted_address: str) -> str:
    """
    Extract the country from a Google-formatted address string.

    Google Places consistently places the country as the last comma-separated
    component of the formatted address.
    e.g. "123 Main St, Houston, TX 77001, United States" → "United States"

    Args:
        formatted_address: Full address string from Google Places result.

    Returns:
        Country string, or empty string if not determinable.
    """
    if not formatted_address:
        return ""
    parts = [p.strip() for p in formatted_address.split(",")]
    return parts[-1] if parts else ""
