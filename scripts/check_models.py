"""Call each configured Gemini model once and report latency plus token use."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import get_settings


async def main() -> int:
    settings = get_settings()
    if not settings.gemini_api_key:
        print("GEMINI_API_KEY is not configured; live model availability is unverified.")
        return 2
    client = genai.Client(api_key=settings.gemini_api_key)
    for model in (
        settings.gemini_primary_model,
        settings.gemini_secondary_model,
        settings.gemini_economy_model,
    ):
        started = time.perf_counter()
        response = await client.aio.models.generate_content(
            model=model,
            contents="Reply with the single word READY.",
            config=types.GenerateContentConfig(max_output_tokens=8, temperature=0),
        )
        usage = response.usage_metadata
        print(
            {
                "model": model,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "input_tokens": getattr(usage, "prompt_token_count", 0),
                "output_tokens": getattr(usage, "candidates_token_count", 0),
            }
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
