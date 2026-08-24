"""Publish one known development spike window for the live demo."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evaluation.dataio import load_split
from scripts.stream_transactions import replay


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", default="VAL_S1")
    parser.add_argument("--speed", type=float, default=300.0)
    args = parser.parse_args()
    validation = load_split("validation")
    event = validation.spike_events.loc[
        validation.spike_events["event_id"].eq(args.event)
    ]
    if event.empty:
        parser.error(f"Unknown validation event: {args.event}")
    row = event.iloc[0]
    start = (row["start_timestamp"] - __import__("pandas").Timedelta(hours=3)).isoformat()
    end = (row["end_timestamp"] + __import__("pandas").Timedelta(hours=1)).isoformat()
    count = asyncio.run(replay("validation", args.speed, start, end))
    print({"published": count, "event": args.event, "from": start, "to": end})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
