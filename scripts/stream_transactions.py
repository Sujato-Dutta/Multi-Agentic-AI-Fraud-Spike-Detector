"""Replay feature-only transactions into Redpanda with visible progress."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from uuid import uuid4

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import get_settings
from backend.app.core.runtime import AppError, VirtualClock
from backend.app.streaming.producer import EventProducer
from backend.app.streaming.topics import TopicSet
from evaluation.dataio import load_features


async def replay(
    split: str,
    speed: float,
    start: str | None,
    end: str | None,
    *,
    limit: int | None = None,
    no_wait: bool = False,
    progress_every: int = 100,
) -> int:
    if speed <= 0:
        raise ValueError("Replay speed must be positive")
    if limit is not None and limit < 1:
        raise ValueError("Replay limit must be positive")
    if progress_every < 1:
        raise ValueError("Progress interval must be positive")

    settings = get_settings()
    frame = load_features(split, settings.data_dir)
    if start:
        frame = frame.loc[frame["timestamp"].ge(pd.Timestamp(start))]
    if end:
        frame = frame.loc[frame["timestamp"].le(pd.Timestamp(end))]
    if limit is not None:
        frame = frame.head(limit)
    frame = frame.reset_index(drop=True)
    if frame.empty:
        print({"event": "replay_empty", "split": split}, flush=True)
        return 0

    first_timestamp = frame.iloc[0]["timestamp"].to_pydatetime()
    last_timestamp = frame.iloc[-1]["timestamp"].to_pydatetime()
    virtual_span_seconds = max(0.0, (last_timestamp - first_timestamp).total_seconds())
    scheduled_seconds = 0.0 if no_wait else virtual_span_seconds / speed
    topics = TopicSet.from_settings(settings)
    print(
        {
            "event": "replay_plan",
            "split": split,
            "rows": len(frame),
            "topic": topics.transactions,
            "broker": settings.redpanda_bootstrap_servers,
            "first_timestamp": first_timestamp.isoformat(),
            "last_timestamp": last_timestamp.isoformat(),
            "virtual_span_seconds": round(virtual_span_seconds, 1),
            "minimum_wall_seconds": round(scheduled_seconds, 1),
            "no_wait": no_wait,
        },
        flush=True,
    )

    producer = EventProducer(settings)
    print({"event": "replay_connecting"}, flush=True)
    await producer.start()
    print({"event": "replay_connected"}, flush=True)
    clock = None if no_wait else VirtualClock(speed=speed, start=first_timestamp)
    trace_id = str(uuid4())
    wall_started = time.monotonic()
    try:
        for index, row in frame.iterrows():
            timestamp = row["timestamp"].to_pydatetime()
            if index and clock is not None:
                await clock.advance_to(timestamp)
            payload = {
                key: (
                    value.isoformat()
                    if isinstance(value, pd.Timestamp)
                    else value.item()
                    if hasattr(value, "item")
                    else value
                )
                for key, value in row.to_dict().items()
            }
            await producer.send(
                topics.transactions,
                "transaction.received",
                payload,
                trace_id=trace_id,
                key=str(row["transaction_id"]),
            )
            published = index + 1
            if (
                published == 1
                or published % progress_every == 0
                or published == len(frame)
            ):
                remaining_virtual = max(
                    0.0, (last_timestamp - timestamp).total_seconds()
                )
                print(
                    {
                        "event": "replay_progress",
                        "published": published,
                        "total": len(frame),
                        "percent": round(100 * published / len(frame), 1),
                        "virtual_timestamp": timestamp.isoformat(),
                        "elapsed_seconds": round(time.monotonic() - wall_started, 1),
                        "minimum_eta_seconds": round(
                            0.0 if no_wait else remaining_virtual / speed,
                            1,
                        ),
                    },
                    flush=True,
                )
    finally:
        await producer.stop()
    return len(frame)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Publish timestamped transactions to Redpanda. --speed accelerates "
            "event time; it is not a transactions-per-second rate."
        )
    )
    parser.add_argument(
        "--split", choices=("train", "validation", "test"), default="validation"
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=300.0,
        help="Event-time acceleration factor (default: 300).",
    )
    parser.add_argument("--from", dest="start", help="Inclusive event timestamp.")
    parser.add_argument("--to", dest="end", help="Inclusive event timestamp.")
    parser.add_argument(
        "--limit", type=int, help="Publish only the first N selected rows."
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Publish immediately while preserving event timestamps.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Print progress after every N published rows (default: 100).",
    )
    args = parser.parse_args()
    if args.speed <= 0:
        parser.error("--speed must be positive")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.progress_every < 1:
        parser.error("--progress-every must be positive")

    try:
        count = asyncio.run(
            replay(
                args.split,
                args.speed,
                args.start,
                args.end,
                limit=args.limit,
                no_wait=args.no_wait,
                progress_every=args.progress_every,
            )
        )
    except KeyboardInterrupt:
        print({"event": "replay_cancelled", "split": args.split}, flush=True)
        return 130
    except AppError as exc:
        print(
            {"event": "replay_failed", "code": exc.code, "detail": exc.detail},
            flush=True,
        )
        return 1
    print({"event": "replay_complete", "published": count, "split": args.split})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
