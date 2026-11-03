"""Diagnostics for companies the tracker can't read.

    python -m tracker diagnose               # every company failing in data/state.json
    python -m tracker diagnose --only 71,146

For each company it opens the careers page in a headless browser, follows the most
job-like links, and records where they lead plus the job-search API calls each page
makes (XHR/fetch). Output: data/diagnose.json. This is what you (or Claude) use to
add an `ats:` override or a new adapter.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import yaml

from . import detect, http

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
_JOBBY = re.compile(r"job|search|career|requisition|posting|opening|position|vacanc|cxs|recruit", re.I)
_NOISE = re.compile(
    r"google|doubleclick|facebook|linkedin|onetrust|cookielaw|hotjar|clarity\.ms|adobe|demdex|"
    r"newrelic|nr-data|segment|optimizely|qualtrics|analytics|gtm|fonts\.|\.css|\.png|\.jpg|\.svg|\.woff|"
    r"\.gif|\.ico|youtube|vimeo|tiktok|twitter|bing|yahoo|cloudflareinsights|sentry|datadog",
    re.I,
)

_local = threading.local()


def _browser():
    b = getattr(_local, "b", None)
    if b is None:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        br = pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = br.new_context(user_agent=http.UA, locale="en-US", viewport={"width": 1366, "height": 900})
        b = _local.b = (pw, br, ctx)
    return b[2]


def visit(url: str, wait_ms: int = 6000) -> dict:
    ctx = _browser()
    page = ctx.new_page()
    calls: list[dict] = []

    def on_req(req):
        if req.resource_type in ("xhr", "fetch") and _JOBBY.search(req.url) and not _NOISE.search(req.url):
            calls.append({"m": req.method, "url": req.url[:400], "body": (req.post_data or "")[:400]})

    page.on("request", on_req)
    out: dict = {"url": url}
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(wait_ms)
        html, final = page.content(), page.url
        out.update(status=resp.status if resp else None, final=final, title=page.title()[:120], size=len(html))
        out["fingerprint"] = detect.fingerprint(html, final)
        hosts = Counter(
            urlparse(m).netloc for m in re.findall(r"""(?:href|src)=["'](https?://[^"']+)""", html)
        )
        out["hosts"] = [h for h, _ in hosts.most_common(40) if not _NOISE.search(h)][:25]
        out["links"] = detect.candidate_links(html, final, limit=12)
        out["_html"] = html
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        out["api_calls"] = calls[:30]
        try:
            page.close()
        except Exception:
            pass
    return out


def diagnose_company(c: dict) -> dict:
    t0 = time.time()
    rec = {"id": c["id"], "name": c["name"], "careers_url": c["careers_url"]}
    try:
        r = http.get(c["careers_url"], timeout=20)
        rec["plain_http"] = {"status": r.status_code, "final": r.url, "size": len(r.text)}
    except Exception as e:
        rec["plain_http"] = {"error": f"{type(e).__name__}: {str(e)[:150]}"}
    first = visit(c["careers_url"])
    first.pop("_html", None)
    rec["page"] = first
    rec["followed"] = []
    for link in (first.get("links") or [])[:5]:
        if time.time() - t0 > 150:
            break
        v = visit(link, wait_ms=5000)
        v.pop("_html", None)
        v.pop("links", None)
        rec["followed"].append(v)
    rec["secs"] = round(time.time() - t0, 1)
    return rec


def cmd_diagnose(args) -> int:
    companies = yaml.safe_load((ROOT / "companies.yaml").read_text())
    if args.only:
        ids = {int(x) for x in args.only.split(",")}
    else:
        state = json.loads((DATA / "state.json").read_text())
        ids = {int(k) for k, v in state["companies"].items() if v.get("consecutive_failures")}
    todo = [c for c in companies if c["id"] in ids]
    print(f"Diagnosing {len(todo)} companies…", flush=True)
    out = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(diagnose_company, c) for c in todo]
        for f in as_completed(futs):
            try:
                r = f.result()
            except Exception as e:  # never lose the whole run
                print("  error:", e)
                continue
            out.append(r)
            fps = [r["page"].get("fingerprint")] + [v.get("fingerprint") for v in r["followed"]]
            fp = next((x for x in fps if x), None)
            print(f"  [{r['id']:>3}] {r['name'][:35]:<35} {r['secs']:>6}s  {fp}", flush=True)
    out.sort(key=lambda r: r["id"])
    DATA.mkdir(exist_ok=True)
    (DATA / "diagnose.json").write_text(json.dumps(out, indent=1))
    print(f"Wrote data/diagnose.json ({len(out)} companies)")
    return 0
