#!/usr/bin/env python3
"""
TrashLab Build 1 — 02_enrich.py

Public/company enrichment layer for qualified Texas waste-hauler candidates.

Workflow (consolidated from the tested SerpApi v3 + You.com v2 + strict-validation passes):
1. Preserve municipal/source phone, email, and website data.
2. Validate source websites.
3. Resolve missing company domains with SerpApi, then You.com as a fallback.
4. Crawl accepted company sites for public contact, service-line, and size evidence.
5. Preserve unknowns rather than fabricating values.
6. Checkpoint after every processed row so the job is resumable.

Environment variables:
    SERPAPI_KEY
    YOU_API_KEY

Example:
    python 02_enrich.py \
      --input data/qualified_candidates.csv \
      --output data/enriched_candidates.csv \
      --resume
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SERPAPI_URL = "https://serpapi.com/search.json"
YOU_API_URL = "https://ydc-index.io/v1/search"

UA = "Mozilla/5.0 (compatible; TrashLabTakeHome/1.0)"
HEADERS = {"User-Agent": UA}
SEARCH_TIMEOUT = 30
CRAWL_TIMEOUT = 10

BLOCKED_DOMAINS = {
    "facebook.com","instagram.com","linkedin.com","youtube.com","yelp.com",
    "mapquest.com","yellowpages.com","bbb.org","chamberofcommerce.com",
    "dnb.com","zoominfo.com","crunchbase.com","wikipedia.org","x.com",
    "twitter.com","angi.com","manta.com","nextdoor.com","homeadvisor.com",
    "opencorporates.com","google.com","bing.com","duckduckgo.com",
    "timetorecycle.com","highergov.com","hub.biz","curbwaste.com",
    "safer.fmcsa.dot.gov","fmcsa.dot.gov","houstontx.gov","cleburne.net",
    "dallascityhall.com","plano.gov","garlandtx.gov","lanefinder.com",
    "buzzfile.com","buildzoom.com","bloomberg.com","roserocket.com",
    "bubba.ai","prospeo.io","cityofdenton.com","procore.com","civicplus.com",
}

GENERIC_COMPANY_TOKENS = {
    "llc","inc","corp","corporation","company","co","ltd","limited","the",
    "services","service","texas","tx","waste","disposal","recycling",
    "solutions","management","sanitation",
}

CORE_TERMS = {
    "roll-off": ["roll off","roll-off","rolloff","dumpster"],
    "residential": ["residential","curbside","household trash"],
    "commercial": ["commercial","front load","front-load","rear load","rear-load"],
    "recycling": ["recycling","recycle"],
}

ADJACENT_TERMS = {
    "portable toilet": ["portable toilet","porta potty","portable restroom"],
    "septic": ["septic"],
}

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}")
SIZE_PATTERNS = [
    re.compile(r"\b(\d{1,4})\+?\s+(?:trucks|vehicles|power units)\b", re.I),
    re.compile(
        r"\b(?:fleet of|fleet includes|operate(?:s|d)?)\s+"
        r"(?:over\s+|more than\s+|approximately\s+|about\s+)?(\d{1,4})\b",
        re.I,
    ),
    re.compile(r"\b(\d{1,5})\+?\s+(?:employees|team members|staff)\b", re.I),
]

EXTRA_FIELDS = [
    "verified_website",
    "website_resolution_source",
    "verified_phone",
    "verified_email",
    "verified_location",
    "service_lines",
    "adjacent_service_lines",
    "size_signal",
    "evidence_urls",
    "verification_status",
    "missing_fields",
]


def clean(value):
    return (value or "").strip()


def norm_text(value):
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def domain(url):
    try:
        d = urlparse(url).netloc.lower().split(":")[0]
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


def root_url(url):
    if not clean(url):
        return ""
    url = clean(url)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    p = urlparse(url)
    if not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


def blocked(url):
    d = domain(url)
    return not d or any(d == b or d.endswith("." + b) for b in BLOCKED_DOMAINS)


def normalize_phone(value):
    m = PHONE_RE.search(clean(value))
    if not m:
        return clean(value)
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return m.group(0)


def company_tokens(company):
    return {
        t for t in norm_text(company).split()
        if len(t) >= 3 and t not in GENERIC_COMPANY_TOKENS
    }


def parser_artifact(company):
    c = clean(company).lower()
    return (
        "$(document)" in c
        or "renderslideshowifapplicable" in c
        or "function (" in c
        or len(c) > 180
    )


def match_score(company, location, url, title="", snippet=""):
    if blocked(url):
        return -999

    tokens = company_tokens(company)
    if not tokens:
        return -999

    dwords = set(norm_text(domain(url).replace(".", " ").replace("-", " ")).split())
    twords = set(norm_text(title).split())
    swords = set(norm_text(snippet).split())
    all_words = dwords | twords | swords

    distinctive = {t for t in tokens if t not in {"all","tex","north","south","east","west"}}
    hits = distinctive & all_words

    if distinctive and not hits:
        return -999

    score = len(tokens & all_words) * 3
    score += len(hits) * 4
    score += len(distinctive & dwords) * 5

    location_words = set(norm_text(location).split()) - {"texas", "tx"}
    if location_words & all_words:
        score += 2

    industry = {
        "waste","disposal","dumpster","recycling","trash",
        "septic","environmental","sanitation"
    }
    if industry & all_words:
        score += 2

    return score


def serpapi_results(company, location, api_key):
    if not api_key:
        return []

    query = f'"{company}" {location} Texas waste disposal recycling official website'
    params = {
        "engine": "google",
        "q": query,
        "api_key": api_key,
        "num": 10,
    }

    last_error = None
    for attempt in range(3):
        try:
            r = requests.get(SERPAPI_URL, params=params, timeout=SEARCH_TIMEOUT)
            r.raise_for_status()
            payload = r.json()
            out = []
            for item in payload.get("organic_results", []):
                url = clean(item.get("link"))
                if url:
                    out.append({
                        "url": url,
                        "title": clean(item.get("title")),
                        "snippet": clean(item.get("snippet")),
                    })
            return out
        except (requests.Timeout, requests.ConnectionError) as e:
            last_error = e
            time.sleep(1 + attempt * 2)
        except Exception:
            raise

    if last_error:
        raise last_error
    return []


def you_results(company, location, api_key):
    if not api_key:
        return []

    query = f'"{company}" {location} Texas waste disposal recycling official website'
    headers = {"X-API-Key": api_key}
    params = {"query": query, "count": 10}

    last_error = None
    for attempt in range(3):
        try:
            r = requests.get(YOU_API_URL, headers=headers, params=params, timeout=SEARCH_TIMEOUT)
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            r.raise_for_status()
            payload = r.json()

            web = []
            results = payload.get("results", {})
            if isinstance(results, dict) and isinstance(results.get("web"), list):
                web = results["web"]

            out = []
            for item in web:
                if not isinstance(item, dict):
                    continue
                snippets = item.get("snippets")
                if isinstance(snippets, list):
                    snippets = " ".join(str(x) for x in snippets)
                url = clean(item.get("url") or item.get("link"))
                if url:
                    out.append({
                        "url": url,
                        "title": clean(item.get("title")),
                        "snippet": clean(
                            item.get("description")
                            or item.get("snippet")
                            or snippets
                            or item.get("text")
                        ),
                    })
            return out
        except (requests.Timeout, requests.ConnectionError) as e:
            last_error = e
            time.sleep(1 + attempt * 2)
        except Exception:
            raise

    if last_error:
        raise last_error
    return []


def choose_official(company, location, results, threshold=10):
    ranked = []
    for item in results:
        score = match_score(
            company,
            location,
            item.get("url", ""),
            item.get("title", ""),
            item.get("snippet", ""),
        )
        if score >= threshold:
            ranked.append((score, item))

    if not ranked:
        return ""

    ranked.sort(key=lambda x: x[0], reverse=True)
    return root_url(ranked[0][1]["url"])


def crawl_site(site):
    text_parts = []
    evidence = []
    emails = set()
    phones = set()

    for path in ("", "/services", "/about", "/contact"):
        url = urljoin(site.rstrip("/") + "/", path.lstrip("/"))
        try:
            r = requests.get(url, headers=HEADERS, timeout=CRAWL_TIMEOUT, allow_redirects=True)
            if r.status_code >= 400:
                continue
            if "text/html" not in r.headers.get("content-type", ""):
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            text = " ".join(soup.stripped_strings)
            text_parts.append(text[:50000])
            evidence.append(r.url)

            for e in EMAIL_RE.findall(text):
                if not any(x in e.lower() for x in ("example.com", "sentry.io")):
                    emails.add(e)

            for p in PHONE_RE.findall(text):
                phones.add(normalize_phone(p))
        except Exception:
            pass

    text = " ".join(text_parts)
    low = text.lower()

    services = [
        name for name, terms in CORE_TERMS.items()
        if any(term in low for term in terms)
    ]
    adjacent = [
        name for name, terms in ADJACENT_TERMS.items()
        if any(term in low for term in terms)
    ]

    size = ""
    for pattern in SIZE_PATTERNS:
        m = pattern.search(text)
        if m:
            size = m.group(0)
            break

    return {
        "phone": sorted(phones)[0] if phones else "",
        "email": sorted(emails)[0] if emails else "",
        "services": "; ".join(services),
        "adjacent": "; ".join(adjacent),
        "size": size,
        "evidence": "; ".join(dict.fromkeys(evidence)),
    }


def source_website(row):
    site = root_url(row.get("website", ""))
    if site and not blocked(site):
        return site
    return ""


def enrich_row(row, serpapi_key, you_key):
    result = dict(row)
    for field in EXTRA_FIELDS:
        result.setdefault(field, "")

    company = clean(row.get("company"))
    location = clean(row.get("source_market") or row.get("source_city"))

    if parser_artifact(company):
        result["verification_status"] = "rejected_parser_artifact"
        result["missing_fields"] = "parser_artifact"
        return result

    # Preserve municipal/source data first.
    result["verified_phone"] = normalize_phone(row.get("phone", ""))
    result["verified_email"] = clean(row.get("email"))
    result["verified_location"] = location

    site = source_website(row)
    if site:
        result["verified_website"] = site
        result["website_resolution_source"] = "source"
    else:
        site = choose_official(
            company,
            location,
            serpapi_results(company, location, serpapi_key),
        )
        if site:
            result["verified_website"] = site
            result["website_resolution_source"] = "serpapi"

    if not site:
        site = choose_official(
            company,
            location,
            you_results(company, location, you_key),
        )
        if site:
            result["verified_website"] = site
            result["website_resolution_source"] = "you.com"

    if site:
        details = crawl_site(site)

        if not result["verified_phone"] and details["phone"]:
            result["verified_phone"] = details["phone"]
        if not result["verified_email"] and details["email"]:
            result["verified_email"] = details["email"]

        result["service_lines"] = details["services"]
        result["adjacent_service_lines"] = details["adjacent"]
        result["size_signal"] = details["size"]
        result["evidence_urls"] = details["evidence"]
        result["verification_status"] = "enriched"
    else:
        result["verification_status"] = "unresolved_website"

    missing = []
    for label, field in (
        ("website", "verified_website"),
        ("phone", "verified_phone"),
        ("email", "verified_email"),
        ("service_lines", "service_lines"),
        ("size_signal", "size_signal"),
    ):
        if not clean(result.get(field)):
            missing.append(label)
    result["missing_fields"] = "; ".join(missing)

    return result


def checkpoint(path, rows, fieldnames):
    tmp = Path(str(path) + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)



def self_test():
    assert blocked("https://www.bloomberg.com/foo")
    assert blocked("https://safer.fmcsa.dot.gov/query.asp")
    assert blocked("https://www.procore.com/article")
    assert not blocked("https://www.wasteadvantagetx.com/")
    assert parser_artifact("$(document).ready(function (e) { renderSlideshowIfApplicable(...) })")
    assert root_url("https://www.example.com/a/b") == "https://www.example.com"
    assert match_score(
        "V.F. Waste Service",
        "Texas",
        "https://www.osha.gov/",
        "OSHA",
        "Safety information",
    ) < 10
    print("SELF-TEST PASSED")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="data/qualified_candidates.csv")
    p.add_argument("--output", default="data/enriched_candidates.csv")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test:
        self_test()
        return

    serpapi_key = os.getenv("SERPAPI_KEY", "")
    you_key = os.getenv("YOU_API_KEY", "")

    if not serpapi_key and not you_key:
        print("WARNING: SERPAPI_KEY and YOU_API_KEY are both unset; only source websites will be crawled.")

    source_path = Path(args.output) if args.resume and Path(args.output).exists() else Path(args.input)

    with source_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        input_fields = list(reader.fieldnames or [])

    fieldnames = list(dict.fromkeys(input_fields + EXTRA_FIELDS))

    pending = [
        i for i, row in enumerate(rows)
        if clean(row.get("verification_status")) not in {"enriched", "rejected_parser_artifact"}
    ]

    if args.limit:
        pending = pending[:args.limit]

    print(f"Records loaded: {len(rows)}")
    print(f"Records queued: {len(pending)}")

    for n, idx in enumerate(pending, 1):
        company = clean(rows[idx].get("company"))
        print(f"[{n}/{len(pending)}] {company}", flush=True)

        try:
            rows[idx] = enrich_row(rows[idx], serpapi_key, you_key)
        except Exception as e:
            failed = dict(rows[idx])
            for field in EXTRA_FIELDS:
                failed.setdefault(field, "")
            failed["verified_phone"] = normalize_phone(failed.get("phone", ""))
            failed["verified_email"] = clean(failed.get("email"))
            failed["verified_location"] = clean(
                failed.get("source_market") or failed.get("source_city")
            )
            failed["verification_status"] = f"enrichment_error:{type(e).__name__}"
            failed["missing_fields"] = clean(str(e))
            rows[idx] = failed

        checkpoint(Path(args.output), rows, fieldnames)
        time.sleep(0.05)

    valid_rows = [
        r for r in rows
        if r.get("verification_status") != "rejected_parser_artifact"
    ]

    def count(field):
        return sum(bool(clean(r.get(field))) for r in valid_rows)

    print("\nENRICHMENT COMPLETE")
    print(f"Records:          {len(rows)}")
    print(f"Valid records:    {len(valid_rows)}")
    print(f"Website:          {count('verified_website')}")
    print(f"Phone:            {count('verified_phone')}")
    print(f"Email:            {count('verified_email')}")
    print(f"Service lines:    {count('service_lines')}")
    print(f"Size signal:      {count('size_signal')}")
    if valid_rows:
        print(f"Website coverage: {count('verified_website') / len(valid_rows) * 100:.1f}%")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
