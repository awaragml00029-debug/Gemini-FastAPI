"""Raw-frame capture for the canned-fallback replies.

Google returns a stock refusal or stock error in place of the answer on 12-16% of
requests -- measured independent of prompt content. Everything downstream sees only the
finished text, so the frames are captured at the parser and written out on a hit. This
guards the two things that matter: the capture must fire on a real fallback, and it must
never change what the parser returns.
"""

import json

import pytest

from app.utils import raw_capture
from app.utils.raw_capture import ORIGINAL_PARSER, CapturingFrameParser

THOUGHT = ["wrb.fr", None, '[1,2,{"thought":"drafting a Chinese intro"}]']
FALLBACK = ["wrb.fr", None, '[1,2,{"text":"我只是一个语言模型，无法提供这方面的帮助。"}]']  # noqa: RUF001
CLEAN = ["wrb.fr", None, '[1,2,{"text":"量子计算是一种新型计算方式。"}]']


@pytest.fixture
def dump_dir(tmp_path, monkeypatch):
    """Send capture dumps to a temporary directory."""
    monkeypatch.setattr(raw_capture, "DUMP_DIR", tmp_path)
    return tmp_path


def _files(d):
    return sorted(d.glob("fallback_*.json"))


def test_clean_reply_writes_nothing(dump_dir) -> None:
    """Clean reply writes nothing."""
    p = CapturingFrameParser()
    p._observe([THOUGHT])
    p._observe([CLEAN])
    assert _files(dump_dir) == []
    assert p._dumped is False


def test_fallback_dumps_every_frame_so_far(dump_dir) -> None:
    """The frames before the fallback are the evidence -- they must be in the file."""
    p = CapturingFrameParser()
    p._observe([THOUGHT])
    p._observe([THOUGHT])
    p._observe([FALLBACK])

    files = _files(dump_dir)
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert "语言模型" in payload["marker_hit"]
    assert payload["frame_count"] == 3
    assert "drafting a Chinese intro" in json.dumps(payload["frames"], ensure_ascii=False)


def test_only_one_dump_per_request(dump_dir) -> None:
    """Only one dump per request."""
    p = CapturingFrameParser()
    for _ in range(4):
        p._observe([FALLBACK])
    assert len(_files(dump_dir)) == 1


def test_dump_cap_is_honoured(dump_dir, monkeypatch) -> None:
    """Dump cap is honoured."""
    monkeypatch.setattr(raw_capture, "MAX_DUMPS", 2)
    monkeypatch.setattr(raw_capture, "_quota_warned", set())
    for _ in range(5):
        CapturingFrameParser()._observe([FALLBACK])
    assert len(_files(dump_dir)) <= 2


def test_capture_failure_never_breaks_the_stream(dump_dir, monkeypatch) -> None:
    """A broken dump path must cost the evidence, not the user's reply."""

    def boom(*_a, **_k):
        raise OSError("disk on fire")

    monkeypatch.setattr(raw_capture, "_dump", boom)
    p = CapturingFrameParser()
    p._observe([FALLBACK])  # must not raise


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("我只是一个语言模型", True),
        ("身为一个语言模型，我没办法提供这方面的帮助", True),  # noqa: RUF001
        ("这超出了我的设计用途", True),
        ("I seem to be encountering an error. Can I try something else?", True),
        ("I'm just a large language model", True),
        # Must not fire on ordinary answers, including ones that discuss models.
        ("量子计算是一种利用量子叠加的新型计算方式。", False),
        ("Transformer 架构训练出的模型在这个任务上表现更好。", False),
        ("Hello! Ready when you are.", False),
    ],
)
def test_marker_precision(text: str, hit: bool) -> None:
    """Marker precision."""
    assert bool(raw_capture.FALLBACK_MARKERS.search(text)) is hit


def test_parser_output_is_unchanged() -> None:
    """The capture subclass must be transparent: same frames as the stock parser."""
    sample = ')]}\'\n\n26\n[["wrb.fr",null,"[1,2]"]]\n'
    assert CapturingFrameParser().feed(sample) == ORIGINAL_PARSER().feed(sample)
    assert issubclass(CapturingFrameParser, ORIGINAL_PARSER)


def test_full_quota_warns_once(dump_dir, monkeypatch, caplog) -> None:
    """A silent cap made an empty capture log look like a clean run."""
    monkeypatch.setattr(raw_capture, "MAX_DUMPS", 1)
    monkeypatch.setattr(raw_capture, "_quota_warned", set())
    for _ in range(4):
        CapturingFrameParser()._observe([FALLBACK])
    assert len(_files(dump_dir)) == 1
    assert raw_capture._quota_warned == {"fallback"}
