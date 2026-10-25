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
    # Google keeps inventing phrasings. Every one below was seen live on 2026-09-09, and the
    # first alternative is deliberately loose -- "文本 AI" slipped past a list that only knew
    # "语言模型" and "文本模型", so that refusal was never recorded at all. Matching the shape
    # ("I am a <something> model/AI") costs a few extra dumps and misses nothing.
    r"我(?:只是|是|就是)?一?个?[^，。！？\n]{0,12}(?:语言模型|文本模型|文本\s*AI|AI\s*模型|大模型)"  # noqa: RUF001
    r"|身为一个[^，。！？\n]{0,12}(?:模型|AI)"  # noqa: RUF001
    r"|超出了我的设计用途|无法提供这方面的帮助|没办法提供这方面的帮助|没法帮到你|帮不上忙|没法帮上忙"
    # "由于程序代码的局限，我没法办到" -- seen 2026-09-09 right after leaked filenames  # noqa: RUF003
    # made the model doubt its own context. New wordings keep appearing; match intent.
    r"|由于程序代码的局限|程序代码的局限|我没法办到|没法办到|做不到这一点|无法完成这个请求"
    r"|I'?m (?:just )?an? (?:large )?(?:language|text) (?:model|AI)"
    r"|encountering an error|I encountered an error"
    r"|I can'?t help (?:you )?with (?:that|this)",
    re.IGNORECASE,
)

# `<GenerateWidget …>` carries a *prompt for a renderer*, not the rendered component, and
# the render happens in Google's frontend. Its `component_placeholder_id` hints there may be
# a matching content block elsewhere in the response -- unverified, which is why the frames
# are captured: they settle whether anything is recoverable over the reverse API.
WIDGET_MARKER = re.compile(r"GenerateWidget")

DUMP_DIR = Path(os.environ.get("GEMINI_DUMP_DIR", "/app/data/debug_dumps"))
MAX_DUMPS = 100
MAX_DUMP_BYTES = 2 * 1024 * 1024
_TRUNCATED_FRAME_CHARS = 4000


# Warn once per kind rather than on every hit: a full quota is worth knowing about, a
# message per request is not.
_quota_warned: set[str] = set()


def _enabled() -> bool:
    return os.environ.get("GEMINI_DUMP_FALLBACK", "1") not in ("0", "false", "no")


def _dump(frames: list[Any], marker: str, kind: str = "fallback") -> None:
    """Write the frames collected so far. Never raises into the stream.

    Each `kind` gets its own quota so one class of capture cannot starve the other.
    """
    try:
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        if len(list(DUMP_DIR.glob(f"{kind}_*.json"))) >= MAX_DUMPS:
            if kind not in _quota_warned:
                _quota_warned.add(kind)
                logger.warning(
                    f"{kind} dump quota is full ({MAX_DUMPS} files in {DUMP_DIR}). Captures "
                    "are being dropped, so an empty capture log no longer means no hits -- "
                    "archive or delete the directory to resume recording."
                )
            return

        path = DUMP_DIR / f"{kind}_{time.strftime('%Y%m%d-%H%M%S')}_{os.getpid()}.json"
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
            # Cutting a JSON document in half leaves something no parser will read: a 1MB
            # maps_local capture had to be analysed with regexes because of exactly that.
            # Keep a bounded head and tail as plain strings so the file always parses.
            def _preview(items: list[Any]) -> list[str]:
                return [str(item)[:_TRUNCATED_FRAME_CHARS] for item in items]

            payload = json.dumps(
                {
                    "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "marker_hit": marker,
                    "frame_count": len(frames),
                    "truncated": True,
                    "note": f"payload exceeded {MAX_DUMP_BYTES} bytes; frames abridged",
                    "frames_head": _preview(frames[:3]),
                    "frames_tail": _preview(frames[-3:]),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        path.write_text(payload, encoding="utf-8")
        logger.warning(f"{kind} capture ({marker!r}); raw frames written to {path}")
    except Exception as exc:  # diagnosis must never break a reply
        logger.debug(f"Could not write fallback dump: {exc}")


# Bound before install() swaps the name, so the stock parser stays reachable -- the
# subclass needs it as a base, and tests need it to prove the subclass is transparent.
ORIGINAL_PARSER = _gemini_parsing.StreamingFrameParser


def dump_prompt(text: str, message_count: int) -> None:
    """Write the exact bytes handed to Google. Never raises into the request.

    The response side has been capturable since the frame parser was hooked, but the
    request side was only ever a character count in the log -- which is why both the
    ChatML framing and the leaked `img_<uuid>` filenames had to be found by reading the
    model's own exposed reasoning instead of simply looking at what we sent.
    """
    try:
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        if len(list(DUMP_DIR.glob("prompt_*.txt"))) >= MAX_DUMPS:
            if "prompt" not in _quota_warned:
                _quota_warned.add("prompt")
                logger.warning(f"prompt dump quota is full ({MAX_DUMPS} files in {DUMP_DIR}).")
            return
        path = DUMP_DIR / f"prompt_{time.strftime('%Y%m%d-%H%M%S')}_{os.getpid()}.txt"
        header = f"# messages={message_count} chars={len(text)} at={time.strftime('%H:%M:%S')}\n\n"
        path.write_text(header + text, encoding="utf-8")
    except Exception as exc:
        logger.debug(f"Could not write prompt dump: {exc}")


class CapturingFrameParser(ORIGINAL_PARSER):
    """StreamingFrameParser that remembers its frames and dumps them on a fallback hit."""

    def __init__(self) -> None:
        """Start with no captured frames and nothing dumped yet."""
        super().__init__()
        self._captured: list[Any] = []
        self._dumped = False
        self._widget_dumped = False

    def _observe(self, frames: list[Any]) -> None:
        if not frames:
            return
        try:
            self._captured.extend(frames)
            # Only the frames from this call are scanned, so the cost stays linear.
            blob = str(frames)
            if not self._dumped and (match := FALLBACK_MARKERS.search(blob)):
                self._dumped = True
                _dump(self._captured, match.group(0), "fallback")
            if not self._widget_dumped and WIDGET_MARKER.search(blob):
                self._widget_dumped = True
                _dump(self._captured, "GenerateWidget", "widget")
        except Exception as exc:
            logger.debug(f"Frame capture skipped: {exc}")

    def feed(self, content: str) -> list[Any]:
        """Parse a chunk of the stream and observe the frames it yields."""
        frames = super().feed(content)
        self._observe(frames)
        return frames

    def flush(self) -> list[Any]:
        """Parse what is left of the stream and observe the final frames."""
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
