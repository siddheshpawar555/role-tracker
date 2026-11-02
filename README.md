# Role Tracker

Checks the official careers sites of 250 target employers twice a day. It keeps only roles that
match your 15 target role families in US locations, stores every match with the date it was
**first seen**, and publishes a tracker page that highlights roles that are **new since the last
check**.

## How "new" is decided

- The first time a company is read successfully, its current roles are saved as a **baseline**.
  They are not flagged as new.
- After that, any role ID that wasn't there before is **new**, stamped with the time of the check
  that found it. The page shows New since last check, or within the last 24 hours, 3, 7 or 30 days.
- A role that is missing from **2 checks in a row** is marked **closed**. If it comes back, it's
  reopened and keeps its original first-seen date.
- If a site fails, or suddenly returns far fewer roles than before (a broken page, not a hiring
  freeze), nothing is closed for that company that run.
- Where the site publishes a posting date, it's shown as "Posted". The tracker's own "First seen"
  is the reliable signal, because many sites refresh posting dates on old roles.

## How it reads each site

`companies.yaml` holds each company's careers URL. On the first run (and every 14 days after),
the tracker works out which platform the site uses and calls that platform's job-search API:

| Platform | Method |
|---|---|
| Workday | `/wday/cxs/{tenant}/{site}/jobs` JSON search |
| Oracle Recruiting Cloud | `recruitingCEJobRequisitions` REST API |
| Eightfold | `/api/apply/v2/jobs` |
| Phenom | search-results page (embedded JSON) |
| Radancy / TalentBrew | `/search-jobs/results` |
| SAP SuccessFactors | career-site search pages |
| iCIMS, Jibe, SmartRecruiters, Greenhouse, Lever, Ashby | their public job APIs |
| Tesla | its listings JSON, through a headless browser |
| Anything else | headless browser, reading job links from the page (marked "page links only") |

Search-based platforms are queried with the `search_terms` in `roles.yaml`. Every title is then
checked against the role-family patterns, so a fuzzy site search can't add irrelevant roles.

## Files

- `companies.yaml`: the employer list. Fix a `careers_url`, add an `ats:` override, or set
  `enabled: false`.
- `roles.yaml`: role families (title regexes), global exclusions, and search terms.
- `data/state.json`: the full role history, committed after every run.
- `data/ats_cache.json`: the platform detected for each company.
- `data/last_run.md`: the new roles from the latest run (also shown in each Actions run summary).
- `.github/workflows/track.yml`: the twice-daily schedule (13:40 and 23:40 UTC, which is about
  7:40 am and 5:40 pm US Central).

## Fixing a company that shows as "Failing"

Open the **Companies** tab on the tracker page to see the error. Usually the careers URL is wrong,
or the site uses an unusual platform. Find the company's job-search page. If its URL contains
`myworkdayjobs.com`, `oraclecloud.com`, `greenhouse.io` and so on, add an override:

```yaml
- id: 71
  name: Medtronic
  careers_url: https://jobs.medtronic.com/
  ats: {type: workday, url: "https://medtronic.wd1.myworkdayjobs.com/MedtronicCareers"}
```

Then go to Actions → Track new roles → Run workflow, and put the company ID in "only".

## Running locally

```bash
pip install -r requirements.txt && python -m playwright install chromium
python -m tracker run                # all companies
python -m tracker run --only 71,146  # specific ids
python -m pytest -q                  # tests
```
Open `site/index.html` to view the tracker.
