"""Gemini's interactive-component block must not reach the client as raw markup.

`<GenerateWidget …>{spec}</GenerateWidget>` holds a *prompt for a renderer* ("Create an
interactive tactical map…"), not the rendered component; the render happens in Google's
frontend, so over the reverse API there is nothing to run. Dropping it silently would leave
an unexplained hole in the reply, so it collapses to one placeholder line.

The block spans kilobytes and therefore many chunks, so both markers arrive split.
"""

import pytest

from app.server.chat import StreamingOutputFilter
from app.utils.helper import WIDGET_BLOCK_RE, WIDGET_PLACEHOLDER

# Shape observed live on 2026-09-09 (Strait of Hormuz tactical map request).
WIDGET = (
    '<GenerateWidget component_placeholder_id="im_95599b8678063dd6" height="700px">\n'
    "```json\n"
    '{"widgetSpec": {"height": "700px", "prompt": "Create an interactive map..."}}\n'
    "```\n"
    "</GenerateWidget>"
)
BEFORE = "霍尔木兹海峡海空态势与部署交互地图\n"
AFTER = "\n以上是态势图。"


def _stream(text: str, size: int) -> str:
    f = StreamingOutputFilter()
    out = [f.process(text[i : i + size]) for i in range(0, len(text), size)]
    out.append(f.flush())
    return "".join(out)


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13, 29, 64, 500])
def test_block_collapses_at_every_chunk_boundary(size: int) -> None:
    """Block collapses at every chunk boundary."""
    assert _stream(BEFORE + WIDGET + AFTER, size) == BEFORE + WIDGET_PLACEHOLDER + AFTER


@pytest.mark.parametrize("size", [1, 3, 7, 100])
def test_longer_name_starting_the_same_way_is_untouched(size: int) -> None:
    """A longer tag name that starts the same way is not the widget marker.

    Regression: `<GenerateWidgetX>` once matched the opening marker and the filter then
    swallowed the rest of the stream looking for a close tag that never came.
    """
    text = "这里讲 <GenerateWidgetX> 不是真标签"
    assert _stream(text, size) == text


@pytest.mark.parametrize("size", [1, 8, 200])
def test_unclosed_block_still_yields_a_placeholder(size: int) -> None:
    """A stream cut mid-block already had the content swallowed; say so rather than nothing."""
    out = _stream(BEFORE + WIDGET[:70], size)
    assert out == BEFORE + WIDGET_PLACEHOLDER


@pytest.mark.parametrize("size", [1, 4, 50])
def test_ordinary_text_is_untouched(size: int) -> None:
    """Ordinary text is untouched."""
    text = "无标签的普通中文回答，含 < 和 > 符号。"  # noqa: RUF001
    assert _stream(text, size) == text


def test_two_blocks_both_collapse() -> None:
    """Two blocks both collapse."""
    assert _stream(f"a{WIDGET}b{WIDGET}c", 9) == f"a{WIDGET_PLACEHOLDER}b{WIDGET_PLACEHOLDER}c"


def test_non_streaming_regex_replaces_the_block() -> None:
    """Non streaming regex replaces the block."""
    assert WIDGET_BLOCK_RE.sub(WIDGET_PLACEHOLDER, BEFORE + WIDGET + AFTER) == (
        BEFORE + WIDGET_PLACEHOLDER + AFTER
    )


def test_non_streaming_regex_ignores_a_longer_name() -> None:
    """Non streaming regex ignores a longer name."""
    text = "<GenerateWidgetX>foo</GenerateWidget>"
    assert WIDGET_BLOCK_RE.sub(WIDGET_PLACEHOLDER, text) == text


def test_widget_frames_are_captured_under_their_own_quota(tmp_path, monkeypatch) -> None:
    """Widget captures must not eat the fallback quota, and vice versa."""
    from app.utils import raw_capture
    from app.utils.raw_capture import CapturingFrameParser

    monkeypatch.setattr(raw_capture, "DUMP_DIR", tmp_path)
    p = CapturingFrameParser()
    p._observe(
        [["wrb.fr", None, '[1,2,{"text":"<GenerateWidget id=\\"x\\">spec</GenerateWidget>"}]']]
    )

    assert len(list(tmp_path.glob("widget_*.json"))) == 1
    assert list(tmp_path.glob("fallback_*.json")) == []
