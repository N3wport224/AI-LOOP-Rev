import pytest

from tests.conftest import FakeTransport
from tools.circuit_breaker import CircuitBreaker, RateLimiter
from tools.errors import CircuitOpenError, HttpError, OperationalFailure, RobotsDisallowed
from tools.http_client import HttpClient, Response, redact


def make_client(transport, **kw):
    errors = []
    kw.setdefault("respect_robots", False)
    client = HttpClient(
        RateLimiter(6000, burst=100),
        transport=transport,
        error_sink=lambda src, msg, tb: errors.append(msg),
        backoff_seconds=0,
        sleep=lambda s: None,
        **kw,
    )
    return client, errors


def test_success_returns_json():
    t = FakeTransport()
    t.add_json("https://api.example.com/x", {"ok": True})
    client, errors = make_client(t)
    assert client.get_json("https://api.example.com/x", params={"q": "a b"}) == {"ok": True}
    assert t.calls[0]["url"] == "https://api.example.com/x?q=a+b"
    assert not errors


def test_retries_non_200_then_succeeds_with_adjusted_headers():
    t = FakeTransport()
    t.add("https://api.example.com/", [Response(503, "u"), Response(503, "u"), Response(200, "u", b'{"v": 1}')])
    client, errors = make_client(t)
    assert client.get_json("https://api.example.com/data") == {"v": 1}
    assert len(t.calls) == 3
    uas = [c["headers"]["User-Agent"] for c in t.calls]
    assert len(set(uas)) == 3, "user agent must rotate on each retry"
    assert t.calls[1]["headers"]["Accept"] == "*/*"
    assert t.calls[2]["timeout"] > t.calls[0]["timeout"], "timeout grows on retry"
    assert len(errors) == 2 and "503" in errors[0]


def test_gives_up_after_three_attempts():
    t = FakeTransport()
    t.add("https://api.example.com/", Response(500, "u", b"err"))
    client, errors = make_client(t)
    with pytest.raises(OperationalFailure) as info:
        client.get("https://api.example.com/data")
    assert len(t.calls) == 3
    assert isinstance(info.value.last_exception, HttpError)
    assert len(errors) == 3


def test_network_exceptions_are_retried():
    t = FakeTransport()
    t.add("https://api.example.com/", [ConnectionResetError("reset"), Response(200, "u", b"[]")])
    client, _ = make_client(t)
    assert client.get_json("https://api.example.com/") == []


def test_permanent_4xx_fails_fast():
    t = FakeTransport()
    t.add("https://api.example.com/", Response(404, "u"))
    client, errors = make_client(t)
    with pytest.raises(HttpError) as info:
        client.get("https://api.example.com/missing")
    assert info.value.status == 404
    assert len(t.calls) == 1
    assert errors


def test_429_honours_retry_after():
    slept = []
    t = FakeTransport()
    t.add("https://api.example.com/", [Response(429, "u", b"", {"retry-after": "7"}), Response(200, "u", b"1")])
    client = HttpClient(RateLimiter(6000, burst=100), transport=t, respect_robots=False, backoff_seconds=0, sleep=slept.append)
    client.get("https://api.example.com/")
    assert 7.0 in slept


def test_robots_txt_disallow():
    t = FakeTransport()
    t.add("https://site.example/robots.txt", Response(200, "u", b"User-agent: *\nDisallow: /private\n"))
    t.add_json("https://site.example/public", {"ok": 1})
    client, _ = make_client(t, respect_robots=True)
    assert client.get_json("https://site.example/public") == {"ok": 1}
    with pytest.raises(RobotsDisallowed):
        client.get("https://site.example/private/page")
    # robots.txt is cached per origin
    assert sum(1 for c in t.calls if c["url"].endswith("robots.txt")) == 1


def test_missing_robots_allows_everything():
    t = FakeTransport()
    t.add_json("https://site.example/a", {"ok": 1})
    client, _ = make_client(t, respect_robots=True)
    assert client.get_json("https://site.example/a") == {"ok": 1}


def test_api_budget_circuit():
    t = FakeTransport()
    t.add_json("https://api.example.com/", {})
    client, _ = make_client(t, breaker=CircuitBreaker(max_api_calls_per_cycle=2))
    client.get("https://api.example.com/1")
    client.get("https://api.example.com/2")
    with pytest.raises(CircuitOpenError):
        client.get("https://api.example.com/3")


def test_rejects_non_http_schemes():
    client, _ = make_client(FakeTransport())
    with pytest.raises(ValueError):
        client.get("file:///etc/passwd")


def test_secrets_redacted_in_errors():
    assert redact("https://x/y?access_token=abc123&after=1") == "https://x/y?access_token=***&after=1"
    t = FakeTransport()
    t.add("https://api.example.com/", Response(500, "u"))
    client, errors = make_client(t)
    with pytest.raises(OperationalFailure) as info:
        client.get("https://api.example.com/sales", params={"access_token": "supersecret"})
    assert all("supersecret" not in e for e in errors)
    assert "supersecret" not in str(info.value)
