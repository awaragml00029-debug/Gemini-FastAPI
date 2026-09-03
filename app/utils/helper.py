import asyncio
import base64
import hashlib
import html
import ipaddress
import mimetypes
import re
import reprlib
import socket
import struct
import tempfile
import time
import unicodedata
from collections.abc import Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urljoin, urlparse

import gemini_webapi.client as _gemini_client
import gemini_webapi.constants as _gemini_constants
import orjson
import regex
from curl_cffi import BrowserTypeLiteral, CurlHttpVersion, CurlOpt, requests
from jsonschema import SchemaError, ValidationError, validators
from jsonschema.validators import validator_for
from loguru import logger
from pydantic import BaseModel

from app.models import (
    AppContentItem,
    AppMessage,
    AppMessageRole,
    AppToolCall,
    AppToolCallFunction,
    ChatCompletionMessage,
    ChatCompletionNamedToolChoice,
    ImageGeneration,
    StructuredOutputRequirement,
    ToolChoiceFunction,
    ToolChoiceTypes,
)
from app.utils import g_config

MAX_REMOTE_FETCH_BYTES = 20 * 1024 * 1024

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None

# Google emits an artifact URL alongside a generated image. The library's own
# ARTIFACTS_RE ends in `\d+`, but the final path segment is now `0_452` -- digits,
# underscore, digits -- so it consumes only the leading `0` and leaves `_452` sitting
# in the reply just above the image. Match the whole segment instead.
ARTIFACT_URL_RE = re.compile(r"https?://googleusercontent\.com/(?:\w+/)+[\w-]+\n*")

# The library strips these in _parse_candidate, before any of our code sees the text,
# and it does so with its own pattern ending in `\d+`. Against `.../0_452` that consumes
# `0`, stops at the underscore, and hands us an orphaned `_452` whose URL is already
# gone -- nothing downstream can recognise it any more. So the pattern has to be
# replaced at the source. client.py binds the name at import time, hence both targets.
_gemini_client.ARTIFACTS_RE = ARTIFACT_URL_RE
_gemini_constants.ARTIFACTS_RE = ARTIFACT_URL_RE

MAX_REMOTE_MEDIA_BYTES = 25 * 1024 * 1024
MAX_REMOTE_REDIRECTS = 5
REMOTE_URL_SCHEMES = {"http", "https"}
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}

VALID_TAG_ROLES = {"user", "assistant", "system", "tool"}
# 上游原版。实测在真实路径上只有 10% 的工具调用命中率 (40 轮), 代理复现 13% (30 轮)。
# 把 `SYSTEM: ... (MANDATORY)` 抬头、`END ...` 收尾和 4 条大写 MUST 规则去掉之后,
# 同一套 [ToolCalls] 语法能跑到 95~100% (38/40、30/30) —— 语法不是瓶颈, 措辞才是。
# 保留原文以便随时回退对照, 下面这份对齐上游 66dc3f7。
# TOOL_WRAP_HINT = (
#     "\n\nSYSTEM: TOOL CALLING PROTOCOL (MANDATORY)\n"
#     "Either emit the tool-call block alone, or answer in natural language with no protocol tags. Never both.\n\n"
#     "1. Names MUST match the schemas exactly; every required parameter MUST be present with its declared JSON type.\n"
#     "2. Each value MUST stand alone between two fences of 3 backticks with no language tag; if it contains a backtick run, both fences MUST be longer.\n"
#     "3. Every opening tag MUST be closed in reverse order of opening. A fence closes only itself, never a tag. An unclosed tag voids the call.\n"
#     "4. Emit the block and nothing else. No preamble or commentary.\n\n"
#     "REQUIRED SYNTAX, reproduce literally:\n"
#     "[ToolCalls]\n"
#     "[Call:tool_name]\n"
#     "[CallParameter:parameter_name]\n"
#     "```\n"
#     "value\n"
#     "```\n"
#     "[/CallParameter]\n"
#     "[/Call]\n"
#     "[/ToolCalls]\n\n"
#     "END TOOL CALLING PROTOCOL"
# )
# 首行和末行会被 _hint_anchors() 拿去构造剥离正则, 所以两头必须是独特且非标签的文本:
# 末行若是 `[/ToolCalls]`, HINT_END_RES 会把输出里任何一个闭合标签都删掉。
TOOL_WRAP_HINT = (
    "\n\nTool call syntax reference\n"
    "Either emit the tool-call block alone, or answer in natural language. Never both.\n"
    "Emit the block and nothing else. No preamble or commentary.\n\n"
    "REQUIRED SYNTAX, reproduce literally:\n"
    "[ToolCalls]\n"
    "[Call:tool_name]\n"
    "[CallParameter:parameter_name]\n"
    "```\n"
    "value\n"
    "```\n"
    "[/CallParameter]\n"
    "[/Call]\n"
    "[/ToolCalls]\n\n"
    "(end of tool call syntax reference)"
)
STRUCTURED_JSON_WRAP_HINT = (
    "\n\nSYSTEM: STRUCTURED JSON PROTOCOL (MANDATORY)\n"
    "1. Return exactly one fenced block holding one strict JSON document. No prose, no second block.\n"
    "2. Open with ```json and close with a fence of the same length; if the JSON contains a backtick run, both fences MUST be longer.\n"
    "3. NEVER truncate the document or omit the closing fence.\n\n"
    "REQUIRED SYNTAX:\n"
    "```json\n"
    '{"field":"value"}\n'
    "```\n\n"
    "END STRUCTURED JSON PROTOCOL"
)
# Appended to the protocol above when the client supplied a schema; JSON mode sends the
# protocol alone, because valid JSON of any shape satisfies it.
SCHEMA_ADHERENCE_PROMPT = (
    "The JSON document MUST validate against the JSON Schema below. "
    "Emit every required field with its declared type."
)
STRICT_SCHEMA_ADHERENCE_PROMPT = (
    "Strict schema adherence is required: the JSON must conform exactly to the schema."
)
TOOL_INTERFACE_PROMPT = (
    "SYSTEM INTERFACE: Call an available tool whenever the request requires one, with arguments that "
    "validate against its JSON Schema. Never invent an undeclared tool or parameter."
)
TOOL_DESCRIPTION_PROMPT = "Tool `{name}`: {description}"
TOOL_ARGUMENTS_SCHEMA_PROMPT = "Parameters JSON Schema:"
TOOL_EMPTY_ARGUMENTS_SCHEMA_PROMPT = "Parameters JSON Schema: {} (takes no parameters)"
TOOL_CHOICE_NONE_PROMPT = (
    "TOOL CHOICE = none: You MUST NOT call a tool or emit any protocol tag this turn. "
    "Answer in natural language."
)
TOOL_CHOICE_REQUIRED_PROMPT = (
    "TOOL CHOICE = required: You MUST call at least one tool this turn; "
    "a natural-language answer alone is invalid."
)
TOOL_CHOICE_NAMED_PROMPT = (
    "TOOL CHOICE = `{target_name}`: You MUST call `{target_name}` this turn and no other tool."
)
IMAGE_GENERATION_PROMPT = "\n\n".join(
    (
        "IMAGE PROTOCOL: Every image request MUST be answered with a generated image attachment.",
        "A new request MUST produce a new image; an edit MUST return the edited image.",
        "NEVER substitute text for the image: no explanation, apology, progress note, or placeholder.",
    )
)
IMAGE_GENERATION_FORCED_PROMPT = (
    "IMAGE REQUIRED: You MUST return at least one generated image; a text-only reply is a failure."
)
TOOL_BLOCK_RE = re.compile(
    r"\\?\[ToolCalls\\?](.*?)\\?\[\\?/ToolCalls\\?]",
    re.DOTALL | re.IGNORECASE,
)
TOOL_CALL_RE = re.compile(
    r"\\?\[Call\\?:(?P<name>[^]]+)\\?](?P<body>.*?)\\?\[\\?/Call\\?]",
    re.DOTALL | re.IGNORECASE,
)
RESPONSE_BLOCK_RE = re.compile(
    r"\\?\[ToolResults\\?](.*?)\\?\[\\?/ToolResults\\?]",
    re.DOTALL | re.IGNORECASE,
)
RESPONSE_ITEM_RE = re.compile(
    r"\\?\[Result\\?:(?P<name>[^]]+)\\?](?P<body>.*?)\\?\[\\?/Result\\?]",
    re.DOTALL | re.IGNORECASE,
)
TAGGED_ARG_RE = re.compile(
    r"\\?\[CallParameter\\?:(?P<name>[^]]+)\\?](?P<body>.*?)\\?\[\\?/CallParameter\\?]",
    re.DOTALL | re.IGNORECASE,
)
TAGGED_RESULT_RE = re.compile(
    r"\\?\[ToolResult\\?](.*?)\\?\[\\?/ToolResult\\?]",
    re.DOTALL | re.IGNORECASE,
)
CONTROL_TOKEN_RE = re.compile(r"\\?<\\?\|im\\?_(?:start|end)\\?\|\\?>", re.IGNORECASE)
CHATML_START_RE = re.compile(r"\\?<\\?\|im\\?_start\\?\|\\?>(\w+)\n?", re.IGNORECASE)
CHATML_END_RE = re.compile(r"\\?<\\?\|im\\?_end\\?\|\\?>", re.IGNORECASE)
COMMONMARK_UNESCAPE_RE = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])")
PARAM_FENCE_RE = re.compile(r"^(?P<fence>`{3,})(?P<tag>[A-Za-z0-9_+-]*)[ \t]*(?:\r?\n)?")
MIME_SUBTYPE_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
TOOL_HINT_STRIPPED = TOOL_WRAP_HINT.strip()
SYSTEM_HINTS = (TOOL_WRAP_HINT, STRUCTURED_JSON_WRAP_HINT)


def _hint_anchors(hint: str) -> tuple[str, str]:
    """Return a hint's first and last non-empty lines, used to locate echoed copies."""
    lines = [line.strip() for line in hint.split("\n") if line.strip()]
    return (lines[0], lines[-1]) if lines else ("", "")


HINT_START_ANCHORS: list[str] = []
HINT_END_ANCHORS: list[str] = []
HINT_FULL_RES: list[re.Pattern[str]] = []
HINT_START_RES: list[re.Pattern[str]] = []
HINT_END_RES: list[re.Pattern[str]] = []

for _hint in SYSTEM_HINTS:
    _start, _end = _hint_anchors(_hint)
    if not _start or not _end:
        continue
    _start_esc, _end_esc = re.escape(_start), re.escape(_end)
    HINT_START_ANCHORS.append(_start)
    HINT_END_ANCHORS.append(_end)
    HINT_FULL_RES.append(
        re.compile(rf"\n?{_start_esc}:?.*?{_end_esc}\n?", re.DOTALL | re.IGNORECASE)
    )
    HINT_START_RES.append(re.compile(rf"\n?{_start_esc}:?\s*", re.IGNORECASE))
    HINT_END_RES.append(re.compile(rf"\s*{_end_esc}\n?", re.IGNORECASE))

# --- Streaming Specific Patterns ---
_START_PATTERNS = {
    "TOOL": r"\\?\[ToolCalls\\?]",
    "ORPHAN": r"\\?\[Call\\?:[^]]+\\?]",
    "RESP": r"\\?\[ToolResults\\?]",
    "ARG": r"\\?\[CallParameter\\?:[^]]+\\?]",
    "RESULT": r"\\?\[ToolResult\\?]",
    "ITEM": r"\\?\[Result\\?:[^]]+\\?]",
    "TAG": r"\\?<\\?\|im\\?_start\\?\|\\?>",
}

_PROTOCOL_ENDS = r"\\?\[\\?/(?:ToolCalls|Call|ToolResults|CallParameter|ToolResult|Result)\\?]"
_TAG_END = r"\\?<\\?\|im\\?_end\\?\|\\?>"

if HINT_START_ANCHORS and HINT_END_ANCHORS:
    _starts = "|".join(re.escape(anchor) for anchor in HINT_START_ANCHORS)
    _START_PATTERNS["HINT"] = rf"\n?(?:{_starts}):?\s*"

_master_parts = [f"(?P<{name}_START>{pattern})" for name, pattern in _START_PATTERNS.items()]
_master_parts.extend((f"(?P<PROTOCOL_EXIT>{_PROTOCOL_ENDS})", f"(?P<TAG_EXIT>{_TAG_END})"))
if HINT_START_ANCHORS and HINT_END_ANCHORS:
    _ends = "|".join(re.escape(anchor) for anchor in HINT_END_ANCHORS)
    _master_parts.append(f"(?P<HINT_EXIT>(?:{_ends})\n?)")

STREAM_MASTER_RE = re.compile("|".join(_master_parts), re.IGNORECASE)

# Partial markers held back until the next chunk completes them.
_PARTIAL_MARKER = r"\\|\\?\[[^]]*|\\?<\\?\|?i?m?\\?_?(?:s?t?a?r?t?|e?n?d?)\\?\|?\\?>?"

# Hint anchors are prose, so a chunk boundary inside one leaks the header.
# The line-start requirement spares ordinary words sharing a prefix.
_partial_anchors = sorted(
    {
        anchor[:length]
        for anchor in (*HINT_START_ANCHORS, *HINT_END_ANCHORS)
        for length in range(1, len(anchor))
    },
    key=len,
    reverse=True,
)
if _partial_anchors:
    _partial_anchor_alt = "|".join(re.escape(prefix) for prefix in _partial_anchors)
    _PARTIAL_MARKER = rf"{_PARTIAL_MARKER}|(?:^|\n)(?:{_partial_anchor_alt})"

STREAM_TAIL_RE = re.compile(rf"(?:{_PARTIAL_MARKER})$", re.IGNORECASE)

# Flush discards what it matches, so it may only drop genuine protocol fragments.
STREAM_FLUSH_TAIL_RE = re.compile(
    r"(?:\\|\\?\[[^]]*|\\?<\\?\|?i?m?\\?_?(?:s?t?a?r?t?|e?n?d?)\\?\|?\\?>?)$",
    re.IGNORECASE,
)


def add_tag(role: str, content: str, unclose: bool = False) -> str:
    """Surround content with ChatML role tags."""
    if role not in VALID_TAG_ROLES:
        logger.warning(f"Unknown role: {role}, returning content without tags")
        return content

    return f"<|im_start|>{role}\n{content}" + ("" if unclose else "\n<|im_end|>")


def normalize_llm_text(s: str) -> str:
    """
    Safely normalize LLM-generated text for both display and hashing.
    Includes: HTML unescaping, NFC normalization, and line ending standardization.
    """
    if not s:
        return ""

    s = html.unescape(s)
    s = unicodedata.normalize("NFC", s)
    return s.replace("\r\n", "\n").replace("\r", "\n")


def unescape_text(s: str) -> str:
    """Remove CommonMark backslash escapes from LLM-generated text."""
    return COMMONMARK_UNESCAPE_RE.sub(r"\1", s) if s else ""


def strip_markdown_fence(s: str) -> str:
    """
    Remove one outer Markdown code fence layer for protected LLM payloads.

    The fence length is detected from the opening fence so tool parameters and
    structured JSON can safely contain shorter backtick sequences inside.
    Any optional language tag on the opening fence is stripped cleanly.
    """
    s = s.strip()
    if not s:
        return ""

    match = PARAM_FENCE_RE.match(s)
    if not match:
        return s

    fence = match.group("fence")
    if len(s) < len(fence) * 2 or not s.endswith(fence):
        return s

    lines = s.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == fence:
        return "\n".join(lines[1:-1])

    return s[len(match.group(0)) : -len(fence)].strip()


def _parse_tool_argument_value(raw_value: str) -> JsonValue:
    """
    Convert a tagged tool argument into the most specific JSON-compatible value.

    JSON literals, arrays, and objects are preserved so downstream clients receive
    strict argument types, while plain text values remain strings for compatibility.
    """
    value = strip_markdown_fence(raw_value)
    if not value:
        return ""

    try:
        parsed_value: Any = orjson.loads(value)
    except orjson.JSONDecodeError:
        return value

    return parsed_value


def estimate_tokens(text: str | None) -> int:
    """Estimate the number of tokens heuristically based on character count."""
    return len(text) // 3 if text else 0


class StructuredOutputValidationError(ValueError):
    """Raised when model output cannot satisfy a requested JSON Schema."""


class SchemaEvaluationTimeoutError(ValueError):
    """Raised when a client-provided schema exhausts its regex evaluation budget."""


# One cumulative budget for every regex keyword in a response, so it is sized for total workload,
# not for a single pattern: a large conforming payload is ordinary, not pathological.
SCHEMA_REGEX_BUDGET_SECONDS: float = g_config.server.schema_validation_budget_seconds
_schema_regex_deadline: ContextVar[float | None] = ContextVar("schema_regex_deadline", default=None)
_bounded_validator_classes: dict[type[Any], type[Any]] = {}


def _bounded_regex_search(pattern: str, value: str) -> bool:
    """Search with the remaining request-scoped regex budget."""
    deadline = _schema_regex_deadline.get()
    remaining = SCHEMA_REGEX_BUDGET_SECONDS if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise SchemaEvaluationTimeoutError("JSON Schema regex evaluation exceeded its time limit")
    try:
        return regex.search(pattern, value, timeout=remaining) is not None
    except TimeoutError as exc:
        raise SchemaEvaluationTimeoutError(
            "JSON Schema regex evaluation exceeded its time limit"
        ) from exc


def _validate_bounded_pattern(validator, pattern, instance, schema):
    if validator.is_type(instance, "string") and not _bounded_regex_search(pattern, instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


def _validate_bounded_pattern_properties(validator, pattern_properties, instance, schema):
    if not validator.is_type(instance, "object"):
        return
    for pattern, subschema in pattern_properties.items():
        for key, value in instance.items():
            if _bounded_regex_search(pattern, key):
                yield from validator.descend(
                    value,
                    subschema,
                    path=key,
                    schema_path=pattern,
                )


def _validate_bounded_additional_properties(validator, additional, instance, schema):
    if not validator.is_type(instance, "object"):
        return

    properties = schema.get("properties", {})
    patterns = tuple(schema.get("patternProperties", {}))
    extras = {
        key
        for key in instance
        if key not in properties
        and not any(_bounded_regex_search(pattern, key) for pattern in patterns)
    }
    if validator.is_type(additional, "object"):
        for extra in extras:
            yield from validator.descend(instance[extra], additional, path=extra)
    elif not additional and extras:
        joined = ", ".join(repr(each) for each in sorted(extras, key=str))
        yield ValidationError(f"Additional properties are not allowed ({joined} unexpected)")


def _bounded_validator_for(schema: dict[str, Any]):
    """Return a dialect-appropriate validator with timeout-bounded regex keywords."""
    base = validator_for(schema)
    bounded = _bounded_validator_classes.get(base)
    if bounded is None:
        bounded = validators.extend(
            base,
            {
                "pattern": _validate_bounded_pattern,
                "patternProperties": _validate_bounded_pattern_properties,
                "additionalProperties": _validate_bounded_additional_properties,
            },
        )
        _bounded_validator_classes[base] = bounded
    return bounded(schema)


def validate_json_schema(schema: dict[str, Any]) -> None:
    """Raise ValueError when a client-provided JSON Schema is not valid."""
    try:
        validator_cls = validator_for(schema)
        validator_cls.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"Invalid JSON Schema: {exc.message}") from exc


_JSON_SCHEMA_TYPE_NAMES = frozenset(
    {"string", "number", "integer", "boolean", "array", "object", "null"}
)
_NESTED_SCHEMA_KEYS = frozenset(
    {"items", "additionalProperties", "not", "if", "then", "else", "contains", "propertyNames"}
)
_SCHEMA_LIST_KEYS = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})
_SCHEMA_MAP_KEYS = frozenset({"properties", "$defs", "definitions", "patternProperties"})
# OpenAPI-only annotations with no JSON Schema equivalent.
_OPENAPI_ONLY_KEYS = frozenset({"propertyOrdering", "example"})


def normalize_openapi_schema(schema: Any) -> Any:
    """Translate Gemini's OpenAPI 3.0 Schema subset into equivalent JSON Schema.

    `generationConfig.responseSchema` spells its types in uppercase (`STRING`, `OBJECT`) and
    marks optional values with OpenAPI's `nullable` flag. Neither is valid JSON Schema, so the
    schema has to be translated before it can be checked or used to validate a response.
    `responseJsonSchema` is already JSON Schema and does not go through here.
    """
    if isinstance(schema, list):
        return [normalize_openapi_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _OPENAPI_ONLY_KEYS:
            continue
        if key == "type" and isinstance(value, str) and value.lower() in _JSON_SCHEMA_TYPE_NAMES:
            result[key] = value.lower()
        elif key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
            result[key] = {name: normalize_openapi_schema(sub) for name, sub in value.items()}
        elif key in _SCHEMA_LIST_KEYS and isinstance(value, list):
            result[key] = [normalize_openapi_schema(sub) for sub in value]
        elif key in _NESTED_SCHEMA_KEYS:
            result[key] = normalize_openapi_schema(value)
        else:
            result[key] = value

    if result.pop("nullable", None) is True:
        declared = result.get("type")
        if isinstance(declared, str):
            result["type"] = [declared, "null"]
        elif isinstance(declared, list) and "null" not in declared:
            result["type"] = [*declared, "null"]
    return result


def decode_base64_data(value: str | bytes) -> bytes:
    """Decode raw or data-URL Base64 strictly, ignoring transport whitespace.

    Both the standard and URL-safe alphabets are accepted, since clients that build a payload
    with `base64.urlsafe_b64encode` send `-` and `_`. Validation stays on either way: a decode
    that silently discarded stray characters would hand Gemini a corrupt file - which is also
    why a non-ASCII character is an error rather than something to strip, since dropping it
    could turn a corrupt payload into one that decodes cleanly to the wrong bytes.
    """
    if isinstance(value, str):
        try:
            raw = value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("Base64 payload contains non-ASCII characters") from exc
    else:
        raw = value
    if raw.startswith(b"data:"):
        metadata, separator, raw = raw.partition(b",")
        if not separator or b";base64" not in metadata.lower():
            raise ValueError("Data URL must contain a Base64 payload")

    payload = b"".join(raw.split())
    for altchars in (None, b"-_"):
        try:
            return base64.b64decode(payload, altchars=altchars, validate=True)
        except ValueError:
            continue
    raise ValueError("Invalid Base64 payload")


def guess_extension_for_mime(mime_type: str | None) -> str:
    """Best-effort filename extension for a MIME type, never empty.

    `mimetypes` only knows registered types, so unregistered but widely sent ones (`audio/mp3`,
    `application/x-*`) fall back to the subtype. That subtype is client-controlled and ends up in
    a `NamedTemporaryFile` suffix, so it is scrubbed of anything that could escape the directory.
    """
    if not mime_type:
        return ".bin"

    mime_type = mime_type.split(";")[0].strip()
    if suffix := mimetypes.guess_extension(mime_type):
        return suffix

    _, _, subtype = mime_type.partition("/")
    subtype = MIME_SUBTYPE_UNSAFE_RE.sub("", subtype).lstrip(".")
    return f".{subtype}" if subtype else ".bin"


def _mime_from_data_url(value: str | bytes) -> str:
    """MIME type declared by a data URL, or "" for anything else."""
    head = value[:200].decode("ascii", "replace") if isinstance(value, bytes) else value[:200]
    if not head.startswith("data:"):
        return ""
    return head[5:].partition(",")[0].split(";")[0].strip()


# Leading signatures of the formats that actually arrive as chat attachments. Checked in
# order, so the longer/more specific patterns come first.
_MAGIC_SUFFIXES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"II*\x00", ".tiff"),
    (b"MM\x00*", ".tiff"),
    (b"OggS", ".ogg"),
    (b"fLaC", ".flac"),
    (b"ID3", ".mp3"),
    (b"\x1f\x8b", ".gz"),
)

# Inside a Zip container the first entry names tell an Office document apart from a plain
# archive, and Google treats those very differently.
_ZIP_MEMBER_SUFFIXES: tuple[tuple[bytes, str], ...] = (
    (b"word/", ".docx"),
    (b"xl/", ".xlsx"),
    (b"ppt/", ".pptx"),
)


def _sniff_suffix(data: bytes) -> str:
    """Extension implied by the bytes themselves, or "" when nothing matches.

    Last resort for a payload that arrived with no filename and no MIME type. Google needs
    *some* usable extension: a suffix-less upload is not reliably classified, and the same
    bytes that read fine as `.pdf` come back as "I cannot read this file" with no suffix.
    """
    for signature, suffix in _MAGIC_SUFFIXES:
        if data.startswith(signature):
            return suffix

    if data.startswith(b"PK\x03\x04"):
        head = data[:4096]
        for member, suffix in _ZIP_MEMBER_SUFFIXES:
            if member in head:
                return suffix
        return ".zip"

    if data[:4] == b"RIFF" and len(data) >= 12:
        return {b"WAVE": ".wav", b"WEBP": ".webp", b"AVI ": ".avi"}.get(data[8:12], "")

    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".m4a" if data[8:12].startswith(b"M4A") else ".mp4"

    # An MPEG audio frame sync, for an mp3 that carries no ID3 tag.
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return ".mp3"

    # Anything that is legible UTF-8 is worth sending as text rather than as an unlabelled
    # blob; control bytes other than the usual whitespace mean it is not.
    try:
        text = data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return ""
    if text and not any(ord(c) < 32 and c not in "\t\r\n\f\v" for c in text):
        return ".txt"
    return ""


def _suffix_for_upload(file_in_base64: str | bytes, file_name: str, data: bytes) -> str:
    """Pick the temp-file extension to hand Gemini's uploader.

    Gemini will not read an upload whose temp file ends in `.bin` even when the bytes are
    intact, so `.bin` is never used as a fallback. A caller-supplied filename wins; failing
    that the data URL's own MIME type is asked, which is the only extension hint an
    OpenAI-style `file_data` payload carries; failing that the bytes are sniffed. Leaving the
    file suffix-less is the last resort and not a reliable one - measured against the live
    API, the same PDF came back readable as `.pdf` and unreadable with no suffix at all.
    """
    if file_name and (suffix := Path(file_name).suffix):
        return suffix
    mime = _mime_from_data_url(file_in_base64)
    if mime and (suffix := guess_extension_for_mime(mime)) != ".bin":
        return suffix
    return _sniff_suffix(data)


async def save_file_to_tempfile(
    file_in_base64: str | bytes, file_name: str = "", tempdir: Path | None = None
) -> Path:
    """Decode base64 file data and save to a temporary file.

    The payload goes through `decode_base64_data`, not a bare `base64.b64decode`: clients send
    `file_data` in OpenAI's official form, a `data:<mime>;base64,` URL, and a bare decode keeps
    only the prefix characters that happen to be in the Base64 alphabet instead of stripping
    them - silently yielding a corrupt file rather than raising.
    """
    started = time.perf_counter()
    input_size = len(file_in_base64)
    decoded = decode_base64_data(file_in_base64)
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=_suffix_for_upload(file_in_base64, file_name, decoded), dir=tempdir
    ) as tmp:
        tmp.write(decoded)
        path = Path(tmp.name)
    logger.info(
        "Saved base64 upload to temp file: filename={}, input_bytes={}, decoded_bytes={}, path={}, elapsed={:.3f}s",
        file_name or "<none>",
        input_size,
        len(decoded),
        path,
        time.perf_counter() - started,
    )
    return path


def _is_public_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if mapped := getattr(ip, "ipv4_mapped", None):
        ip = mapped
    return ip.is_global


def _validate_remote_url(url: str) -> tuple[str, str | None]:
    """Reject URLs that resolve anywhere other than a public address (SSRF guard).

    Returns the URL together with the address it was checked at, or None when no
    lookup happened -- an IP literal, or private fetches deliberately allowed. The
    caller pins the connection to that address so curl cannot resolve the name a
    second time and reach somewhere this function never approved.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in REMOTE_URL_SCHEMES:
        raise ValueError("Unsupported or unsafe URL")
    if g_config.gemini.allow_private_url_fetch:
        return url, None
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Unsupported or unsafe URL")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Unsupported or unsafe URL")

    # An address literal needs no lookup, so there is nothing to pin and nothing to leak.
    if _is_public_ip(hostname):
        return url, None

    if hostname == parsed.hostname and not re.fullmatch(r"[A-Za-z0-9.-]+", hostname):
        raise ValueError("Unsupported or unsafe URL")

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Unsupported or unsafe URL") from exc

    resolved_ips = {str(info[4][0]) for info in addr_infos}
    if not resolved_ips or any(not _is_public_ip(ip) for ip in resolved_ips):
        raise ValueError("Unsupported or unsafe URL")
    return url, sorted(resolved_ips)[0]


def _pin_options(url: str, pinned_ip: str | None, proxy: str | None) -> dict[CurlOpt, list[str]]:
    """Force curl to connect to the address `_validate_remote_url` actually approved.

    Without this the name is resolved twice -- once for the check, once by curl -- and
    the answer may differ in between, which is the whole of a DNS rebinding attack.

    A proxy defeats it, and does so silently: measured against this deployment's own
    SOCKS proxy, a request pinned to 127.0.0.1 still returned 200 from the real host,
    under socks5 and socks5h alike, because curl hands the destination to the proxy
    rather than dialling it. So the option is omitted rather than set and believed in.
    A proxied fetch keeps only the pre-flight check, which reads this resolver rather
    than the proxy's, and is therefore advisory. Saying that out loud is the point:
    the previous code left the same gap while looking like it had closed it.
    """
    if not pinned_ip or proxy:
        return {}

    parsed = urlparse(url)
    if not parsed.hostname:
        return {}
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    return {CurlOpt.RESOLVE: [f"{parsed.hostname.rstrip('.').lower()}:{port}:{pinned_ip}"]}


def _suffix_from_mime_or_url(mime_type: str | None, url: str | None = None) -> str:
    if mime_type:
        suffix = mimetypes.guess_extension(mime_type.split(";")[0].strip())
        if suffix:
            return suffix
    if url:
        suffix = Path(urlparse(url).path).suffix
        if suffix and re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
            return suffix
    return ".bin"


def _write_bytes_to_tempfile(data: bytes, suffix: str, tempdir: Path | None = None) -> Path:
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=tempdir) as tmp:
        tmp.write(data)
        return Path(tmp.name)


async def _decode_data_url(url: str) -> tuple[bytes, str]:
    decode_started = time.perf_counter()
    try:
        metadata_part, payload = url.split(",", 1)
    except ValueError as exc:
        raise ValueError("Invalid data URL") from exc

    # Coarse bound on the raw payload so a hostile one is not decoded just to be measured;
    # the exact cap is enforced on the decoded bytes below. The doubling is slack for the
    # line separators a client may have wrapped the Base64 with.
    if len(payload) > (((MAX_REMOTE_MEDIA_BYTES + 2) // 3) * 4 + 8192) * 2:
        raise ValueError("Remote media is too large")

    metadata = metadata_part[5:]
    parts = metadata.split(";") if metadata else []
    mime_type = parts[0] if parts and "/" in parts[0] else "application/octet-stream"
    if "base64" not in {part.lower() for part in parts[1:]}:
        raise ValueError("Invalid data URL")

    # `decode_base64_data`, not a bare `b64decode(validate=True)`: the strict form rejects
    # the newline-wrapped Base64 that 76-column encoders emit and the URL-safe alphabet that
    # `urlsafe_b64encode` clients send, so a perfectly good image came back as a 503. The
    # file-attachment path has always accepted both; this one has no reason to be stricter.
    try:
        data = await asyncio.to_thread(decode_base64_data, payload)
    except ValueError as exc:
        raise ValueError("Invalid data URL") from exc
    if len(data) > MAX_REMOTE_MEDIA_BYTES:
        raise ValueError("Remote media is too large")

    logger.info(
        "Decoded image data URL: mime_type={}, encoded_bytes={}, decoded_bytes={}, elapsed={:.3f}s",
        mime_type,
        len(payload),
        len(data),
        time.perf_counter() - decode_started,
    )
    return data, _suffix_from_mime_or_url(mime_type)


async def save_url_to_tempfile(
    url: str,
    tempdir: Path | None = None,
    *,
    proxy: str | None = None,
    impersonate: str | None = None,
) -> Path:
    """Download content from a URL and save to a temporary file.

    Redirects are followed manually so every hop is re-validated -- letting
    curl follow them would let a public URL bounce into the private network.
    """
    started = time.perf_counter()
    data: bytes
    suffix: str
    source = "data_url" if url.startswith("data:") else "remote_url"

    if url.startswith("data:"):
        data, suffix = await _decode_data_url(url)
    else:
        download_started = time.perf_counter()
        current_url, pinned_ip = _validate_remote_url(url)
        async with requests.AsyncSession(
            impersonate=cast(BrowserTypeLiteral, impersonate or "chrome"),
            proxy=proxy,
            allow_redirects=False,
            # Upstream hit QUIC idle timeouts forcing HTTP/3 here; let curl
            # negotiate the version instead (upstream 7b2b32f).
            http_version=CurlHttpVersion.NONE,
            timeout=g_config.gemini.url_fetch_timeout,
        ) as client:
            buffer = bytearray()
            oversized = False

            def receive_chunk(chunk: bytes) -> None:
                # Enforced as the body arrives so an oversized response is
                # aborted mid-transfer rather than buffered in full and then
                # rejected (upstream 85d7a89 does the same). Raising here is what
                # aborts the transfer, but curl swallows the exception and
                # surfaces a generic RequestException, so the flag carries the
                # real reason back out to the caller.
                nonlocal oversized
                if len(buffer) + len(chunk) > MAX_REMOTE_MEDIA_BYTES:
                    oversized = True
                    raise ValueError("Remote media is too large")
                buffer.extend(chunk)

            for _ in range(MAX_REMOTE_REDIRECTS + 1):
                buffer.clear()
                oversized = False
                # curl_options is a session attribute rather than a request argument, and
                # it is read on every request, so each hop is re-pinned here: a redirect
                # reaches a different host, and carrying the previous hop's entry would
                # leave the new one unpinned without saying so.
                pin = _pin_options(current_url, pinned_ip, proxy)
                # curl_cffi types the mapping as dict[CurlOpt, str], but CURLOPT_RESOLVE is
                # an slist and only accepts a list: handed a plain string, curl walks it one
                # character at a time and fails with "Could not parse CURLOPT_RESOLVE entry".
                client.curl_options = cast(dict[CurlOpt, str], pin)
                try:
                    resp = await client.get(current_url, content_callback=receive_chunk)
                except Exception as exc:
                    if oversized:
                        raise ValueError("Remote media is too large") from exc
                    raise
                if resp.status_code in REDIRECT_STATUS_CODES:
                    location = resp.headers.get("location")
                    if not location:
                        raise ValueError("Unsafe redirect")
                    current_url, pinned_ip = _validate_remote_url(urljoin(current_url, location))
                    continue

                resp.raise_for_status()
                if content_length := resp.headers.get("content-length"):
                    try:
                        if int(content_length) > MAX_REMOTE_MEDIA_BYTES:
                            raise ValueError("Remote media is too large")
                    except ValueError as exc:
                        raise ValueError("Remote media is too large") from exc
                data = bytes(buffer)
                content_type = resp.headers.get("content-type")
                suffix = _suffix_from_mime_or_url(content_type, current_url)
                logger.info(
                    "Downloaded upload URL: url={}, status_code={}, content_type={}, bytes={}, "
                    "proxied={}, impersonate={}, address_pinned={}, elapsed={:.3f}s",
                    reprlib.repr(current_url),
                    resp.status_code,
                    content_type,
                    len(data),
                    bool(proxy),
                    impersonate or "chrome",
                    bool(pin),
                    time.perf_counter() - download_started,
                )
                break
            else:
                raise ValueError("Too many redirects")

    write_started = time.perf_counter()
    path = await asyncio.to_thread(_write_bytes_to_tempfile, data, suffix, tempdir)
    logger.info(
        "Saved URL upload to temp file: source={}, suffix={}, bytes={}, path={}, write_elapsed={:.3f}s,total_elapsed={:.3f}s",
        source,
        suffix,
        len(data),
        path,
        time.perf_counter() - write_started,
        time.perf_counter() - started,
    )
    return path


def strip_tagged_blocks(text: str) -> str:
    """
    Remove ChatML role blocks (<|im_start|>role...<|im_end|>).
    Role 'tool' blocks are removed entirely; others have markers stripped but content preserved.
    """
    if not text:
        return text

    result = []
    idx = 0
    while idx < len(text):
        match_start = CHATML_START_RE.search(text, idx)
        if not match_start:
            result.append(text[idx:])
            break

        result.append(text[idx : match_start.start()])
        role = match_start.group(1).lower()
        content_start = match_start.end()

        match_end = CHATML_END_RE.search(text, content_start)
        if not match_end:
            if role != "tool":
                result.append(text[content_start:])
            break

        if role != "tool":
            result.append(text[content_start : match_end.start()])
        idx = match_end.end()

    return "".join(result)


def strip_system_hints(text: str) -> str:
    """Remove system hints, ChatML tags, and technical protocol markers from text."""
    if not text:
        return text

    t_unescaped = unescape_text(text)

    cleaned = t_unescaped
    for hint in SYSTEM_HINTS:
        cleaned = cleaned.replace(hint, "").replace(hint.strip(), "")

    for pattern in (*HINT_FULL_RES, *HINT_START_RES, *HINT_END_RES):
        cleaned = pattern.sub("", cleaned)

    cleaned = strip_tagged_blocks(cleaned)
    cleaned = CONTROL_TOKEN_RE.sub("", cleaned)
    cleaned = TOOL_BLOCK_RE.sub("", cleaned)
    cleaned = TOOL_CALL_RE.sub("", cleaned)
    cleaned = RESPONSE_BLOCK_RE.sub("", cleaned)
    cleaned = RESPONSE_ITEM_RE.sub("", cleaned)
    cleaned = TAGGED_ARG_RE.sub("", cleaned)
    return TAGGED_RESULT_RE.sub("", cleaned)


def _process_tools_internal(text: str, extract: bool = True) -> tuple[str, list[AppToolCall]]:
    """
    Extract tool metadata and return text stripped of technical markers.
    Tagged arguments preserve JSON-compatible types and receive deterministic call IDs.
    """
    if not text:
        return text, []

    tool_calls: list[AppToolCall] = []

    def _create_tool_call(name: str, raw_args: str) -> None:
        if not extract:
            return
        if not name:
            logger.warning("Encountered tool_call without a function name.")
            return

        name = unescape_text(name.strip())
        raw_args = unescape_text(raw_args)

        # Leftovers mean the call was cut short: drop it rather than emit partial arguments.
        residue = TAGGED_ARG_RE.sub("", raw_args).strip()
        if residue:
            logger.warning(
                f"Dropping malformed tool call '{name}'. Unparsed content: {reprlib.repr(residue)}"
            )
            return

        arg_matches = TAGGED_ARG_RE.findall(raw_args)
        args_dict = {
            arg_name.strip(): _parse_tool_argument_value(arg_value)
            for arg_name, arg_value in arg_matches
        }
        arguments = orjson.dumps(args_dict).decode("utf-8")
        logger.debug(f"Successfully parsed {len(args_dict)} arguments for tool: {name}")

        index = len(tool_calls)
        seed = f"{name}:{arguments}:{index}".encode()
        call_id = f"call_{hashlib.sha256(seed).hexdigest()[:24]}"

        tool_calls.append(
            AppToolCall(
                id=call_id,
                type="function",
                function=AppToolCallFunction(name=name, arguments=arguments),
            )
        )

    for match in TOOL_CALL_RE.finditer(text):
        _create_tool_call(match.group(1), match.group(2))

    cleaned = strip_system_hints(text)
    return cleaned, tool_calls


def remove_tool_call_blocks(text: str) -> str:
    """Strip tool call blocks from text for display."""
    cleaned, _ = _process_tools_internal(text, extract=False)
    return cleaned


def extract_tool_calls(text: str) -> tuple[str, list[AppToolCall]]:
    """Extract tool calls and return cleaned text."""
    return _process_tools_internal(text, extract=True)


def text_from_message(message: AppMessage) -> str:
    """Concatenate text and tool arguments from a message for token estimation."""
    base_text = ""
    if isinstance(message.content, str):
        base_text = message.content
    elif isinstance(message.content, list):
        base_text = "\n".join(
            item.text or "" for item in message.content if getattr(item, "type", "") == "text"
        )
    elif message.content is None:
        base_text = ""

    if message.tool_calls:
        tool_arg_text = "".join(call.function.arguments or "" for call in message.tool_calls)
        base_text = f"{base_text}\n{tool_arg_text}" if base_text else tool_arg_text

    return base_text


def extract_image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Return image dimensions (width, height) if PNG or JPEG headers are present."""
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        try:
            width, height = struct.unpack(">II", data[16:24])
            return int(width), int(height)
        except struct.error:
            return None, None

    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        idx = 2
        length = len(data)
        sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while idx < length:
            if data[idx] != 0xFF:
                idx += 1
                continue
            while idx < length and data[idx] == 0xFF:
                idx += 1
            if idx >= length:
                break
            marker = data[idx]
            idx += 1
            if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                continue
            if idx + 1 >= length:
                break
            segment_length = (data[idx] << 8) + data[idx + 1]
            idx += 2
            if segment_length < 2:
                break
            if marker in sof_markers:
                if idx + 4 < length:
                    height = (data[idx + 1] << 8) + data[idx + 2]
                    width = (data[idx + 3] << 8) + data[idx + 4]
                    return int(width), int(height)
                break
            idx += segment_length - 2
    return None, None


def detect_image_extension(data: bytes) -> str | None:
    """Detect image extension from magic bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"GIF8"):
        return ".gif"
    return ".webp" if data.startswith(b"RIFF") and data[8:12] == b"WEBP" else None


def dump_model(model: BaseModel) -> dict[str, Any]:
    """Serialize a Pydantic model into a JSON-compatible dict with None values excluded."""
    return model.model_dump(mode="json", exclude_none=True)


def serialize_tools_for_response(tools: Sequence[Any] | None) -> list[dict[str, Any]]:
    """Serialize tool objects into clean dictionary representations without None values."""
    if not tools:
        return []
    result: list[dict[str, Any]] = []
    for t in tools:
        if hasattr(t, "model_dump"):
            result.append(t.model_dump(exclude_none=True))
        elif hasattr(t, "dict"):
            result.append(t.dict(exclude_none=True))
        elif isinstance(t, dict):
            result.append({k: v for k, v in t.items() if v is not None})
        else:
            result.append(t)
    return result


def serialize_tool_choice_for_response(tool_choice: Any) -> Any:
    """Serialize tool choice object into a clean dictionary or string representation."""
    if tool_choice is None:
        return "auto"
    if hasattr(tool_choice, "model_dump"):
        return tool_choice.model_dump(exclude_none=True)
    if hasattr(tool_choice, "dict"):
        return tool_choice.dict(exclude_none=True)
    return tool_choice


def calculate_usage(
    messages: list[AppMessage],
    assistant_text: str | None,
    tool_calls: list[AppToolCall] | None,
    thoughts: str | None = None,
) -> tuple[int, int, int, int]:
    """Calculate prompt, completion, total and reasoning tokens consistently."""
    prompt_tokens = sum(estimate_tokens(text_from_message(msg)) for msg in messages)
    tool_args_text = ""
    if tool_calls:
        for call in tool_calls:
            tool_args_text += call.function.arguments or ""

    completion_basis = assistant_text or ""
    if tool_args_text:
        completion_basis = (
            f"{completion_basis}\n{tool_args_text}" if completion_basis else tool_args_text
        )

    completion_tokens = estimate_tokens(completion_basis)
    reasoning_tokens = estimate_tokens(thoughts) if thoughts else 0
    total_completion_tokens = completion_tokens + reasoning_tokens

    return (
        prompt_tokens,
        total_completion_tokens,
        prompt_tokens + total_completion_tokens,
        reasoning_tokens,
    )


def normalize_app_message_role(role_name: str) -> AppMessageRole:
    """Normalize and validate input role string to a valid AppMessage role."""
    roles: dict[str, AppMessageRole] = {
        "developer": "system",
        "function": "tool",
        "user": "user",
        "assistant": "assistant",
        "tool": "tool",
        "system": "system",
    }
    return roles.get(role_name, "system")


def convert_to_app_messages(messages: list[ChatCompletionMessage]) -> list[AppMessage]:
    """Convert OpenAI ChatCompletionMessage list into AppMessage format."""
    app_messages: list[AppMessage] = []
    for msg in messages:
        app_content: str | list[AppContentItem] | None = None
        if isinstance(msg.content, str):
            app_content = msg.content
        elif isinstance(msg.content, list):
            app_content = []
            for item in msg.content:
                if item.type == "text":
                    app_content.append(AppContentItem(type="text", text=item.text))
                elif item.type == "image_url":
                    media_dict = getattr(item, "image_url", None)
                    url = media_dict.get("url") if media_dict else None
                    app_content.append(AppContentItem(type="image_url", url=url))
                elif item.type == "file":
                    file_dict = getattr(item, "file", None)
                    filename = file_dict.get("filename") if file_dict else None
                    file_data = file_dict.get("file_data") if file_dict else None
                    app_content.append(
                        AppContentItem(type="file", filename=filename, file_data=file_data)
                    )
                elif item.type == "input_audio":
                    audio_dict = getattr(item, "input_audio", None)
                    audio_data = audio_dict.get("data") if audio_dict else None
                    app_content.append(
                        AppContentItem(
                            type="input_audio",
                            file_data=audio_data,
                            raw_data=audio_dict,
                        )
                    )
                elif item.type in ("refusal", "reasoning"):
                    text_val = getattr(item, "text", None) or getattr(item, item.type, None)
                    app_content.append(AppContentItem(type=item.type, text=text_val))

        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                AppToolCall(
                    id=tc.id,
                    type="function",
                    function=AppToolCallFunction(
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                    ),
                )
                for tc in msg.tool_calls
            ]

        role = normalize_app_message_role(msg.role)

        app_messages.append(
            AppMessage(
                role=role,
                content=app_content,
                tool_calls=tool_calls,
                tool_call_id=msg.tool_call_id,
                name=msg.name,
                reasoning_content=getattr(msg, "reasoning_content", None),
            )
        )
    return app_messages


def canonicalize_structured_output(
    visible_output: str, structured_requirement: StructuredOutputRequirement
) -> str | None:
    """Parse raw or fenced structured JSON and return its canonical JSON representation.

    `None` means the model failed the format, never that this wrapper could not run the check:
    a schema that cannot be evaluated still yields the canonical payload, so only the model's
    own failures can be enforced against it.
    """
    candidate = strip_markdown_fence(visible_output)
    try:
        structured_payload = orjson.loads(candidate)
    except orjson.JSONDecodeError:
        logger.warning(
            f"Failed to decode JSON for structured response (schema={structured_requirement.schema_name})."
        )
        return None

    # An empty schema is JSON mode: parsing was the whole requirement.
    if structured_requirement.schema:
        try:
            deadline_token = _schema_regex_deadline.set(
                time.monotonic() + SCHEMA_REGEX_BUDGET_SECONDS
            )
            try:
                _bounded_validator_for(structured_requirement.schema).validate(structured_payload)
            finally:
                _schema_regex_deadline.reset(deadline_token)
        except ValidationError as exc:
            logger.warning(
                f"Structured response failed schema validation "
                f"(schema={structured_requirement.schema_name}): {exc.message}"
            )
            return None
        # Both branches below are this wrapper failing to check, not the model failing to comply,
        # so neither reports a violation: under `strict` that would 502 a conforming reply.
        except SchemaEvaluationTimeoutError as exc:
            logger.warning(
                f"Structured response left unverified, schema evaluation timed out "
                f"(schema={structured_requirement.schema_name}): {exc}"
            )
        except Exception as exc:
            # `check_schema` does not resolve `$ref`s, so unresolvable references and foreign
            # dialects surface only here.
            logger.warning(
                f"Structured response left unverified, schema is not usable "
                f"({structured_requirement.schema_name!r}): {exc}"
            )

    canonical_output = orjson.dumps(structured_payload).decode("utf-8")
    logger.debug(f"Structured response fulfilled (schema={structured_requirement.schema_name}).")
    return canonical_output


def process_llm_output(
    thoughts: str | None,
    raw_text: str,
    structured_requirement: StructuredOutputRequirement | None,
) -> tuple[str | None, str, str, list[AppToolCall]]:
    """
    Post-process Gemini output to extract tool calls, unwrap structured JSON fences, and prepare clean text for display and storage.
    Returns: (thoughts, visible_text, storage_output, tool_calls)
    """
    if thoughts:
        thoughts = thoughts.strip()

    raw_text = ARTIFACT_URL_RE.sub("", raw_text)
    visible_output, tool_calls = extract_tool_calls(raw_text)
    if tool_calls:
        logger.debug(f"Detected {len(tool_calls)} tool call(s) in model output.")

    visible_output = visible_output.strip()
    storage_output = visible_output

    if structured_requirement and visible_output:
        canonical_output = canonicalize_structured_output(visible_output, structured_requirement)
        if canonical_output is not None:
            visible_output = canonical_output
            storage_output = canonical_output
        elif tool_calls:
            # The format constrains the final answer, not a turn that asks for a tool.
            logger.debug(
                "Skipping structured-output enforcement for a turn that returned tool call(s)."
            )
        elif structured_requirement.strict:
            raise StructuredOutputValidationError(
                f"Model output did not satisfy JSON Schema {structured_requirement.schema_name!r}"
            )
        else:
            logger.warning(
                f"Returning unstructured text for best-effort response format "
                f"{structured_requirement.schema_name!r}."
            )

    return thoughts, visible_output, storage_output, tool_calls


def extract_tool_info(tool: Any) -> tuple[str, str, dict[str, Any] | None]:
    """Extract (name, description, parameters) from any tool representation."""
    if hasattr(tool, "function") and tool.function is not None:
        fn = tool.function
        if isinstance(fn, dict):
            name = fn.get("name", "")
            description = fn.get("description") or "No description provided."
            parameters = fn.get("parameters")
        else:
            name = getattr(fn, "name", "")
            description = getattr(fn, "description", None) or "No description provided."
            parameters = getattr(fn, "parameters", None)
        return name, description, parameters

    if isinstance(tool, dict):
        if "function" in tool and isinstance(tool["function"], dict):
            fn = tool["function"]
            return (
                fn.get("name", ""),
                fn.get("description") or "No description provided.",
                fn.get("parameters"),
            )
        return (
            tool.get("name", ""),
            tool.get("description") or "No description provided.",
            tool.get("parameters"),
        )

    name = getattr(tool, "name", "")
    description = getattr(tool, "description", None) or "No description provided."
    parameters = getattr(tool, "parameters", None)
    return name, description, parameters


def extract_named_tool_choice(tool_choice: Any) -> str | None:
    """Extract target function name from any named tool choice representation."""
    if isinstance(tool_choice, ChatCompletionNamedToolChoice):
        return tool_choice.function.name
    if isinstance(tool_choice, ToolChoiceFunction):
        return tool_choice.name
    if isinstance(tool_choice, dict):
        if "function" in tool_choice and isinstance(tool_choice["function"], dict):
            return tool_choice["function"].get("name")
        return tool_choice.get("name")
    return None


def build_tool_prompt(
    tools: Sequence[Any],
    tool_choice: (
        Literal["none", "auto", "required"]
        | ChatCompletionNamedToolChoice
        | ToolChoiceFunction
        | ToolChoiceTypes
        | None
    ),
) -> str:
    """Generate a system prompt describing available tools and the PascalCase protocol."""
    if not tools:
        return ""

    lines: list[str] = [TOOL_INTERFACE_PROMPT]

    for tool in tools:
        name, description, parameters = extract_tool_info(tool)
        if not name:
            continue
        lines.append(TOOL_DESCRIPTION_PROMPT.format(name=name, description=description))
        if parameters:
            schema_text = orjson.dumps(parameters, option=orjson.OPT_SORT_KEYS).decode("utf-8")
            lines.extend((TOOL_ARGUMENTS_SCHEMA_PROMPT, schema_text))
        else:
            lines.append(TOOL_EMPTY_ARGUMENTS_SCHEMA_PROMPT)

    if tool_choice == "none":
        lines.append(TOOL_CHOICE_NONE_PROMPT)
    elif tool_choice == "required":
        lines.append(TOOL_CHOICE_REQUIRED_PROMPT)
    elif (target_name := extract_named_tool_choice(tool_choice)) is not None:
        lines.append(TOOL_CHOICE_NAMED_PROMPT.format(target_name=target_name))

    lines.append(TOOL_WRAP_HINT)

    return "\n".join(lines)


def build_image_generation_instruction(
    tools: list[ImageGeneration] | None,
    tool_choice: ToolChoiceTypes | None,
) -> str | None:
    """Construct explicit guidance so Gemini emits images when requested."""
    has_forced_choice = tool_choice is not None and tool_choice.type == "image_generation"
    primary = tools[0] if tools else None

    if not has_forced_choice and primary is None:
        return None

    instructions = [IMAGE_GENERATION_PROMPT]

    if has_forced_choice:
        instructions.append(IMAGE_GENERATION_FORCED_PROMPT)

    return "\n\n".join(instructions)


def append_tool_hint_to_last_user_message(messages: list[AppMessage]) -> None:
    """Ensure the last user message carries the tool wrap hint."""
    for msg in reversed(messages):
        if msg.role != "user" or msg.content is None:
            continue

        if isinstance(msg.content, str):
            if TOOL_HINT_STRIPPED not in msg.content:
                msg.content = f"{msg.content}\n{TOOL_WRAP_HINT}"
            return

        if isinstance(msg.content, list):
            for part in reversed(msg.content):
                if getattr(part, "type", None) != "text":
                    continue
                text_value = getattr(part, "text", "") or ""
                if TOOL_HINT_STRIPPED in text_value:
                    return
                part.text = f"{text_value}\n{TOOL_WRAP_HINT}"
                return

            messages_text = TOOL_WRAP_HINT.strip()
            msg.content.append(AppContentItem(type="text", text=messages_text))
            return
