"""Build the static tracker page (site/index.html) from the stored state."""
from __future__ import annotations

import json
from pathlib import Path

TEMPLATE = Path(__file__).with_name("dashboard.html")


def build(state: dict, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    keep = ("company", "company_id", "group", "lca_evidence", "title", "url", "location", "posted",
            "family", "family_id", "tier", "first_seen", "last_seen", "baseline", "status", "closed", "reopened")
    jobs = [{k: v.get(k) for k in keep if v.get(k) is not None} for v in state["jobs"].values()]
    payload = {
        "jobs": jobs,
        "companies": state.get("companies", {}),
        "runs": state.get("runs", [])[-60:],
    }
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text().replace("/*__DATA__*/null", data)
    path = out / "index.html"
    path.write_text(html)
    (out / ".nojekyll").write_text("")
    return path
