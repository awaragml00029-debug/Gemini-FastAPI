"""Attachment decoding: what `save_file_to_tempfile` hands Gemini's uploader.

Both properties pinned here were regressions found in production, and both fail silently -
the upload succeeds and only the model's answer shows the damage - so they get explicit
tests rather than relying on an end-to-end check noticing.
"""

import asyncio
import base64
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.models.core import AppContentItem, AppMessage
from app.server.chat import _process_conversation_for_client
from app.services.client import GeminiClientWrapper
from app.utils.helper import (
    MAX_REMOTE_MEDIA_BYTES,
    _decode_data_url,
    _sniff_suffix,
    _suffix_for_upload,
    save_file_to_tempfile,
)

PDF = b"%PDF-1.4\nSECRET_CODE=REGRESSION-PIN\n%%EOF\n"
PDF_B64 = base64.b64encode(PDF).decode()
PDF_DATA_URL = f"data:application/pdf;base64,{PDF_B64}"


@pytest.mark.parametrize(
    ("payload", "file_name"),
    [
        (PDF_B64, "probe.pdf"),
        (PDF_B64.encode(), "probe.pdf"),
        (PDF_DATA_URL, "probe.pdf"),
        (PDF_DATA_URL, ""),
        (PDF_DATA_URL.encode(), ""),
    ],
)
def test_payload_survives_decoding(payload, file_name, tmp_path):
    """A `data:` prefix must be stripped, not fed to the Base64 decoder.

    A bare `base64.b64decode` keeps whichever prefix characters happen to sit in the Base64
    alphabet, so `data:application/pdf;base64,` shifts the payload and yields a corrupt file
    without raising.
    """
    path = asyncio.run(save_file_to_tempfile(payload, file_name, tmp_path))
    assert path.read_bytes() == PDF


@pytest.mark.parametrize(
    ("payload", "file_name", "expected"),
    [
        (PDF_B64, "probe.pdf", ".pdf"),
        (PDF_DATA_URL, "report.pdf", ".pdf"),
        (PDF_DATA_URL, "", ".pdf"),
        (f"data:image/png;base64,{PDF_B64}", "", ".png"),
        # No usable hint from the caller: fall through to the bytes themselves. Measured
        # against the live API, the same PDF is readable as `.pdf` and unreadable with no
        # suffix at all, so guessing from the content beats shipping it unlabelled.
        (PDF_B64, "", ".pdf"),
        (PDF_B64, "report", ".pdf"),
        (f"data:application/octet-stream;base64,{PDF_B64}", "", ".pdf"),
    ],
)
def test_suffix_never_falls_back_to_bin(payload, file_name, expected, tmp_path):
    """Gemini refuses to read an upload saved as `.bin`, even when the bytes are intact."""
    path = asyncio.run(save_file_to_tempfile(payload, file_name, tmp_path))
    assert path.suffix == expected


@pytest.mark.parametrize("payload", ["not!!base64!!", "data:application/pdf,notbase64"])
def test_malformed_payload_raises_instead_of_corrupting(payload, tmp_path):
    """Better a rejected request than an upload the model reads as noise."""
    with pytest.raises(ValueError, match=r"Base64|Data URL"):
        asyncio.run(save_file_to_tempfile(payload, "probe.pdf", tmp_path))


# --- image data URLs -------------------------------------------------------------------
#
# The attachment path has always tolerated whitespace and the URL-safe alphabet; the image
# path used a strict `b64decode` and turned both into a 503 for an otherwise valid image.

IMAGE = b"\x89PNG\r\n\x1a\n" + bytes(range(250, 256)) * 8
IMAGE_B64 = base64.b64encode(IMAGE).decode()


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("unwrapped", IMAGE_B64),
        (
            "wrapped at 76 columns",
            "\n".join(IMAGE_B64[i : i + 76] for i in range(0, len(IMAGE_B64), 76)),
        ),
        (
            "wrapped at 40 columns",
            "\n".join(IMAGE_B64[i : i + 40] for i in range(0, len(IMAGE_B64), 40)),
        ),
        ("url-safe alphabet", base64.urlsafe_b64encode(IMAGE).decode()),
    ],
)
def test_image_data_url_accepts_what_clients_actually_send(label, payload):
    data, suffix = asyncio.run(_decode_data_url(f"data:image/png;base64,{payload}"))
    assert data == IMAGE, label
    assert suffix == ".png"


@pytest.mark.parametrize(
    "url",
    [
        "data:image/png;base64,not!!base64!!",
        "data:image/png,notdeclaredbase64",
        "no-comma-at-all",
    ],
)
def test_image_data_url_still_rejects_malformed_input(url):
    with pytest.raises(ValueError, match="Invalid data URL"):
        asyncio.run(_decode_data_url(url))


def test_image_data_url_still_enforces_the_size_cap():
    oversized = base64.b64encode(b"\x00" * (MAX_REMOTE_MEDIA_BYTES + 1)).decode()
    with pytest.raises(ValueError, match="too large"):
        asyncio.run(_decode_data_url(f"data:image/png;base64,{oversized}"))


# --- input_audio -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared_format", "expected"),
    [
        ("mp3", ".mp3"),
        ("wav", ".wav"),
        ("flac", ".flac"),
        ("m4a", ".m4a"),
        (".mp3", ".mp3"),
        ("MP3", ".mp3"),
        # Nothing declared, or something unusable as a suffix: keep the historical default.
        (None, ".wav"),
        ("", ".wav"),
        ("../../etc/passwd", ".wav"),
        ("wav/../x", ".wav"),
    ],
)
def test_input_audio_honours_the_declared_container(declared_format, expected, tmp_path):
    """An mp3 named `audio.wav` is not classified as audio by Google's uploader."""
    item = AppContentItem(
        type="input_audio",
        file_data=base64.b64encode(b"ID3 fake audio payload").decode(),
        raw_data={"format": declared_format} if declared_format is not None else {},
    )
    _text, path = asyncio.run(GeminiClientWrapper._process_content_item(item, "user", tmp_path))
    assert isinstance(path, Path)
    assert path.suffix == expected


# --- how a bad attachment is reported --------------------------------------------------


@pytest.mark.parametrize(
    "bad_payload",
    ["not!!base64!!", "data:application/pdf,notdeclaredbase64"],
)
def test_undecodable_attachment_is_a_400_not_a_503(bad_payload, tmp_path):
    """503 is retryable, so an OpenAI SDK would resend a permanently-bad request forever."""
    messages = [
        AppMessage(
            role="user",
            content=[AppContentItem(type="file", filename="x.pdf", file_data=bad_payload)],
        )
    ]
    with pytest.raises(HTTPException) as caught:
        asyncio.run(_process_conversation_for_client(None, messages, tmp_path))
    assert caught.value.status_code == 400


def test_a_good_attachment_still_goes_through(tmp_path):
    model_input, files = asyncio.run(
        _process_conversation_for_client(
            None,
            [
                AppMessage(
                    role="user",
                    content=[
                        AppContentItem(type="text", text="hi"),
                        AppContentItem(type="file", filename="x.pdf", file_data=PDF_DATA_URL),
                    ],
                )
            ],
            tmp_path,
        )
    )
    assert len(files) == 1
    attachment = files[0]
    assert isinstance(attachment, Path)
    assert attachment.read_bytes() == PDF
    assert "hi" in model_input


# --- content sniffing, the last-resort extension ----------------------------------------


@pytest.mark.parametrize(
    ("label", "data", "expected"),
    [
        ("pdf", b"%PDF-1.7\ntrailer", ".pdf"),
        ("png", b"\x89PNG\r\n\x1a\n" + bytes(32), ".png"),
        ("jpeg", b"\xff\xd8\xff\xe0" + bytes(32), ".jpg"),
        ("gif", b"GIF89a" + bytes(32), ".gif"),
        ("wav", b"RIFF\x24\x00\x00\x00WAVEfmt ", ".wav"),
        ("webp", b"RIFF\x24\x00\x00\x00WEBPVP8 ", ".webp"),
        ("mp3 with ID3", b"ID3\x03\x00" + bytes(32), ".mp3"),
        ("mp3 bare frame sync", b"\xff\xfb\x90\x00" + bytes(32), ".mp3"),
        ("flac", b"fLaC" + bytes(32), ".flac"),
        ("m4a", b"\x00\x00\x00\x20ftypM4A " + bytes(16), ".m4a"),
        ("mp4", b"\x00\x00\x00\x20ftypisom" + bytes(16), ".mp4"),
        ("docx", b"PK\x03\x04" + bytes(20) + b"word/document.xml", ".docx"),
        ("xlsx", b"PK\x03\x04" + bytes(20) + b"xl/workbook.xml", ".xlsx"),
        ("pptx", b"PK\x03\x04" + bytes(20) + b"ppt/presentation.xml", ".pptx"),
        ("plain zip", b"PK\x03\x04" + bytes(40), ".zip"),
        ("utf-8 text", "hello\n世界\n".encode(), ".txt"),
        ("csv", b"a,b,c\n1,2,3\n", ".txt"),
        ("unrecognisable", bytes(range(32)) * 4, ""),
        ("empty", b"", ""),
    ],
)
def test_sniffed_suffix(label, data, expected):
    assert _sniff_suffix(data) == expected, label


def test_hint_precedence_is_filename_then_mime_then_content(tmp_path):
    """A caller's own filename outranks the data URL, which outranks the bytes."""
    assert _suffix_for_upload(PDF_B64, "report.md", PDF) == ".md"
    assert _suffix_for_upload(f"data:image/png;base64,{PDF_B64}", "", PDF) == ".png"
    assert _suffix_for_upload(PDF_B64, "", PDF) == ".pdf"
    # `application/octet-stream` maps to `.bin`, which is exactly what must never be used.
    assert _suffix_for_upload(f"data:application/octet-stream;base64,{PDF_B64}", "", PDF) == ".pdf"
