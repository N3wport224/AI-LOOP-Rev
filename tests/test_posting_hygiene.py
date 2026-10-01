"""Phases 290-294: tidy titles, no personal emails, clean links, one posting one row, dead links."""

from strategies import posting_hygiene as ph
from strategies import product_factory as pf
from strategies.b2b_lead_aggregator import POOL_NICHE
from tests.test_product_factory import postings
from tools.http_client import Response


def test_tidy_titles():
    assert ph.tidy_title("Sr. Backend Engineer (m/w/d)") == "Senior Backend Engineer"
    assert ph.tidy_title("Jr Developer - Remote") == "Junior Developer"
    assert ph.tidy_title("Data Engineer | Berlin") == "Data Engineer"
    assert ph.tidy_title("SENIOR PYTHON ENGINEER") == "Senior Python Engineer"
    assert ph.tidy_title("Engineer (f/m/x) - Hybrid") == "Engineer" and ph.tidy_title("SRE") == "SRE"
    assert ph.tidy_title("Head of Engineering - Payments Platform") == "Head of Engineering - Payments Platform"


def test_only_role_mailboxes_are_kept():
    assert ph.role_mailbox("jobs@acme.com") and ph.role_mailbox("careers.eu@acme.com") and ph.role_mailbox("talent+x@acme.io")
    assert not ph.role_mailbox("jane.doe@acme.com") and not ph.role_mailbox("")


def test_clean_links():
    assert ph.clean_url("https://x.example/j/1?utm_source=a&id=7&ref=hn#apply") == "https://x.example/j/1?id=7"
    assert ph.clean_url("https://x.example/j/1") == "https://x.example/j/1" and ph.clean_url("mailto:a@b") == "mailto:a@b"


def test_one_posting_one_row():
    a = {"company": "Acme Inc", "title": "Engineer", "location": "Berlin", "url": "u1", "source": "remoteok", "posted_at": "2026-09-01"}
    b = {**a, "company": "Acme", "url": "u2", "source": "remotive", "salary_min": 90000}
    c = {**a, "url": "u3"}                                        # same board: a second opening, kept
    d = {**a, "url": "u1", "posted_at": "2026-09-05"}             # same link: the newer copy
    out = ph.dedupe([a, b, c, d])
    assert len(out) == 2 and any(r.get("salary_min") for r in out) and {r["url"] for r in out} == {"u2", "u3"}


def test_clean_applies_to_factory_rows(state, clock):
    postings(state, clock, 3, ["rust"])
    state._exec("UPDATE leads SET data = json_set(data, '$.title', 'Sr. Rust Engineer (m/w/d)', "
                "'$.contact_email', 'jane@co.example', '$.url', 'https://j.example/1?utm_medium=x&id=' || id)")
    rows = pf.fresh_leads(state, 60)
    assert {r["title"] for r in rows} == {"Senior Rust Engineer"} and not any(r["contact_email"] for r in rows)
    assert all("utm" not in r["url"] for r in rows)


def test_dead_links_are_left_out(toolkit, state, config, clock, transport):
    config.link_checks = True
    postings(state, clock, 4, ["rust"])
    gone = state.leads_for_niche(POOL_NICHE)[0]["url"]
    transport.add("https://jobs.example/", lambda m, url, h: Response(404 if url == gone else 200, url, b"", {}))
    assert ph.check_links(toolkit) == 1
    assert len(pf.fresh_leads(state, 60)) == 3
    assert all(c["method"] == "HEAD" for c in transport.calls_to("https://jobs.example/"))
    assert ph.check_links(toolkit) == 0  # once a day
    clock.advance(hours=25)
    before = len(transport.calls_to("https://jobs.example/"))
    ph.check_links(toolkit)
    assert len(transport.calls_to("https://jobs.example/")) == before  # each link is checked once
    config.link_checks = False
    clock.advance(hours=25)
    assert ph.check_links(toolkit) == 0


def test_only_public_addresses_are_checked():
    for url in ("https://jobs.example.com/1", "http://careers.acme.io/x"):
        assert ph.public_url(url), url
    for url in ("http://localhost/x", "http://127.0.0.1/admin", "http://192.168.1.1/", "http://[::1]/", "http://router.lan/",
                "http://printer.local/", "ftp://jobs.example/1", "http://intranet/", "javascript:alert(1)"):
        assert not ph.public_url(url), url
