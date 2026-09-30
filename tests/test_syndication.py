import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.conftest import NOW
from tools.http_client import Response
from tools.syndication import MIN_INTERVAL_DAYS, Syndicator, build_article
from tools.syndication.devto_publisher import DevToPublisher
from tools.syndication.hashnode_publisher import HashnodePublisher
from tools.syndication.hn_algolia_tracker import HNHiringTracker

LANDER = "https://me.github.io/python-remote/"
SAMPLE = {"fields": ["company", "urgency_score", "stack"],
          "rows": [{"company": f"Sample{i}", "urgency_score": 70 - i, "stack": ["PostgreSQL", "Snowflake"]} for i in range(6)]}


def records(n=50):
    return [{"company": f"Co{i}", "urgency_score": 95 - i, "openings": 1,
             "stack": ["PostgreSQL", "Snowflake"] if i % 2 == 0 else ["PostgreSQL", "AWS"],
             "intent_signals": ["migration"] if i < 30 else [], "contact_email": "leak@x.example"} for i in range(n)]


def article():
    return build_article("python-remote", "Python Remote", records(), NOW, lander_url=LANDER,
                         showcase_url="https://github.com/me/showcase/tree/main/showcase/python-remote",
                         checkout_url="https://buy.stripe.com/abc", price_cents=1400, sample=SAMPLE)


# ------------------------------------------------------------------ content
def test_value_first_breakdown_with_preview_and_single_purchase_link():
    a = article()
    assert a.title == "State of Python Migrations Q3 2026: 30 Companies Hiring for PostgreSQL & Snowflake"
    md = a.for_channel("devto").markdown
    for section in ("## Key findings", "## Stack adoption", "## Intent signals", "## Sample: 5 companies from the dataset",
                    "## Top 10 by hiring urgency", "## Method"):
        assert section in md
    preview_rows = [l for l in md.split("## Sample")[1].split("## Top")[0].splitlines() if l.startswith("| Sample")]
    assert len(preview_rows) == 5  # capped at the 5-record preview
    assert md.count("buy.stripe.com") == 1  # one purchase link, in the footer
    assert "leak@x.example" not in md


@pytest.mark.parametrize("channel", ["devto", "hashnode", "github", "rss"])
def test_links_are_tagged_per_channel(channel):
    a = article().for_channel(channel)
    lander = next(u for u in _links(a.markdown) if u.startswith(LANDER))
    q = parse_qs(urlsplit(lander).query)
    assert q["utm_source"] == [channel] and q["utm_campaign"] == [a.guid]
    buy = next(u for u in _links(a.markdown) if "buy.stripe.com" in u)
    assert parse_qs(urlsplit(buy).query)["client_reference_id"] == [f"am--{channel}--radar_python_remote_2026_w40"]
    assert a.canonical_url == LANDER


def _links(md):
    import re

    return re.findall(r"\]\((https?://[^)]+)\)", md)


# ------------------------------------------------------------------ Dev.to
def test_devto_payload_and_post(toolkit, transport):
    transport.add_json("https://dev.to/api/articles/me/all", [{"title": "Something else"}])
    transport.add_json("https://dev.to/api/articles", {"id": 7, "url": "https://dev.to/me/state-of-python"}, 201)
    pub = DevToPublisher(toolkit.http, "dk")
    url = pub.publish(article().for_channel("devto"), published=True)
    assert url == "https://dev.to/me/state-of-python"
    post = transport.calls_to("https://dev.to/api/articles", "POST")[0]
    assert post["headers"]["api-key"] == "dk" and post["headers"]["Accept"] == "application/vnd.forem.api-v1+json"
    body = json.loads(post["body"])["article"]
    assert body["published"] is True and body["canonical_url"] == LANDER and len(body["tags"]) <= 4
    assert len(body["title"]) <= 128 and len(body["description"]) <= 150
    assert "utm_source=devto" in body["body_markdown"]


def test_devto_refuses_duplicate_title(toolkit, transport):
    a = article()
    transport.add_json("https://dev.to/api/articles/me/all", [{"title": a.title}])
    assert DevToPublisher(toolkit.http, "dk").publish(a, True) == "already published"
    assert not transport.calls_to("https://dev.to/api/articles", "POST")


# ------------------------------------------------------------------ Hashnode
def test_hashnode_graphql_payload_and_errors(toolkit, transport):
    pub = HashnodePublisher(toolkit.http, "hn-token", "pub_1")
    p = pub.payload(article().for_channel("hashnode"))
    inp = p["variables"]["input"]
    assert "publishPost" in p["query"] and inp["publicationId"] == "pub_1" and inp["originalArticleURL"] == LANDER
    assert all(set(t) == {"slug", "name"} for t in inp["tags"]) and "utm_source=hashnode" in inp["contentMarkdown"]
    transport.add("https://gql.hashnode.com", [
        Response(200, "u", json.dumps({"data": {"publishPost": {"post": {"id": "p1", "url": "https://me.hashnode.dev/x"}}}}).encode()),
        Response(200, "u", json.dumps({"errors": [{"message": "Invalid publication"}]}).encode()),
    ])
    assert pub.publish(article(), True) == "https://me.hashnode.dev/x"
    assert transport.calls_to("https://gql.hashnode.com")[0]["headers"]["Authorization"] == "hn-token"
    with pytest.raises(RuntimeError, match="Invalid publication"):
        pub.publish(article(), True)
    assert pub.publish(article(), False) == ""  # never auto-publishes in draft mode


# ------------------------------------------------------------------ cadence
class Recorder:
    def __init__(self, name):
        self.name = self.channel = name
        self.articles = []

    def configured(self):
        return True

    def publish(self, a, published):
        self.articles.append(a)
        return f"https://{self.name}.example/{len(self.articles)}"


def test_five_day_floor_even_if_configured_lower(config, state, toolkit, clock):
    config.syndication_interval_days = 1
    rec = Recorder("devto")
    syn = Syndicator(config, state, toolkit.files, [rec])
    assert MIN_INTERVAL_DAYS == 5 and syn.interval == timedelta(days=5)
    for day in range(10):
        a = build_article("python-remote", "Python Remote", records(), clock(), lander_url=LANDER)
        a.guid = f"g{day}"  # a fresh article every day
        syn.syndicate(a, clock())
        clock.advance(days=1)
    assert len(rec.articles) == 2  # day 0 and day 5
    assert rec.articles[0].channel == "devto"


# ------------------------------------------------------------------ HN tracker
def _hn(transport, comments):
    transport.add_json("https://hn.algolia.com/api/v1/search_by_date", {"hits": [
        {"objectID": "4100", "title": "Ask HN: Who is hiring? (October 2026)", "created_at": "2026-10-01T15:00:00Z"},
        {"objectID": "4000", "title": "Ask HN: Who wants to be hired? (October 2026)"},
    ]})
    transport.add_json("https://hn.algolia.com/api/v1/items/4100", {"id": 4100, "children": comments})


COMMENTS = [
    {"id": 1, "text": "Acme | Senior Data Engineer | REMOTE | https://acme.example/jobs<p>We are migrating from Redshift "
                      "to Snowflake with dbt and Airflow on AWS. Email jobs@acme.example</p>"},
    {"id": 2, "text": "Beta | Backend Engineer (Go) | NYC<p>Kubernetes, PostgreSQL, Terraform. Urgent hire.</p>"},
    {"id": 3, "text": "Acme | Platform Engineer | REMOTE<p>Kubernetes and Terraform.</p>"},
    {"id": 4, "text": "no header here"},
]


def test_hn_tracker_publishes_sanitized_gist_with_dataset_refs(toolkit, transport, state, config, make_hypothesis, clock):
    config.pages_base_url = "https://me.github.io"
    hyp = make_hypothesis()
    aid = state.add_asset(hyp["id"], "lead_directory", "Python Remote Tech Stack Intel", "a.zip", 1, 10, 900)
    state.update_asset(aid, niche="python-remote")
    toolkit.github.token = "ghp"
    _hn(transport, COMMENTS)
    transport.add_json("https://api.github.com/gists", {"id": "g1", "html_url": "https://gist.github.com/g1"}, 201)
    res = HNHiringTracker(toolkit).run()
    assert res == {"status": "published", "thread": "4100", "companies": 2, "gist": "https://gist.github.com/g1"}
    body = json.loads(transport.calls_to("https://api.github.com/gists", "POST")[0]["body"])
    assert body["public"] is True
    md = next(iter(body["files"].values()))["content"]
    assert "Ask HN: Who is hiring? (October 2026): stack breakdown of 2 companies" in md
    assert "| Acme | 2 | yes |" in md and "Snowflake" in md and "Kubernetes" in md
    assert "@" not in md and "acme.example" not in md and "migrating from Redshift" not in md  # derived facts only
    assert "https://me.github.io/python-remote/?utm_source=github&utm_medium=gist&utm_campaign=hn_4100" in md
    assert state.get("feed_items")[-1]["guid"] == "hn-4100"
    # refresh window: no update for 24h, then the same gist is patched
    assert HNHiringTracker(toolkit).run()["status"] == "fresh"
    clock.advance(hours=25)
    transport.add_json("https://api.github.com/gists/g1", {"id": "g1", "html_url": "https://gist.github.com/g1"})
    HNHiringTracker(toolkit).run()
    assert transport.calls_to("https://api.github.com/gists/g1", "PATCH")
    assert sum(i["guid"] == "hn-4100" for i in state.get("feed_items")) == 1


def test_hn_tracker_without_github_writes_locally(toolkit, transport):
    _hn(transport, COMMENTS)
    res = HNHiringTracker(toolkit).run()
    assert res["status"] == "written locally" and toolkit.files.exists("syndication/hn/4100.md")
