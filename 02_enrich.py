#!/usr/bin/env python3
"""
TrashLab Build 1 — 02_enrich.py

Purpose
-------
Turn qualified Texas hauler candidates into a reproducible enrichment workstream
without requiring a paid search API or scraping Google results.

The script:
1) reads qualified_candidates.csv
2) preserves municipal-source phone/email/website data
3) generates deterministic Google research URLs for missing/verification fields
4) crawls any known company website to extract public evidence
5) normalizes service lines and adjacent TrashLab-relevant services
6) extracts explicit size signals (fleet/trucks/employees/customers/locations)
7) creates a review-ready CSV with evidence/status fields
8) never invents missing values

This is intentionally human-in-the-loop for Google discovery. Search-result selection
is a judgment step; known websites are automatically crawled and structured.

Usage
-----
!pip install requests beautifulsoup4
!python /content/02_enrich.py --self-test
!python /content/02_enrich.py --input /content/data/qualified_candidates.csv
"""

from __future__ import annotations
import argparse, csv, re, time
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUTS = [
    Path("/content/data/qualified_candidates.csv"),
    ROOT / "data" / "qualified_candidates.csv",
    ROOT / "qualified_candidates.csv",
]
UA = "Mozilla/5.0 (compatible; TrashLab-ICP-Research/1.0)"
TIMEOUT = 12

# Curated website registry: verified through Google/web research, then validated
# against company name/phone/location before inclusion. This is deliberately
# explicit rather than silently trusting a search-engine first result.
WEBSITE_REGISTRY = {
    "balcones recycling": "https://www.balconesrecycling.com/",
    "fusion waste & recycling": "https://fusionwaste.com/",
    "frontier waste solutions": "https://frontierwaste.com/",
    "independent waste": "https://www.independentwaste.net/",
    "republic services": "https://www.republicservices.com/",
    "supreme recycling": "https://dfwwastehaulers.com/",
    "texas disposal systems": "https://www.texasdisposal.com/",
    "waste advantage": "https://wasteadvantagetx.com/",
    "waste management": "https://www.wm.com/",
    "advantage waste disposal": "https://www.advantagewastedisposal.net/",
}


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]"
    r"([2-9]\d{2})[\s.\-](\d{4})(?!\d)"
)

SERVICE_PATTERNS = {
    "roll-off": [r"\broll[\s-]?off\b", r"\bdumpster(?: rental| service)?\b", r"\bopen top\b"],
    "residential": [r"\bresidential\b", r"\bcurbside\b", r"\bhousehold (?:trash|waste)\b"],
    "commercial": [r"\bcommercial\b", r"\bfront[\s-]?load\b", r"\brear[\s-]?load\b", r"\bbusiness waste\b"],
    "recycling": [r"\brecycl(?:e|ing)\b", r"\bcardboard\b", r"\bmaterials recovery\b"],
}
ADJACENT_PATTERNS = {
    "portable toilet": [r"\bportable toilet\b", r"\bportable restroom\b", r"\bporta[\s-]?pot"],
    "septic": [r"\bseptic\b"],
    "compactor": [r"\bcompactor\b"],
    "construction & demolition": [r"\bconstruction\b", r"\bdemolition\b", r"\bc&d\b", r"\bconstruction debris\b"],
}
SIZE_PATTERNS = [
    re.compile(r"\b(?:fleet of|operate[sd]?|operating)\s+(?:more than |over |approximately |about )?(\d{1,4})\s+(?:trucks?|vehicles?)\b", re.I),
    re.compile(r"\b(\d{1,4})\s+(?:truck|vehicle)\s+fleet\b", re.I),
    re.compile(r"\b(?:employs?|team of)\s+(?:more than |over |approximately |about )?(\d{1,5})\s+(?:employees?|people|team members?)\b", re.I),
    re.compile(r"\b(\d{1,5})\s+employees?\b", re.I),
    re.compile(r"\b(?:serv(?:es|ing))\s+(?:more than |over |approximately |about )?([\d,]+)\s+(?:customers?|homes?|businesses?)\b", re.I),
    re.compile(r"\b(\d{1,3})\s+(?:locations?|facilities?|transfer stations?)\b", re.I),
]

def clean(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()

def first_value(v: str) -> str:
    # discovery output sometimes uses " | " for multiple values
    return clean(v).split(" | ")[0].strip()

def normalize_url(v: str) -> str:
    v = first_value(v)
    if not v:
        return ""
    if not re.match(r"^https?://", v, re.I):
        v = "https://" + v.lstrip("/")
    return v

def google_search(query: str) -> str:
    return "https://www.google.com/search?q=" + quote_plus(query)

def normalize_phone(v: str) -> str:
    m = PHONE_RE.search(clean(v))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else clean(v)

def domain(url: str) -> str:
    try:
        h = urlparse(url).netloc.lower().split(":")[0]
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""

def fetch(url: str) -> Tuple[str, str]:
    if not url:
        return "", ""
    candidates = [url]
    if url.startswith("https://"):
        candidates.append("http://" + url[len("https://"):])
    for candidate in candidates:
        try:
            r = requests.get(candidate, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            if "html" not in r.headers.get("content-type", "").lower() and "<html" not in r.text[:1000].lower():
                continue
            return r.text, clean(r.url)
        except requests.RequestException:
            pass
    return "", ""

def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return clean(soup.get_text(" ", strip=True))

def internal_links(base: str, html: str, limit: int = 5) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    base_domain = domain(base)
    ranked = []
    seen = set()
    weights = {
        "service": 10, "dumpster": 10, "roll": 9, "commercial": 9,
        "residential": 9, "recycl": 9, "about": 7, "contact": 6,
        "location": 6, "fleet": 6,
    }
    for a in soup.find_all("a", href=True):
        href = clean(a["href"])
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        u = urljoin(base, href)
        if domain(u) != base_domain:
            continue
        p = urlparse(u)
        u = f"{p.scheme}://{p.netloc}{p.path}"
        if u in seen:
            continue
        seen.add(u)
        label = (clean(a.get_text(" ", strip=True)) + " " + p.path).lower()
        score = max([w for k, w in weights.items() if k in label] or [0])
        if score:
            ranked.append((score, u))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return [u for _, u in ranked[:limit]]

def extract_labels(text: str, patterns: Dict[str, List[str]]) -> List[str]:
    return [label for label, pats in patterns.items() if any(re.search(p, text, re.I) for p in pats)]

def extract_size(text: str) -> List[str]:
    found = []
    for pat in SIZE_PATTERNS:
        for m in pat.finditer(text):
            snippet = clean(text[max(0, m.start()-55):min(len(text), m.end()+55)])
            if snippet not in found:
                found.append(snippet)
            if len(found) >= 3:
                return found
    return found

def choose_email(text: str, site_domain: str) -> str:
    emails = sorted(set(e.lower().strip(".,;:()[]<>") for e in EMAIL_RE.findall(text)))
    emails = [e for e in emails if "example.com" not in e and "sentry" not in e]
    if not emails:
        return ""
    same_domain = [e for e in emails if e.split("@")[-1] == site_domain]
    priorities = ("info@", "sales@", "contact@", "office@", "hello@", "service@")
    for prefix in priorities:
        for e in same_domain:
            if e.startswith(prefix):
                return e
    return same_domain[0] if same_domain else emails[0]

def crawl_known_site(url: str) -> Dict[str, str]:
    html, final = fetch(url)
    if not html:
        return {
            "website_status": "unreachable", "website_evidence_urls": "",
            "web_phone": "", "web_email": "", "web_service_lines": "",
            "web_adjacent_services": "", "web_size_signal": "",
        }

    pages = [(final or url, page_text(html))]
    for link in internal_links(final or url, html):
        time.sleep(0.15)
        h, f = fetch(link)
        if h:
            pages.append((f or link, page_text(h)))

    combined = clean(" ".join(t for _, t in pages))
    phone_match = PHONE_RE.search(combined)
    phone = normalize_phone(phone_match.group(0)) if phone_match else ""
    email = choose_email(combined, domain(final or url))
    services = extract_labels(combined, SERVICE_PATTERNS)
    adjacent = extract_labels(combined, ADJACENT_PATTERNS)
    size = extract_size(combined)

    return {
        "website_status": "crawled",
        "website_evidence_urls": " | ".join(u for u, _ in pages),
        "web_phone": phone,
        "web_email": email,
        "web_service_lines": " | ".join(services),
        "web_adjacent_services": " | ".join(adjacent),
        "web_size_signal": " | ".join(size),
    }

def enrich(row: Dict[str, str]) -> Dict[str, str]:
    out = dict(row)
    company = clean(row.get("company"))
    market = clean(row.get("source_market") or row.get("source_city") or "Texas")

    source_site = normalize_url(row.get("website", ""))
    registry_site = WEBSITE_REGISTRY.get(company.lower(), "")
    known_site = source_site or registry_site
    website_source = "municipal_source" if source_site else ("verified_web_registry" if registry_site else "")
    known_phone = normalize_phone(first_value(row.get("phone", "")))
    known_email = first_value(row.get("email", ""))

    site_data = crawl_known_site(known_site) if known_site else {
        "website_status": "missing_needs_google",
        "website_evidence_urls": "", "web_phone": "", "web_email": "",
        "web_service_lines": "", "web_adjacent_services": "", "web_size_signal": "",
    }

    out.update({
        "verified_website": known_site,
        "website_source": website_source,
        "verified_phone": known_phone or site_data["web_phone"],
        "verified_email": known_email or site_data["web_email"],
        "verified_location": market,
        "service_lines": site_data["web_service_lines"],
        "adjacent_service_lines": site_data["web_adjacent_services"],
        "size_signal": site_data["web_size_signal"],
        "size_signal_source": "company_website" if site_data["web_size_signal"] else "",
        "website_status": site_data["website_status"],
        "evidence_urls": site_data["website_evidence_urls"],
        "google_company_search": google_search(f'"{company}" {market} Texas waste hauler'),
        "google_services_search": google_search(f'"{company}" Texas roll off residential commercial recycling'),
        "google_size_search": google_search(f'"{company}" Texas fleet trucks employees'),
        "fmcsa_size_search": google_search(f'site:safer.fmcsa.dot.gov "{company}" Texas'),
        "google_contact_search": google_search(f'"{company}" Texas phone email contact'),
    })

    missing = []
    if not out["verified_website"]: missing.append("website")
    if not out["verified_phone"]: missing.append("phone")
    if not out["verified_email"]: missing.append("email")
    if not out["service_lines"]: missing.append("service_lines")
    if not out["size_signal"]: missing.append("size_signal")

    if not missing:
        status = "enriched"
    elif known_site and site_data["website_status"] == "crawled":
        status = "partially_enriched"
    else:
        status = "needs_google_research"

    out["verification_status"] = status
    out["missing_fields"] = " | ".join(missing)
    return out

EXTRA = [
    "verified_website","website_source","verified_phone","verified_email","verified_location",
    "service_lines","adjacent_service_lines","size_signal","size_signal_source","website_status",
    "evidence_urls","google_company_search","google_services_search",
    "google_size_search","fmcsa_size_search","google_contact_search",
    "verification_status","missing_fields"
]

def find_input(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise FileNotFoundError(p)
        return p
    for p in DEFAULT_INPUTS:
        if p.exists():
            return p
    raise FileNotFoundError("qualified_candidates.csv not found")

def run(input_path: Path, output_path: Path, limit: int | None) -> None:
    with input_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    if "company" not in fields:
        raise ValueError("Input requires a company column")
    if limit:
        rows = rows[:limit]

    results = []
    for i, row in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {clean(row.get('company'))}")
        try:
            results.append(enrich(row))
        except Exception as e:
            failed = dict(row)
            for k in EXTRA:
                failed.setdefault(k, "")
            failed["verification_status"] = "error"
            failed["missing_fields"] = f"{type(e).__name__}: {e}"
            results.append(failed)

    out_fields = list(dict.fromkeys(fields + EXTRA))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)

    def count(field): return sum(bool(clean(r.get(field))) for r in results)
    statuses = {}
    for r in results:
        s = clean(r.get("verification_status")) or "blank"
        statuses[s] = statuses.get(s, 0) + 1

    print("\nENRICHMENT COMPLETE")
    print(f"Records:       {len(results)}")
    print(f"Website:       {count('verified_website')}")
    print(f"Phone:         {count('verified_phone')}")
    print(f"Email:         {count('verified_email')}")
    print(f"Service lines: {count('service_lines')}")
    print(f"Size signal:   {count('size_signal')}")
    print("Statuses:")
    for k, v in sorted(statuses.items()):
        print(f"  {k}: {v}")
    print(f"Output: {output_path}")

def self_test() -> None:
    assert normalize_phone("(972) 555-1212") == "972-555-1212"
    assert "q=" in google_search('"Acme Waste" Texas')
    assert set(extract_labels(
        "Residential trash, commercial front-load, roll-off dumpsters and recycling.",
        SERVICE_PATTERNS
    )) == {"roll-off", "residential", "commercial", "recycling"}
    assert "portable toilet" in extract_labels("Portable toilet rentals", ADJACENT_PATTERNS)
    assert extract_size("We operate a fleet of 42 trucks across North Texas.")
    assert WEBSITE_REGISTRY["independent waste"] == "https://www.independentwaste.net/"
    assert WEBSITE_REGISTRY["supreme recycling"] == "https://dfwwastehaulers.com/"
    assert "safer.fmcsa.dot.gov" in google_search('site:safer.fmcsa.dot.gov "Supreme Recycling" Texas')
    print("SELF-TEST PASSED")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input")
    p.add_argument("--output", default="/content/data/enriched_candidates.csv")
    p.add_argument("--limit", type=int)
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    if args.self_test:
        self_test()
        return
    run(find_input(args.input), Path(args.output), args.limit)

if __name__ == "__main__":
    main()
