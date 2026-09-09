"""Capture Google's raw stream frames when a reply comes back as a canned fallback.

The symptom this exists for: the model thinks its way to a full answer -- visible in
`reasoning_content` -- and the final text arrives as a stock refusal ("我只是一个语言模型")
or a stock error ("I seem to be encountering an error"). Measured at 12-16% of requests,
independent of prompt content: bare `hello world` fails at the same rate as a long Chinese
question, with or without an injected tool prompt.

Everything downstream of the library only ever sees the finished text, so there is no way
to tell *what Google actually sent* -- whether the fallback replaces the answer in a single
frame, arrives as a second candidate, or overwrites a partial one. This hooks the frame
parser, keeps the frames for the current request, and writes them out when a fallback
marker shows up. Diagnosis only; it changes no behaviour.

Disable with `GEMINI_DUMP_FALLBACK=0`.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import gemini_webapi.client as _gemini_client
import gemini_webapi.utils as _gemini_utils
import gemini_webapi.utils.parsing as _gemini_parsing
from loguru import logger

# Every phrasing observed live on 2026-09-09, plus the English forms of the same two
# families. The list is deliberately loose: a false positive costs one dump file, a
# false negative costs the evidence.
FALLBACK_MARKERS = re.compile(
    r"语言模型|文本模型|超出了我的设计用途|无法提供这方面的帮助|没办法提供这方面的帮助"
    r"|I'?m (?:just )?an? (?:large )?(?:language|text) model"
    r"|encountering an error"
    r"|I can'?t help (?:you )?with (?:that|this)",
    re.IGNORECASE,
)

DUMP_DIR = Path(os.environ.get("GEMINI_DUMP_DIR", "/app/data/debug_dumps"))
MAX_DUMPS = 30
MAX_DUMP_BYTES = 2 * 1024 * 1024


def _enabled() -> bool:
    return os.environ.get("GEMINI_DUMP_FALLBACK", "1") not in ("0", "false", "no")


def _dump(frames: list[Any], marker: str) -> None:
    """Write the frames collected so far. Never raises into the stream."""
    try:
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        if len(list(DUMP_DIR.glob("fallback_*.json"))) >= MAX_DUMPS:
            return

        path = DUMP_DIR / f"fallback_{time.strftime('%Y%m%d-%H%M%S')}_{os.getpid()}.json"
        payload = json.dumps(
            {
                "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "marker_hit": marker,
                "frame_count": len(frames),
                "frames": frames,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        if len(payload.encode("utf-8")) > MAX_DUMP_BYTES:
            payload = payload[: MAX_DUMP_BYTES // 2] + '\n… truncated …\n"}'
        path.write_text(payload, encoding="utf-8")
        logger.warning(f"Fallback reply detected ({marker!r}); raw frames written to {path}")
    except Exception as exc:  # diagnosis must never break a reply
        logger.debug(f"Could not write fallback dump: {exc}")


# Bound before install() swaps the name, so the stock parser stays reachable -- the
# subclass needs it as a base, and tests need it to prove the subclass is transparent.
ORIGINAL_PARSER = _gemini_parsing.StreamingFrameParser


class CapturingFrameParser(ORIGINAL_PARSER):
    """StreamingFrameParser that remembers its frames and dumps them on a fallback hit."""

    def __init__(self) -> None:
        super().__init__()
        self._captured: list[Any] = []
        self._dumped = False

    def _observe(self, frames: list[Any]) -> None:
        if not frames:
            return
        try:
            self._captured.extend(frames)
            if self._dumped:
                return
            # Only the frames from this call are scanned, so the cost stays linear.
            match = FALLBACK_MARKERS.search(str(frames))
            if match:
                self._dumped = True
                _dump(self._captured, match.group(0))
        except Exception as exc:
            logger.debug(f"Frame capture skipped: {exc}")

    def feed(self, content: str) -> list[Any]:
        frames = super().feed(content)
        self._observe(frames)
        return frames

    def flush(self) -> list[Any]:
        frames = super().flush()
        self._observe(frames)
        return frames


def install() -> None:
    """Swap the parser in every module that already bound the name at import time."""
    if not _enabled():
        logger.info("Fallback frame capture disabled by GEMINI_DUMP_FALLBACK.")
        return
    # client.py does `from .utils import StreamingFrameParser`, so patching the source
    # module alone would not reach the name it already holds -- same reason the project
    # patches ARTIFACTS_RE in two places.
    _gemini_client.StreamingFrameParser = CapturingFrameParser  # ty: ignore[invalid-assignment]
    _gemini_utils.StreamingFrameParser = CapturingFrameParser  # ty: ignore[invalid-assignment]
    _gemini_parsing.StreamingFrameParser = CapturingFrameParser  # ty: ignore[invalid-assignment]
    logger.info(f"Fallback frame capture active; dumps go to {DUMP_DIR} (max {MAX_DUMPS}).")
