"""`x:` voices read X's own public syndication endpoint, the one that serves embedded timelines.

Nitter, which this used to go through, was served cease-and-desist letters in August 2026 and the
public mirrors went dark, so the old reader advertised a source that could not answer. The payload
under tests/fixtures/x was captured from the live endpoint on 2026-09-07.
"""

from pathlib import Path

import httpx
import pytest
import respx

from nota.skills.contract import SourceUnavailable
from nota.skills.sources import XPublic, parse_syndication

FIXTURE = (Path(__file__).parent / "fixtures" / "x" / "ryodigital.html").read_text(encoding="utf-8")
BASE = "https://syndication.twitter.com/srv/timeline-profile/screen-name"


def test_parses_dated_posts_out_of_a_real_syndication_payload():
    msgs = parse_syndication(FIXTURE, "x:ryodigital")
    assert len(msgs) == 3
    first = msgs[0]
    assert first.voice == "x:ryodigital"
    assert "RYO-CHAN Virtual Hackathon" in first.text
    assert first.at.startswith("2026-08-17T") and first.at.endswith("+00:00")   # dated, in UTC
    assert first.url.startswith("https://x.com/") and first.id.isdigit()


@respx.mock
def test_the_reader_asks_the_way_a_browser_does_and_returns_the_posts():
    route = respx.get(f"{BASE}/ryodigital").mock(return_value=httpx.Response(200, text=FIXTURE))
    msgs = XPublic().fetch("@ryodigital")            # a leading @ is accepted
    assert len(msgs) == 3
    sent = route.calls[0].request
    assert "Mozilla/5.0" in sent.headers["user-agent"]


@respx.mock
@pytest.mark.parametrize("response,expected", [
    (httpx.Response(429), "syndication HTTP 429"),
    (httpx.Response(404), "syndication HTTP 404"),
    (httpx.Response(200, text="Rate limit exceeded"), "rate limit reached"),
    (httpx.Response(200, text="<html><body>nothing here</body></html>"), "no timeline payload"),
])
def test_every_failure_is_reported_as_unavailable_never_guessed(response, expected):
    respx.get(f"{BASE}/whoever").mock(return_value=response)
    with pytest.raises(SourceUnavailable, match=expected):
        XPublic().fetch("whoever")


@respx.mock
def test_a_network_error_is_unavailable_too():
    respx.get(f"{BASE}/whoever").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(SourceUnavailable, match="network error ConnectError"):
        XPublic().fetch("whoever")
