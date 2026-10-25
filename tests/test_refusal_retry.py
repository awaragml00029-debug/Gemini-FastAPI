"""A stock refusal standing in for a finished answer is worth one resend.

Google swaps the rendered text at the output layer. Captured live: a question about Austin,
Minnesota came back with over 1MB of real data -- 27 maps_local calls, "Hormel" 296 times,
"SPAM" 145 -- and a final text of "我只是一个文本 AI". Another reply reasoned for 1542
characters, judged the user's message "safe according to the existing rules", and still came
back as a template. The answer exists; only the rendering was replaced, so drawing again
usually wins. Confirmed by the user retrying by hand.

Every sample below was seen in production on 2026-09-09.
"""

from types import SimpleNamespace
from typing import cast

import pytest
from gemini_webapi import ModelOutput

from app.server.chat import _looks_like_refusal


def _Output(text: str, thoughts: str) -> ModelOutput:  # noqa: N802
    """Stands in for ModelOutput; only text and thoughts are read."""
    return cast(ModelOutput, SimpleNamespace(text=text, thoughts=thoughts))


REFUSALS = [
    "我只是一个语言模型，无法提供这方面的帮助。",  # noqa: RUF001
    "我是一个语言模型，这超出了我的设计用途。",  # noqa: RUF001
    "我只是一个文本 AI，在这方面没法帮到你。",  # noqa: RUF001
    "身为一个语言模型，我没办法提供这方面的帮助。",  # noqa: RUF001
    "由于程序代码的局限，我没法办到。",  # noqa: RUF001
    "I seem to be encountering an error. Can I try something else for you?",
]


@pytest.mark.parametrize("text", REFUSALS)
def test_real_refusals_are_retried(text: str) -> None:
    """Real refusals are retried."""
    assert _looks_like_refusal(_Output(text, "reasoned at length" * 40)) is True


@pytest.mark.parametrize("text", REFUSALS)
def test_refusal_without_reasoning_is_not_retried(text: str) -> None:
    """`thoughts == 0` is the other failure: refused before generating. Those refuse again."""
    assert _looks_like_refusal(_Output(text, "")) is False


def test_self_introduction_is_not_a_refusal() -> None:
    """The exact false positive an earlier substring match produced."""
    text = (
        "我是 Gemini，由 Google 训练的大型语言模型。"  # noqa: RUF001
        "关于你刚才提到的拖延问题，或是其他任何需要分析、规划和执行的任务，我都可以帮你。"  # noqa: RUF001
    )
    assert _looks_like_refusal(_Output(text, "x" * 1059)) is False


def test_structured_output_is_not_a_refusal() -> None:
    """Structured output is not a refusal."""
    text = '{"follow_ups": ["你能帮我处理哪些具体的任务？", "你刚才的回答能再详细点吗？"]}'  # noqa: RUF001
    assert _looks_like_refusal(_Output(text, "x" * 902)) is False


@pytest.mark.parametrize(
    "text",
    [
        "",
        "量子计算是一种利用量子叠加与纠缠的新型计算方式。",
        "这个大模型的参数量很大。",
    ],
)
def test_ordinary_answers_are_not_retried(text: str) -> None:
    """Ordinary answers are not retried."""
    assert _looks_like_refusal(_Output(text, "x" * 500)) is False


def test_long_reply_mentioning_the_phrase_is_not_retried() -> None:
    """Anchored and length-capped: a real answer that merely opens that way still stands."""
    text = "我只是一个语言模型，" + "但我可以帮你分析这个问题。" * 8  # noqa: RUF001
    assert len(text) > 80
    assert _looks_like_refusal(_Output(text, "x" * 500)) is False


# The library titles images "[Image 1]", so an attached image arrives with brackets in its
# alt text -- which broke both the stripper's regex and, through it, the length check.
_ATTACHED_IMAGE = "![[Image 1]](http://10.0.0.1:8092/media/img_ab12cd34ef56.jpg?token=xyz)"


def test_refusal_with_an_attached_image_is_still_retried() -> None:
    """Seen live: a 20-char refusal reached 333 chars this way and was never resent."""
    text = "身为一个语言模型，我没办法提供这方面的帮助。" + _ATTACHED_IMAGE  # noqa: RUF001
    assert len(text) > 80  # the raw string is well over the cap
    assert _looks_like_refusal(_Output(text, "x" * 2050)) is True


def test_real_answer_with_an_attached_image_is_not_retried() -> None:
    """Stripping markup must not turn every short captioned image into a refusal."""
    text = "这是霸王龙的复原图，特征如下：羽毛分布在颈部与背部。" + _ATTACHED_IMAGE  # noqa: RUF001
    assert _looks_like_refusal(_Output(text, "x" * 900)) is False


def test_retry_count_is_configurable() -> None:
    """An A/B measurement of the raw refusal rate needs the retry switched off."""
    from app.utils import g_config

    assert g_config.gemini.refusal_retry_attempts >= 1
