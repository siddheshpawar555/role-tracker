import json
from pathlib import Path

import pytest

from tracker import adapters, detect, http
from tracker.match import RoleMatcher, is_us_location
from tracker.store import Store

ROOT = Path(__file__).resolve().parent.parent
M = RoleMatcher(ROOT / "roles.yaml")


@pytest.mark.parametrize(
    "title,family",
    [
        ("Industrial Engineer II", 1),
        ("Sr. Manufacturing Engineer - Final Assembly", 2),
        ("Process Engineer, Cell Manufacturing", 3),
        ("Continuous Improvement Engineer", 4),
        ("Lean Six Sigma Black Belt", 5),
        ("Operational Excellence Manager", 5),
        ("Production Engineer", 6),
        ("Quality Engineer III", 7),
        ("Supplier Quality Engineer", 8),
        ("Process Validation Engineer", 9),
        ("CAPA Engineer", 10),
        ("Manufacturing Project Engineer", 11),
        ("Operations Analyst - Plant", 12),
        ("NPI Engineer", 13),
        ("Packaging Engineer", 14),
        ("Product Engineer, DFM", 15),
    ],
)
def test_families(title, family):
    f = M.match(title)
    assert f is not None and f.id == family, (title, f and f.name)


@pytest.mark.parametrize(
    "title",
    ["Software Engineer", "Manufacturing Engineering Intern", "Director, Manufacturing Engineering",
     "Process Technician", "Sales Engineer", "Business Process Engineer", "Software Quality Engineer",
     "Accountant", "Production Operator"],
)
def test_excluded(title):
    assert M.match(title) is None


@pytest.mark.parametrize(
    "loc,expected",
    [
        ("Austin, TX", True), ("Fremont, California", True), ("US-MI-Auburn Hills", True),
        ("United States - Remote", True), ("West Point, Georgia", True), ("Indianapolis, IN, USA", True),
        ("Monterrey, Mexico", False), ("Bangalore, India", False), ("Toronto, ON, Canada", False),
        ("Shanghai", False), ("3 Locations", None), ("", None),
    ],
)
def test_us(loc, expected):
    assert is_us_location(loc) is expected


def test_parse_workday():
    c = adapters.parse_workday("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x")
    assert c == {"type": "workday", "host": "nvidia.wd5.myworkdayjobs.com", "tenant": "nvidia", "site": "NVIDIAExternalCareerSite"}
    c = adapters.parse_workday("https://wd3.myworkdaysite.com/en-US/recruiting/acme/External")
    assert c["host"] == "wd3.myworkdaysite.com" and c["tenant"] == "acme" and c["site"] == "External"


def test_fingerprints():
    assert detect.fingerprint('<a href="https://abbott.wd5.myworkdayjobs.com/abbottcareers/job/1">x</a>', "https://x.com")["site"] == "abbottcareers"
    assert detect.fingerprint('<iframe src="https://boards.greenhouse.io/embed/job_board?for=rivian">', "https://x.com") == {"type": "greenhouse", "board": "rivian"}
    o = detect.fingerprint('<a href="https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions">', "https://x.com")
    assert o == {"type": "oracle", "host": "eeho.fa.us2.oraclecloud.com", "site": "CX_1"}
    p = detect.fingerprint('<script src="https://cdn.phenompeople.com/x.js"></script><a href="/global/en/search-results">', "https://jobs.example.com/global/en/home")
    assert p == {"type": "phenom", "host": "jobs.example.com", "prefix": "/global/en/"}
    assert detect.fingerprint('<link href="https://tbcdn.talentbrew.com/x.css">', "https://jobs.boeing.com/")["type"] == "radancy"
    assert detect.fingerprint('<a href="https://jobs.smartrecruiters.com/BoschGroup">', "https://x")["board"] == "BoschGroup"
    e = detect.fingerprint('<script>var c={"domain": "micron.com"}</script>', "https://micron.eightfold.ai/careers")
    assert e == {"type": "eightfold", "host": "micron.eightfold.ai", "domain": "micron.com"}


class FakeResp:
    def __init__(self, payload=None, text="", status=200, url=""):
        self._p, self.text, self.status_code, self.url = payload, text, status, url

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_workday_adapter(monkeypatch):
    calls = []

    def fake_post(url, json=None, **kw):
        calls.append(json)
        if json["offset"] == 0:
            posts = [{"title": f"Process Engineer {i}", "externalPath": f"/job/Austin-TX/Process-Engineer_R{i}",
                      "locationsText": "Austin, TX", "postedOn": "Posted Today"} for i in range(20)]
            return FakeResp({"total": 25, "jobPostings": posts})
        posts = [{"title": f"Process Engineer {i}", "externalPath": f"/job/X/Y_R{i}", "locationsText": "Pune, India",
                  "postedOn": "Posted 3 Days Ago"} for i in range(20, 25)]
        return FakeResp({"total": 0, "jobPostings": posts})

    monkeypatch.setattr(http, "post", fake_post)
    monkeypatch.setattr(http, "polite_sleep", lambda *_: None)
    jobs = adapters.workday({"type": "workday", "host": "t.wd5.myworkdayjobs.com", "tenant": "t", "site": "Ext"}, ["process engineer"])
    assert len(jobs) == 25 and len(calls) == 2
    assert jobs[0]["url"] == "https://t.wd5.myworkdayjobs.com/Ext/job/Austin-TX/Process-Engineer_R0"
    assert jobs[0]["id"] == "R0" and jobs[0]["posted"]


def test_phenom_ddo():
    page = 'x phApp.ddo = {"eagerLoadRefineSearch":{"status":200,"hits":2,"totalHits":2,"data":{"jobs":[{"jobId":"R1","title":"Quality Engineer","location":"Abbott Park, Illinois, United States"}]}}}; y'
    d = adapters._phenom_ddo(page)
    assert d["data"]["jobs"][0]["jobId"] == "R1"


def test_radancy_and_rmk(monkeypatch):
    rad_html = '<section id="search-results" data-total-pages="1"><ul><li><a href="/job/seattle/manufacturing-engineer/185/123" data-job-id="123"><h2>Manufacturing Engineer</h2><span class="job-location">Seattle, Washington</span></a></li></ul></section>'
    rmk_html = '<span class="paginationLabel">Results 1 – 1 of 1</span><table><tr class="data-row"><td><a class="jobTitle-link" href="/job/Auburn-Hills-Quality-Engineer-MI-48326/1112223/">Quality Engineer</a></td><td><span class="jobLocation">Auburn Hills, MI, US</span></td><td><span class="jobDate">Oct 30, 2026</span></td></tr></table>'

    def fake_get(url, **kw):
        if "search-jobs/results" in url:
            return FakeResp({"results": rad_html})
        return FakeResp(text=rmk_html)

    monkeypatch.setattr(http, "get", fake_get)
    r = adapters.radancy({"host": "jobs.boeing.com"}, ["manufacturing engineer"])
    assert r[0]["id"] == "123" and r[0]["title"] == "Manufacturing Engineer" and r[0]["location"] == "Seattle, Washington"
    s = adapters.successfactors({"host": "jobs.zf.com"}, ["quality engineer"])
    assert s[0]["id"] == "1112223" and s[0]["posted"] == "2026-10-30"


def _job(i, fam=2):
    return {"id": str(i), "title": f"Manufacturing Engineer {i}", "url": f"https://x/{i}", "location": "Austin, TX",
            "posted": None, "family": "Manufacturing Engineer", "family_id": fam, "tier": "Very strong"}


def test_store_lifecycle(tmp_path):
    s = Store(tmp_path / "state.json")
    co = {"id": 7, "name": "Acme", "group": "G", "lca_evidence": "direct"}
    cfg = {"type": "workday"}
    # run 1: baseline
    assert s.record_success(co, [_job(1), _job(2)], cfg, "t1", 100) == 0
    assert all(j["baseline"] for j in s.jobs.values())
    # run 2: job 3 new, job 2 missing once (still open)
    assert s.record_success(co, [_job(1), _job(3)], cfg, "t2", 100) == 1
    assert s.jobs["7:3"]["first_seen"] == "t2" and not s.jobs["7:3"]["baseline"]
    assert s.jobs["7:2"]["status"] == "open"
    # run 3: job 2 missing twice -> closed
    s.record_success(co, [_job(1), _job(3)], cfg, "t3", 100)
    assert s.jobs["7:2"]["status"] == "closed"
    # failure does not close anything
    s.record_failure(co, "boom", cfg, "t4")
    assert s.jobs["7:1"]["status"] == "open"
    # job 2 comes back -> reopened, first_seen kept
    s.record_success(co, [_job(1), _job(2), _job(3)], cfg, "t5", 100)
    assert s.jobs["7:2"]["status"] == "open" and s.jobs["7:2"]["first_seen"] == "t1"
    s.save()
    assert json.loads((tmp_path / "state.json").read_text())["jobs"]


def test_store_suspicious_drop(tmp_path):
    s = Store(tmp_path / "state.json")
    co = {"id": 1, "name": "A"}
    s.record_success(co, [_job(i) for i in range(20)], {"type": "x"}, "t1", 0)
    for t in ("t2", "t3"):
        s.record_success(co, [_job(0)], {"type": "x"}, t, 0)
    assert sum(j["status"] == "open" for j in s.jobs.values()) == 20


def test_dashboard_build(tmp_path):
    from tracker.dashboard import build

    s = Store(tmp_path / "state.json")
    co = {"id": 1, "name": "A</script>", "group": "G", "lca_evidence": "direct"}
    s.record_success(co, [_job(1)], {"type": "x"}, "2026-11-02T13:40:00+00:00", 1)
    s.add_run({"ts": "2026-11-02T13:40:00+00:00"})
    p = build(s.data, tmp_path / "site")
    html = p.read_text()
    assert "/*__DATA__*/" not in html and "A<\\/script>" in html


def test_deadline_stops_requests():
    import time as _t
    http.set_deadline(_t.time() - 1)
    try:
        with pytest.raises(http.DeadlineExceeded):
            http.get("https://example.invalid/")
    finally:
        http.set_deadline(None)


def test_over_terms_propagates_deadline():
    def fn(term):
        if term == "b":
            raise http.DeadlineExceeded("x")
        return [{"id": term, "title": term, "url": term}]
    with pytest.raises(http.DeadlineExceeded):
        adapters._over_terms(["a", "b", "c"], fn)
