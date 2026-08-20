import asyncio
import base64
import binascii
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
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlparse

import orjson
from curl_cffi import BrowserTypeLiteral, CurlHttpVersion, requests
from loguru import logger

from app.models import AppMessage, AppToolCall, AppToolCallFunction
from app.utils import g_config

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

MAX_REMOTE_MEDIA_BYTES = 25 * 1024 * 1024
MAX_REMOTE_REDIRECTS = 5
REMOTE_URL_SCHEMES = {"http", "https"}
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}

VALID_TAG_ROLES = {"user", "assistant", "system", "tool"}
TOOL_WRAP_HINT = (
    "\n\n### SYSTEM: TOOL CALLING PROTOCOL (MANDATORY) ###\n"
    "If tool execution is required, you MUST adhere to this EXACT protocol. No exceptions.\n\n"
    "1. OUTPUT RESTRICTION: Your response MUST contain ONLY the [ToolCalls] block. Conversational filler, preambles, or concluding remarks are STRICTLY PROHIBITED.\n"
    "2. WRAPPING LOGIC: Every parameter value MUST be enclosed in a markdown code block. Use 3 backticks (```) by default. If the value contains backticks, the outer fence MUST be longer than any sequence inside (e.g., ````).\n"
    "3. TAG SYMMETRY: All tags MUST be balanced and closed in the exact reverse order of opening. Incomplete or unclosed blocks are strictly prohibited.\n\n"
    "REQUIRED SYNTAX:\n"
    "[ToolCalls]\n"
    "[Call:tool_name]\n"
    "[CallParameter:parameter_name]\n"
    "```\n"
    "value\n"
    "```\n"
    "[/CallParameter]\n"
    "[/Call]\n"
    "[/ToolCalls]\n\n"
    "CRITICAL: Do NOT mix natural language with protocol tags. Either respond naturally OR provide the protocol block alone. There is no middle ground."
)
STRUCTURED_JSON_WRAP_HINT = (
    "\n\n### SYSTEM: STRUCTURED JSON PROTOCOL (MANDATORY) ###\n"
    "Return ONLY one markdown code block containing a single strict JSON document that conforms to the provided JSON Schema.\n"
    "Use ```json by default. If the JSON contains backticks, the outer fence MUST be longer than any backtick sequence inside (e.g., ````json).\n"
    "REQUIRED SYNTAX:\n"
    "```json\n"
    '{"field":"value"}\n'
    "```\n\n"
    "CRITICAL: Do NOT mix natural language with the fenced JSON block. Provide the protocol block alone. There is no middle ground."
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
PARAM_FENCE_RE = re.compile(r"^(?P<fence>`{3,})")
TOOL_HINT_STRIPPED = TOOL_WRAP_HINT.strip()
_hint_lines = [line.strip() for line in TOOL_WRAP_HINT.split("\n") if line.strip()]
TOOL_HINT_LINE_START = _hint_lines[0] if _hint_lines else ""
TOOL_HINT_LINE_END = _hint_lines[-1] if _hint_lines else ""
TOOL_HINT_START_ESC = re.escape(TOOL_HINT_LINE_START) if TOOL_HINT_LINE_START else ""
TOOL_HINT_END_ESC = re.escape(TOOL_HINT_LINE_END) if TOOL_HINT_LINE_END else ""

HINT_FULL_RE = (
    re.compile(rf"\n?{TOOL_HINT_START_ESC}:?.*?{TOOL_HINT_END_ESC}\n?", re.DOTALL | re.IGNORECASE)
    if TOOL_HINT_START_ESC and TOOL_HINT_END_ESC
    else None
)
HINT_START_RE = (
    re.compile(rf"\n?{TOOL_HINT_START_ESC}:?\s*", re.IGNORECASE) if TOOL_HINT_START_ESC else None
)
HINT_END_RE = (
    re.compile(rf"\s*{TOOL_HINT_END_ESC}\n?", re.IGNORECASE) if TOOL_HINT_END_ESC else None
)

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

if TOOL_HINT_START_ESC and TOOL_HINT_END_ESC:
    _START_PATTERNS["HINT"] = rf"\n?{TOOL_HINT_START_ESC}:?\s*"

_master_parts = [f"(?P<{name}_START>{pattern})" for name, pattern in _START_PATTERNS.items()]
_master_parts.extend((f"(?P<PROTOCOL_EXIT>{_PROTOCOL_ENDS})", f"(?P<TAG_EXIT>{_TAG_END})"))
if TOOL_HINT_START_ESC and TOOL_HINT_END_ESC:
    _master_parts.append(f"(?P<HINT_EXIT>{TOOL_HINT_END_ESC}\n?)")

STREAM_MASTER_RE = re.compile("|".join(_master_parts), re.IGNORECASE)
STREAM_TAIL_RE = re.compile(
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
    s = s.replace("\r\n", "\n").replace("\r", "\n")

    return s


def unescape_text(s: str) -> str:
    """Remove CommonMark backslash escapes from LLM-generated text."""
    return COMMONMARK_UNESCAPE_RE.sub(r"\1", s) if s else ""


def strip_markdown_fence(s: str) -> str:
    """
    Remove one outer Markdown code fence layer for protected LLM payloads.

    The fence length is detected from the opening fence so tool parameters and
    structured JSON can safely contain shorter backtick sequences inside.
    """
    s = s.strip()
    if not s:
        return ""

    match = PARAM_FENCE_RE.match(s)
    if not match or not s.endswith(match.group("fence")):
        return s

    fence = match.group("fence")
    lines = s.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == fence:
        return "\n".join(lines[1:-1])

    return s[len(fence) : -len(fence)].strip()


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


async def save_file_to_tempfile(
    file_in_base64: str | bytes, file_name: str = "", tempdir: Path | None = None
) -> Path:
    """Decode base64 file data and save to a temporary file."""
    started = time.perf_counter()
    input_size = len(file_in_base64)
    decoded = base64.b64decode(file_in_base64)
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=Path(file_name).suffix if file_name else ".bin", dir=tempdir
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


def _validate_remote_url(url: str) -> str:
    """Reject URLs that resolve anywhere other than a public address (SSRF guard)."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in REMOTE_URL_SCHEMES:
        raise ValueError("Unsupported or unsafe URL")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Unsupported or unsafe URL")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Unsupported or unsafe URL")

    if _is_public_ip(hostname):
        return url

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
    return url


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

    if len(payload) > ((MAX_REMOTE_MEDIA_BYTES + 2) // 3) * 4 + 8192:
        raise ValueError("Remote media is too large")

    metadata = metadata_part[5:]
    parts = metadata.split(";") if metadata else []
    mime_type = parts[0] if parts and "/" in parts[0] else "application/octet-stream"
    if "base64" not in {part.lower() for part in parts[1:]}:
        raise ValueError("Invalid data URL")

    try:
        data = await asyncio.to_thread(base64.b64decode, payload, validate=True)
    except binascii.Error as exc:
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
        current_url = _validate_remote_url(url)
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
                    current_url = _validate_remote_url(urljoin(current_url, location))
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
                    "Downloaded upload URL: url={}, status_code={}, content_type={}, bytes={}, proxied={}, impersonate={}, elapsed={:.3f}s",
                    reprlib.repr(current_url),
                    resp.status_code,
                    content_type,
                    len(data),
                    bool(proxy),
                    impersonate or "chrome",
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

    cleaned = t_unescaped.replace(TOOL_WRAP_HINT, "").replace(TOOL_HINT_STRIPPED, "")

    if HINT_FULL_RE:
        cleaned = HINT_FULL_RE.sub("", cleaned)
    if HINT_START_RE:
        cleaned = HINT_START_RE.sub("", cleaned)
    if HINT_END_RE:
        cleaned = HINT_END_RE.sub("", cleaned)

    cleaned = strip_tagged_blocks(cleaned)
    cleaned = CONTROL_TOKEN_RE.sub("", cleaned)
    cleaned = TOOL_BLOCK_RE.sub("", cleaned)
    cleaned = TOOL_CALL_RE.sub("", cleaned)
    cleaned = RESPONSE_BLOCK_RE.sub("", cleaned)
    cleaned = RESPONSE_ITEM_RE.sub("", cleaned)
    cleaned = TAGGED_ARG_RE.sub("", cleaned)
    cleaned = TAGGED_RESULT_RE.sub("", cleaned)

    return cleaned


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

        arg_matches = TAGGED_ARG_RE.findall(raw_args)
        if arg_matches:
            args_dict = {
                arg_name.strip(): _parse_tool_argument_value(arg_value)
                for arg_name, arg_value in arg_matches
            }
            arguments = orjson.dumps(args_dict).decode("utf-8")
            logger.debug(f"Successfully parsed {len(args_dict)} arguments for tool: {name}")
        else:
            cleaned_raw = raw_args.strip()
            if not cleaned_raw:
                logger.debug(f"Successfully parsed 0 arguments for tool: {name}")
            else:
                logger.warning(
                    f"Malformed arguments for tool '{name}'. Text found but no valid tags: {reprlib.repr(cleaned_raw)}"
                )
            arguments = "{}"

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
