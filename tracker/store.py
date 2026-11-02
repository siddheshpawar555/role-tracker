"""Role history: first seen, last seen, closed. Stored as JSON in data/state.json."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

MISSES_BEFORE_CLOSED = 2      # a role must be missing from 2 checks in a row to count as closed
SUSPICIOUS_DROP = 0.4         # if a scrape returns < 40% of the open roles we knew, don't close anything
PRUNE_CLOSED_AFTER_DAYS = 120


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        else:
            self.data = {"jobs": {}, "companies": {}, "runs": []}

    @property
    def jobs(self) -> dict:
        return self.data["jobs"]

    @property
    def companies(self) -> dict:
        return self.data["companies"]

    def record_failure(self, company: dict, error: str, ats: dict | None, run_ts: str) -> None:
        c = self.companies.setdefault(str(company["id"]), {})
        c.update(
            name=company["name"],
            last_attempt=run_ts,
            last_error=error[:300],
            ats=(ats or {}).get("type"),
            consecutive_failures=c.get("consecutive_failures", 0) + 1,
        )

    def record_success(self, company: dict, jobs: list[dict], ats: dict, run_ts: str, scanned: int) -> int:
        """Merge one company's current matched roles. Returns number of new (non-baseline) roles."""
        cid = str(company["id"])
        c = self.companies.setdefault(cid, {})
        baseline = not c.get("baseline_done", False)
        seen_keys = set()
        new_count = 0
        for j in jobs:
            key = f"{cid}:{j['id']}"
            seen_keys.add(key)
            rec = self.jobs.get(key)
            if rec is None:
                self.jobs[key] = {
                    "company_id": company["id"],
                    "company": company["name"],
                    "group": company.get("group", ""),
                    "lca_evidence": company.get("lca_evidence", ""),
                    "job_id": j["id"],
                    "title": j["title"],
                    "url": j["url"],
                    "location": j.get("location", ""),
                    "posted": j.get("posted"),
                    "family": j["family"],
                    "family_id": j["family_id"],
                    "tier": j["tier"],
                    "first_seen": run_ts,
                    "last_seen": run_ts,
                    "baseline": baseline,
                    "status": "open",
                    "misses": 0,
                }
                if not baseline:
                    new_count += 1
            else:
                rec.update(
                    title=j["title"], url=j["url"], location=j.get("location", rec.get("location", "")),
                    family=j["family"], family_id=j["family_id"], tier=j["tier"],
                    last_seen=run_ts, misses=0,
                )
                if j.get("posted") and not rec.get("posted"):
                    rec["posted"] = j["posted"]
                if rec["status"] == "closed":
                    rec["status"] = "open"
                    rec["reopened"] = run_ts
                    rec.pop("closed", None)

        open_before = [k for k, v in self.jobs.items() if v["company_id"] == company["id"] and v["status"] == "open" and k not in seen_keys]
        known_open = len(open_before) + len(seen_keys)
        suspicious = known_open >= 10 and len(seen_keys) < SUSPICIOUS_DROP * known_open
        if not suspicious:
            for k in open_before:
                rec = self.jobs[k]
                rec["misses"] = rec.get("misses", 0) + 1
                if rec["misses"] >= MISSES_BEFORE_CLOSED:
                    rec["status"] = "closed"
                    rec["closed"] = run_ts

        c.update(
            name=company["name"],
            last_attempt=run_ts,
            last_ok=run_ts,
            last_error=None,
            ats=ats.get("type"),
            ats_detail={k: v for k, v in ats.items() if k != "type"},
            baseline_done=True,
            consecutive_failures=0,
            scanned=scanned,
            matched=len(seen_keys),
            suspicious_drop=suspicious,
        )
        return new_count

    def prune(self) -> None:
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=PRUNE_CLOSED_AFTER_DAYS)).isoformat()
        for k in [k for k, v in self.jobs.items() if v["status"] == "closed" and v.get("closed", "") < cutoff]:
            del self.jobs[k]

    def add_run(self, run: dict) -> None:
        self.data["runs"].append(run)
        self.data["runs"] = self.data["runs"][-200:]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        tmp.replace(self.path)
