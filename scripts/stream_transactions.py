"""Replay feature-only transactions into Redpanda on a virtual clock."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import get_settings
from backend.app.core.runtime import VirtualClock
from backend.app.streaming.producer import EventProducer
from backend.app.streaming.topics import TopicSet
from evaluation.dataio import load_features


async def replay(split: str, speed: float, start: str | None, end: str | None) -> int:
    settings = get_settings()
    frame = load_features(split, settings.data_dir)
    if start:
        frame = frame.loc[frame["timestamp"].ge(pd.Timestamp(start))]
    if end:
        frame = frame.loc[frame["timestamp"].le(pd.Timestamp(end))]
    frame = frame.reset_index(drop=True)
    if frame.empty:
        return 0
    producer = EventProducer(settings)
    await producer.start()
    clock = VirtualClock(speed=speed, start=frame.iloc[0]["timestamp"].to_pydatetime())
    trace_id = str(uuid4())
    try:
        for index, row in frame.iterrows():
            timestamp = row["timestamp"].to_pydatetime()
            if index:
                await clock.advance_to(timestamp)
            payload = {
                key: (value.isoformat() if isinstance(value, pd.Timestamp) else value.item() if hasattr(value, "item") else value)
                for key, value in row.to_dict().items()
            }
            await producer.send(
                TopicSet.from_settings(settings).transactions,
                "transaction.received",
                payload,
                trace_id=trace_id,
                key=str(row["transaction_id"]),
            )
    finally:
        await producer.stop()
    return len(frame)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("train", "validation", "test"), default="validation")
    parser.add_argument("--speed", type=float, default=300.0)
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")
    args = parser.parse_args()
    count = asyncio.run(replay(args.split, args.speed, args.start, args.end))
    print({"published": count, "split": args.split})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
