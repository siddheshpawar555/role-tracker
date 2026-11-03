"""Command line entry point.

    python -m tracker run                 # check every company, update history, build dashboard
    python -m tracker run --only 71,146   # just these company ids
    python -m tracker run --no-browser    # skip the headless-browser fallback
    python -m tracker dashboard           # rebuild site/ from data/state.json only
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from . import adapters, detect, http
from .dashboard import build as build_dashboard
from .match import RoleMatcher, is_us_location
from .store import Store, now_iso

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ATS_CACHE = DATA / "ats_cache.json"
REDISCOVER_AFTER_DAYS = 14
COMPANY_LIMIT_SECS = 6 * 60          # phase 1 (plain HTTP) per company
BROWSER_COMPANY_LIMIT_SECS = 150     # phase 2 (headless browser) per company


def load_companies(only: set[int] | None) -> list[dict]:
    cs = yaml.safe_load((ROOT / "companies.yaml").read_text())
    return [c for c in cs if c.get("enabled", True) and (not only or c["id"] in only)]


def load_cache() -> dict:
    return json.loads(ATS_CACHE.read_text()) if ATS_CACHE.exists() else {}


class Runner:
    def __init__(self, matcher: RoleMatcher, use_browser: bool):
        self.matcher = matcher
        self.use_browser = use_browser
        self.cache = load_cache()
        self.lock = threading.Lock()
        self.shared: dict[str, tuple] = {}  # same feed used by two companies -> scrape once
        self.deferred: list[dict] = []
        self.stop_at: float | None = None  # overall wall-clock limit for the current phase

    # ---- config resolution
    def resolve(self, company: dict, browser=None) -> tuple[dict | None, str]:
        if company.get("ats"):
            return dict(company["ats"]), "set in companies.yaml"
        key = str(company["id"])
        hit = self.cache.get(key)
        if hit and hit.get("config") and hit.get("careers_url") == company["careers_url"]:
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(hit["discovered"])
            if age.days < REDISCOVER_AFTER_DAYS and hit.get("fails", 0) < 2:
                return hit["config"], hit.get("note", "cached")
        cfg, note = detect.discover(company["careers_url"], browser=browser)
        with self.lock:
            self.cache[key] = {
                "config": cfg, "note": note, "careers_url": company["careers_url"],
                "discovered": now_iso(), "fails": 0,
            }
        return cfg, note

    def mark_fail(self, company: dict) -> None:
        with self.lock:
            hit = self.cache.get(str(company["id"]))
            if hit:
                hit["fails"] = hit.get("fails", 0) + 1

    # ---- scraping
    def scrape(self, cfg: dict, browser=None) -> list[dict]:
        t = cfg["type"]
        terms = self.matcher.search_terms
        key = json.dumps(cfg, sort_keys=True)
        with self.lock:
            if key in self.shared:
                return self.shared[key]
        if t in adapters.ADAPTERS:
            jobs = adapters.ADAPTERS[t](cfg, terms)
        else:
            from .browser import BROWSER_ADAPTERS

            if browser is None:
                raise RuntimeError(f"{t} needs the headless browser")
            jobs = BROWSER_ADAPTERS[t](browser, cfg, terms)
        with self.lock:
            self.shared[key] = jobs
        return jobs

    def filter(self, raw: list[dict]) -> list[dict]:
        out = []
        for j in raw:
            fam = self.matcher.match(j.get("title", ""))
            if not fam:
                continue
            if is_us_location(j.get("location")) is False:
                continue
            out.append({**j, "family": fam.name, "family_id": fam.id, "tier": fam.tier})
        return out

    def run_company(self, company: dict, browser=None) -> dict:
        t0 = time.time()
        if self.stop_at and t0 >= self.stop_at:
            return {"company": company, "ok": False, "error": "not reached this run (time limit)", "cfg": None,
                    "skipped": True}
        cap = BROWSER_COMPANY_LIMIT_SECS if browser is not None else COMPANY_LIMIT_SECS
        http.set_deadline(min(t0 + cap, self.stop_at or float("inf")))
        try:
            return self._run_company(company, browser, t0)
        finally:
            http.set_deadline(None)

    def _run_company(self, company: dict, browser, t0: float) -> dict:
        try:
            cfg, note = self.resolve(company, browser)
        except Exception as e:
            cfg, note = None, f"discovery error: {e}"
        if cfg is None:
            return {"company": company, "ok": False, "error": note, "cfg": None}
        if cfg["type"] in ("tesla", "browser") and browser is None:
            return {"company": company, "ok": False, "error": "needs browser", "cfg": cfg, "defer": True}
        try:
            raw = self.scrape(cfg, browser)
        except Exception as e:
            self.mark_fail(company)
            return {"company": company, "ok": False, "error": f"{cfg['type']}: {e}", "cfg": cfg}
        return {
            "company": company, "ok": True, "cfg": cfg, "scanned": len(raw),
            "jobs": self.filter(raw), "secs": round(time.time() - t0, 1),
        }


def cmd_run(args) -> int:
    only = {int(x) for x in args.only.split(",")} if args.only else None
    companies = load_companies(only)
    matcher = RoleMatcher(ROOT / "roles.yaml")
    store = Store(DATA / "state.json")
    runner = Runner(matcher, use_browser=not args.no_browser)
    run_ts = now_iso()
    results: dict[int, dict] = {}
    start = time.time()
    # drop stored roles that the current rules no longer accept (e.g. location rules got stricter)
    dropped = [k for k, j in store.jobs.items()
               if is_us_location(j.get("location")) is False or not matcher.match(j.get("title", ""))]
    for k in dropped:
        del store.jobs[k]
    if dropped:
        print(f"Removed {len(dropped)} stored roles that no longer match the rules.", flush=True)
    if args.rebaseline:
        # treat everything found this run as already-open (used after changing how sites are read)
        for c in store.companies.values():
            c["baseline_done"] = False
    budget = args.budget_minutes * 60
    runner.stop_at = start + budget * 0.65

    print(f"Checking {len(companies)} companies with {args.workers} workers "
          f"(time budget {args.budget_minutes} min)…", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(runner.run_company, c): c for c in companies}
        for f in as_completed(futs):
            r = f.result()
            c = r["company"]
            results[c["id"]] = r
            status = f"{len(r['jobs'])} matches / {r['scanned']} scanned" if r["ok"] else f"FAILED – {r['error']}"
            print(f"  [{c['id']:>3}] {c['name']:<40} {(r['cfg'] or {}).get('type', '-'):<15} {status}", flush=True)

    print(f"Plain-HTTP pass finished in {(time.time() - start) / 60:.1f} min", flush=True)
    runner.stop_at = start + budget
    retry = [r["company"] for r in results.values() if not r["ok"] and not r.get("skipped")]
    if retry and runner.use_browser and time.time() < runner.stop_at:
        try:
            from .browser import Browser

            browser = Browser()
        except Exception as e:
            print(f"Headless browser unavailable ({e}); skipping browser retries.")
            browser = None
        if browser:
            print(f"Retrying {len(retry)} companies with the headless browser…", flush=True)
            try:
                for c in retry:
                    if time.time() >= runner.stop_at:
                        print("Time budget used up; remaining companies will be retried next run.", flush=True)
                        break
                    prev = results[c["id"]]
                    if prev.get("cfg") and prev["cfg"]["type"] not in ("tesla", "browser"):
                        runner.cache.pop(str(c["id"]), None)  # force rediscovery through the browser
                    r = runner.run_company(c, browser)
                    if not r["ok"] and r.get("cfg") is None:
                        # last resort: read job links straight off the careers page
                        try:
                            http.set_deadline(time.time() + 60)
                            raw = runner.scrape({"type": "browser", "url": c["careers_url"]}, browser)
                            r = {"company": c, "ok": True, "cfg": {"type": "browser", "url": c["careers_url"]},
                                 "scanned": len(raw), "jobs": runner.filter(raw), "partial": True}
                        except Exception as e:
                            r["error"] += f"; browser: {e}"
                        finally:
                            http.set_deadline(None)
                    results[c["id"]] = r
                    status = f"{len(r['jobs'])} matches" if r["ok"] else f"FAILED – {r['error']}"
                    print(f"  [{c['id']:>3}] {c['name']:<40} {status}", flush=True)
            finally:
                browser.close()

    new_total, ok, failed, new_roles = 0, 0, 0, []
    for cid, r in sorted(results.items()):
        c = r["company"]
        if r["ok"]:
            # a generic link-scrape that finds nothing tells us nothing — don't treat as "all closed"
            if r.get("partial") and not r["jobs"]:
                store.record_failure(c, "careers page readable but no listings found (unsupported site)", r["cfg"], run_ts)
                failed += 1
                continue
            n = store.record_success(c, r["jobs"], r["cfg"], run_ts, r["scanned"])
            store.companies[str(cid)]["partial"] = bool(r.get("partial"))
            ok += 1
            new_total += n
        else:
            store.record_failure(c, r["error"], r.get("cfg"), run_ts)
            failed += 1
    new_roles = [j for j in store.jobs.values() if j["first_seen"] == run_ts and not j["baseline"]]
    store.prune()
    store.add_run({"ts": run_ts, "companies_ok": ok, "companies_failed": failed, "new_roles": new_total,
                   "partial_run": bool(only)})
    store.save()
    DATA.mkdir(exist_ok=True)
    ATS_CACHE.write_text(json.dumps(runner.cache, indent=1, sort_keys=True))
    build_dashboard(store.data, ROOT / "site")
    write_summary(run_ts, ok, failed, new_roles)
    print(f"\nDone in {(time.time() - start) / 60:.1f} min: {ok} companies OK, {failed} failed, "
          f"{new_total} new roles.")
    return 0


def write_summary(run_ts: str, ok: int, failed: int, new_roles: list[dict]) -> None:
    lines = [f"## Job tracker run {run_ts}", "", f"Companies OK: **{ok}** · failed: **{failed}** · new roles: **{len(new_roles)}**", ""]
    if new_roles:
        lines += ["| Company | Role | Location | Family |", "|---|---|---|---|"]
        for j in sorted(new_roles, key=lambda j: (j["family_id"], j["company"])):
            lines.append(f"| {j['company']} | [{j['title']}]({j['url']}) | {j['location'][:60]} | {j['family']} |")
    text = "\n".join(lines) + "\n"
    (DATA / "last_run.md").write_text(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text)


def main() -> int:
    p = argparse.ArgumentParser(prog="tracker")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--only", help="comma-separated company ids")
    r.add_argument("--workers", type=int, default=16)
    r.add_argument("--no-browser", action="store_true")
    r.add_argument("--rebaseline", action="store_true",
                   help="record roles found this run as baseline instead of new")
    r.add_argument("--budget-minutes", type=float, default=75,
                   help="stop starting new companies after this long (results so far are saved)")
    sub.add_parser("dashboard")
    d = sub.add_parser("diagnose")
    d.add_argument("--only", help="comma-separated company ids (default: all failing)")
    d.add_argument("--workers", type=int, default=4)
    v = sub.add_parser("verify")
    v.add_argument("--sample", type=int, default=40)
    v.add_argument("--recall", type=int, default=8)
    v.add_argument("--seed", type=int, default=7)
    args = p.parse_args()
    if args.cmd == "verify":
        from .verify import cmd_verify

        return cmd_verify(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "diagnose":
        from .diagnose import cmd_diagnose

        return cmd_diagnose(args)
    store = Store(DATA / "state.json")
    build_dashboard(store.data, ROOT / "site")
    return 0


if __name__ == "__main__":
    sys.exit(main())
