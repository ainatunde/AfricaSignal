"""Record one real claim-extraction response as a test fixture (AS-021).

The test suite replays recordings through ``FakeProvider``. This is the only place that makes a
real model call for that purpose. It needs ``ANTHROPIC_API_KEY`` and spends a few cents.

    ANTHROPIC_API_KEY=... python -m africasignal.llm.record article.txt \\
        --title "Headline" --published 2026-09-12 \\
        --out tests/fixtures/llm/claim_extract_v1_recorded.json

``article.txt`` is the plain text of a document. Use text you are allowed to keep in the
repository (your own words, or a source whose terms permit it). The file is written in the same
format as ``claim_extract_v1_synthetic.json`` with ``"recorded": true``; the skipped test
``test_a_real_recording_exists`` passes once such a file exists.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from africasignal.extract.claims import (
    MAX_OUTPUT_TOKENS,
    PROMPT_VERSION,
    PURPOSE,
    claim_schema,
    keyword_hits,
    select_window,
    system_prompt,
    user_message,
)
from africasignal.llm.anthropic_provider import AnthropicProvider
from africasignal.llm.config import load_llm_config
from africasignal.models import EvidenceDocument


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("article", type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--published", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    text = args.article.read_text(encoding="utf-8")
    published = datetime.fromisoformat(args.published).replace(tzinfo=UTC)
    document = EvidenceDocument(title=args.title, published_at=published)
    start, end = select_window(text, keyword_hits(text))

    purpose = load_llm_config().purpose(PURPOSE)
    reply = AnthropicProvider(api_key=key).complete(
        model=purpose.model,
        system=system_prompt(),
        user=user_message(document, text[start:end]),
        schema=claim_schema(),
        max_tokens=MAX_OUTPUT_TOKENS,
        effort=purpose.effort,
    )
    args.out.write_text(
        json.dumps(
            {
                "recorded": True,
                "provenance": "Real response from the Anthropic API.",
                "model_id": purpose.model,
                "prompt_version": PROMPT_VERSION,
                "recorded_at": datetime.now(UTC).isoformat(),
                "document": {
                    "title": args.title,
                    "published_at": published.isoformat(),
                    "text": text,
                },
                "response_text": reply.text,
                "usage": {
                    "input_tokens": reply.input_tokens,
                    "output_tokens": reply.output_tokens,
                },
                "stop_reason": reply.stop_reason,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out} ({reply.input_tokens} in, {reply.output_tokens} out)")


if __name__ == "__main__":
    main()
