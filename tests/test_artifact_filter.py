"""Googleusercontent artifact fragments must never reach the client.

The library strips these URLs only once the trailing digits arrive, so during a stream the
URL is emitted piece by piece and its tail shows up as stray text in front of a generated
image. StreamingOutputFilter withholds anything that might still become one.
"""

import pytest

from app.server.chat import StreamingOutputFilter

# Observed live on 2026-08-20. The final segment is `0_452`, not bare digits -- the
# library's own ARTIFACTS_RE ends in `\d+` and so leaves `_452` behind.
ARTIFACT = "http://googleusercontent.com/image_generation_content/0_452"
LEGACY_ARTIFACT = "http://googleusercontent.com/image_generation_content/451"


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
    text = f"a\n{ARTIFACT}\nb\n{LEGACY_ARTIFACT}\nc"
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


@pytest.mark.parametrize("size", [1, 5, 17, 200])
def test_underscored_segment_leaves_nothing(size: int) -> None:
    """The `_452` tail is the actual production symptom; nothing may survive."""
    out = _stream(f"自画像。\n\n{ARTIFACT}\n\n", size)
    assert "_452" not in out
    assert "googleusercontent" not in out
    assert out.strip() == "自画像。"


def test_non_streaming_path_strips_artifact() -> None:
    """process_llm_output feeds the non-streaming reply, which showed the same tail."""
    from app.utils.helper import process_llm_output

    _, visible, storage, _ = process_llm_output(None, f"一只猫。\n\n{ARTIFACT}\n\n", None)
    assert "_452" not in visible
    assert "_452" not in storage
    assert visible.strip() == "一只猫。"


def test_library_pattern_is_replaced_at_source() -> None:
    """The library strips artifacts before our code runs, so its pattern must be ours.

    gemini_webapi cleans the text inside _parse_candidate. Its own pattern ends in
    `\\d+`, which against `.../0_452` consumes `0` and hands us an orphaned `_452`
    with no URL left to recognise. Patching only our side cannot fix that -- verified
    in production, where the leak survived a filter that looked correct in isolation.
    """
    import gemini_webapi.client as gemini_client

    import app.utils.helper as helper  # noqa: F401  (import applies the patch)

    raw = "x\n\nhttp://googleusercontent.com/image_generation_content/0_452\n\n"
    assert gemini_client.ARTIFACTS_RE.sub("", raw) == "x\n\n"
