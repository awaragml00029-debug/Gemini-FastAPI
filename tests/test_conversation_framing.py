"""How a turn is framed before it reaches Google.

Gemini knows ChatML control tokens from its own training data. Captured live on
2026-09-09, with the tags on, the model's exposed reasoning discusses an "injected system
prompt" written in "fake tokens" that "simulate a system message", and the reply comes back
as a canned refusal. So the tags are off by default and a lone user turn goes out bare --
exactly what the text would look like typed into the web UI.

`chatml_tags: true` restores the old framing, so the two can be compared without a rebuild.
"""

import asyncio

import pytest

from app.models.core import AppMessage
from app.services.client import GeminiClientWrapper
from app.utils import g_config


@pytest.fixture
def chatml(request):
    """Run the body with gemini.chatml_tags forced to the parametrised value."""
    original = g_config.gemini.chatml_tags
    g_config.gemini.chatml_tags = request.param
    yield request.param
    g_config.gemini.chatml_tags = original


def _render(messages: list[AppMessage]) -> str:
    text, _ = asyncio.run(GeminiClientWrapper.process_conversation(messages))
    return text


def test_default_is_off() -> None:
    """The tags stay off unless someone opts back in."""
    assert g_config.gemini.chatml_tags is False


@pytest.mark.parametrize("chatml", [False], indirect=True)
def test_lone_user_turn_goes_out_verbatim(chatml) -> None:
    """No framing at all: 'hello world' must arrive as 'hello world'."""
    assert _render([AppMessage(role="user", content="hello world")]) == "hello world"


@pytest.mark.parametrize("chatml", [False], indirect=True)
def test_multi_turn_uses_plain_labels(chatml) -> None:
    """Role information survives; it just stops imitating a control format."""
    out = _render(
        [
            AppMessage(role="user", content="你好"),
            AppMessage(role="assistant", content="在的"),
            AppMessage(role="user", content="继续"),
        ]
    )
    assert out == "User: 你好\nAssistant: 在的\nUser: 继续\nAssistant:"
    assert "<|im_start|>" not in out
    assert "<|im_end|>" not in out


@pytest.mark.parametrize("chatml", [True], indirect=True)
def test_flag_restores_chatml(chatml) -> None:
    """The old framing must come back intact, for A/B comparison in one image."""
    out = _render([AppMessage(role="user", content="hello world")])
    assert out == "<|im_start|>user\nhello world\n<|im_end|>\n<|im_start|>assistant\n"
    assert len(out) == 62  # the input_chars=62 seen in the logs for a bare "hello world"


@pytest.mark.parametrize("chatml", [False], indirect=True)
def test_system_turn_is_not_dropped(chatml) -> None:
    """A system message still reaches the model -- it is more than one turn, so it is labelled."""
    out = _render(
        [
            AppMessage(role="system", content="Be terse."),
            AppMessage(role="user", content="hi"),
        ]
    )
    assert "Be terse." in out
    assert out.startswith("System: ")
    assert "<|im_start|>" not in out
