"""Build the blind reviewer workbook for the current 120-review control sample."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.services.quality import (
    combine_proposals_csv,
    combine_source_csv,
    make_blind_review_workbook,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/quality_current_DemoPhone/review_blind_120.xlsx"),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sample_dir = root / "outputs/quality_current_DemoPhone"
    reviews = combine_source_csv(
        [sample_dir / "sample_100.csv", sample_dir / "sample_extra_20.csv"]
    )
    catalog = (sample_dir / "reasons_32.csv").read_text(encoding="utf-8-sig")
    proposals = combine_proposals_csv(
        [sample_dir / "first_pass_100.csv", sample_dir / "first_pass_extra_20.csv"]
    )
    workbook = make_blind_review_workbook(reviews, catalog, proposals_csv=proposals)
    destination = args.output if args.output.is_absolute() else root / args.output
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(workbook)


if __name__ == "__main__":
    main()
