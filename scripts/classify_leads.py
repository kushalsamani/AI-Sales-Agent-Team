"""
scripts/classify_leads.py
--------------------------
Classify leads already in the Leads tab as Strong, Weak, or Not a Lead.

Uses Gemini's URL Context tool: you give it a website URL, Gemini fetches and
reads the site itself, then classifies the company based on what it finds.

Two new columns are written back to the Leads tab:
  - classification:        Strong / Weak / Not a Lead
  - classification_reason: One sentence explaining why.

Usage:
  python scripts/classify_leads.py --company "Your Company Name"
  python scripts/classify_leads.py --company "Your Company Name" --reclassify

Arguments:
  --company      Company name. Must match an existing research cache and spreadsheet.
  --reclassify   Re-classify leads that already have a classification (default: skip them).
"""

import argparse
import json
import os
import sys

# Allow imports from the project root (agents/, tools/, config.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.genai import types

from agents.research_agent import get_research
from tools import llm, sheets
import config


# ─── Prompt ───────────────────────────────────────────────────────────────────

_CLASSIFY_PROMPT = """
You are a B2B sales analyst. Your job is to classify whether a company is a
good sales lead for the seller described below.

SELLER: {seller_company}
SELLER PRODUCTS: {products}
TARGET BUYERS (ICP types): {icp_types}
DO NOT TARGET (Anti-ICP): {anti_icp}

COMPANY TO CLASSIFY: {company_name}
WEBSITE: {url}

Visit the website at {url} and based on what this company sells and does,
classify them as one of:

  "Strong"     - Clearly buys or would buy the seller's products. Right
                 industry, right company type, strong product or use-case match.

  "Weak"       - Possibly relevant but not clearly a buyer. Tangentially
                 related industry, unclear product overlap, or insufficient
                 information on the website.

  "Not a Lead" - Clearly wrong. Completely unrelated industry, the company
                 manufactures the same competing products, or it is not a
                 real operating business.

If the website cannot be fetched or loaded, classify as "Weak" with reason
"Website could not be accessed — manual review needed."

Return ONLY valid JSON with no markdown fences:
{{"classification": "Strong" or "Weak" or "Not a Lead", "reason": "One sentence."}}
"""


# ─── Main Logic ───────────────────────────────────────────────────────────────

def classify_leads(company_name: str, reclassify: bool = False) -> None:
    """
    Classify all leads in the Leads tab and write results back to the sheet.

    Reads the research cache for seller context (products, ICP, anti-ICP).
    Skips leads that already have a classification unless --reclassify is set.
    Writes each result immediately so progress is saved if the script is interrupted.

    Args:
        company_name: Seller company name. Must match an existing research cache
                      and a spreadsheet entry in data/spreadsheets.json.
        reclassify:   If True, re-classify leads that already have a classification.
    """
    # Load research cache for seller context.
    print(f"[CLASSIFY] Loading research for '{company_name}'...")
    research = get_research(company_name)

    products  = ", ".join(str(p) for p in research.get("products", [])[:12])
    icp_types = ", ".join(p["type"] for p in research.get("icp_profiles", []))
    anti_icp  = ", ".join(a["type"] for a in research.get("anti_icp", []))

    # Get the spreadsheet for this company.
    spreadsheet_id = sheets.get_or_create_spreadsheet(company_name)

    # Ensure classification and classification_reason columns exist in header.
    col_classification, col_reason = sheets.ensure_classification_columns(spreadsheet_id)

    # Read all leads.
    all_leads = sheets.read_leads_for_classification(spreadsheet_id)
    if not all_leads:
        print("[CLASSIFY] No leads found in the sheet.")
        return

    # Filter to only unclassified leads (unless --reclassify).
    to_classify = [
        lead for lead in all_leads
        if reclassify or not lead.get("classification", "").strip()
    ]

    print(f"[CLASSIFY] {len(all_leads)} total leads, {len(to_classify)} to classify.")
    if not to_classify:
        print("[CLASSIFY] All leads already classified. Use --reclassify to redo.")
        return

    client = llm.get_client()
    strong_count   = 0
    weak_count     = 0
    not_lead_count = 0

    for i, lead in enumerate(to_classify, start=1):
        company  = lead.get("company_name", "Unknown")
        website  = lead.get("website", "").strip()
        row      = lead["_row_number"]

        # Skip leads with no website.
        if not website:
            _write_and_print(
                spreadsheet_id, row, col_classification, col_reason,
                "Weak", "No website available to evaluate.",
                i, len(to_classify), company,
            )
            weak_count += 1
            continue

        # Ensure URL has a scheme.
        url = website if website.startswith("http") else f"https://{website}"

        prompt = _CLASSIFY_PROMPT.format(
            seller_company=company_name,
            products=products,
            icp_types=icp_types,
            anti_icp=anti_icp,
            company_name=company,
            url=url,
        )

        classification, reason = _call_gemini(client, prompt, url)

        _write_and_print(
            spreadsheet_id, row, col_classification, col_reason,
            classification, reason,
            i, len(to_classify), company,
        )

        if classification == "Strong":
            strong_count += 1
        elif classification == "Not a Lead":
            not_lead_count += 1
        else:
            weak_count += 1

    print(f"\n[CLASSIFY] Done.")
    print(f"  Strong:      {strong_count}")
    print(f"  Weak:        {weak_count}")
    print(f"  Not a Lead:  {not_lead_count}")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _call_gemini(client, prompt: str, url: str) -> tuple[str, str]:
    """
    Call Gemini with URL Context tool to classify a company.

    Gemini fetches the URL itself and reads the page content. The response
    is expected to be a JSON object with 'classification' and 'reason' keys.

    Args:
        client: Authenticated Gemini client.
        prompt: Classification prompt including the URL and seller context.
        url:    Company website URL (included in the prompt for Gemini to fetch).

    Returns:
        Tuple of (classification, reason). Falls back to ("Weak", error message)
        on any failure.
    """
    try:
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(url_context=types.UrlContext())],
                temperature=0.1,
            ),
        )

        # response.text can be None when the URL Context tool returns a result
        # but Gemini doesn't emit a separate text part. Fall back to extracting
        # text directly from the response parts in that case.
        raw = response.text
        if not raw:
            try:
                parts = response.candidates[0].content.parts
                raw = "".join(p.text for p in parts if getattr(p, "text", None))
            except (IndexError, AttributeError):
                raw = ""

        if not raw:
            return "Weak", "Website could not be accessed — manual review needed."

        raw = raw.strip()

        # Extract the JSON object from the response. When using the URL Context
        # tool, Gemini may wrap the JSON in prose or markdown — find the first
        # { and last } to isolate the JSON object.
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start == -1 or end == 0:
            return "Weak", "Classification failed (no JSON in response) — manual review needed."
        raw = raw[start:end]

        result = json.loads(raw)
        classification = result.get("classification", "Weak")
        reason         = result.get("reason", "")

        # Normalise to allowed values.
        if classification not in ("Strong", "Weak", "Not a Lead"):
            classification = "Weak"

        return classification, reason

    except json.JSONDecodeError:
        return "Weak", "Classification failed (JSON parse error) — manual review needed."
    except Exception as e:
        return "Weak", f"Classification failed ({type(e).__name__}) — manual review needed."


def _write_and_print(
    spreadsheet_id: str,
    row: int,
    col_classification: int,
    col_reason: int,
    classification: str,
    reason: str,
    current: int,
    total: int,
    company: str,
) -> None:
    """
    Write one classification result to the sheet and print progress.

    Writes immediately so progress is saved even if the script is interrupted.
    """
    sheets.write_classification(
        spreadsheet_id, row, col_classification, col_reason, classification, reason
    )
    print(f"[CLASSIFY] ({current}/{total}) {company} — {classification}")


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Classify leads in the Leads tab using Gemini URL Context."
    )
    parser.add_argument(
        "--company",
        required=True,
        help=(
            'Seller company name. Must match an existing research cache. '
            'Example: "Your Company Name"'
        ),
    )
    parser.add_argument(
        "--reclassify",
        action="store_true",
        help="Re-classify leads that already have a classification (default: skip them).",
    )
    args = parser.parse_args()

    try:
        classify_leads(args.company, args.reclassify)
    except KeyboardInterrupt:
        print("\n[CLASSIFY] Session interrupted by user. Progress already saved to sheet.")
