# TrashLab Texas Hauler ICP Engine

A lightweight, reproducible pipeline for discovering, qualifying, enriching, and ranking Texas waste haulers against a TrashLab-style ICP.

The prototype prioritizes **traceability and accuracy over artificial completeness**. Unknown data is intentionally left unknown rather than inferred or fabricated.

The exercise was also built within a practical constraint: **no additional paid data or enrichment budget**. Public sources and free/trial API capacity were used to demonstrate the system. The architecture is intentionally modular so those sources can be replaced with licensed production providers without rebuilding the pipeline.

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
Public data + free/trial search APIs
        ↓
Website validation + first-party enrichment
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

They also fit the constraint of this prototype: they are publicly available and do not require purchasing a commercial dataset before proving the underlying discovery model.

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

### Enrichment approach and prototype constraints

This take-home was built without purchasing additional enrichment data or API capacity.

The goal was therefore to build the best reproducible enrichment workflow possible within a **free-data / free-API guardrail**, rather than assume access to a production enrichment budget.

The enrichment sequence is:

1. preserve websites and contact data already supplied by municipal sources
2. use SerpApi to search for unresolved company domains
3. use You.com as a secondary search source for remaining gaps
4. validate candidate domains against company-name and location evidence
5. reject known government, directory, social, and third-party domains
6. crawl accepted company websites for public phone, email, service-line, and size evidence

SerpApi and You.com were selected because free/trial API capacity was available for the prototype. They were useful for demonstrating automated domain resolution without requiring the purchase of an enrichment dataset or subscription.

They should not be interpreted as a recommendation for the final production enrichment stack.

With a production budget, I would benchmark licensed company and contact enrichment providers against this public-data baseline and select the provider mix based on verified coverage, accuracy, false-positive rate, and cost.

The important architectural point is that the **provider layer is replaceable**. Discovery, validation, normalization, evidence collection, and scoring do not need to be redesigned when a better enrichment provider is introduced.

### Data quality principle

Missing enrichment is treated as **unknown**, not as evidence that a company is a poor ICP.

This distinction matters because smaller independent haulers often have limited public web presence even when they may be strong operational fits.

The system therefore separates ICP fit from evidence confidence instead of artificially penalizing companies simply because public enrichment is incomplete.

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

Search API credentials are optional:

```bash
export SERPAPI_KEY="your_key"
export YOU_API_KEY="your_key"
```

If credentials are not supplied, the enrichment script still runs using available source data and known company websites.

Credentials are not stored in the repository.

## Scaling to the U.S. and Canada

I would scale the **framework, not the Texas file**.

Texas validates the ingestion, qualification, enrichment, validation, and scoring model. National expansion would separate reusable pipeline logic from individual source configurations.

I would prioritize discovery sources in this order:

1. statewide regulatory / licensing datasets where available
2. municipal permit, franchise, and approved-hauler lists
3. industry associations and structured directories
4. targeted web discovery to fill geographic gaps
5. licensed enrichment providers for company and contact verification

The unit of scale becomes **source coverage**, rather than manually finding individual companies.

For example, rather than attempting to discover thousands of haulers individually, I would build a source registry identifying the highest-value state and municipal datasets, automate ingestion for those sources, and route all records through the same normalization, validation, deduplication, enrichment, and scoring layers demonstrated here.

## Cost per 1,000

The prototype was intentionally built within a **free-data / free-API guardrail**.

Discovery uses public municipal and regulatory sources at no data-acquisition cost.

Website resolution used free/trial API capacity from SerpApi and You.com. No commercial enrichment dataset or additional paid enrichment subscription was purchased for the exercise.

As a result, the direct incremental data cost of the prototype was effectively **$0**, excluding development time and normal compute/network usage.

I would **not extrapolate a $0 cost per 1,000 records to a production national system**.

For U.S. and Canada scale, I would run a representative batch through candidate licensed enrichment providers and measure:

- cost per 1,000 attempted records
- cost per verified record
- verified-domain match rate
- contact fill rate
- false-positive rate
- refresh cost

The production decision would be based primarily on **cost per usable verified record**, rather than simply cost per API call.

For example, a more expensive provider that produces materially higher verified-domain and contact coverage may have a lower effective acquisition cost than a cheaper search API requiring substantial downstream validation.

The prototype demonstrates where that provider plugs into the system and provides a public-data baseline against which paid providers can be evaluated.

## Refresh Cadence

Different data types should refresh at different frequencies:

- municipal / regulatory sources: monthly or quarterly
- company websites and service lines: quarterly
- contact data: monthly or provider-dependent
- full ICP rescoring: after each material enrichment refresh

Source-level change detection would allow unchanged municipal datasets to be skipped rather than repeatedly processing identical records.

At production scale I would also track source freshness and parser health so a changed municipal page or PDF format generates an alert instead of silently degrading coverage.

## Expected Failure Modes

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
- free/trial API limits that are unsuitable for production volume

The prototype addresses these by preserving provenance, validating sources independently, applying domain guardrails, keeping evidence URLs, and treating missing information as unknown.

The search API stage is intentionally treated as a **candidate-generation mechanism**, not unquestioned ground truth. Candidate websites are validated before being used as enrichment evidence.

At production scale I would add stronger entity resolution, automated domain verification, source freshness monitoring, licensed enrichment, and ongoing QA sampling.

## Production Improvements

With production access and budget, the next improvements would be:

1. replace or supplement free search APIs with licensed enrichment providers
2. expand the municipal and regulatory source registry nationally
3. strengthen entity resolution across company names, DBAs, locations, and parent companies
4. add automated source freshness and parser-health monitoring
5. improve fleet and employee-size enrichment
6. add contact-level enrichment for relevant operational and executive personas
7. measure enrichment providers against verified-record yield and cost
8. schedule recurring refresh and automatic ICP rescoring

The prototype is therefore not dependent on SerpApi or You.com. Those providers were appropriate tools for the constraints of this exercise; the provider layer can be upgraded independently.

## Design Principle

The system is intentionally modular:

```text
Discover → Qualify → Enrich → Validate → Score
```

Each layer can be replaced or scaled independently.

The goal of the prototype is not to claim that free public web data can produce a perfectly enriched national hauler database.

It is to demonstrate a reproducible system for turning fragmented vertical data into a traceable, ranked ICP dataset — **using the resources available within the exercise's constraints** — and to show a clear path for scaling that system with better data sources and enrichment providers in production.
