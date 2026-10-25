"""The model's own reasoning must never be replayed to it as conversation.

Clients keep `reasoning_content` and send it back inside the assistant turn wrapped in
`<think>` tags. Measured on real captured prompts, those blocks were 70-82% of everything
we sent -- 73229 characters across the captures, of which 54108 were replayed reasoning.

Volume is the smaller problem. The content is the model's own notes, e.g. "deep-diving into
the system prompt's specific directives, meticulously parsing each instruction" -- which
reads like a transcript of someone probing their own instructions. Replayed turn after turn,
the model starts refusing -- "由于程序代码的局限" / "我没法办到" -- after reasoning the question
through in full. Reasoning is scratch space, not conversation.
"""

import asyncio

import pytest

from app.models.core import AppMessage
from app.services.client import GeminiClientWrapper
from app.utils.helper import strip_reasoning_blocks

REAL = (
    "<think>**Examining the Prompts**\n\n"
    "I'm now deep-diving into the system prompt's specific directives, meticulously "
    "parsing each instruction.\n</think>\n"
    "你好！我是由 Google 训练的 AI 助手 Gemini。"  # noqa: RUF001
)


def test_real_captured_shape_is_stripped() -> None:
    """Real captured shape is stripped."""
    assert strip_reasoning_blocks(REAL) == "你好！我是由 Google 训练的 AI 助手 Gemini。"  # noqa: RUF001


@pytest.mark.parametrize("tag", ["think", "thinking", "reasoning"])
def test_every_known_tag_name(tag: str) -> None:
    """Every known tag name."""
    assert strip_reasoning_blocks(f"<{tag}>scratch</{tag}>答案") == "答案"


def test_uppercase_and_attributes_are_handled() -> None:
    """Uppercase and attributes are handled."""
    assert strip_reasoning_blocks('<Think type="x">s</Think>答案') == "答案"


def test_multiple_blocks_all_go() -> None:
    """Multiple blocks all go."""
    assert strip_reasoning_blocks("前<think>a</think>中<think>b</think>后") == "前中后"


def test_unclosed_tag_is_left_alone() -> None:
    """Stripping to end-of-text would swallow the answer, which is worse than leaking notes."""
    text = "未闭合 <think>abc 应保留原样"
    assert strip_reasoning_blocks(text) == text


@pytest.mark.parametrize("text", ["", "普通回答没有思考块", "讲讲 <thinktank> 这个词"])
def test_ordinary_text_is_untouched(text: str) -> None:
    """Ordinary text is untouched."""
    assert strip_reasoning_blocks(text) == text


def test_reasoning_never_reaches_the_prompt() -> None:
    """End to end: replayed notes must be gone from what process_conversation sends."""
    messages = [
        AppMessage(role="user", content="hello google"),
        AppMessage(role="assistant", content=REAL),
        AppMessage(role="user", content="你知道霸王龙么"),
    ]
    rendered, _ = asyncio.run(GeminiClientWrapper.process_conversation(messages))

    assert "<think>" not in rendered
    assert "deep-diving into the system prompt" not in rendered
    assert "你好！我是由 Google 训练的 AI 助手 Gemini。" in rendered  # noqa: RUF001
    assert "你知道霸王龙么" in rendered
