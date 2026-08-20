"""Googleusercontent artifact fragments must never reach the client.

The library strips these URLs only once the trailing digits arrive, so during a stream the
URL is emitted piece by piece and its tail shows up as stray text in front of a generated
image. StreamingOutputFilter withholds anything that might still become one.
"""

import pytest

from app.server.chat import StreamingOutputFilter

ARTIFACT = "http://googleusercontent.com/image_generation_content/451"


def _stream(text: str, size: int) -> str:
    f = StreamingOutputFilter()
    out = [f.process(text[i : i + size]) for i in range(0, len(text), size)]
    out.append(f.flush())
    return "".join(out)


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13, 29, 100])
def test_artifact_url_never_leaks(size: int) -> None:
    """No fragment of the URL survives, at any chunk boundary."""
    text = f"这是一只猫。\n{ARTIFACT}\n好了。"
    assert _stream(text, size) == "这是一只猫。\n好了。"


@pytest.mark.parametrize("size", [1, 4, 11, 64])
def test_multiple_artifacts_are_all_dropped(size: int) -> None:
    text = f"a\n{ARTIFACT}\nb\nhttp://googleusercontent.com/other_content/499\nc"
    assert _stream(text, size) == "a\nb\nc"


@pytest.mark.parametrize("size", [1, 3, 8, 50])
def test_ordinary_text_is_preserved(size: int) -> None:
    """Only complete artifact URLs are dropped; nothing else is swallowed."""
    text = "见 https://example.com/a/1 和 http://googleusercontent.com/other 这些。"
    assert _stream(text, size) == text


@pytest.mark.parametrize("size", [1, 6, 40])
def test_trailing_partial_url_is_released_on_flush(size: int) -> None:
    """A stream that ends mid-URL must still hand the text over rather than eat it."""
    text = "看 http://googleusercontent.com/image_gen"
    assert _stream(text, size) == text
