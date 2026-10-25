"""A client's JSON Schema must never make this server open a connection.

jsonschema, given no registry, resolves a remote `$ref` by fetching it with a bare urlopen:
no timeout, no address check. response_format schemas come straight from the API caller, so
{"$ref": "http://169.254.169.254/latest/meta-data"} had the server call the cloud metadata
endpoint -- and, where that address does not answer, hang the request on connect forever.
Upstream's own test used exactly that schema and only passed where the connect failed fast.
"""

import time
import urllib.request

import pytest

from app.models.models import StructuredOutputRequirement
from app.utils.helper import canonicalize_structured_output, validate_json_schema

METADATA_REF = {"$ref": "http://169.254.169.254/latest/meta-data"}


@pytest.fixture
def connections(monkeypatch) -> list[object]:
    """Record, and refuse, every connection jsonschema's default retriever would open."""
    opened: list[object] = []

    def refuse(request, *args, **kwargs):
        opened.append(request)
        raise AssertionError(f"schema validation opened a connection: {request!r}")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    return opened


def _requirement(schema: dict) -> StructuredOutputRequirement:
    return StructuredOutputRequirement(
        schema_name="r", schema=schema, instruction="", raw_format={}, strict=True
    )


def test_a_remote_ref_is_never_fetched_and_leaves_the_reply_unverified(connections):
    """A remote ref is never fetched and leaves the reply unverified."""
    started = time.monotonic()

    assert canonicalize_structured_output('{"a": 1}', _requirement(METADATA_REF)) == '{"a":1}'

    assert connections == []
    assert time.monotonic() - started < 1


def test_checking_a_schema_with_a_remote_ref_opens_nothing(connections):
    """Checking a schema with a remote ref opens nothing."""
    validate_json_schema(METADATA_REF)

    assert connections == []


@pytest.mark.parametrize(
    "schema",
    [
        {"$defs": {"n": {"type": "integer"}}, "properties": {"a": {"$ref": "#/$defs/n"}}},
        {
            "$schema": "http://json-schema.org/draft-04/schema#",
            "definitions": {"n": {"type": "integer"}},
            "properties": {"a": {"$ref": "#/definitions/n"}},
        },
        {
            "$id": "https://example.com/root.json",
            "$defs": {"n": {"type": "integer"}},
            "properties": {"a": {"$ref": "https://example.com/root.json#/$defs/n"}},
        },
    ],
)
def test_local_refs_are_still_enforced(connections, schema):
    """Only retrieval is gone: a ref into the schema itself still validates the payload."""
    assert canonicalize_structured_output('{"a": 1}', _requirement(schema)) == '{"a":1}'
    assert canonicalize_structured_output('{"a": "x"}', _requirement(schema)) is None
    assert connections == []
