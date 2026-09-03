"""SSRF guard: what the address check actually promises, and where it stops promising.

The check resolves a hostname and rejects anything that is not public. Nothing made
curl use that answer, so the name was resolved a second time on the wire and could
land somewhere else -- the whole of a DNS rebinding attack. The connection is now
pinned to the address that was approved.

Pinning is impossible through a proxy: curl hands the destination to the proxy
instead of dialling it, so CURLOPT_RESOLVE is ignored. Measured against this
deployment's own SOCKS proxy, a request pinned to 127.0.0.1 still returned 200 from
the real host under both socks5 and socks5h. These tests pin that asymmetry down so
nobody later "fixes" it into a silent no-op.
"""

import ipaddress
import socket

import pytest
from curl_cffi import CurlOpt

from app.utils.helper import _pin_options, _validate_remote_url

PUBLIC_IP = "93.184.216.34"
PRIVATE_IP = "10.0.0.7"


@pytest.fixture
def resolves_to(monkeypatch):
    """Point every hostname lookup at the addresses a test names."""

    def _install(*ips: str):
        def fake(host, port, **kwargs):
            # Real getaddrinfo hands an address literal straight back; a stub that answers
            # every host alike would let a private literal through on the test's say-so.
            try:
                ipaddress.ip_address(host)
            except ValueError:
                answers = ips
            else:
                answers = (host,)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in answers]

        monkeypatch.setattr(socket, "getaddrinfo", fake)

    return _install


def test_a_resolved_hostname_comes_back_with_the_address_it_was_checked_at(resolves_to):
    resolves_to(PUBLIC_IP)
    url, pinned = _validate_remote_url("https://example.com/a.png")
    assert url == "https://example.com/a.png"
    assert pinned == PUBLIC_IP


def test_an_address_literal_needs_no_lookup_and_so_pins_nothing(resolves_to):
    resolves_to(PRIVATE_IP)  # must not be consulted at all
    _url, pinned = _validate_remote_url(f"https://{PUBLIC_IP}/a.png")
    assert pinned is None


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/a.png",
        "https://sub.localhost/a.png",
        f"https://{PRIVATE_IP}/a.png",
        "ftp://example.com/a.png",
        "https://user:pw@example.com/a.png",
    ],
)
def test_the_obvious_unsafe_shapes_are_still_refused(url, resolves_to):
    resolves_to(PUBLIC_IP)
    with pytest.raises(ValueError, match="Unsupported or unsafe URL"):
        _validate_remote_url(url)


def test_a_hostname_that_resolves_anywhere_private_is_refused(resolves_to):
    resolves_to(PUBLIC_IP, PRIVATE_IP)
    with pytest.raises(ValueError, match="Unsupported or unsafe URL"):
        _validate_remote_url("https://example.com/a.png")


def test_the_connection_is_pinned_to_the_checked_address():
    opts = _pin_options("https://example.com/a.png", PUBLIC_IP, None)
    assert opts == {CurlOpt.RESOLVE: [f"example.com:443:{PUBLIC_IP}"]}


def test_the_port_in_the_url_wins_over_the_scheme_default():
    opts = _pin_options("https://example.com:8443/a.png", PUBLIC_IP, None)
    assert opts == {CurlOpt.RESOLVE: [f"example.com:8443:{PUBLIC_IP}"]}


def test_plain_http_pins_at_eighty():
    opts = _pin_options("http://example.com/a.png", PUBLIC_IP, None)
    assert opts == {CurlOpt.RESOLVE: [f"example.com:80:{PUBLIC_IP}"]}


def test_a_proxied_fetch_sets_no_pin_because_the_proxy_would_ignore_it():
    assert _pin_options("https://example.com/a.png", PUBLIC_IP, "socks5h://127.0.0.1:1080") == {}


def test_nothing_to_pin_when_no_lookup_happened():
    assert _pin_options("https://example.com/a.png", None, None) == {}


def test_the_resolve_entry_is_a_list_because_curl_walks_a_string_per_character():
    """A str value makes curl fail with "Could not parse CURLOPT_RESOLVE entry 'h'"."""
    value = _pin_options("https://example.com/a.png", PUBLIC_IP, None)[CurlOpt.RESOLVE]
    assert isinstance(value, list)
