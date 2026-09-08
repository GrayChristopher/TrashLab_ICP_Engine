"""
01_discover.py
TrashLab Texas Hauler Discovery — v4

Purpose
-------
Build a reproducible Texas waste-hauler candidate universe from official public
municipal sources.

Changes from v3
---------------
- Keeps sources that succeeded in Colab.
- Adds College Station and Mesquite official hauler lists.
- Adds current Plano permitted-hauler PDFs.
- Moves known Colab-blocked sources out of the live run rather than repeatedly
  failing on 403 responses.
- Adds PDF parsing with pypdf.
- Preserves provenance and source-market context.
- Uses source-level minimum-count validation to detect broken parsers.

Install
-------
pip install pandas requests beautifulsoup4 lxml pypdf

Run
---
python 01_discover.py

Offline regression tests
------------------------
python 01_discover.py --self-test
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse
from io import BytesIO
import argparse
import re
import sys
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader


ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "data" / "raw_candidates.csv"
FAILURE_LOG = ROOT / "data" / "discovery_failures.csv"

REQUEST_TIMEOUT = 30
REQUEST_DELAY_SECONDS = 0.5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    )
}

PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.\-]?)?"
    r"(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)"
)

EMAIL_RE = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.I,
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
    "franchised haulers - city of college station code",
)


@dataclass(frozen=True)
class Source:
    name: str
    city: str
    url: str
    parser: str
    source_class: str
    minimum_expected: int


# These are the live sources used by the pipeline.
# Fort Worth, Irving, and Richardson are intentionally NOT in this run because
# Colab returned HTTP 403 for them during live validation on 2026-09-08.
# They remain valid browser-accessible sources, but repeatedly calling blocked
# endpoints is not a reliable automation design.
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
        name="City of College Station — Franchised Haulers",
        city="College Station",
        url="https://www.cstx.gov/living-here/trash-and-recycling/commercial-services/",
        parser="college_station",
        source_class="municipal_solid_waste",
        minimum_expected=15,
    ),
    Source(
        name="City of Mesquite — Permitted Recycling Haulers",
        city="Mesquite",
        url="https://www.cityofmesquite.com/2080/Permitted-Recycling-Haulers",
        parser="mesquite",
        source_class="municipal_recycling",
        minimum_expected=4,
    ),
    Source(
        name="City of Plano — General Permitted Haulers",
        city="Plano",
        url="https://content.civicplus.com/api/assets/a1b2b099-3346-482b-8fd8-e009a14f449b?cache=1800",
        parser="plano_pdf",
        source_class="municipal_recycling",
        minimum_expected=14,
    ),
    Source(
        name="City of Plano — C&D Permitted Haulers",
        city="Plano",
        url="https://content.civicplus.com/api/assets/tx-plano/92895e8f-c9f6-4259-88b6-ac913cae7660?cache=1800",
        parser="plano_pdf",
        source_class="municipal_cd_recycling",
        minimum_expected=9,
    ),
]


def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n|:-")


def normalize_company_key(name: str) -> str:
    value = clean_text(name).lower()
    value = LEGAL_SUFFIX_RE.sub(" ", value)
    value = re.sub(r"\bd\/?b\/?a\b", " dba ", value)
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
    if EMAIL_RE.fullmatch(value):
        return False
    if re.fullmatch(r"[\d\W_]+", value):
        return False
    if len(value.split()) > 18:
        return False
    return True


def is_external_company_url(source_url: str, href: str) -> bool:
    if not href:
        return False
    absolute = urljoin(source_url, href)
    source_host = urlparse(source_url).netloc.lower().replace("www.", "")
    target_host = urlparse(absolute).netloc.lower().replace("www.", "")
    return bool(target_host and target_host != source_host)


def make_record(
    source: Source,
    company: str,
    phone: str = "",
    website: str = "",
    email: str = "",
    service_hint: str = "",
) -> dict:
    return {
        "company": clean_text(company),
        "source_market": f"{source.city}, TX",
        "phone": clean_text(phone),
        "website": clean_text(website),
        "email": clean_text(email),
        "service_hint": clean_text(service_hint),
        "source": source.name,
        "source_city": source.city,
        "source_url": source.url,
        "source_class": source.source_class,
        "source_type": "official_municipal_source",
    }


def fetch_bytes(url: str) -> bytes:
    response = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers=HEADERS,
    )
    response.raise_for_status()
    return response.content


def fetch_html(url: str) -> str:
    return fetch_bytes(url).decode("utf-8", errors="replace")


def find_heading(soup: BeautifulSoup, pattern: str):
    regex = re.compile(pattern, re.I)
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if regex.search(clean_text(tag.get_text(" ", strip=True))):
            return tag
    return None


def parse_garland(source: Source, raw: bytes) -> list[dict]:
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    heading = find_heading(soup, r"approved franchise haulers")
    if not heading:
        raise ValueError("Garland hauler heading not found")

    container = heading.parent
    lines = [
        clean_text(x)
        for x in container.get_text("\n", strip=True).splitlines()
        if clean_text(x)
    ]

    records = []
    pending_company = ""

    for line in lines:
        if line.lower() == "contact us":
            break

        phone_match = PHONE_RE.search(line)

        if phone_match:
            phone = phone_match.group(0)
            before = clean_text(line[: phone_match.start()])
            company = before or pending_company

            if (
                is_plausible_company(company)
                and company.lower() != "city of garland sanitation"
            ):
                records.append(make_record(source, company, phone=phone))

            pending_company = ""
        elif is_plausible_company(line):
            pending_company = line

    return records


def parse_weatherford(source: Source, raw: bytes) -> list[dict]:
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    records = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            company = clean_text(cells[0].get_text(" ", strip=True))
            contact = clean_text(cells[1].get_text(" ", strip=True))
            match = PHONE_RE.search(contact)
            phone = match.group(0) if match else ""

            if (
                is_plausible_company(company)
                and "company name" not in company.lower()
            ):
                records.append(make_record(source, company, phone=phone))

    return records


def parse_austin(source: Source, raw: bytes) -> list[dict]:
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    records = []

    for table in soup.find_all("table"):
        headers = [
            clean_text(cell.get_text(" ", strip=True)).lower()
            for cell in table.find_all("th")
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

            website = ""
            link = cells[0].find("a", href=True)
            if link:
                website = urljoin(source.url, link["href"])

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

    return records


def parse_college_station(source: Source, raw: bytes) -> list[dict]:
    """
    The city page exposes private hauler names as outbound links followed by
    phone numbers and, for several records, email addresses.
    """
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    heading = find_heading(soup, r"commercial services")
    if not heading:
        raise ValueError("College Station Commercial Services heading not found")

    records = []
    seen = set()

    # Start at the sentence introducing private haulers.
    start_node = None
    for node in soup.find_all(string=re.compile(r"Services may be acquired through", re.I)):
        start_node = node.parent
        break

    if start_node is None:
        raise ValueError("College Station private-hauler section not found")

    for link in start_node.find_all_next("a", href=True):
        label = clean_text(link.get_text(" ", strip=True))

        if label.lower().startswith("prohibited waste"):
            break

        if not is_plausible_company(label):
            continue

        href = urljoin(source.url, link["href"])

        # Only outbound company links belong in this section.
        if not is_external_company_url(source.url, href):
            continue

        key = normalize_company_key(label)
        if key in seen:
            continue
        seen.add(key)

        # Inspect nearby text after the link for phone/email.
        context_parts = [label]
        node = link
        for _ in range(4):
            node = node.next_element
            if node is None:
                break
            if isinstance(node, str):
                context_parts.append(clean_text(node))

        context = " ".join(x for x in context_parts if x)
        phone_match = PHONE_RE.search(context)
        email_match = EMAIL_RE.search(context)

        records.append(
            make_record(
                source,
                label,
                phone=phone_match.group(0) if phone_match else "",
                website=href,
                email=email_match.group(0) if email_match else "",
                service_hint="commercial recycling / hauling",
            )
        )

    # One current listing (Team 3 Rental Services) is plain text, not linked.
    # We deliberately do NOT hardcode it here; later enrichment can recover
    # unlinked candidates from secondary discovery sources.

    return records


def parse_mesquite(source: Source, raw: bytes) -> list[dict]:
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    heading = find_heading(soup, r"permitted recycling haulers")
    if not heading:
        raise ValueError("Mesquite permitted-hauler heading not found")

    records = []
    seen = set()

    # The page places each company name/address/phone/site in the main content.
    main = heading.parent
    text = main.get_text("\n", strip=True)
    lines = [clean_text(x) for x in text.splitlines() if clean_text(x)]

    for idx, line in enumerate(lines):
        # Candidate company line is followed within a few lines by a phone.
        if not is_plausible_company(line):
            continue

        window = " ".join(lines[idx: idx + 5])
        phone_match = PHONE_RE.search(window)
        if not phone_match:
            continue

        low = line.lower()
        if any(
            x in low
            for x in (
                "city of mesquite",
                "commercial recycling",
                "request additional information",
                "permitted recycling haulers",
            )
        ):
            continue

        key = normalize_company_key(line)
        if key in seen:
            continue

        website = ""
        for a in main.find_all("a", href=True):
            label = clean_text(a.get_text(" ", strip=True))
            if normalize_company_key(label) == key:
                website = urljoin(source.url, a["href"])
                break

        seen.add(key)
        records.append(
            make_record(
                source,
                line,
                phone=phone_match.group(0),
                website=website,
                service_hint="commercial recycling",
            )
        )

    return records


def extract_pdf_text(raw: bytes) -> str:
    reader = PdfReader(BytesIO(raw))
    text = []
    for page in reader.pages:
        text.append(page.extract_text() or "")
    return "\n".join(text)


def parse_plano_pdf(source: Source, raw: bytes) -> list[dict]:
    """
    Plano's current permitted-hauler lists are PDFs. The layout repeats:
       Company
       Address
       Phone

    We use phone tokens as anchors, then infer the company from the preceding
    text block. This is deterministic and avoids OCR.
    """
    text = extract_pdf_text(raw)
    text = text.replace("\xa0", " ")
    lines = [clean_text(x) for x in text.splitlines() if clean_text(x)]

    records = []
    used_keys = set()

    service_hint = (
        "construction & demolition recycling"
        if "C&D" in source.name
        else "commercial recycling"
    )

    for idx, line in enumerate(lines):
        phone_match = PHONE_RE.search(line)
        if not phone_match:
            continue

        # Look backward for the nearest plausible company line.
        company = ""
        for back in range(1, 6):
            if idx - back < 0:
                break
            candidate = clean_text(lines[idx - back])

            # Skip address-like lines.
            if re.search(r"\bTX\s+\d{5}\b", candidate, re.I):
                continue
            if re.match(r"^\d+\s+", candidate):
                continue
            if candidate.lower() in {"company name", "address", "phone"}:
                continue
            if "commercial waste and recycling" in candidate.lower():
                continue
            if "permitted haulers" in candidate.lower():
                continue

            if is_plausible_company(candidate):
                company = candidate
                break

        if not company:
            continue

        key = normalize_company_key(company)
        if key in used_keys:
            continue

        used_keys.add(key)
        records.append(
            make_record(
                source,
                company,
                phone=phone_match.group(0),
                service_hint=service_hint,
            )
        )

    return records


PARSERS = {
    "garland": parse_garland,
    "weatherford": parse_weatherford,
    "austin": parse_austin,
    "college_station": parse_college_station,
    "mesquite": parse_mesquite,
    "plano_pdf": parse_plano_pdf,
}


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
            vals = sorted({
                clean_text(v)
                for v in group[column]
                if clean_text(v)
            })
            return " | ".join(vals)

        output.append({
            "company": first["company"],
            "source_market": joined("source_market"),
            "phone": joined("phone"),
            "website": joined("website"),
            "email": joined("email"),
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
            "Source markup or PDF layout may have changed."
        )


def self_test() -> None:
    test_source = Source(
        name="Test",
        city="Test",
        url="https://example.com/page",
        parser="",
        source_class="test",
        minimum_expected=1,
    )

    garland_html = b"""
    <main><h1>List of Approved Franchise Haulers</h1>
    <div>Alpha Waste | 972-111-2222</div>
    <div>Beta Dumpsters</div><div>| 214-333-4444</div>
    <div>City of Garland Sanitation | 972-205-3500</div>
    <h3>Contact Us</h3></main>
    """
    got = parse_garland(test_source, garland_html)
    assert {r["company"] for r in got} == {"Alpha Waste", "Beta Dumpsters"}

    weatherford_html = b"""
    <table>
      <tr><th>Company Name</th><th>Contact Information</th></tr>
      <tr><td>Alpha Waste</td><td>972-111-2222</td></tr>
    </table>
    """
    got = parse_weatherford(test_source, weatherford_html)
    assert len(got) == 1
    assert got[0]["company"] == "Alpha Waste"

    austin_html = b"""
    <h1>Universal Recycling Ordinance Licensed Haulers</h1>
    <table>
      <tr><th>Hauler</th><th>Organic Diversion</th><th>Recycling</th><th>Landfill Trash</th></tr>
      <tr><td><a href="https://alpha.example">Alpha Waste</a></td><td></td><td>X</td><td>X</td></tr>
    </table>
    """
    got = parse_austin(test_source, austin_html)
    assert len(got) == 1
    assert got[0]["service_hint"] == "recycling, landfill trash"

    college_html = b"""
    <h1>Commercial Services</h1>
    <p>Services may be acquired through any of the following private haulers:</p>
    <p><a href="https://alpha.example">Alpha Waste</a> 979.777.6795 info@alpha.example</p>
    <p><a href="/prohibited">Prohibited Waste</a></p>
    """
    got = parse_college_station(test_source, college_html)
    assert len(got) == 1
    assert got[0]["company"] == "Alpha Waste"

    print("SELF-TEST PASSED")


def main() -> None:
    argp = argparse.ArgumentParser()
    argp.add_argument(
        "--self-test",
        action="store_true",
        help="run parser regression tests without internet",
    )
    args = argp.parse_args()

    if args.self_test:
        self_test()
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    all_records = []
    failures = []

    print(f"Discovering from {len(SOURCES)} live public sources...\n")

    for index, source in enumerate(SOURCES, start=1):
        print(f"[{index}/{len(SOURCES)}] {source.name}")

        try:
            raw = fetch_bytes(source.url)
            parser_fn = PARSERS[source.parser]
            records = parser_fn(source, raw)
            validate_source(source, records)

            unique_count = len({
                normalize_company_key(r["company"]) for r in records
            })
            print(f"    OK — {unique_count} unique candidates")
            all_records.extend(records)

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
            "No candidates extracted. Nothing was written."
        )

    columns = [
        "company",
        "source_market",
        "phone",
        "website",
        "email",
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
    print(f"With website from source:   {(final_df['website'].str.len() > 0).sum()}")
    print(f"With email from source:     {(final_df['email'].str.len() > 0).sum()}")
    print(f"With phone from source:     {(final_df['phone'].str.len() > 0).sum()}")

    if failures:
        print(
            f"\nWARNING: {len(failures)} source(s) failed validation. "
            f"See {FAILURE_LOG}"
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
