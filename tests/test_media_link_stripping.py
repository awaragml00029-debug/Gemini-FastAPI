"""Our own media URLs must not travel back to Google as assistant history.

Generated media reaches the client as `![title](<our host>/media/img_<uuid>.jpg?token=…)`,
and an OpenAI-compatible client replays that line as assistant history on the next turn.
Google then sees a filename for a file it cannot fetch. Captured live on 2026-09-09 the
model reasoned: "I see a filename in the text (img_2923f9…jpg), but no actual image …
leaning toward not passing image_references if I can't confirm the image's existence" --
and quietly dropped the image work it had been asked to do.

The URL is useless to Google either way, so it is replaced by a marker.
"""

import asyncio

import pytest

from app.models.core import AppMessage
from app.services.client import GeminiClientWrapper
from app.utils.helper import strip_local_media_links

HOST = "http://10.0.0.1:8092/"
IMG = f"![Image]({HOST}media/img_2923f945f1b64bddbd6fa51c2753d2c9.jpg?token=abc123)"
TITLED = f"![奔跑的小鸡]({HOST}media/img_deadbeef.png?token=x)"
VIDEO = f"[![Video]({HOST}media/img_a.jpg?token=1)]({HOST}media/vid_b.mp4?token=2)"


def test_untitled_image_becomes_a_marker() -> None:
    """Untitled image becomes a marker."""
    assert strip_local_media_links(f"这是图\n\n{IMG}") == "这是图\n\n[generated image]"


def test_title_is_kept_because_it_carries_meaning() -> None:
    """Title is kept because it carries meaning."""
    assert strip_local_media_links(TITLED) == "[generated image: 奔跑的小鸡]"


def test_nested_media_link_collapses() -> None:
    """Video and audio arrive as a thumbnail image wrapped in a link -- both halves ours."""
    assert strip_local_media_links(VIDEO) == "[generated media]"


def test_external_images_are_left_alone() -> None:
    """Only our own /media/ URLs are rewritten; a real remote image still means something."""
    text = "外部图片 ![cat](https://example.com/cat.jpg) 应保留"
    assert strip_local_media_links(text) == text


@pytest.mark.parametrize("text", ["", "普通文本没有链接", "裸的 /media/ 两个字不该触发什么"])
def test_text_without_our_links_is_unchanged(text: str) -> None:
    """Text without our links is unchanged."""
    assert strip_local_media_links(text) == text


def test_filename_never_reaches_the_prompt() -> None:
    """End to end: the uuid must be gone from what process_conversation hands to Google."""
    messages = [
        AppMessage(role="user", content="画一只小鸡"),
        AppMessage(role="assistant", content=f"给你\n\n{IMG}"),
        AppMessage(role="user", content="让它跑起来"),
    ]
    rendered, _ = asyncio.run(GeminiClientWrapper.process_conversation(messages))

    assert "img_2923f945f1b64bddbd6fa51c2753d2c9" not in rendered
    assert "token=abc123" not in rendered
    assert "[generated image]" in rendered
    # The surrounding conversation must survive intact.
    assert "画一只小鸡" in rendered
    assert "让它跑起来" in rendered


def test_filename_used_as_title_is_also_dropped() -> None:
    """The responses path titles images with their own filename.

    "keep the title, drop the URL" therefore still leaked the name -- captured live as
    `[generated image: img_47d46252….jpg]` in a prompt, immediately before a refusal.
    """
    leaked = (
        f"![img_47d46252cb524149bd784ad7bc6b8aa6.jpg]"
        f"({HOST}media/img_47d46252cb524149bd784ad7bc6b8aa6.jpg?token=a)"
    )
    out = strip_local_media_links(leaked)
    assert out == "[generated image]"
    assert "img_47d46252" not in out


def test_a_real_title_still_survives() -> None:
    """Only generated-looking names are dropped; a title the user would recognise stays."""
    assert strip_local_media_links(TITLED) == "[generated image: 奔跑的小鸡]"
