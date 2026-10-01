"""Phases 245-249: role families, role datasets, by region, top employers by family, family trends."""

from datetime import timedelta

from strategies import kinds_roles as kr
from strategies import market_trends as mt
from strategies import product_factory as pf
from strategies import product_types as pt
from strategies.b2b_lead_aggregator import POOL_NICHE
from tests import test_business_ops
from tests.test_product_factory import unique_links

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def titled(state, clock, n, title, location="Berlin, Germany", prefix="t", companies=12, days_ago=3, stack=("python",)):
    when = (clock() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    for i in range(n):
        state.upsert_lead(f"{prefix}-{title}-{location}-{i}", POOL_NICHE, {
            "company": f"{prefix.title()} Co {i % companies}", "title": title, "location": location,
            "remote": "remote" in location.lower(), "stack": list(stack), "seniority": "mid",
            "url": f"https://jobs.example/{prefix}/{i}", "posted_at": when, "source": "remoteok"})


def tagged(state):
    return [(lead, pf.facets(lead)) for lead in pf.fresh_leads(state, 60)]


# ------------------------------------------------------------------ Phase 245
def test_role_families_by_whole_words():
    fam = lambda t: kr.families_of({"title": t})  # noqa: E731
    assert fam("Senior Machine Learning Engineer") == {"ai-ml"}
    assert fam("Android Developer") == {"mobile"} and fam("Mainland Operations Lead") == set()
    assert fam("Site Reliability Engineer (SRE)") == {"devops"} and fam("AI/ML Platform Engineer") >= {"ai-ml", "devops"}
    assert fam("Engineering Manager, Payments") == {"management"} and fam("Sales Manager") == set()


# ------------------------------------------------------------------ Phases 246-247
def test_role_datasets_and_regional_variants(state, config, clock):
    titled(state, clock, 30, "Machine Learning Engineer", prefix="ml")
    titled(state, clock, 25, "Senior ML Engineer", location="Austin, TX, United States", prefix="us")
    cands = {c["slug"]: c for c in kr.role_candidates(tagged(state), config, state)}
    assert {"role-ai-ml", "role-ai-ml-europe", "role-ai-ml-us"} <= set(cands)
    assert cands["role-ai-ml"]["title"] == "Companies Hiring AI & Machine Learning Engineers"
    assert len(cands["role-ai-ml"]["rows"]) == 55 and cands["role-ai-ml-europe"]["filters"]["region"] == "europe"
    assert "role-ai-ml-remote" not in cands  # not enough remote roles


def test_a_role_product_is_made_and_described(kit, state, config, clock, transport):
    import io
    import zipfile

    unique_links(transport)
    titled(state, clock, 30, "Security Engineer", prefix="sec")
    state.set(pt.TURN, pt.TYPES.index("role"))
    made = pf.tick(kit, force=True)["made"]
    assert made["slug"] == "role-security" and made["rows"] == 30
    asset = state.get_asset(made["asset_id"])
    readme = zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(asset["path"]))).read("role-security/README.md").decode()
    assert "filtered to: Security roles (by job title)" in readme


# ------------------------------------------------------------------ Phase 248
def test_top_employers_by_family(kit, state, config, clock, transport):
    titled(state, clock, 40, "Data Engineer", prefix="d", companies=12)
    cands = kr.employer_candidates(tagged(state), config, state)
    assert [c["slug"] for c in cands] == ["role-employers-data"] and cands[0]["companies"] == 12
    unique_links(transport)
    state.set(pt.TURN, pt.TYPES.index("role_employers"))
    made = pf.tick(kit, force=True)["made"]
    assert made["slug"] == "role-employers-data" and made["price_cents"] == kr.EMPLOYERS_PRICE
    from strategies.catalog_insight import by_type

    assert by_type(state)[0]["name"] == "Top employers by role"


# ------------------------------------------------------------------ Phase 249
def test_family_trends_on_the_trends_page(state, clock):
    titled(state, clock, 12, "iOS Engineer", prefix="i", stack=("swift",))
    t = mt.trends(state, force=True)
    assert t["families"]["Mobile"][-1] == 12
    page = mt.trends_page(state, lambda title, body, desc: body)
    assert "<h2>By kind of role</h2>" in page and "<td>Mobile</td>" in page
