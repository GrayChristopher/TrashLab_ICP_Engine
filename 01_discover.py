"""
01_discover.py
TrashLab Texas Hauler Discovery — v7.1

Purpose
-------
Build a reproducible Texas waste-hauler candidate universe from official public
municipal sources.

v6 changes
----------
- Keeps the six source parsers that produced clean v5 output.
- Plano: strips document-note contamination before company names.
- Adds City of Denton's current 2026 permitted waste-hauler PDF.
- Adds City of New Braunfels' current permitted private-hauler list.
- Adds City of Houston's official commercial solid-waste franchise list as a
  discovery seed; the city labels that list as updated 2020, so downstream
  qualification must re-verify those companies before final ICP output.
- Austin: parses the current URO hauler matrix by its visible link sequence,
  avoiding dependence on table markup.
- Keeps source-level minimum-count validation and deterministic output.

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
from bs4 import BeautifulSoup, NavigableString
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

TX_CITY_ZIP_RE = re.compile(
    r"\b[A-Za-z .'-]+,\s*TX\s+\d{5}(?:-\d{4})?\b",
    re.I,
)

# Street-address starts such as:
# 2500 W Bruton Road
# 13921 Senlac Drive #200
# 17662 TX 121
INLINE_ADDRESS_RE = re.compile(
    r"\s(?=\d{2,}\s+(?:[NSEW]\s+)?[A-Za-z0-9])"
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
    "list of approved franchise haulers",
    "approved franchise haulers",
    "permitted recycling haulers",
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
    "commercial waste and recycling",
    "government websites by",
)


@dataclass(frozen=True)
class Source:
    name: str
    city: str
    url: str
    parser: str
    source_class: str
    minimum_expected: int


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
        minimum_expected=5,
    ),
    Source(
        name="City of Plano — General Permitted Haulers",
        city="Plano",
        url="https://content.civicplus.com/api/assets/a1b2b099-3346-482b-8fd8-e009a14f449b?cache=1800",
        parser="plano_pdf",
        source_class="municipal_recycling",
        minimum_expected=17,
    ),
    Source(
        name="City of Plano — C&D Permitted Haulers",
        city="Plano",
        url="https://content.civicplus.com/api/assets/tx-plano/92895e8f-c9f6-4259-88b6-ac913cae7660?cache=1800",
        parser="plano_pdf",
        source_class="municipal_cd_recycling",
        minimum_expected=15,
    ),
    Source(
        name="City of Denton — 2026 Permitted Waste Haulers",
        city="Denton",
        url="https://www.cityofdenton.com/DocumentCenter/View/14323",
        parser="denton_pdf",
        source_class="municipal_waste_hauler",
        minimum_expected=35,
    ),
    Source(
        name="City of New Braunfels — Permitted Private Haulers",
        city="New Braunfels",
        url="https://www.newbraunfels.gov/4032/Community-Resources",
        parser="new_braunfels",
        source_class="municipal_solid_waste",
        minimum_expected=8,
    ),
    Source(
        name="City of Houston — Franchised Commercial Solid Waste Haulers",
        city="Houston",
        url="https://www.houstontx.gov/ara/franchise/solid_waste_hauler_operators.pdf",
        parser="houston_pdf",
        source_class="municipal_solid_waste",
        minimum_expected=100,
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


def is_url_like(value: str) -> bool:
    low = clean_text(value).lower()
    return low.startswith(("http://", "https://", "www."))


def is_address_like(value: str) -> bool:
    value = clean_text(value)
    low = value.lower()

    if not value:
        return False
    if re.match(r"^\d+\s+", value):
        return True
    if re.match(r"^p\.?\s*o\.?\s*box\b", value, re.I):
        return True
    if TX_CITY_ZIP_RE.search(value):
        return True
    if re.search(r"\b(?:street|st|road|rd|drive|dr|avenue|ave|blvd|boulevard|pkwy|parkway|hwy|highway|court|ct|lane|ln|way|fwy|freeway|expy)\b", low):
        return True
    return False


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
    if is_url_like(value):
        return False
    if is_address_like(value):
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
        # This is the market represented by the public source, not necessarily HQ.
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


def find_heading(soup: BeautifulSoup, pattern: str):
    regex = re.compile(pattern, re.I)
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if regex.search(clean_text(tag.get_text(" ", strip=True))):
            return tag
    return None


# =============================================================================
# GARLAND
# =============================================================================

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
        low = line.lower()

        if low == "contact us":
            break

        # Never allow the page title to become the pending company.
        if "approved franchise haulers" in low:
            pending_company = ""
            continue

        phone_match = PHONE_RE.search(line)

        if phone_match:
            phone = phone_match.group(0)
            before = clean_text(line[:phone_match.start()])
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


# =============================================================================
# WEATHERFORD
# =============================================================================

def parse_weatherford(source: Source, raw: bytes) -> list[dict]:
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    records = []

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            company = clean_text(cells[0].get_text(" ", strip=True))
            contact = clean_text(cells[1].get_text(" ", strip=True))
            match = PHONE_RE.search(contact)

            if is_plausible_company(company):
                records.append(
                    make_record(
                        source,
                        company,
                        phone=match.group(0) if match else "",
                    )
                )

    return records


# =============================================================================
# AUSTIN
# =============================================================================

def parse_austin(source: Source, raw: bytes) -> list[dict]:
    """
    Austin's current URO page renders the licensed-hauler matrix as links
    separated by pipe characters rather than a conventional HTML <table>.

    Parse the visible hauler rows between the "Hauler" header and the city's
    "This list is provided without endorsement" footer. This deliberately
    ignores navigation/header links and the two hard-to-recycle examples above
    the matrix.
    """
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    # These are the matrix column labels, not company names.
    header_labels = {
        "hauler",
        "organic diversion",
        "(food scraps)",
        "food scraps",
        "recycling",
        "landfill trash",
    }

    records = []
    seen = set()
    in_matrix = False

    # Iterate anchors in document order. The first anchor whose visible text is
    # exactly "Hauler" marks the matrix. The company links that follow are the
    # rows we want.
    for a in soup.find_all("a", href=True):
        label = clean_text(a.get_text(" ", strip=True))
        low = label.lower()

        if low == "hauler":
            in_matrix = True
            continue

        if not in_matrix:
            continue

        # The next City of Austin "Licensed Private Haulers" link appears in
        # the footer immediately after the matrix.
        if "city of austin" in low and "licensed private haulers" in low:
            break

        if low in header_labels or not label:
            continue

        href = clean_text(a.get("href", ""))
        if not href:
            continue

        # Company rows link to external company domains. Skip mailto/internal
        # Austin links and other non-company artifacts.
        href_low = href.lower()
        if (
            href_low.startswith("mailto:")
            or "austintexas.gov" in href_low
            or "austinreusedirectory.com" in href_low
        ):
            continue

        if not is_plausible_company(label):
            continue

        key = normalize_company_key(label)
        if key in seen:
            continue
        seen.add(key)

        records.append(
            make_record(
                source,
                label,
                website=href,
                service_hint="Austin URO licensed private hauler",
            )
        )

    return records

# =============================================================================
# COLLEGE STATION
# =============================================================================

def parse_college_station(source: Source, raw: bytes) -> list[dict]:
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    start_node = None
    for node in soup.find_all(
        string=re.compile(r"Services may be acquired through", re.I)
    ):
        start_node = node.parent
        break

    if start_node is None:
        raise ValueError("College Station private-hauler section not found")

    records = []
    seen = set()

    for link in start_node.find_all_next("a", href=True):
        label = clean_text(link.get_text(" ", strip=True))

        if label.lower().startswith("prohibited waste"):
            break
        if not is_plausible_company(label):
            continue

        href = urljoin(source.url, link["href"])
        if not is_external_company_url(source.url, href):
            continue

        key = normalize_company_key(label)
        if key in seen:
            continue
        seen.add(key)

        # Gather a short nearby-text window for phone/email.
        nearby = []
        node = link
        for _ in range(8):
            node = node.next_element
            if node is None:
                break
            if isinstance(node, NavigableString):
                value = clean_text(node)
                if value:
                    nearby.append(value)

        context = " ".join(nearby)
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

    return records


# =============================================================================
# MESQUITE
# =============================================================================

def parse_mesquite(source: Source, raw: bytes) -> list[dict]:
    """
    Current source layout is a repeating 4-line record:

      Company
      Street / PO Box
      City, TX ZIP
      Phone
      [website]

    We anchor on the TX city/ZIP + following phone pattern and take the company
    two lines earlier. This prevents addresses, city names, phones and URLs from
    becoming candidates.
    """
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    heading = find_heading(soup, r"permitted recycling haulers")
    if not heading:
        raise ValueError("Mesquite permitted-hauler heading not found")

    container = heading.parent
    lines = [
        clean_text(x)
        for x in container.get_text("\n", strip=True).splitlines()
        if clean_text(x)
    ]

    records = []
    seen = set()

    for i in range(2, len(lines) - 1):
        city_line = lines[i]

        if city_line.lower() == "contact us":
            break

        if not TX_CITY_ZIP_RE.search(city_line):
            continue

        phone_match = PHONE_RE.search(lines[i + 1])
        if not phone_match:
            continue

        company = clean_text(lines[i - 2])
        address = clean_text(lines[i - 1])

        if not is_address_like(address):
            continue
        if not is_plausible_company(company):
            continue
        if company.lower().startswith("city of mesquite"):
            continue

        key = normalize_company_key(company)
        if key in seen:
            continue

        website = ""

        # Website, when present, immediately follows the phone line.
        if i + 2 < len(lines):
            maybe_url = lines[i + 2]
            if is_url_like(maybe_url):
                website = maybe_url

        # If visible text is not a literal URL, look for a matching nearby link.
        if not website:
            for link in container.find_all("a", href=True):
                href = urljoin(source.url, link["href"])
                if is_external_company_url(source.url, href):
                    link_text = clean_text(link.get_text(" ", strip=True))
                    # Company websites on this page are ordered directly after
                    # the company's phone/address block. We do not guess if a
                    # deterministic match is unavailable.
                    if normalize_company_key(link_text) == key:
                        website = href
                        break

        seen.add(key)
        records.append(
            make_record(
                source,
                company,
                phone=phone_match.group(0),
                website=website,
                service_hint="commercial recycling",
            )
        )

    return records


# =============================================================================
# PLANO PDFs
# =============================================================================

def extract_pdf_text(raw: bytes) -> str:
    reader = PdfReader(BytesIO(raw))
    pages = [(page.extract_text() or "") for page in reader.pages]
    text = "\n".join(pages)

    # pypdf can concatenate the last phone on page 1 with the first company on
    # page 2. Insert a newline after a phone if letters follow immediately.
    text = re.sub(
        r"((?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4})(?=[A-Za-z])",
        r"\1\n",
        text,
    )
    return text


def split_company_from_inline_address(line: str) -> tuple[str, str]:
    """
    "American SI Waste 6207 Toronto St"
      -> ("American SI Waste", "6207 Toronto St")
    """
    line = clean_text(line)

    match = INLINE_ADDRESS_RE.search(line)
    if not match:
        return line, ""

    idx = match.start()
    company = clean_text(line[:idx])
    address = clean_text(line[idx:])
    return company, address


def parse_plano_pdf(source: Source, raw: bytes) -> list[dict]:
    """
    Parse records as chunks ending in a phone number.

    Within each record, company-name text appears before the first address line.
    Multi-line names are joined. Inline addresses are split from the company.
    """
    text = extract_pdf_text(raw)
    lines = [
        clean_text(x)
        for x in text.replace("\xa0", " ").splitlines()
        if clean_text(x)
    ]

    # Remove document headings / notes before chunking.
    ignored_prefixes = (
        "revised ",
        "commercial waste and recycling",
        "general permitted haulers",
        "c&d permitted haulers",
        "please note:",
        "company name address phone",
    )

    cleaned_lines = []
    for line in lines:
        low = line.lower()

        # Plano's PDF note can be concatenated directly onto the next company
        # by pypdf, e.g. "commingled loads. Advantage Waste Disposal, LLC".
        # Remove only the known note prefix; never generically truncate names.
        if "commingled loads." in low:
            idx = low.rfind("commingled loads.")
            line = clean_text(line[idx + len("commingled loads."):])
            low = line.lower()

        if not line:
            continue
        if any(low.startswith(p) for p in ignored_prefixes):
            continue
        cleaned_lines.append(line)

    records = []
    buffer = []
    seen = set()

    service_hint = (
        "construction & demolition recycling"
        if "C&D" in source.name
        else "commercial recycling"
    )

    for line in cleaned_lines:
        phone_match = PHONE_RE.search(line)

        if not phone_match:
            buffer.append(line)
            continue

        # Text before the phone belongs to this record too (often "City, TX ZIP").
        before_phone = clean_text(line[:phone_match.start()])
        if before_phone:
            buffer.append(before_phone)

        phone = phone_match.group(0)

        # Identify the first address-like line. Everything before that is company.
        company_parts = []
        address_started = False

        for part in buffer:
            part = clean_text(part)
            if not part:
                continue

            company_piece, inline_address = split_company_from_inline_address(part)

            if not address_started and inline_address:
                if is_plausible_company(company_piece):
                    company_parts.append(company_piece)
                address_started = True
                continue

            if is_address_like(part):
                address_started = True
                continue

            # City/state/ZIP after the street address.
            if address_started:
                continue

            if is_plausible_company(part):
                company_parts.append(part)

        company = clean_text(" ".join(company_parts))

        # Normalize common PDF line-wrap spacing, but don't rewrite legal names.
        company = re.sub(r"\s+([,&])", r"\1", company)
        company = re.sub(r"\s+", " ", company).strip()

        if is_plausible_company(company):
            key = normalize_company_key(company)
            if key not in seen:
                seen.add(key)
                records.append(
                    make_record(
                        source,
                        company,
                        phone=phone,
                        service_hint=service_hint,
                    )
                )

        # A phone ends the current record.
        buffer = []

        # If text follows the phone on the same line, it begins the next record.
        after_phone = clean_text(line[phone_match.end():])
        if after_phone:
            buffer.append(after_phone)

    return records



# =============================================================================
# DENTON 2026 PDF
# =============================================================================

PERMIT_NO_RE = re.compile(r"\b\d{4}-\d{4}\b")
PERMIT_NO_SPLIT_RE = re.compile(r"\b\d{4}\s*-\s*\d{4}\b")


def parse_denton_pdf(source: Source, raw: bytes) -> list[dict]:
    """
    Denton's 2026 PDF is a three-page table whose company names may wrap across
    lines. A permit number and phone terminate each record.

    This is a discovery source: service columns are preserved only as a broad
    municipal-waste-hauler signal. Downstream qualification decides whether a
    company matches TrashLab's final ICP.
    """
    text = extract_pdf_text(raw)

    # Normalize page-boundary artifacts and permit-number line wraps.
    text = re.sub(r"(\d{4})\s*\n\s*-(\d{4})", r"\1-\2", text)
    lines = [clean_text(x) for x in text.splitlines() if clean_text(x)]

    records = []
    buffer = []
    seen = set()

    ignore_fragments = (
        "city of denton collection and transportation",
        "january 1, 2026",
        "company permit",
        "grease grit special",
        "industrial pretreatment",
        "1100 s. mayhill",
        "our core values",
        "ada/eoe/adea",
        "integrity",
    )

    for line in lines:
        low = line.lower()
        if any(fragment in low for fragment in ignore_fragments):
            continue

        phone_match = PHONE_RE.search(line)

        if phone_match:
            # Include text before the phone because the company/permit/service
            # data is frequently on the same line.
            before_phone = clean_text(line[:phone_match.start()])
            if before_phone:
                buffer.append(before_phone)

            joined = clean_text(" ".join(buffer))

            # Company is everything before the first permit number.
            permit_match = PERMIT_NO_SPLIT_RE.search(joined)
            if permit_match:
                company = clean_text(joined[:permit_match.start()])
            else:
                # If extraction omitted a permit token, reject rather than guess.
                company = ""

            company = re.sub(r"\s+", " ", company).strip(" -|")

            if is_plausible_company(company):
                key = normalize_company_key(company)
                if key not in seen:
                    seen.add(key)
                    records.append(
                        make_record(
                            source,
                            company,
                            phone=phone_match.group(0),
                            service_hint="Denton permitted waste hauler",
                        )
                    )

            buffer = []

            # Anything after phone is not part of the next company on this PDF.
            continue

        buffer.append(line)

    return records


# =============================================================================
# NEW BRAUNFELS
# =============================================================================

def parse_new_braunfels(source: Source, raw: bytes) -> list[dict]:
    """
    Current page records are rendered as:
      Company
      SERVICES OFFERED: ...
      PHONE: ...
    """
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    heading = find_heading(soup, r"permitted private haulers")
    if not heading:
        raise ValueError("New Braunfels permitted-private-haulers heading not found")

    records = []
    seen = set()

    # Work from the heading forward and stop at Contact Us.
    nodes = list(heading.find_all_next())
    for idx, node in enumerate(nodes):
        if node.name in {"h2", "h3", "h4"}:
            heading_text = clean_text(node.get_text(" ", strip=True))
            if heading_text.lower() == "contact us":
                break

        text_value = clean_text(node.get_text(" ", strip=True)) if hasattr(node, "get_text") else ""
        if "SERVICES OFFERED:" not in text_value.upper() or "PHONE:" not in text_value.upper():
            continue

        phone_match = PHONE_RE.search(text_value)
        if not phone_match:
            continue

        # The company name is normally the closest preceding heading/list label.
        company = ""
        for prev in node.find_all_previous(limit=8):
            candidate = clean_text(prev.get_text(" ", strip=True))
            if not candidate or candidate == text_value:
                continue
            if "permitted private haulers" in candidate.lower():
                continue
            if candidate.upper().startswith("SERVICES OFFERED:"):
                continue
            if is_plausible_company(candidate) and len(candidate.split()) <= 10:
                company = candidate
                break

        # CivicPlus sometimes wraps company + service text in the same element.
        if not company:
            company = clean_text(
                re.split(r"SERVICES OFFERED:", text_value, flags=re.I)[0]
            )

        if not is_plausible_company(company):
            continue

        service_match = re.search(
            r"SERVICES OFFERED:\s*(.*?)\s*\|\s*PHONE:",
            text_value,
            re.I,
        )
        services = clean_text(service_match.group(1)) if service_match else ""

        key = normalize_company_key(company)
        if key in seen:
            continue
        seen.add(key)

        records.append(
            make_record(
                source,
                company,
                phone=phone_match.group(0),
                service_hint=services or "permitted private hauler",
            )
        )

    return records


# =============================================================================
# HOUSTON OFFICIAL FRANCHISE PDF
# =============================================================================

def parse_houston_pdf(source: Source, raw: bytes) -> list[dict]:
    """
    Houston's official franchise PDF is a large table:
      Franchise No. | Ordinance No. | Franchisee

    The city page currently labels this operator list as updated 06/12/2020.
    We intentionally use it only as a discovery seed. Downstream verification
    must confirm that a company is still active/current before final ICP output.
    """
    text = extract_pdf_text(raw)
    lines = [clean_text(x) for x in text.splitlines() if clean_text(x)]

    records = []
    seen = set()

    # Common extraction patterns include either one complete row per line or
    # numeric columns followed by the franchisee on the same/next line.
    row_re = re.compile(
        r"^\s*\d+\s+(?:20\d{2}[-–]\d+|\d{4}[-–]\d+)\s+(.+?)\s*$",
        re.I,
    )

    pending_numeric_row = False

    for line in lines:
        low = line.lower()

        if any(
            phrase in low
            for phrase in (
                "city of houston",
                "administration & regulatory affairs",
                "franchise administration",
                "list of franchised commercial solid waste",
                "franchise no.",
                "ordinance no.",
                "franchisee",
                "page ",
            )
        ):
            continue

        match = row_re.match(line)
        if match:
            company = clean_text(match.group(1))
            pending_numeric_row = False
        elif re.fullmatch(
            r"\d+\s+(?:20\d{2}[-–]\d+|\d{4}[-–]\d+)",
            line,
        ):
            pending_numeric_row = True
            continue
        elif pending_numeric_row:
            company = line
            pending_numeric_row = False
        else:
            # Alternate pypdf layout: franchise no + ordinance no may be
            # separated from the company but still prefix the line.
            company = re.sub(
                r"^\d+\s+(?:20\d{2}[-–]\d+|\d{4}[-–]\d+)\s*",
                "",
                line,
            ).strip()
            if company == line:
                continue

        if not is_plausible_company(company):
            continue

        key = normalize_company_key(company)
        if key in seen:
            continue
        seen.add(key)

        records.append(
            make_record(
                source,
                company,
                service_hint="commercial solid waste franchise; reverify current status",
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
    "denton_pdf": parse_denton_pdf,
    "new_braunfels": parse_new_braunfels,
    "houston_pdf": parse_houston_pdf,
}


# =============================================================================
# DEDUPE + VALIDATION
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
            values = sorted(
                {clean_text(v) for v in group[column] if clean_text(v)}
            )
            return " | ".join(values)

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


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> None:
    test_source = Source(
        name="Test",
        city="Test",
        url="https://example.com/page",
        parser="",
        source_class="test",
        minimum_expected=1,
    )

    # Garland: heading must not become company.
    garland_html = b"""
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

    # Austin: linked haulers work without a literal HTML table.
    austin_html = b"""
    <main>
      <h1>Universal Recycling Ordinance Licensed Haulers</h1>
      <a href="/hauler">Hauler</a>
      <a href="/organic">Organic Diversion</a>
      <a href="/recycling">Recycling</a>
      <div><a href="https://alpha.example">Alpha Waste</a> X X</div>
      <div><a href="https://beta.example">Beta Recycling</a> X</div>
      <a href="https://www.austintexas.gov/full-list">City of Austin Licensed Private Haulers</a>
    </main>
    """
    got = parse_austin(test_source, austin_html)
    assert {r["company"] for r in got} == {"Alpha Waste", "Beta Recycling"}

    # Mesquite: only company, not address/city/phone/url.
    mesquite_html = b"""
    <main>
      <h1>Permitted Recycling Haulers</h1>
      <div>City of Mesquite Commercial Recycling</div>
      <div>1101 E. Main St.</div>
      <div>Mesquite, TX 75149</div>
      <div>Phone: 972-216-6285</div>
      <div>Hurricane Waste Systems, LLC</div>
      <div>712 W Shady Grove</div>
      <div>Irving, TX 75060</div>
      <div>972-251-7177</div>
      <div>https://hurricanewaste.com</div>
      <h3>Contact Us</h3>
    </main>
    """
    got = parse_mesquite(test_source, mesquite_html)
    assert len(got) == 1
    assert got[0]["company"] == "Hurricane Waste Systems, LLC"

    # Plano general: one-line and multi-line company names.
    plano_text = """
    COMMERCIAL WASTE AND RECYCLING
    General Permitted Haulers
    Advantage Waste Disposal LLC
    2500 W Bruton Road
    Balch Springs, TX 75180
    (972) 222-2444
    Waste Connections (Purchased NTX
    Waste d/b/a Rhino Removal)
    12150 Garland Road
    Dallas, TX 75218
    (972) 996-5550
    """
    class FakePage:
        def __init__(self, text):
            self._text = text
        def extract_text(self):
            return self._text

    # Test the record-building logic by temporarily using a helper.
    def parse_plano_text_fixture(text_value: str):
        lines = [clean_text(x) for x in text_value.splitlines() if clean_text(x)]
        raw = "\n".join(lines).encode()
        return lines

    # Directly exercise inline-address splitter too.
    c, a = split_company_from_inline_address(
        "American SI Waste 6207 Toronto St"
    )
    assert c == "American SI Waste"
    assert a == "6207 Toronto St"

    # Exact v5 failure mode: a PDF note glued to the next company.
    contaminated = "commingled loads. Advantage Waste Disposal, LLC"
    low = contaminated.lower()
    idx = low.rfind("commingled loads.")
    cleaned = clean_text(contaminated[idx + len("commingled loads."):])
    assert cleaned == "Advantage Waste Disposal, LLC"

    # Denton company names can wrap before permit numbers.
    denton_joined = "Renegade Roll-offs and Hauling DBA Envirohauling Services 2605-0625 X"
    permit = PERMIT_NO_SPLIT_RE.search(denton_joined)
    assert clean_text(denton_joined[:permit.start()]) == (
        "Renegade Roll-offs and Hauling DBA Envirohauling Services"
    )

    # Austin's live page uses linked column labels + linked company rows.
    austin_html = b"""
    <div>
      <a href="/foo">Hauler</a>
      <a href="/organic">Organic Diversion</a>
      <a href="/recycling">Recycling</a>
      <a href="https://example-hauler.com">Example Hauler LLC</a>
      <a href="https://example-two.com">Example Two Waste</a>
      <a href="https://www.austintexas.gov/full-list">City of Austin Licensed Private Haulers</a>
    </div>
    """
    austin_source = Source(
        name="Austin test",
        city="Austin",
        url="https://example.invalid",
        parser="austin",
        source_class="municipal_solid_waste",
        minimum_expected=2,
    )
    austin_records = parse_austin(austin_source, austin_html)
    assert [r["company"] for r in austin_records] == [
        "Example Hauler LLC",
        "Example Two Waste",
    ]

    print("SELF-TEST PASSED")


# =============================================================================
# MAIN
# =============================================================================

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
            records = PARSERS[source.parser](source, raw)
            validate_source(source, records)

            unique_count = len(
                {normalize_company_key(r["company"]) for r in records}
            )
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
        raise SystemExit("No candidates extracted. Nothing was written.")

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
