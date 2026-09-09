"""A replaced reply must not be spliced onto the half-sentence already streamed.

gemini_webapi derives a streaming delta with SequenceMatcher whenever the new text does not
extend the old one (`get_delta_by_fp_len`). That is right for the mid-string edits it was
written for, and wrong when Google replaces the whole answer: the matcher finds incidental
overlap and returns the tail after it.

Seen live on 2026-09-09 -- the model was answering "在当前这个会话窗口内" + "可以完全记住…",
Google swapped in "Sorry, something went wrong. Please try your request again.", and the
client received:

    ...接下来的交流中 / 你提到的所有wrong. Please try your request again.

The tail of the English template welded onto an unfinished Chinese sentence. Worse than a
plain refusal, and invisible to the refusal predicate, which requires the whole reply to be
the refusal.
"""

import pytest

from app.server.chat import _delta_continues

SENT = "在当前这个会话窗口内，可以完全记住。\n\n接下来的交流中，你提到的所有"  # noqa: RUF001


@pytest.mark.parametrize(
    "replacement",
    [
        "Sorry, something went wrong. Please try your request again.",
        "我只是一个语言模型，无法提供这方面的帮助。",  # noqa: RUF001
        "I seem to be encountering an error. Can I try something else?",
        "由于程序代码的局限，我没法办到。",  # noqa: RUF001
    ],
)
def test_replacements_are_rejected(replacement: str) -> None:
    """Length cannot decide this: the English template is longer than what it replaced."""
    assert _delta_continues(replacement, SENT) is False


def test_normal_growth_is_kept() -> None:
    assert _delta_continues(SENT + "偏好设定我都会记住。", SENT) is True


@pytest.mark.parametrize("trimmed", [2, 6])
def test_tail_flicker_is_kept(trimmed: int) -> None:
    """The library rewrites the tail during code-block transitions; that must still pass."""
    assert _delta_continues(SENT[:-trimmed] + "改写的尾巴", SENT) is True


@pytest.mark.parametrize(("new", "sent"), [(None, SENT), ("", SENT), ("anything", "")])
def test_missing_sides_are_permissive(new: str | None, sent: str) -> None:
    """Nothing to compare against is not evidence of a replacement."""
    assert _delta_continues(new, sent) is True


def test_identical_text_is_kept() -> None:
    assert _delta_continues(SENT, SENT) is True
