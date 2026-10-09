"""Validate or explicitly freeze the offline Phase 7 synthetic candidate set."""
import argparse
import json
from pathlib import Path

from phase7_offline import check_frozen, export_reviewed_queries, freeze


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verify existing frozen artifacts without writing")
    mode.add_argument("--freeze", action="store_true", help="write candidate manifest, queries, and static report")
    parser.add_argument("--require-reviewed", action="store_true",
                        help="require current independent approvals for every label")
    parser.add_argument("--export-reviewed-queries", type=Path,
                        help="export reviewed query rows to a separate file after validating every approval")
    args = parser.parse_args()
    if args.export_reviewed_queries:
        if args.freeze:
            parser.error("--export-reviewed-queries requires --check")
        result = export_reviewed_queries(args.export_reviewed_queries)
    else:
        result = check_frozen(require_reviewed=args.require_reviewed) if args.check else freeze()
    if args.freeze and args.require_reviewed:
        # Freeze never generates or replaces approval records. Validate the requested gate afterwards.
        result = check_frozen(require_reviewed=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
