"""
email_automation/seed_approached.py
-------------------------------------
One-time script to pre-populate the Approached sheet with companies
that were contacted manually before the email automation was set up.

Looks up each domain in the Leads sheet to pull full company data,
then writes them to the Approached sheet with status "Approached".

Run once before your first automated send, then delete this file.

Usage:
  python email_automation/seed_approached.py --company "Your Company Name"
"""

import sys
import os
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import sheets
from tools.sheets import normalize_domain

# ─── Companies already contacted manually ─────────────────────────────────────
# Add or remove domains here as needed before running.
_ALREADY_CONTACTED = [
    "gwspipe.com",
    "pipelinepsc.com",
    "qrcvalves.com",
    "superiorvalves.com",
    "chemflowproducts.com",
    "deltavalvesandcontrols.com",
    "independencevalve.com",
    "reactivealloy.com",
    "advindustries.com",
    "industrialpipeandvalve.com",
]


def seed(company_name: str) -> None:
    """
    Pre-populate the Approached sheet with manually contacted companies.

    Looks up each domain in the Leads sheet to get company name, region,
    and email. Falls back to domain as company name if not found in sheet.

    Args:
        company_name: Seller company name (must match spreadsheets.json).
    """
    spreadsheet_id = sheets.get_or_create_spreadsheet(company_name)

    # Build a domain → lead lookup from the Leads sheet.
    print("[SEED] Reading Leads sheet...")
    all_leads = sheets.read_leads_for_classification(spreadsheet_id)
    leads_by_domain: dict[str, dict] = {}
    for lead in all_leads:
        domain = normalize_domain(lead.get("website", ""))
        if domain:
            leads_by_domain[domain] = lead

    # Check what's already in the Approached sheet to avoid duplicates.
    existing = sheets.get_approached_companies(spreadsheet_id)
    existing_domains: set[str] = {
        normalize_domain(r.get("website", "")) or ""
        for r in existing
    }

    today = date.today().isoformat()
    seeded = 0

    for domain in _ALREADY_CONTACTED:
        if domain in existing_domains:
            print(f"  [SKIP] {domain} already in Approached sheet.")
            continue

        lead = leads_by_domain.get(domain, {})
        company_name_lead = lead.get("company_name", domain)
        email             = lead.get("email", "")
        region            = lead.get("region", "")

        sheets.write_approached(spreadsheet_id, {
            "company_name":      company_name_lead,
            "website":           domain,
            "region":            region,
            "email_sent_to":     email,
            "email_subject":     "Lined Pipes, Fittings and Valves Inquiry",
            "sent_on":           today,
            "follow_up_sent_on": "",
            "status":            "Approached",
            "reply_date":        "",
        })
        print(f"  [OK] Seeded: {company_name_lead} ({domain})")
        seeded += 1

    print(f"\n[SEED] Done. {seeded} companies added to Approached sheet.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Pre-populate Approached sheet with manually contacted companies."
    )
    parser.add_argument(
        "--company", required=True,
        help='Seller company name. Example: "Your Company Name"',
    )
    args = parser.parse_args()
    seed(args.company)
