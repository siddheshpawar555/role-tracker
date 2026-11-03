"""Adapters: one per careers-site platform. Each returns a list of raw job dicts:

    {"id": str, "title": str, "url": str, "location": str, "posted": str|None}

Search-based adapters receive the list of search terms and union the results.
"""
from __future__ import annotations

import datetime as dt
import html as htmlmod
import json
import re
from typing import Callable, Iterable
from urllib.parse import quote, quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from . import http

MAX_PAGES_PER_TERM = 10


class AdapterError(Exception):
    pass


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _iso(d) -> str | None:
    if d is None or d == "":
        return None
    if isinstance(d, (int, float)):
        # epoch seconds or ms
        if d > 1e12:
            d = d / 1000
        return dt.datetime.fromtimestamp(d, dt.timezone.utc).date().isoformat()
    s = str(d).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    for fmt in ("%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d/%m/%Y", "%b %d %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def workday_posted(text: str | None) -> str | None:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    if not text:
        return None
    t = text.lower()
    if "today" in t:
        return _today().isoformat()
    if "yesterday" in t:
        return (_today() - dt.timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*days?", t)
    if m:
        if "+" in t:
            return None  # "30+ days ago" — unknown exact date
        return (_today() - dt.timedelta(days=int(m.group(1)))).isoformat()
    return None


def _dedupe(jobs: Iterable[dict]) -> list[dict]:
    seen, out = set(), []
    for j in jobs:
        if not j.get("id") or not j.get("title"):
            continue
        if j["id"] in seen:
            continue
        seen.add(j["id"])
        out.append(j)
    return out


def _over_terms(terms: list[str], fn: Callable[[str], list[dict]]) -> list[dict]:
    out, errors = [], []
    for i, t in enumerate(terms):
        try:
            out.extend(fn(t))
        except http.DeadlineExceeded:
            raise
        except Exception as e:  # keep going with other terms, unless the very first one fails
            errors.append(f"{t}: {e}")
            if i == 0:
                raise AdapterError(f"{e}") from e
    if errors and not out:
        raise AdapterError("; ".join(errors[:3]))
    return _dedupe(out)


# ---------------------------------------------------------------- Workday
WD_RE = re.compile(
    r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"
)
WD_SITE_RE = re.compile(
    r"https?://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([\w-]+)/([A-Za-z0-9_-]+)"
)
_WD_BAD_SITES = {"wday", "job", "login", "jobs", "apply", "en-US", "userHome", "recruiting"}


def parse_workday(url: str) -> dict | None:
    m = WD_RE.match(url)
    if m and m.group(3) not in _WD_BAD_SITES:
        host = f"{m.group(1)}.{m.group(2)}.myworkdayjobs.com"
        return {"type": "workday", "host": host, "tenant": m.group(1), "site": m.group(3)}
    m = WD_SITE_RE.match(url)
    if m:
        host = f"{m.group(1)}.myworkdaysite.com"
        return {"type": "workday", "host": host, "tenant": m.group(2), "site": m.group(3), "myworkdaysite": True}
    return None


def workday(cfg: dict, terms: list[str]) -> list[dict]:
    if "url" in cfg and "host" not in cfg:
        cfg = {**cfg, **(parse_workday(cfg["url"]) or {})}
    host, tenant, site = cfg["host"], cfg["tenant"], cfg["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    base = f"https://{host}/recruiting/{tenant}/{site}" if cfg.get("myworkdaysite") else f"https://{host}/{site}"

    def search(term: str) -> list[dict]:
        out, offset, total = [], 0, None
        for _ in range(MAX_PAGES_PER_TERM):
            r = http.post(
                api,
                json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            if r.status_code >= 400:
                raise AdapterError(f"workday HTTP {r.status_code}")
            data = r.json()
            if total is None:
                total = data.get("total") or 0
            posts = data.get("jobPostings") or []
            for p in posts:
                path = p.get("externalPath") or ""
                if not path:
                    continue
                out.append(
                    {
                        "id": path.rsplit("_", 1)[-1] if "_" in path else path,
                        "title": p.get("title", ""),
                        "url": base + path,
                        "location": p.get("locationsText", ""),
                        "posted": workday_posted(p.get("postedOn")),
                    }
                )
            offset += 20
            if not posts or offset >= total:
                break
            http.polite_sleep(0.25)
        return out

    return _over_terms(terms, search)


# ---------------------------------------------------------------- Greenhouse / Lever / Ashby
def greenhouse(cfg: dict, terms: list[str]) -> list[dict]:
    data = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{cfg['board']}/jobs")
    return [
        {
            "id": str(j["id"]),
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "posted": _iso(j.get("first_published") or j.get("updated_at")),
        }
        for j in data.get("jobs", [])
    ]


def lever(cfg: dict, terms: list[str]) -> list[dict]:
    data = http.get_json(f"https://api.lever.co/v0/postings/{cfg['board']}?mode=json")
    return [
        {
            "id": j["id"],
            "title": j.get("text", ""),
            "url": j.get("hostedUrl", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            "posted": _iso(j.get("createdAt")),
        }
        for j in data
    ]


def ashby(cfg: dict, terms: list[str]) -> list[dict]:
    data = http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{cfg['board']}")
    out = []
    for j in data.get("jobs", []):
        loc = j.get("location", "")
        sec = ", ".join(x.get("location", "") for x in j.get("secondaryLocations", []) if isinstance(x, dict))
        out.append(
            {
                "id": j.get("id", ""),
                "title": j.get("title", ""),
                "url": j.get("jobUrl", ""),
                "location": f"{loc}; {sec}" if sec else loc,
                "posted": _iso(j.get("publishedAt")),
            }
        )
    return out


def smartrecruiters(cfg: dict, terms: list[str]) -> list[dict]:
    company = cfg["board"]

    def search(term: str) -> list[dict]:
        out, offset = [], 0
        for _ in range(MAX_PAGES_PER_TERM):
            url = (
                f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
                f"?limit=100&offset={offset}&country=us&q={quote_plus(term)}"
            )
            data = http.get_json(url)
            for j in data.get("content", []):
                loc = j.get("location") or {}
                out.append(
                    {
                        "id": str(j["id"]),
                        "title": j.get("name", ""),
                        "url": f"https://jobs.smartrecruiters.com/{company}/{j['id']}",
                        "location": ", ".join(
                            x for x in (loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()) if x
                        ),
                        "posted": _iso(j.get("releasedDate")),
                    }
                )
            offset += 100
            if offset >= data.get("totalFound", 0):
                break
        return out

    return _over_terms(terms, search)


# ---------------------------------------------------------------- Eightfold
def eightfold(cfg: dict, terms: list[str]) -> list[dict]:
    host, domain = cfg["host"], cfg["domain"]

    def search(term: str) -> list[dict]:
        out, start = [], 0
        for _ in range(MAX_PAGES_PER_TERM * 2):
            url = (
                f"https://{host}/api/apply/v2/jobs?domain={domain}&start={start}&num=10"
                f"&query={quote_plus(term)}&location=United%20States&sort_by=timestamp"
            )
            data = http.get_json(url)
            pos = data.get("positions") or []
            for p in pos:
                locs = p.get("locations") or [p.get("location", "")]
                out.append(
                    {
                        "id": str(p.get("id") or p.get("ats_job_id")),
                        "title": p.get("name", ""),
                        "url": p.get("canonicalPositionUrl") or f"https://{host}/careers?pid={p.get('id')}&domain={domain}",
                        "location": "; ".join(x for x in locs if x),
                        "posted": _iso(p.get("t_create")),
                    }
                )
            start += len(pos)
            if not pos or start >= (data.get("count") or 0):
                break
            http.polite_sleep(0.2)
        return out

    return _over_terms(terms, search)


# ---------------------------------------------------------------- Oracle Recruiting Cloud
ORACLE_RE = re.compile(
    r"https?://([\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[\w-]+/sites/([\w-]+)"
)


def oracle(cfg: dict, terms: list[str]) -> list[dict]:
    host, site = cfg["host"], cfg["site"]

    def search(term: str) -> list[dict]:
        out, offset = [], 0
        for _ in range(MAX_PAGES_PER_TERM):
            finder = (
                f"findReqs;siteNumber={site},facetsList=LOCATIONS,limit=25,offset={offset},"
                f'keyword="{term}",sortBy=POSTING_DATES_DESC'
            )
            url = (
                f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                f"?onlyData=true&expand=requisitionList.secondaryLocations&finder={quote(finder, safe='=;,')}"
            )
            data = http.get_json(url)
            items = data.get("items") or [{}]
            reqs = items[0].get("requisitionList") or []
            for j in reqs:
                out.append(
                    {
                        "id": str(j.get("Id")),
                        "title": j.get("Title", ""),
                        "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{j.get('Id')}",
                        "location": j.get("PrimaryLocation", ""),
                        "posted": _iso(j.get("PostedDate")),
                    }
                )
            offset += 25
            if not reqs or offset >= (items[0].get("TotalJobsCount") or 0):
                break
        return out

    return _over_terms(terms, search)


# ---------------------------------------------------------------- Phenom People
def _phenom_ddo(page_html: str) -> dict | None:
    i = page_html.find('"eagerLoadRefineSearch"')
    if i < 0:
        return None
    j = page_html.find("{", i)
    try:
        obj, _ = json.JSONDecoder().raw_decode(page_html[j:])
        return obj
    except ValueError:
        return None


def phenom(cfg: dict, terms: list[str]) -> list[dict]:
    host, prefix = cfg["host"], cfg.get("prefix", "/us/en/")

    def search(term: str) -> list[dict]:
        out, start = [], 0
        for _ in range(MAX_PAGES_PER_TERM):
            url = f"https://{host}{prefix}search-results?keywords={quote_plus(term)}&from={start}&s=1"
            r = http.get(url)
            if r.status_code >= 400:
                raise AdapterError(f"phenom HTTP {r.status_code}")
            ddo = _phenom_ddo(r.text)
            if ddo is None:
                raise AdapterError("phenom: no embedded results")
            data = ddo.get("data") or {}
            jobs = data.get("jobs") or []
            for j in jobs:
                jid = str(j.get("jobId") or j.get("reqId") or j.get("jobSeqNo"))
                loc = j.get("location") or ", ".join(
                    x for x in (j.get("city"), j.get("state"), j.get("country")) if x
                )
                out.append(
                    {
                        "id": jid,
                        "title": j.get("title", ""),
                        "url": f"https://{host}{prefix}job/{jid}",
                        "location": loc,
                        "posted": _iso(j.get("postedDate") or j.get("dateCreated")),
                    }
                )
            start += len(jobs)
            total = ddo.get("totalHits") or ddo.get("hits") or 0
            if not jobs or start >= total:
                break
            http.polite_sleep(0.3)
        return out

    return _over_terms(terms, search)


# ---------------------------------------------------------------- Radancy / TalentBrew
def radancy(cfg: dict, terms: list[str]) -> list[dict]:
    host = cfg["host"]

    def search(term: str) -> list[dict]:
        out = []
        for page in range(1, MAX_PAGES_PER_TERM + 1):
            url = (
                f"https://{host}/search-jobs/results?ActiveFacetID=0&CurrentPage={page}"
                f"&RecordsPerPage=50&Distance=50&RadiusUnitType=0&Keywords={quote_plus(term)}"
                "&Location=&ShowRadius=False&IsPagination=False&CustomFacetName=&FacetTerm=&FacetType=0"
                "&SearchResultsModuleName=Search+Results&SearchFiltersModuleName=Search+Filters"
                "&SortCriteria=0&SortDirection=0&SearchType=5&ResultsType=0"
            )
            r = http.get(url, headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
            if r.status_code >= 400:
                raise AdapterError(f"radancy HTTP {r.status_code}")
            soup = BeautifulSoup(r.json().get("results", ""), "html.parser")
            links = soup.select("a[data-job-id]") or soup.select('a[href*="/job/"]')
            for a in links:
                title_el = a.find(["h2", "h3"])
                loc_el = a.select_one(".job-location") or (a.parent.select_one(".job-location") if a.parent else None)
                date_el = a.select_one(".job-date-posted") or (a.parent.select_one(".job-date-posted") if a.parent else None)
                href = urljoin(f"https://{host}", a.get("href", ""))
                out.append(
                    {
                        "id": a.get("data-job-id") or href,
                        "title": (title_el or a).get_text(" ", strip=True),
                        "url": href,
                        "location": loc_el.get_text(" ", strip=True) if loc_el else "",
                        "posted": _iso(date_el.get_text(strip=True)) if date_el else None,
                    }
                )
            sec = soup.select_one("#search-results, section[data-total-pages]")
            total_pages = int(sec.get("data-total-pages", 1)) if sec and sec.get("data-total-pages") else 1
            if not links or page >= total_pages:
                break
        return out

    return _over_terms(terms, search)


# ---------------------------------------------------------------- SAP SuccessFactors (RMK career sites)
def successfactors(cfg: dict, terms: list[str]) -> list[dict]:
    host = cfg["host"]
    base = f"https://{host}"

    def search(term: str) -> list[dict]:
        out, start = [], 0
        for _ in range(MAX_PAGES_PER_TERM):
            r = http.get(f"{base}/search/?q={quote_plus(term)}&startrow={start}")
            if r.status_code >= 400:
                raise AdapterError(f"successfactors HTTP {r.status_code}")
            soup = BeautifulSoup(r.text, "html.parser")
            rows = soup.select("tr.data-row")
            for row in rows:
                a = row.select_one("a.jobTitle-link")
                if not a:
                    continue
                href = urljoin(base, a.get("href", ""))
                loc = row.select_one(".jobLocation")
                date = row.select_one(".jobDate")
                m = re.search(r"/(\d+)/?$", href)
                out.append(
                    {
                        "id": m.group(1) if m else href,
                        "title": a.get_text(" ", strip=True),
                        "url": href,
                        "location": loc.get_text(" ", strip=True) if loc else "",
                        "posted": _iso(date.get_text(strip=True)) if date else None,
                    }
                )
            start += len(rows)
            label = soup.select_one(".paginationLabel")
            total = int(re.findall(r"\d+", label.get_text())[-1]) if label and re.findall(r"\d+", label.get_text()) else 0
            if not rows or start >= total:
                break
        return out

    return _over_terms(terms, search)


# ---------------------------------------------------------------- iCIMS
def icims(cfg: dict, terms: list[str]) -> list[dict]:
    host = cfg["host"]

    def search(term: str) -> list[dict]:
        out = []
        for page in range(MAX_PAGES_PER_TERM):
            r = http.get(f"https://{host}/jobs/search?ss=1&searchKeyword={quote_plus(term)}&in_iframe=1&pr={page}")
            if r.status_code >= 400:
                raise AdapterError(f"icims HTTP {r.status_code}")
            soup = BeautifulSoup(r.text, "html.parser")
            found = 0
            for a in soup.select('a[href*="/jobs/"]'):
                href = a.get("href", "")
                m = re.search(r"/jobs/(\d+)/", href)
                if not m:
                    continue
                row = a.find_parent(class_=re.compile("row|iCIMS_JobsTable")) or a.parent
                title = a.get("title") or a.get_text(" ", strip=True)
                title = re.sub(r"^\d+\s*-\s*", "", title).strip()
                text = row.get_text(" ", strip=True) if row else ""
                lm = re.search(r"\b([A-Z]{2}-[A-Z]{2}-[\w .'-]+)", text)
                out.append(
                    {"id": m.group(1), "title": title, "url": href.split("?")[0], "location": lm.group(1) if lm else text[:120], "posted": None}
                )
                found += 1
            if not found or not soup.select_one('a[href*="pr=%d"]' % (page + 1)):
                break
        return out

    return _over_terms(terms, search)


# ---------------------------------------------------------------- Jibe (iCIMS Jibe / Google CTS sites)
def jibe(cfg: dict, terms: list[str]) -> list[dict]:
    host = cfg["host"]

    def search(term: str) -> list[dict]:
        out = []
        for page in range(1, MAX_PAGES_PER_TERM + 1):
            data = http.get_json(f"https://{host}/api/jobs?keywords={quote_plus(term)}&page={page}&limit=100")
            jobs = data.get("jobs") or []
            for w in jobs:
                j = w.get("data", w)
                slug = j.get("slug") or j.get("req_id")
                out.append(
                    {
                        "id": str(j.get("req_id") or slug),
                        "title": j.get("title", ""),
                        "url": f"https://{host}/jobs/{slug}",
                        "location": j.get("full_location") or j.get("location_name")
                        or ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x),
                        "posted": _iso(j.get("posted_date") or j.get("create_date")),
                    }
                )
            if not jobs or page * 100 >= (data.get("totalCount") or 0):
                break
        return out

    return _over_terms(terms, search)


# ---------------------------------------------------------------- Generic page (JSON-LD + links)
_JOBLINK = re.compile(r"(job|career|position|opening|requisition|vacanc)", re.I)


def parse_generic_html(page_html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
    out = []
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.string or "")
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if isinstance(it, dict) and it.get("@type") == "JobPosting":
                loc = it.get("jobLocation") or {}
                if isinstance(loc, list):
                    loc = loc[0] if loc else {}
                addr = (loc.get("address") or {}) if isinstance(loc, dict) else {}
                out.append(
                    {
                        "id": str(it.get("identifier", {}).get("value") if isinstance(it.get("identifier"), dict) else it.get("url") or it.get("title")),
                        "title": htmlmod.unescape(it.get("title", "")),
                        "url": it.get("url") or base_url,
                        "location": ", ".join(str(addr.get(k, "")) for k in ("addressLocality", "addressRegion", "addressCountry") if addr.get(k)),
                        "posted": _iso(it.get("datePosted")),
                    }
                )
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = urljoin(base_url, a["href"])
        if 8 <= len(text) <= 140 and _JOBLINK.search(href) and re.search(r"\d{3,}|/job/", href):
            out.append({"id": href, "title": text, "url": href, "location": "", "posted": None})
    return _dedupe(out)


def generic(cfg: dict, terms: list[str]) -> list[dict]:
    url = cfg.get("url")
    r = http.get(url)
    if r.status_code >= 400:
        raise AdapterError(f"HTTP {r.status_code}")
    return parse_generic_html(r.text, r.url)


ADAPTERS: dict[str, Callable[[dict, list[str]], list[dict]]] = {
    "workday": workday,
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "eightfold": eightfold,
    "oracle": oracle,
    "phenom": phenom,
    "radancy": radancy,
    "successfactors": successfactors,
    "icims": icims,
    "jibe": jibe,
    "generic": generic,
}

# Platforms that return the whole board in one call (filter locally, no search terms needed).
FULL_BOARD = {"greenhouse", "lever", "ashby", "generic"}


def host_of(url: str) -> str:
    return urlparse(url).netloc
