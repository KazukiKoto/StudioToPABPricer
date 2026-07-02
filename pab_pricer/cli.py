"""CLI entry point: price a bricks CSV against LEGO Pick a Brick and write outputs/*.csv"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pab_pricer.pricer import price_rows, read_brick_rows, write_priced_csv

REPO_ROOT = Path(__file__).resolve().parent.parent


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=REPO_ROOT / "runway_sample.csv",
        help="Path to the input bricks CSV (default: runway_sample.csv)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to the output priced CSV (default: outputs/<input-name>_priced.csv)",
    )
    parser.add_argument(
        "--locale",
        default="en-gb",
        help="LEGO.com locale to price against (default: en-gb, for GBP pricing)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to wait between requests for distinct part numbers (default: 0.5)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if not args.input.exists():
        print(f"Input CSV not found: {args.input}", file=sys.stderr)
        return 1

    output_path = args.output or (
        REPO_ROOT / "outputs" / f"{args.input.stem}_priced.csv"
    )

    rows = read_brick_rows(args.input)
    if not rows:
        print(f"No priceable rows found in {args.input}", file=sys.stderr)
        return 1

    print(f"Loaded {len(rows)} part lines from {args.input}")

    def progress(done: int, total: int, part_number: str) -> None:
        print(f"  [{done}/{total}] priced part {part_number}")

    priced_rows = price_rows(rows, locale=args.locale, delay=args.delay, progress_callback=progress)
    write_priced_csv(priced_rows, output_path)

    print(f"Wrote priced CSV to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
