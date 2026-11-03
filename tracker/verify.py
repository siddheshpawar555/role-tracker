"""Fact-check the stored roles against the live careers sites.

    python -m tracker verify [--sample 40] [--recall 8]

Part A (accuracy): opens a random sample of open roles and checks the posting is live
and that the title/location match what the tracker stored.
Part B (coverage): for several Workday companies, downloads ALL US postings (no search
terms), applies the role-family rules, and lists matching roles the tracker missed.
Writes data/verify.json and data/verify.md.
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path
from urllib.parse import urlparse

from . import adapters, http
from .match import RoleMatcher, is_us_location

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def check_role(j: dict) -> dict:
    url = j["url"]
    out = {"company": j["company"], "title": j["title"], "location": j["location"], "url": url}
    try:
        wd = adapters.parse_workday(url)
        if wd and "/job/" in url:
            path = url.split(f"/{wd['site']}", 1)[1]
            api = f"https://{wd['host']}/wday/cxs/{wd['tenant']}/{wd['site']}{path}"
            r = http.get(api, headers={"Accept": "application/json"})
            out["status"] = r.status_code
            if r.status_code == 200:
                info = r.json().get("jobPostingInfo", {})
                out["live_title"] = info.get("title")
                out["live_location"] = info.get("location")
                out["live_posted"] = info.get("startDate")
                out["live_country"] = (info.get("country") or {}).get("descriptor")
                out["title_ok"] = _norm(info.get("title")) == _norm(j["title"])
                out["live"] = bool(info.get("canApply", True))
        else:
            r = http.get(url)
            out["status"] = r.status_code
            page = _norm(r.text)
            out["title_ok"] = _norm(j["title"]) in page
            gone = re.search(r"no longer (available|accepting)|job (has been )?(filled|closed|expired)|"
                             r"position (has been )?(filled|closed)|page not found", r.text, re.I)
            out["live"] = r.status_code == 200 and not gone
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


def workday_all_us(cfg: dict, cap: int = 2000) -> list[dict]:
    """Every US posting on a Workday site (no search terms)."""
    host, tenant, site = cfg["host"], cfg["tenant"], cfg["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    base = f"https://{host}/recruiting/{tenant}/{site}" if cfg.get("myworkdaysite") else f"https://{host}/{site}"
    facets = adapters._workday_us_facet(api)
    out, offset, total = [], 0, None
    while offset < cap:
        r = http.post(api, json={"appliedFacets": facets, "limit": 20, "offset": offset, "searchText": ""},
                      headers={"Content-Type": "application/json", "Accept": "application/json"})
        r.raise_for_status()
        d = r.json()
        total = d.get("total") if total is None else total
        posts = d.get("jobPostings") or []
        for p in posts:
            path = p.get("externalPath") or ""
            out.append({"id": path.rsplit("_", 1)[-1], "title": p.get("title", ""), "url": base + path,
                        "location": p.get("locationsText", "")})
        offset += 20
        if not posts or offset >= (total or 0):
            break
        http.polite_sleep(0.2)
    return out, facets, total


def cmd_verify(args) -> int:
    state = json.loads((DATA / "state.json").read_text())
    cache = json.loads((DATA / "ats_cache.json").read_text()) if (DATA / "ats_cache.json").exists() else {}
    matcher = RoleMatcher(ROOT / "roles.yaml")
    rnd = random.Random(args.seed)
    open_jobs = [j for j in state["jobs"].values() if j["status"] == "open"]

    # ---- Part A
    sample = rnd.sample(open_jobs, min(args.sample, len(open_jobs)))
    checks = []
    for j in sample:
        c = check_role(j)
        checks.append(c)
        print(f"  A {c.get('status')} live={c.get('live')} title_ok={c.get('title_ok')}  {j['company'][:20]} | {j['title'][:50]}", flush=True)

    # ---- Part B
    wd_companies = []
    for cid, co in state["companies"].items():
        cfg = (cache.get(cid) or {}).get("config") or {}
        if cfg.get("type") == "workday" and not co.get("consecutive_failures"):
            wd_companies.append((cid, co["name"], cfg))
    recall = []
    seen_cfg = set()
    for cid, name, cfg in rnd.sample(wd_companies, min(args.recall, len(wd_companies))):
        key = json.dumps(cfg, sort_keys=True)
        if key in seen_cfg:
            continue
        seen_cfg.add(key)
        try:
            allj, facets, total = workday_all_us(cfg)
        except Exception as e:
            recall.append({"company": name, "error": str(e)[:150]})
            continue
        should = [j for j in allj if matcher.match(j["title"]) and is_us_location(j["location"]) is not False]
        have = {j["job_id"] for j in open_jobs if str(j["company_id"]) == cid}
        missed = [j for j in should if j["id"] not in have]
        extra = len(have - {j["id"] for j in allj})
        recall.append({"company": name, "us_facet": bool(facets), "us_postings": total, "downloaded": len(allj),
                       "should_match": len(should), "tracked": len(have), "missed": missed[:40],
                       "missed_count": len(missed), "tracked_not_in_us_list": extra})
        print(f"  B {name[:30]:<30} US postings={total} should={len(should)} tracked={len(have)} missed={len(missed)} extra={extra}", flush=True)

    DATA.mkdir(exist_ok=True)
    (DATA / "verify.json").write_text(json.dumps({"accuracy": checks, "coverage": recall}, indent=1))
    ok = [c for c in checks if c.get("live") and c.get("title_ok")]
    lines = [f"# Verification", "", f"Accuracy sample: {len(ok)}/{len(checks)} roles live with matching title", ""]
    for c in checks:
        if not (c.get("live") and c.get("title_ok")):
            lines.append(f"- {c['company']} | {c['title']} | status {c.get('status')} live={c.get('live')} "
                         f"title_ok={c.get('title_ok')} live_title={c.get('live_title')} {c.get('error', '')}")
    lines += ["", "## Coverage (Workday, all US postings)"]
    for r in recall:
        lines.append(f"- {r['company']}: should match {r.get('should_match')}, tracked {r.get('tracked')}, "
                     f"missed {r.get('missed_count')} {r.get('error', '')}")
        for m in r.get("missed", [])[:8]:
            lines.append(f"    - missed: {m['title']} ({m['location']})")
    (DATA / "verify.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0
