"""Role-family matching and US-location filtering."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
    "PR": "Puerto Rico",
}

# Two-letter state codes must be upper case to count (avoid "in", "or", "me").
_US_CODE = re.compile(r"(?:,\s*|\b(?:US|USA)[-,\s]+|\s-\s)(" + "|".join(US_STATES) + r")\b")
_US_WORDS = re.compile(
    r"\b(united states|u\.s\.a?\.?|usa|america)\b|\bUS\b|"
    + r"\b(" + "|".join(re.escape(n) for n in US_STATES.values() if n not in ("Georgia",)) + r")\b",
    re.I,
)
_NON_US = re.compile(
    r"\b(canada|mexico|m[eé]xico|brazil|brasil|argentina|chile|colombia|peru|"
    r"germany|deutschland|france|italy|italia|spain|españa|portugal|united kingdom|uk|england|"
    r"scotland|ireland|netherlands|belgium|switzerland|austria|poland|czech|slovakia|hungary|"
    r"romania|bulgaria|serbia|croatia|slovenia|sweden|norway|denmark|finland|"
    r"india|china|japan|korea|taiwan|singapore|malaysia|thailand|vietnam|philippines|"
    r"indonesia|australia|new zealand|israel|turkey|t[uü]rkiye|egypt|morocco|tunisia|"
    r"south africa|saudi|uae|united arab emirates|qatar|russia|ukraine|"
    r"ontario|quebec|qu[eé]bec|british columbia|alberta|bangalore|bengaluru|hyderabad|pune|"
    r"chennai|shanghai|suzhou|shenzhen|beijing|tokyo|seoul|penang|monterrey|guadalajara|"
    r"juarez|ju[aá]rez|tijuana|chihuahua|queretaro|quer[eé]taro|saltillo|toronto|montreal)\b",
    re.I,
)
_GEORGIA_COUNTRY = re.compile(r"\b(tbilisi|georgia \(country\))\b", re.I)


def is_us_location(text: str | None) -> bool | None:
    """True = US, False = clearly not US, None = unknown (kept)."""
    if not text or not text.strip():
        return None
    t = text.strip()
    if _GEORGIA_COUNTRY.search(t):
        return False
    us = bool(_US_WORDS.search(t) or _US_CODE.search(t) or re.search(r"\bGeorgia\b", t))
    if us:
        return True
    if _NON_US.search(t):
        return False
    # "3 Locations", "Multiple Locations", "Remote" etc.
    return None


@dataclass
class Family:
    id: int
    name: str
    tier: str
    include: list
    exclude: list


class RoleMatcher:
    def __init__(self, path: str | Path):
        cfg = yaml.safe_load(Path(path).read_text())
        self.global_exclude = [re.compile(p, re.I) for p in cfg.get("global_exclude", [])]
        self.search_terms: list[str] = cfg.get("search_terms", [])
        self.families = [
            Family(
                f["id"],
                f["name"],
                f.get("tier", ""),
                [re.compile(p, re.I) for p in f.get("include", [])],
                [re.compile(p, re.I) for p in f.get("exclude", [])],
            )
            for f in cfg["families"]
        ]

    def match(self, title: str) -> Family | None:
        if not title:
            return None
        t = " ".join(title.split())
        if any(p.search(t) for p in self.global_exclude):
            return None
        for f in self.families:
            if any(p.search(t) for p in f.include) and not any(p.search(t) for p in f.exclude):
                return f
        return None
