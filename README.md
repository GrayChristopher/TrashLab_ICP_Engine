# TrashLab Texas Hauler ICP Engine

A lightweight, reproducible pipeline for discovering, qualifying, enriching, and ranking Texas waste haulers against a TrashLab-style ICP.

The prototype prioritizes **traceability and accuracy over artificial completeness**. Unknown data is intentionally left unknown rather than inferred or fabricated.

## Pipeline

```text
Public municipal hauler sources
        ↓
01_discover.py
        ↓
469 unique Texas candidates
        ↓
Qualification + normalization
        ↓
210 qualified candidates
        ↓
02_enrich.py
        ↓
Public / first-party enrichment
        ↓
03_score.py
        ↓
ICP scoring + eligibility guardrail
        ↓
Top 150 ranked Texas haulers
```

## 1. Discovery

`01_discover.py` builds the candidate universe from public municipal and regulatory hauler sources across Texas.

The current prototype uses 10 source configurations spanning markets including:

- Garland
- Weatherford
- Austin
- College Station
- Mesquite
- Plano
- Denton
- New Braunfels
- Houston

Plano includes separate general and C&D hauler sources.

The discovery layer:

- retrieves HTML and PDF sources
- validates each source independently
- parses company records
- normalizes company names
- deduplicates records across sources
- preserves source URLs and market provenance
- flags source failures rather than silently dropping them

Current discovery output:

**469 unique candidate companies**

### Why municipal data?

Municipal permit, franchise, and approved-hauler lists provide a useful high-signal starting point because inclusion often indicates an actual operating relationship with a local waste market.

They are not treated as perfect or complete. The pipeline preserves source provenance so records can be validated downstream.

## 2. Qualification and Enrichment

The qualification layer separates likely operating waste haulers from obvious non-ICP records.

Relevant operations include:

- roll-off
- residential collection
- commercial collection
- recycling
- portable toilet
- septic
- mixed waste operations

The enrichment layer preserves existing municipal data first, then attempts to resolve and crawl company websites for additional public evidence.

Fields include:

- company
- location
- phone
- email
- website
- service lines
- adjacent service lines
- estimated size signal
- source / evidence URLs

Website resolution can optionally use:

1. existing municipal/source websites
2. SerpApi search
3. You.com search as a fallback

Candidate domains are validated before being accepted, and known government, directory, social, and third-party domains are excluded.

Once a company website is accepted, the pipeline searches public pages for:

- business phone
- business email
- service-line evidence
- fleet / employee size signals
- supporting evidence URLs

API credentials are optional and are read from environment variables. Without them, the enrichment script still runs using available source data and known websites.

### Data quality principle

Missing enrichment is treated as **unknown**, not as evidence that a company is a poor ICP.

This distinction matters because smaller independent haulers often have limited public web presence even when they may be strong operational fits.

## 3. ICP Scoring

`03_score.py` applies a transparent 100-point scoring model.

| Dimension | Points |
| --- | ---: |
| Service fit | 40 |
| Multi-line complexity | 20 |
| Operational scale | 20 |
| Data confidence / contactability | 20 |
| **Total** | **100** |

### Service fit

Rewards evidence of TrashLab-relevant operations such as roll-off, residential, commercial, and recycling.

### Multi-line complexity

Rewards operators serving multiple service lines, where dispatch, routing, billing, and operational complexity are likely higher.

### Operational scale

Uses available fleet, truck, employee, or similar operating-size evidence.

Missing size information is not automatically interpreted as a small company.

### Data confidence

Measures how much verifiable information is available for the record, including website, phone, email, service evidence, and supporting sources.

This allows the model to distinguish:

**lower ICP fit** from **lower evidence confidence**.

## Eligibility Guardrail

Before final ranking, the scoring layer removes obvious parser artifacts and clearly non-core specialty records that lack evidence of relevant hauling operations.

Portable toilet and septic operators are retained because they are relevant to TrashLab's supported operating model.

The final output contains:

**150 ranked Texas hauler ICP records**

Each record includes its total ICP score, component scores, evidence tier, and source evidence.

## Running the Pipeline

Install dependencies:

```bash
pip install requests beautifulsoup4 pypdf
```

Run discovery:

```bash
python 01_discover.py
```

Run enrichment:

```bash
python 02_enrich.py \
  --input data/qualified_candidates.csv \
  --output data/enriched_candidates.csv
```

Run scoring:

```bash
python 03_score.py \
  --input data/enriched_candidates.csv
```

The enrichment layer can also be tested without making search API calls:

```bash
python 02_enrich.py --self-test
```

Optional search API credentials:

```bash
export SERPAPI_KEY="your_key"
export YOU_API_KEY="your_key"
```

Credentials are not stored in the repository.

## Scaling to the U.S. and Canada

I would scale the **framework, not the Texas file**.

Texas validates the ingestion and scoring model. National expansion would separate reusable parsing, normalization, enrichment, and scoring logic from individual source configurations.

I would prioritize discovery sources in this order:

1. statewide regulatory / licensing datasets where available
2. municipal permit, franchise, and approved-hauler lists
3. industry associations and structured directories
4. targeted web discovery to fill geographic gaps
5. licensed enrichment providers for company and contact verification

The unit of scale becomes **source coverage**, rather than manually finding individual companies.

### Cost per 1,000

This prototype relies primarily on public municipal data and used free/trial search API capacity for domain resolution.

At production scale, I would benchmark licensed enrichment/search providers based on:

- verified-domain match rate
- contact fill rate
- false-positive rate
- API cost
- refresh cost

Rather than assume a theoretical cost per 1,000, I would measure it after a representative production batch and optimize the provider mix against verified-record yield.

### Refresh cadence

I would use different refresh schedules by data type:

- municipal / regulatory sources: monthly or quarterly
- company websites and service lines: quarterly
- contact data: monthly or provider-dependent
- full ICP rescoring: after each material enrichment refresh

Source-level change detection would allow unchanged datasets to be skipped.

### Expected failure modes

The main production risks are:

- stale municipal permit lists
- municipal page or PDF format changes
- duplicate companies operating under multiple names
- acquisitions and brand changes
- weak company web presence
- search engines returning directories or unrelated companies
- false-positive domain resolution
- missing fleet / employee data
- service lines that are not explicitly described online

The prototype addresses these by preserving provenance, validating sources independently, applying domain guardrails, keeping evidence URLs, and treating missing information as unknown.

At production scale I would add stronger entity resolution, automated domain verification, source freshness monitoring, licensed enrichment, and QA sampling.

## Design Principle

The system is intentionally modular:

```text
Discover → Qualify → Enrich → Validate → Score
```

Each layer can be replaced or scaled independently.

The goal of the prototype is not to claim that public web data can produce a perfectly enriched national hauler database.

It is to demonstrate a reproducible system for turning fragmented vertical data into a traceable, ranked ICP dataset that can be expanded with better sources and enrichment providers over time.
