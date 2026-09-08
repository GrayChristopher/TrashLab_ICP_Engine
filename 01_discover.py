"""
01_discover.py
TrashLab Texas Hauler Discovery — v3

Purpose
-------
Build a reproducible candidate universe of Texas waste haulers from official
municipal sources. Discovery is intentionally separated from enrichment and ICP
scoring.

Output
------
data/raw_candidates.csv
data/discovery_failures.csv   (only if a source fails)

Install
-------
pip install pandas requests beautifulsoup4 lxml

Run
---
python 01_discover.py

Optional parser checks (no internet required)
---------------------------------------------
python 01_discover.py --self-test
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin
import argparse
import re
import sys
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup


# =============================================================================
# CONFIG
# =============================================================================

ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "data" / "raw_candidates.csv"
FAILURE_LOG = ROOT / "data" / "discovery_failures.csv"

REQUEST_TIMEOUT = 30
REQUEST_DELAY_SECONDS = 0.5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; TrashLabTakeHomeResearch/1.0; "
        "+public-data-research)"
    )
}

PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.\-]?)?"
    r"(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)"
)

LEGAL_SUFFIX_RE = re.compile(
    r"\b(l\.?l\.?c\.?|inc\.?|incorporated|corp\.?|corporation|ltd\.?|lp|llp)\b",
    re.I,
)

JUNK_EXACT = {
    "company",
    "company name",
    "hauler",
    "haulers",
    "contact information",
    "phone",
    "phone number",
    "view list",
    "current gop haulers",
    "city of garland sanitation",
}

JUNK_CONTAINS = (
    "contact us",
    "staff directory",
    "customer care",
    "additional questions",
    "become a",
    "apply for",
    "download",
    "city hall",
    "mailing address",
)


@dataclass(frozen=True)
class Source:
    name: str
    city: str
    url: str
    parser: str
    source_class: str
    minimum_expected: int


# Every enabled source below was verified against its live public page on
# 2026-09-08. minimum_expected is a guardrail: if markup changes and extraction
# suddenly collapses, the script logs the source instead of silently trusting it.
SOURCES = [
    Source(
        name="City of Garland — Approved Franchise Haulers",
        city="Garland",
        url="https://www.garlandtx.gov/3771/Approved-Hauler-List",
        parser="garland",
        source_class="municipal_solid_waste",
        minimum_expected=40,
    ),
    Source(
        name="City of Fort Worth — Current GOP Haulers",
        city="Fort Worth",
        url="https://www.fortworthtexas.gov/departments/environmental-services/solidwaste/commercial",
        parser="fort_worth",
        source_class="municipal_solid_waste",
        minimum_expected=28,
    ),
    Source(
        name="City of Weatherford — Approved Commercial Waste Haulers",
        city="Weatherford",
        url="https://weatherfordtx.gov/3545/Approved-Commercial-Waste-Haulers",
        parser="weatherford",
        source_class="municipal_solid_waste",
        minimum_expected=10,
    ),
    Source(
        name="City of Austin — URO Licensed Haulers",
        city="Austin",
        url="https://www.austintexas.gov/resource-recovery/universal-recycling-ordinance-uro-licensed-haulers",
        parser="austin",
        source_class="municipal_solid_waste",
        minimum_expected=10,
    ),
    Source(
        name="City of Irving — Franchised Commercial Haulers",
        city="Irving",
        url="https://irvingtx.gov/commercial-trash",
        parser="irving",
        source_class="municipal_solid_waste",
        minimum_expected=9,
    ),
    Source(
        name="City of Richardson — Permitted Haulers",
        city="Richardson",
        url="https://www.cor.net/departments/public-services/trash-recycling/solid-waste-hauler-permits/list-of-haulers",
        parser="richardson",
        source_class="municipal_solid_waste",
        minimum_expected=10,
    ),
]


# =============================================================================
# NORMALIZATION
# =============================================================================

def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n|:-")


def normalize_company_key(name: str) -> str:
    value = clean_text(name).lower()
    value = LEGAL_SUFFIX_RE.sub(" ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", clean_text(phone))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else clean_text(phone)


def is_plausible_company(name: str) -> bool:
    value = clean_text(name)
    low = value.lower()

    if not value or len(value) < 3 or len(value) > 180:
        return False
    if low in JUNK_EXACT:
        return False
    if any(junk in low for junk in JUNK_CONTAINS):
        return False
    if PHONE_RE.fullmatch(value):
        return False
    if re.fullmatch(r"[\d\W_]+", value):
        return False
    if len(value.split()) > 18:
        return False
    return True


def make_record(
    source: Source,
    company: str,
    phone: str = "",
    website: str = "",
    service_hint: str = "",
) -> dict:
    return {
        "company": clean_text(company),
        # This is SOURCE MARKET, not claimed HQ.
        "source_market": f"{source.city}, TX",
        "phone": clean_text(phone),
        "website": clean_text(website),
        "service_hint": clean_text(service_hint),
        "source": source.name,
        "source_city": source.city,
        "source_url": source.url,
        "source_class": source.source_class,
        "source_type": "official_municipal_source",
    }


# =============================================================================
# HTTP
# =============================================================================

def fetch(url: str) -> str:
    response = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers=HEADERS,
    )
    response.raise_for_status()
    return response.text


# =============================================================================
# PARSER UTILITIES
# =============================================================================

def find_heading(soup: BeautifulSoup, pattern: str):
    regex = re.compile(pattern, re.I)
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5"]):
        if regex.search(clean_text(tag.get_text(" ", strip=True))):
            return tag
    return None


def external_href(source: Source, href: str) -> str:
    href = clean_text(href)
    if not href or href.startswith(("mailto:", "tel:", "#", "javascript:")):
        return ""
    return urljoin(source.url, href)


# =============================================================================
# SOURCE PARSERS
# =============================================================================

def parse_garland(source: Source, html: str) -> list[dict]:
    """
    Garland has two layouts at once:
      Company | phone
    and, for several entries:
      Company
      | phone

    Parsing the visible text sequentially handles both without hardcoding names.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = find_heading(soup, r"approved franchise haulers")
    if not heading:
        raise ValueError("Garland hauler heading not found")

    # Limit text to the main content when possible.
    container = heading.parent
    text = container.get_text("\n", strip=True)
    lines = [clean_text(x) for x in text.splitlines() if clean_text(x)]

    records = []
    pending_company = ""

    for line in lines:
        # Stop before footer/contact data contaminates results.
        if line.lower() == "contact us":
            break

        phone_match = PHONE_RE.search(line)

        if phone_match:
            phone = phone_match.group(0)
            before = clean_text(line[: phone_match.start()])

            if before:
                candidate = before
            else:
                candidate = pending_company

            candidate = clean_text(candidate)

            if (
                is_plausible_company(candidate)
                and candidate.lower() != "city of garland sanitation"
            ):
                records.append(make_record(source, candidate, phone))

            pending_company = ""
            continue

        # A standalone company line may be followed by a standalone phone line.
        if is_plausible_company(line):
            pending_company = line

    return records


def parse_fort_worth(source: Source, html: str) -> list[dict]:
    """
    Fort Worth exposes the GOP list as linked list items.
    Capture both company name and direct company website when present.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = find_heading(soup, r"current gop haulers")
    if not heading:
        raise ValueError("Fort Worth current GOP heading not found")

    records = []

    # Walk forward until the Recycling section.
    for node in heading.find_all_next():
        if node.name in {"h2", "h3"}:
            text = clean_text(node.get_text(" ", strip=True)).lower()
            if text == "recycling":
                break

        if node.name != "li":
            continue

        company = clean_text(node.get_text(" ", strip=True))
        if not is_plausible_company(company):
            continue

        link = node.find("a", href=True)
        website = external_href(source, link["href"]) if link else ""

        records.append(make_record(source, company, website=website))

    return records


def parse_weatherford(source: Source, html: str) -> list[dict]:
    """
    Weatherford's official page is a simple two-column table.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = find_heading(soup, r"approved commercial waste haulers")
    if not heading:
        raise ValueError("Weatherford hauler heading not found")

    records = []

    for table in soup.find_all("table"):
        headers = [
            clean_text(th.get_text(" ", strip=True)).lower()
            for th in table.find_all("th")
        ]
        if not any("company" in h for h in headers):
            continue

        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            company = clean_text(cells[0].get_text(" ", strip=True))
            contact = clean_text(cells[1].get_text(" ", strip=True))
            phone_match = PHONE_RE.search(contact)
            phone = phone_match.group(0) if phone_match else ""

            if is_plausible_company(company) and "company name" not in company.lower():
                records.append(make_record(source, company, phone=phone))

    # CivicPlus may render the same logical table as plain visible text.
    if not records:
        text = soup.get_text("\n", strip=True)
        lines = [clean_text(x) for x in text.splitlines() if clean_text(x)]
        pending_company = ""

        for line in lines:
            if line.lower().startswith("contact us"):
                break
            phone_match = PHONE_RE.search(line)
            if phone_match:
                before = clean_text(line[:phone_match.start()])
                company = before or pending_company
                if is_plausible_company(company):
                    records.append(
                        make_record(source, company, phone_match.group(0))
                    )
                pending_company = ""
            elif is_plausible_company(line):
                pending_company = line

    return records


def parse_austin(source: Source, html: str) -> list[dict]:
    """
    Austin's URO table supplies:
      - company
      - direct website
      - organics/recycling/landfill service flags
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = find_heading(soup, r"universal recycling ordinance licensed haulers")
    if not heading:
        raise ValueError("Austin URO heading not found")

    records = []

    for table in soup.find_all("table"):
        header_cells = table.find_all("th")
        headers = [
            clean_text(cell.get_text(" ", strip=True)).lower()
            for cell in header_cells
        ]

        if not headers or not any("hauler" in h for h in headers):
            continue

        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            company = clean_text(cells[0].get_text(" ", strip=True))
            if not is_plausible_company(company) or company.lower() == "hauler":
                continue

            link = cells[0].find("a", href=True)
            website = external_href(source, link["href"]) if link else ""

            services = []
            for idx, cell in enumerate(cells[1:], start=1):
                if idx >= len(headers):
                    continue
                value = clean_text(cell.get_text(" ", strip=True)).lower()
                if value in {"x", "yes", "true", "1", "✓"}:
                    header = headers[idx]
                    if "organic" in header:
                        services.append("organics")
                    elif "recycl" in header:
                        services.append("recycling")
                    elif "landfill" in header or "trash" in header:
                        services.append("landfill trash")

            records.append(
                make_record(
                    source,
                    company,
                    website=website,
                    service_hint=", ".join(services),
                )
            )

    # Fallback for Drupal table markup changes: capture outbound company links
    # between the table header and the explanatory text after the list.
    if not records:
        start = heading
        for link in start.find_all_next("a", href=True):
            text = clean_text(link.get_text(" ", strip=True))
            if text.lower().startswith("city of austin"):
                break
            if is_plausible_company(text):
                href = external_href(source, link["href"])
                # Avoid navigation links; company links in this section are external.
                if href and "austintexas.gov" not in href.lower():
                    records.append(make_record(source, text, website=href))

    return records


def parse_irving(source: Source, html: str) -> list[dict]:
    """
    Irving lists franchised commercial haulers as numbered list items containing
    company link + phone.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = find_heading(soup, r"city franchised commercial haulers")
    if not heading:
        raise ValueError("Irving franchised-hauler heading not found")

    records = []

    for node in heading.find_all_next():
        if node.name in {"h2", "h3", "h4", "h5"} and node is not heading:
            break
        if node.name != "li":
            continue

        text = clean_text(node.get_text(" ", strip=True))
        phone_match = PHONE_RE.search(text)
        link = node.find("a", href=True)

        if not link:
            continue

        company = clean_text(link.get_text(" ", strip=True))
        phone = phone_match.group(0) if phone_match else ""
        website = external_href(source, link["href"])

        if is_plausible_company(company):
            records.append(
                make_record(source, company, phone=phone, website=website)
            )

    return records


def parse_richardson(source: Source, html: str) -> list[dict]:
    """
    Richardson's page is rendered as alternating company names and phone
    numbers, with two categories:
      - Commercial/Multifamily Recycling
      - Construction & Demolition

    We parse the sequence around phone tokens rather than relying on brittle
    table markup.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = find_heading(soup, r"permitted haulers")
    if not heading:
        raise ValueError("Richardson permitted-hauler heading not found")

    full_text = clean_text(heading.parent.get_text(" ", strip=True))

    # Trim to the useful part of the page.
    start_marker = "Permitted Commercial/Multifamily"
    start_idx = full_text.find(start_marker)
    if start_idx == -1:
        raise ValueError("Richardson commercial hauler section not found")
    section = full_text[start_idx:]

    # Stop before the city address/footer.
    for stop in ("2360 Campbell Creek", "Mailing Address"):
        idx = section.find(stop)
        if idx != -1:
            section = section[:idx]

    # Identify service category changes embedded in the sequence.
    category_re = re.compile(
        r"(Permitted Commercial/Multifamily\s+Recycling Haulers:|"
        r"Permitted Construction\s*&\s*Demolition Haulers:)",
        re.I,
    )

    # Split into text chunks and phone tokens while preserving order.
    parts = PHONE_RE.split(section)
    phones = PHONE_RE.findall(section)

    records = []
    current_service = ""

    for idx, phone in enumerate(phones):
        chunk = clean_text(parts[idx])

        category_matches = list(category_re.finditer(chunk))
        if category_matches:
            last = category_matches[-1]
            label = last.group(0).lower()
            current_service = (
                "commercial/multifamily recycling"
                if "recycling" in label
                else "construction & demolition"
            )
            chunk = clean_text(chunk[last.end():])

        # If another category label remains anywhere, strip it.
        chunk = category_re.sub(" ", chunk)
        company = clean_text(chunk)

        if is_plausible_company(company):
            records.append(
                make_record(
                    source,
                    company,
                    phone=phone,
                    service_hint=current_service,
                )
            )

    return records


PARSERS = {
    "garland": parse_garland,
    "fort_worth": parse_fort_worth,
    "weatherford": parse_weatherford,
    "austin": parse_austin,
    "irving": parse_irving,
    "richardson": parse_richardson,
}


# =============================================================================
# DEDUPLICATION + VALIDATION
# =============================================================================

def deduplicate(records: list[dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records).fillna("")
    df["company_key"] = df["company"].map(normalize_company_key)
    df["phone_normalized"] = df["phone"].map(normalize_phone)
    df = df[df["company_key"] != ""].copy()

    output = []

    for company_key, group in df.groupby("company_key", sort=False):
        first = group.iloc[0]

        def joined(column: str) -> str:
            values = sorted({
                clean_text(v)
                for v in group[column]
                if clean_text(v)
            })
            return " | ".join(values)

        output.append({
            "company": first["company"],
            "source_market": joined("source_market"),
            "phone": joined("phone"),
            "website": joined("website"),
            "service_hint": joined("service_hint"),
            "source_city": joined("source_city"),
            "source": joined("source"),
            "source_url": joined("source_url"),
            "source_class": joined("source_class"),
            "source_type": "official_municipal_source",
            "source_count": group["source"].nunique(),
            "company_key": company_key,
            "status": "candidate",
        })

    return (
        pd.DataFrame(output)
        .sort_values(["source_count", "company"], ascending=[False, True])
        .reset_index(drop=True)
    )


def validate_source(source: Source, records: list[dict]) -> None:
    unique_names = {
        normalize_company_key(r["company"])
        for r in records
        if r.get("company")
    }

    if len(unique_names) < source.minimum_expected:
        raise ValueError(
            f"Suspiciously low extraction: {len(unique_names)} unique companies; "
            f"expected at least {source.minimum_expected}. "
            "Source markup may have changed."
        )


# =============================================================================
# OFFLINE SELF-TESTS
# =============================================================================

def self_test() -> None:
    """
    Lightweight parser regression checks using simplified fixtures modeled on
    the live page structures. This does NOT prove the internet sources are
    reachable; the live run's minimum-count guardrails handle that.
    """
    test_source = Source(
        name="Test",
        city="Test",
        url="https://example.com/page",
        parser="",
        source_class="test",
        minimum_expected=1,
    )

    # Garland split-line + same-line behavior.
    garland_html = """
    <main>
      <h1>List of Approved Franchise Haulers</h1>
      <div>Alpha Waste | 972-111-2222</div>
      <div>Beta Dumpsters</div>
      <div>| 214-333-4444</div>
      <div>City of Garland Sanitation | 972-205-3500</div>
      <h3>Contact Us</h3>
    </main>
    """
    got = parse_garland(test_source, garland_html)
    assert {r["company"] for r in got} == {"Alpha Waste", "Beta Dumpsters"}

    # Fort Worth linked list.
    fw_html = """
    <main>
      <h4>Current GOP haulers</h4>
      <ul>
        <li><a href="https://alpha.example">Alpha Waste, LLC</a></li>
        <li>Unlinked Hauler</li>
      </ul>
      <h2>Recycling</h2>
    </main>
    """
    got = parse_fort_worth(test_source, fw_html)
    assert len(got) == 2
    assert got[0]["website"] == "https://alpha.example"

    # Austin service flags.
    austin_html = """
    <main>
      <h1>Universal Recycling Ordinance Licensed Haulers</h1>
      <table>
        <tr><th>Hauler</th><th>Organic Diversion</th><th>Recycling</th><th>Landfill Trash</th></tr>
        <tr><td><a href="https://alpha.example">Alpha Waste</a></td><td></td><td>X</td><td>X</td></tr>
      </table>
    </main>
    """
    got = parse_austin(test_source, austin_html)
    assert len(got) == 1
    assert got[0]["service_hint"] == "recycling, landfill trash"

    # Irving company + phone + website.
    irving_html = """
    <main>
      <h5>City Franchised Commercial Haulers</h5>
      <ol>
        <li><a href="https://alpha.example">Alpha Waste</a> | (214) 555-1212</li>
      </ol>
      <h5>E-NEWSLETTER SIGN UP</h5>
    </main>
    """
    got = parse_irving(test_source, irving_html)
    assert len(got) == 1
    assert normalize_phone(got[0]["phone"]) == "2145551212"

    print("SELF-TEST PASSED")


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run parser regression tests without internet",
    )
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    all_records = []
    failures = []

    print(f"Discovering from {len(SOURCES)} verified public sources...\n")

    for index, source in enumerate(SOURCES, start=1):
        print(f"[{index}/{len(SOURCES)}] {source.name}")

        try:
            html = fetch(source.url)
            records = PARSERS[source.parser](source, html)
            validate_source(source, records)

            all_records.extend(records)
            unique_count = len({
                normalize_company_key(r["company"]) for r in records
            })
            print(f"    OK — {unique_count} unique candidates")

        except Exception as exc:
            print(f"    FAILED — {type(exc).__name__}: {exc}")
            failures.append({
                "source": source.name,
                "source_url": source.url,
                "error_type": type(exc).__name__,
                "error": str(exc),
            })

        time.sleep(REQUEST_DELAY_SECONDS)

    final_df = deduplicate(all_records)

    if final_df.empty:
        raise SystemExit(
            "No candidates extracted. Nothing was written to raw_candidates.csv."
        )

    # Deterministic column order.
    columns = [
        "company",
        "source_market",
        "phone",
        "website",
        "service_hint",
        "source_city",
        "source",
        "source_url",
        "source_class",
        "source_type",
        "source_count",
        "company_key",
        "status",
    ]
    final_df = final_df[columns]

    final_df.to_csv(OUTPUT_PATH, index=False)

    if failures:
        pd.DataFrame(failures).to_csv(FAILURE_LOG, index=False)
    elif FAILURE_LOG.exists():
        FAILURE_LOG.unlink()

    print("\nDISCOVERY COMPLETE")
    print(f"Unique candidate companies: {len(final_df)}")
    print(f"Output: {OUTPUT_PATH}")

    with_websites = (final_df["website"].str.len() > 0).sum()
    with_phones = (final_df["phone"].str.len() > 0).sum()

    print(f"With website from source:   {with_websites}")
    print(f"With phone from source:     {with_phones}")

    if failures:
        print(
            f"\nWARNING: {len(failures)} source(s) failed validation. "
            f"See {FAILURE_LOG}"
        )
        # A nonzero exit code is useful in GitHub Actions / automation,
        # while still preserving valid output from sources that succeeded.
        sys.exit(2)


if __name__ == "__main__":
    main()
