# TrashLab Texas Hauler ICP Engine

A lightweight, reproducible pipeline for discovering, qualifying, enriching, and ranking Texas waste haulers against a TrashLab-style ICP.

The prototype prioritizes **accuracy and traceability over artificial data completeness**. When a field cannot be verified from the available public data, it remains unknown rather than being inferred or fabricated.

## Pipeline

```text
Public municipal sources
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
209 eligible candidates
        ↓
Top 150 ranked ICP accounts
```

## 1. Discovery

`01_discover.py`

Discovers Texas waste-hauler candidates from 10 public municipal and regulatory sources, including approved-hauler, franchise, recycling-hauler, and permit lists.

The discovery layer:

- ingests HTML and PDF sources
- validates each source independently
- normalizes company names and fields
- deduplicates companies across sources
- preserves source URLs for traceability
- creates a candidate universe for downstream qualification

Current output: **469 unique candidate companies**

### Why municipal data?

Municipal hauler and franchise lists provide a useful high-intent starting point because inclusion often demonstrates that a company is actively permitted, approved, or recognized as a waste operator in a specific market.

No single source provides complete statewide coverage, so the system combines multiple sources.

## 2. Qualification & Enrichment

`02_enrich.py`

The second stage filters the discovery universe for companies relevant to TrashLab and enriches records using available public and first-party data.

The current ICP includes operators providing services such as:

- roll-off
- residential waste
- commercial waste
- recycling
- portable toilet
- septic
- related mixed hauling operations

Clearly unrelated businesses are excluded where the available evidence supports that decision.

The enrichment layer attempts to populate:

- company
- location
- phone
- email
- website
- service lines
- adjacent service lines
- operational size signals
- supporting evidence

Current prototype:

- **210 qualified candidates**
- 149 with phone data
- 39 with verified/known websites
- 32 with structured service-line enrichment
- 17 with email data
- 5 with explicit public size signals

### Unknown values

Missing data is intentionally left unknown.

The prototype does not interpret an unavailable email, website, or fleet count as evidence that the company lacks one.

This separates:

1. **ICP fit**
2. **data availability**
3. **data confidence**

In production, I would connect this enrichment layer to a licensed provider or enrichment workflow to increase fill rates while preserving the same validation, normalization, and evidence model.

## 3. ICP Scoring

`03_score.py`

Qualified candidates are ranked using a transparent 100-point model.

| Dimension | Weight |
|---|---:|
| Service fit | 40 |
| Multi-line operational complexity | 20 |
| Operational scale | 20 |
| Data confidence / contactability | 20 |
| **Total** | **100** |

### Service Fit — 40 points

Rewards verified evidence of TrashLab-relevant waste operations.

Companies with multiple verified core service lines receive the strongest score.

### Multi-Line Complexity — 20 points

Rewards operators managing multiple service types.

The hypothesis is that operational complexity increases the potential value of a vertical operating platform.

### Operational Scale — 20 points

Uses explicit public signals when available, including:

- fleet/truck counts
- employees
- customers
- locations
- other quantifiable operational indicators

Missing scale data is treated as **unknown**, not as evidence that the company is small.

### Data Confidence / Contactability — 20 points

Rewards records supported by stronger evidence, including:

- multiple discovery sources
- municipal source URLs
- verified website
- phone
- email
- first-party web evidence

An additional evidence-coverage tier distinguishes ICP score from enrichment completeness.

### Eligibility Guardrail

Before final ranking, deterministic guardrails remove obvious parser artifacts and clearly non-core specialty operators.

Adjacent TrashLab-relevant businesses such as portable-toilet and septic operators are retained.

Current result:

- **210 candidates scored**
- **209 eligible after guardrails**
- **150 final ranked ICP accounts**

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
python 02_enrich.py
```

Run scoring:

```bash
python 03_score.py
```

Each script includes a self-test:

```bash
python 01_discover.py --self-test
python 02_enrich.py --self-test
python 03_score.py --self-test
```

## Scaling to the U.S. and Canada

I would scale the **framework rather than the Texas file**.

The production discovery hierarchy would be:

```text
State / provincial datasets
        ↓
Municipal permit & franchise lists
        ↓
Industry associations / directories
        ↓
Web discovery for coverage gaps
        ↓
Licensed enrichment
        ↓
Validation + normalization + deduplication
        ↓
ICP scoring
```

The unit of scale is therefore **source coverage**, not a manually maintained list of companies.

### Refresh Cadence

I would use different refresh schedules by layer:

- discovery sources: monthly or quarterly depending on source update frequency
- contact/enrichment data: monthly
- high-value ICP accounts: more frequent verification where justified
- stale or failed sources: automatically flagged for review

### Cost per 1,000

This prototype intentionally uses public sources and free first-party web data, so direct acquisition cost is effectively negligible outside compute/time.

For production, I would benchmark enrichment vendors against match rate, accuracy, and coverage before committing to a cost-per-1,000 assumption.

The production cost model would be:

```text
public discovery cost
+ enrichment cost
+ verification cost
---------------------
verified records produced
```

I would report the measured cost per 1,000 verified records rather than inventing a vendor cost before selecting the production stack.

## Expected Failure Modes

The system is designed around several predictable data-quality problems:

- municipal source schema changes
- stale municipal/franchise lists
- inaccessible or anti-bot websites
- duplicate DBA and legal entity names
- franchise/location duplication
- missing websites or emails
- ambiguous service descriptions
- uncertain fleet/employee counts
- similarly named companies
- PDF extraction changes
- companies operating across multiple municipalities

The pipeline preserves source and evidence fields so questionable records can be reviewed instead of silently accepted.

## Production Improvements

With additional time and production tooling, the next improvements would be:

1. Add licensed domain/contact enrichment to increase website and email coverage.
2. Add FMCSA/DOT or equivalent fleet evidence where entity matching is sufficiently confident.
3. Expand discovery beyond Texas using configurable state/province source definitions.
4. Add automated source-health monitoring and schema-change alerts.
5. Add confidence thresholds and review queues for ambiguous entity matches.
6. Persist historical snapshots to detect operator additions, removals, and material changes.

## Design Principle

The goal of this prototype is not to manufacture a perfectly complete spreadsheet.

It is to demonstrate a system that can repeatedly:

**discover → qualify → enrich → validate → score**

a fragmented vertical market while preserving enough evidence to understand where every record came from and where additional production-grade enrichment would add value.
