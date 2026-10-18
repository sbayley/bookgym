from pathlib import Path
import argparse
from datetime import date
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.discovery import refresh_discovery


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--days-ahead",
        type=int,
        default=None,
        help="Number of days ahead to scan. Defaults to BOOKGYM_DISCOVERY_DAYS_AHEAD.",
    )
    parser.add_argument(
        "--class-type",
        action="append",
        default=[],
        help="Class type/search term to discover. Can be provided more than once.",
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="Refresh one exact date, formatted YYYY-MM-DD.",
    )
    parser.add_argument(
        "--date-from",
        type=date.fromisoformat,
        default=None,
        help="Start date for a discovery refresh range, formatted YYYY-MM-DD.",
    )
    parser.add_argument(
        "--date-to",
        type=date.fromisoformat,
        default=None,
        help="End date for a discovery refresh range, formatted YYYY-MM-DD.",
    )
    parser.add_argument(
        "--stale-only",
        action="store_true",
        help="Refresh only week ranges that have not been scanned recently.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    settings = get_settings()
    terms = args.class_type or settings.discovery_terms
    date_from = args.date if args.date is not None else args.date_from
    date_to = args.date if args.date is not None else args.date_to
    summary = refresh_discovery(
        search_terms=terms,
        days_ahead=args.days_ahead,
        date_from=date_from,
        date_to=date_to,
        stale_only=args.stale_only,
    )

    print(
        f"Discovery refresh complete: scanned {summary.scanned_ranges} range(s), "
        f"matched {summary.matched_classes} class observation(s) "
        f"in {summary.elapsed_seconds:.1f}s."
    )
    for message in summary.messages:
        print(message)
