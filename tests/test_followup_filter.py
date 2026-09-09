"""Gemini's `<FollowUp .../>` suggestion tag must never reach the client.

The web UI renders the tag as a suggested-question button. The reverse API hands us the
raw tag instead, so it lands verbatim at the end of the reply -- and nothing upstream
strips it: `FollowUp` appears nowhere in gemini_webapi. StreamingOutputFilter withholds
anything that might still become one, the same way it does for artifact URLs.
"""

import pytest

from app.server.chat import StreamingOutputFilter

# Observed live on 2026-09-09, streaming, gemini-flash: the tag arrived appended to an
# otherwise normal Chinese answer, separated by a blank line.
FOLLOWUP = (
    '<FollowUp label="想了解量子比特与传统计算机比特的具体区别吗？" '  # noqa: RUF001
    'query="请详细解释量子比特与传统计算机比特的区别。"/>'
)


def _stream(text: str, size: int) -> str:
    f = StreamingOutputFilter()
    out = [f.process(text[i : i + size]) for i in range(0, len(text), size)]
    out.append(f.flush())
    return "".join(out)


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13, 29, 100])
def test_followup_tag_never_leaks(size: int) -> None:
    """No fragment of the tag survives, at any chunk boundary."""
    text = f"量子计算是一种新型计算方式。\n\n{FOLLOWUP}"
    assert _stream(text, size) == "量子计算是一种新型计算方式。\n\n"


@pytest.mark.parametrize("size", [1, 4, 11, 64])
def test_multiple_followups_are_all_dropped(size: int) -> None:
    text = f"a{FOLLOWUP}b{FOLLOWUP}c"
    assert _stream(text, size) == "abc"


@pytest.mark.parametrize("size", [1, 3, 8, 50])
def test_other_markup_is_preserved(size: int) -> None:
    """Only the FollowUp tag is dropped; unrelated angle brackets are not swallowed."""
    text = "比较 <div>a</div> 与 <Follower> 这两个标签。"
    assert _stream(text, size) == text


@pytest.mark.parametrize("size", [1, 6, 40])
def test_trailing_partial_tag_is_released_on_flush(size: int) -> None:
    """A stream ending mid-tag must hand the text over rather than eat it."""
    text = '好了。<FollowUp label="半'
    assert _stream(text, size) == text


@pytest.mark.parametrize("size", [1, 5, 17, 200])
def test_text_after_the_tag_still_flows(size: int) -> None:
    """The tag is not necessarily last; whatever follows it must survive."""
    assert _stream(f"前{FOLLOWUP}后", size) == "前后"


def test_non_streaming_path_strips_followup() -> None:
    """process_llm_output feeds the non-streaming reply, which carries the same tag."""
    from app.utils.helper import process_llm_output

    _, visible, storage, _ = process_llm_output(None, f"量子计算。\n\n{FOLLOWUP}", None)
    assert "FollowUp" not in visible
    assert "FollowUp" not in storage
    assert visible.strip() == "量子计算。"
