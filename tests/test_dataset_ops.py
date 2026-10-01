"""Phases 75-79: quality report, data dictionary, Excel CSV, delivery quick-start, customer requests."""

import io
import zipfile
from datetime import datetime, timezone

from strategies.base import TaskContext
from strategies.customer_requests import record, summary, topics
from strategies.dataset_extras import excel_csv, fields_md, quality_md
from strategies.digital_asset_packager import DigitalAssetPackager
from strategies.support_desk import SupportDesk
from tests import test_business_ops
from tests.test_business_ops import ctx, dataset

kit = test_business_ops.kit  # the same live-mode toolkit fixture
NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def lead(i, **kw):
    return {"company": f"Co{i % 3}", "title": "Python Engineer", "url": f"https://jobs.example/{i}", "source": "remoteok",
            "posted_at": "2026-09-28T00:00:00+00:00", "remote": True, **kw}


# ------------------------------------------------------------------ Phase 75: quality report
def test_quality_report_counts_only_whats_in_the_file():
    leads = [lead(1, company_domain="co1.example", salary_min=100000), lead(2), lead(3, posted_at="2026-08-01T00:00:00Z"),
             lead(4, source="hn")]
    md = quality_md("python-remote", leads, [{"company": "Co1", "careers_url_verified": True}, {"company": "Co2"}], NOW)
    for expected in ("| Job postings (rows) | 4 |", "| Distinct companies | 3 |", "| Rows with a company domain | 25% |",
                     "| Rows with a salary | 25% |", "| Remote roles | 100% |", "| Posted in the last 7 days | 75% |",
                     "| Careers pages verified live | 50% |", "- remoteok: 3 row(s)", "- hn: 1 row(s)"):
        assert expected in md, expected


# ------------------------------------------------------------------ Phases 76-77: dictionary and Excel copy
def test_fields_and_excel_copy():
    assert "`posted_at`" in fields_md(False) and "`urgency_score`" not in fields_md(False)
    assert "`urgency_score`" in fields_md(True)
    data = "company\nCafé\n".encode()
    assert excel_csv(data).startswith(b"\xef\xbb\xbf") and excel_csv(excel_csv(data)) == excel_csv(data)


def test_every_new_dataset_zip_has_the_extras(kit, state, config):
    config.min_leads_for_asset = 2
    hid = state.create_hypothesis("lead_directory:py:g1", "lead_directory", "py", {"niche": "py"})
    for i in range(5):
        state.upsert_lead(f"k{i}", "py", lead(i, company="Café Ltd" if i == 0 else f"Co{i}"))
    res = DigitalAssetPackager().run("package_asset", TaskContext(kit, {"id": hid, "params": {"niche": "py"}, "iterations": 0}, {}))
    zf = zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(res.metrics["zip"])))
    names = {n.split("/", 1)[1] for n in zf.namelist()}
    assert {"QUALITY.md", "FIELDS.md", "leads-excel.csv", "leads.csv"} <= names
    excel = zf.read("py-intel/leads-excel.csv")
    assert excel.startswith(b"\xef\xbb\xbf") and "Café Ltd".encode() in excel
    assert "| Job postings (rows) | 5 |" in zf.read("py-intel/QUALITY.md").decode()


# ------------------------------------------------------------------ Phase 78: delivery quick-start
def test_delivery_email_explains_how_to_open_the_files(kit, state):
    from strategies.distribution_engine import fulfil_order
    from tests.test_distribution import FakeSMTP

    _, aid = dataset(kit, state, "python-remote")
    state.record_order("stripe", "cs_1", "a@co.example", 1900, "plink_python-remote", aid, None)
    fulfil_order(kit, state.get_order("stripe", "cs_1"))
    body = FakeSMTP.sent[-1].get_body(("plain",)).get_content()
    assert "Getting started" in body and "leads-excel.csv" in body and "Google Sheets" in body


# ------------------------------------------------------------------ Phase 79: customer requests
def test_requests_are_logged_with_topics(state, config):
    config.niches = [{"name": "python-remote", "keywords": ["python", "django"]}, {"name": "rust-systems", "keywords": ["rust"]}]
    assert topics(config, "Do you have one for Rust? Trusted data!") == ["rust"]  # 'trusted' isn't 'rust'
    assert record(state, config, "a@co.example", "Great data. Could you add salaries for the Django roles?")
    assert not record(state, config, "a@co.example", "Thanks, all good.")
    assert record(state, config, "b@co.example", "Do you have a dataset for Rust?")
    assert state.get("requested_topics") == {"django": 1, "rust": 1}
    text = summary(state)
    assert text.startswith("Customer requests in the last 30 days: 2") and "salaries" in text


def test_support_desk_logs_requests_and_still_passes_them_on(kit, state, config):
    _, aid = dataset(kit, state, "python-remote")
    state.record_order("stripe", "cs_1", "a@co.example", 1900, None, aid, None, status="delivered")
    inbox = [{"sender": "a@co.example", "subject": "Idea", "message_id": "<r1>", "body": "Would love a Europe-only version."}]
    res = SupportDesk(scan=lambda *a: inbox).run("answer_support", ctx(kit))
    assert res.metrics["alerted"] == 1 and len(state.get("customer_requests")) == 1
