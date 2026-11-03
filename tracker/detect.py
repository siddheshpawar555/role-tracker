"""Work out which careers platform a company uses, starting from its careers URL.

Looks at the final URL and page HTML for platform fingerprints; if none, follows
up to a handful of "jobs / search / careers" links one level deep.
"""
from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from . import http
from .adapters import ORACLE_RE, WD_RE, WD_SITE_RE, parse_workday

GH_RE = re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)")
LEVER_RE = re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)")
ASHBY_RE = re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")
SR_RE = re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")
ICIMS_RE = re.compile(r"((?:careers|jobs|uscareers|external)[\w-]*\.icims\.com)")
EIGHTFOLD_HOST_RE = re.compile(r"https?://([\w-]+\.eightfold\.ai)")
EIGHTFOLD_DOMAIN_RE = re.compile(r"""(?:[?&]domain=|"domain"\s*:\s*")([\w.-]+\.[a-z]{2,})""")

_NAV_WORDS = re.compile(r"(search|job|opening|opportunit|position|career|join|apply|vacanc)", re.I)
_SKIP = re.compile(r"(linkedin|facebook|twitter|x\.com|instagram|youtube|glassdoor|indeed|mailto:|\.pdf$|privacy|cookie|login|signin)", re.I)


def _most_common(matches):
    return Counter(matches).most_common(1)[0][0] if matches else None


def fingerprint(page_html: str, final_url: str) -> dict | None:
    """Return an adapter config if this page reveals a supported platform."""
    blob = final_url + "\n" + page_html
    host = urlparse(final_url).netloc

    wd = [m.group(0) for m in WD_RE.finditer(blob) if m.group(3) not in ("wday", "job", "login", "jobs")]
    wd += [m.group(0) for m in WD_SITE_RE.finditer(blob)]
    if wd:
        cfg = parse_workday(_most_common(wd))
        if cfg:
            return cfg
    m = ORACLE_RE.search(blob)
    if m:
        return {"type": "oracle", "host": m.group(1), "site": m.group(2)}
    m = GH_RE.search(blob)
    if m and m.group(1).lower() not in ("embed", "js"):
        return {"type": "greenhouse", "board": m.group(1)}
    m = LEVER_RE.search(blob)
    if m:
        return {"type": "lever", "board": m.group(1)}
    m = ASHBY_RE.search(blob)
    if m:
        return {"type": "ashby", "board": m.group(1)}
    m = SR_RE.search(blob)
    if m and m.group(1).lower() not in ("company", "app", "static"):
        return {"type": "smartrecruiters", "board": m.group(1)}

    eh = EIGHTFOLD_HOST_RE.search(blob)
    if eh or "eightfold" in page_html.lower() and "/careers" in final_url:
        dm = EIGHTFOLD_DOMAIN_RE.search(blob)
        if dm:
            return {"type": "eightfold", "host": eh.group(1) if eh else host, "domain": dm.group(1)}

    low = page_html.lower()
    if "jibecdn" in low or "jibe-" in low and "/api/jobs" in low:
        return {"type": "jibe", "host": host}
    if "phenompeople" in low or "phapp.ddo" in low or "phApp" in page_html:
        return {"type": "phenom", "host": host, "prefix": phenom_prefix(page_html, final_url)}
    if "tbcdn.talentbrew.com" in low or "radancy" in low and "search-jobs" in low:
        return {"type": "radancy", "host": host}
    if "rmkcdn.successfactors.com" in low or "jobtitle-link" in low:
        return {"type": "successfactors", "host": host}
    m = ICIMS_RE.search(blob)
    if m:
        return {"type": "icims", "host": m.group(1)}
    return None


def phenom_prefix(page_html: str, final_url: str) -> str:
    m = re.search(r'href="(?:https?://[^/"]+)?(/[a-z]{2,6}/[a-z]{2}/)search-results', page_html)
    if m:
        return m.group(1)
    m = re.search(r"^(/[a-z]{2,6}/[a-z]{2}/)", urlparse(final_url).path)
    return m.group(1) if m else "/us/en/"


def candidate_links(page_html: str, base_url: str, limit: int = 8) -> list[str]:
    soup = BeautifulSoup(page_html, "html.parser")
    base_host = urlparse(base_url).netloc.split(".")[-2:]
    scored = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#")[0]
        text = a.get_text(" ", strip=True)
        if not href.startswith("http") or _SKIP.search(href):
            continue
        if not (_NAV_WORDS.search(href) or _NAV_WORDS.search(text)):
            continue
        h = urlparse(href).netloc
        same_org = h.split(".")[-2:] == base_host
        score = 0
        score += 3 if re.search(r"search|jobs|openings|find", href + " " + text, re.I) else 0
        score += 2 if not same_org else 0  # external links often point at the ATS
        score += 1 if re.search(r"united states|usa|\bus\b|north america", href + " " + text, re.I) else 0
        scored.append((score, href))
    seen, out = set(), []
    for _, href in sorted(scored, key=lambda x: -x[0]):
        if href not in seen and href != base_url:
            seen.add(href)
            out.append(href)
        if len(out) >= limit:
            break
    return out


def fetch(url: str, browser=None) -> tuple[str, str] | None:
    """Return (html, final_url); fall back to a real browser when blocked."""
    try:
        r = http.get(url)
        if r.status_code < 400 and len(r.text) > 500:
            return r.text, r.url
    except http.DeadlineExceeded:
        raise
    except Exception:
        pass
    if browser is not None:
        return browser.render(url)
    return None


def discover(careers_url: str, browser=None) -> tuple[dict | None, str]:
    """Return (config or None, note)."""
    first = parse_workday(careers_url)
    if first:
        return first, "workday URL given"
    got = fetch(careers_url, browser)
    if not got:
        return None, "careers page unreachable"
    page, final = got
    cfg = fingerprint(page, final)
    if cfg:
        return cfg, f"found on {final}"
    for link in candidate_links(page, final):
        http.check_deadline()
        cfg = parse_workday(link)
        if cfg:
            return cfg, f"linked from {final}"
        sub = fetch(link, browser)
        if not sub:
            continue
        cfg = fingerprint(*sub)
        if cfg:
            return cfg, f"found on {sub[1]}"
    return None, "no supported platform found"
