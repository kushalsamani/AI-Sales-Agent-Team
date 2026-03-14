"""
main.py
-------
CLI entry point for the Lead Research Agent.

Usage:
  python main.py --company "Your Company Name" --region "Texas, USA"
  python main.py --company "Your Company Name" --region "Germany"
  python main.py --company "Your Company Name" --region "South America" --force-research

Arguments:
  --company         Full company name to generate leads for. (required)
  --region          Target region: country, state, city, or broad area. (required)
  --force-research  Bypass cached ICP research and re-run it from scratch. (optional)

Exit codes:
  0  — Success (even if 0 leads found — that is a valid result).
  1  — Configuration error (missing API key, missing credentials file, etc.).
"""

import argparse
import sys

from agents import search_agent


def _parse_args() -> argparse.Namespace:
    """Parse and return CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="lead-research-agent",
        description=(
            "AI-powered B2B lead generation. "
            "Researches ICP, searches for matching companies, and writes leads to Google Sheets."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python main.py --company "Your Company Name" --region "Texas, USA"\n'
            '  python main.py --company "Your Company Name" --region "Europe"\n'
            '  python main.py --company "Your Company Name" --region "Germany" --force-research'
        ),
    )
    parser.add_argument(
        "--company",
        required=True,
        metavar="NAME",
        help='Full company name (e.g. "Your Company Name")',
    )
    parser.add_argument(
        "--region",
        required=True,
        metavar="REGION",
        help='Target region (e.g. "Texas, USA", "Germany", "South America", "Europe")',
    )
    parser.add_argument(
        "--force-research",
        action="store_true",
        help="Bypass cached ICP research and re-run from scratch.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    print(f"\n{'═' * 60}")
    print(f"  LEAD RESEARCH AGENT")
    print(f"{'═' * 60}")
    print(f"  Company  :  {args.company}")
    print(f"  Region   :  {args.region}")
    print(f"  Research :  {'Force re-run' if args.force_research else 'Use cache if available'}")
    print(f"{'═' * 60}")

    try:
        leads, written = search_agent.run(
            company_name=args.company,
            region=args.region,
            force_research=args.force_research,
        )

        # ── Final summary ────────────────────────────────────────────────────
        print(f"\n{'═' * 60}")
        print(f"  COMPLETE")
        print(f"{'═' * 60}")
        print(f"  New leads found   :  {len(leads)}")
        print(f"  Written to sheet  :  {written}")

        if len(leads) == 0:
            print(
                f"\n  No new leads found for '{args.region}'.\n"
                f"  Possible reasons:\n"
                f"    - This market is already fully covered in your sheet.\n"
                f"    - Search APIs returned no relevant results for this region.\n"
                f"    - Try a neighbouring region or sub-region for more results."
            )
        elif len(leads) < 15:
            print(
                f"\n  NOTE: Only {len(leads)} leads found. The market for '{args.region}'\n"
                f"  may have limited coverage, or has been largely captured already."
            )
        else:
            print(f"\n  Open your Google Sheet to review the new leads.")

        print(f"{'═' * 60}\n")

    except (ValueError, FileNotFoundError) as e:
        # Configuration or auth errors — show the message and exit cleanly.
        print(e)
        sys.exit(1)

    except KeyboardInterrupt:
        print("\n\n  [STOPPED] Run interrupted by user.\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
