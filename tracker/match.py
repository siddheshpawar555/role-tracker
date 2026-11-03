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
_NON_US_MORE = re.compile(
    r"\b(costa rica|viet nam|puerto rico_|dominican republic|honduras|guatemala|el salvador|panama|uruguay|"
    r"hong kong|macau|sri lanka|pakistan|bangladesh|lithuania|latvia|estonia|greece|luxembourg|"
    r"nuevo le[oó]n|guanajuato|san luis potos[ií]|baja california|coahuila|sonora|jalisco|puebla|"
    r"queensland|new south wales|victoria, au|ranjangaon|noida|gurgaon|gurugram|mumbai|ahmedabad|"
    r"barcelona|madrid|milan|turin|torino|munich|m[uü]nchen|stuttgart|paris|lyon|krak[oó]w|wroc[lł]aw|"
    r"prague|brno|budapest|bucharest|timisoara|istanbul|dublin|cork|galway|limerick|shannon|"
    r"wuxi|tianjin|chongqing|guangzhou|dalian|nanjing|hangzhou|xi'?an|chengdu|kunshan|"
    r"bangkok|rayong|chonburi|kuala lumpur|johor|batam|hanoi|ho chi minh|manila|cebu|"
    r"osaka|nagoya|yokohama|kyoto|daegu|busan|incheon|ulsan|taipei|hsinchu|taichung|tainan|kaohsiung|"
    r"sydney|melbourne|brisbane|adelaide|perth, wa|auckland|"
    r"vancouver|calgary|edmonton|winnipeg|ottawa|mississauga|brampton|guelph|windsor, on|"
    r"s[aã]o paulo|campinas|curitiba|buenos aires|santiago|bogot[aá]|lima)\b",
    re.I,
)
_ISO3_NON_US = re.compile(
    r"\b(GBR|IRL|DEU|FRA|ITA|ESP|PRT|NLD|BEL|CHE|AUT|POL|CZE|SVK|HUN|ROU|SWE|NOR|DNK|FIN|"
    r"MEX|CAN|BRA|ARG|CRI|CHN|IND|JPN|KOR|TWN|SGP|MYS|THA|VNM|PHL|IDN|AUS|NZL|ISR|TUR|ZAF|"
    r"ARE|SAU|DOM|HND|SRB|HRV|BGR|LTU|MAR|TUN|EGY)\b"
)
# "City, ST, CC, zip" (SuccessFactors) / "City, CC" : a trailing 2-letter code that isn't a US state is a country
_TRAILING_CODES = re.compile(r",\s*([A-Z]{2})(?:\s*,\s*([A-Z]{2}))?(?:\s*,\s*[\w -]{3,10})?\s*$")
# "Wilmington-MA", "US-MI-Auburn Hills"
_DASH_STATE = re.compile(r"[A-Za-z]-(" + "|".join(US_STATES) + r")\b")
_US_ZIP = re.compile(r",\s*[A-Z]{2}\s*,?\s*\d{5}(?:-\d{4})?\b")


def is_us_location(text: str | None) -> bool | None:
    """True = US, False = clearly not US, None = unknown (kept)."""
    if not text or not text.strip():
        return None
    t = text.strip()
    if _GEORGIA_COUNTRY.search(t):
        return False
    t = re.sub(r"\s*\+\d+ more.*$", "", t)  # "Presov, SK, 08001 +2 more…"
    m = re.match(r"^([A-Z]{2})-[A-Z]{2,3}-", t)  # RTX style "US-MA-ANDOVER-…", "CA-ON-MIDLAND-…"
    if m:
        return m.group(1) == "US"
    m = _TRAILING_CODES.search(t)
    if m:
        last = m.group(2) or m.group(1)
        if last == "US":
            return True
        if m.group(2) and m.group(1) not in ("US",):
            return False  # "Stuttgart, BW, DE": state/province then country
        if last not in US_STATES:
            return False  # "Lublin, PL", "Kunshan, CN"
    us = bool(_US_WORDS.search(t) or _US_CODE.search(t) or _DASH_STATE.search(t) or re.search(r"\bGeorgia\b", t))
    if us:
        return True
    if _NON_US.search(t) or _NON_US_MORE.search(t) or _ISO3_NON_US.search(t):
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
