#!/usr/bin/env python3
"""
TrashLab Build 1 — 03_score.py

Transparent ICP scoring for qualified/enriched Texas waste haulers.

Input:
    /content/data/enriched_candidates.csv

Outputs:
    /content/data/scored_candidates.csv
    /content/data/final_icp_150.csv

Scoring (100 points)
--------------------
Service fit                 0–40
Multi-line complexity       0–20
Operational scale           0–20
Data confidence             0–20

Missing enrichment is treated as UNKNOWN, not as proof of poor fit.
The score therefore includes an evidence-coverage field so a lower score
can be distinguished from a lower-confidence score.
"""

from __future__ import annotations
import argparse, csv, re
from pathlib import Path

CORE = ("roll-off", "residential", "commercial", "recycling")

# Guardrails: remove obvious parser artifacts and clearly non-core specialty
# operators before ranking. Adjacent TrashLab-supported services such as
# septic and portable toilet are intentionally NOT rejected.
NON_CORE_ONLY_TERMS = (
    "scrap metal", "metal recycling", "metals recycling", "iron and metal",
    "used oil", "oil recycling", "oilfield waste", "hazardous waste",
    "medical waste", "biohazard", "asbestos"
)
CORE_HAULER_TERMS = (
    "waste", "trash", "garbage", "hauler", "hauling", "disposal",
    "roll-off", "roll off", "dumpster", "residential", "commercial",
    "recycling", "sanitation", "solid waste", "portable toilet", "septic"
)

def eligibility(row):
    company = clean(row.get("company"))
    blob = " ".join(clean(row.get(c)).lower() for c in (
        "company","service_hint","qualification_reason","service_lines",
        "adjacent_service_lines"
    ))

    if (
        not company
        or "$(document" in company.lower()
        or "function(" in company.lower()
        or company.lower().startswith("<script")
        or len(company) > 180
    ):
        return False, "parser_artifact"

    non_core = [t for t in NON_CORE_ONLY_TERMS if t in blob]
    # Specialty language alone is not enough to reject if we also have explicit
    # core hauling evidence outside the company name.
    evidence_blob = " ".join(clean(row.get(c)).lower() for c in (
        "service_hint","service_lines","adjacent_service_lines"
    ))
    explicit_core_terms = (
        "trash","garbage","hauler","hauling","disposal","roll-off","roll off",
        "dumpster","residential","commercial","solid waste","portable toilet","septic"
    )
    core_evidence = any(t in evidence_blob for t in explicit_core_terms)

    if non_core and not core_evidence:
        return False, "specialty_non_core_only: " + ", ".join(non_core[:2])

    return True, "eligible"


def clean(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()

def parts(v):
    return {x.strip().lower() for x in clean(v).split("|") if x.strip()}

def has_any(text, terms):
    s = clean(text).lower()
    return any(t in s for t in terms)

def score_row(row):
    services = parts(row.get("service_lines"))
    adjacent = parts(row.get("adjacent_service_lines"))
    hint = clean(row.get("service_hint")).lower()
    reason = clean(row.get("qualification_reason")).lower()
    evidence_blob = " ".join([hint, reason])

    # 1) SERVICE FIT — 40
    # Direct website-derived core service evidence is strongest.
    direct_core = services.intersection(CORE)
    if len(direct_core) >= 2:
        service_fit = 40
        service_reason = f"{len(direct_core)} verified core service lines"
    elif len(direct_core) == 1:
        service_fit = 35
        service_reason = f"verified {next(iter(direct_core))} service"
    elif has_any(evidence_blob, (
        "roll-off","roll off","residential","commercial","recycling",
        "solid waste","waste hauler","trash","garbage","disposal"
    )):
        service_fit = 30
        service_reason = "qualified public-source hauler evidence"
    elif adjacent:
        service_fit = 25
        service_reason = "verified adjacent TrashLab-relevant operation"
    else:
        # Already passed the qualification layer; unknown != non-fit.
        service_fit = 20
        service_reason = "qualified candidate; service detail unknown"

    # 2) MULTI-LINE COMPLEXITY — 20
    # Complexity is valuable for an operations platform.
    all_lines = direct_core.union(adjacent)
    if len(all_lines) >= 4:
        complexity = 20
    elif len(all_lines) == 3:
        complexity = 16
    elif len(all_lines) == 2:
        complexity = 12
    elif len(all_lines) == 1:
        complexity = 7
    else:
        complexity = 3  # unknown, not zero

    # 3) OPERATIONAL SCALE — 20
    size = clean(row.get("size_signal"))
    size_lower = size.lower()
    if size:
        nums = [int(x.replace(",", "")) for x in re.findall(r"\b[\d,]+\b", size)]
        max_n = max(nums) if nums else 0
        if max_n >= 100:
            scale = 20
        elif max_n >= 25:
            scale = 17
        elif max_n >= 10:
            scale = 14
        elif max_n >= 5:
            scale = 11
        else:
            scale = 8
        scale_reason = size
    else:
        scale = 5  # unknown, not "tiny"
        scale_reason = "Unknown"

    # 4) DATA CONFIDENCE / CONTACTABILITY — 20
    # Reward reproducible evidence, not merely filled cells.
    confidence = 0
    conf_reasons = []

    source_count = 0
    try:
        source_count = int(float(clean(row.get("source_count")) or 0))
    except ValueError:
        pass

    if source_count >= 2:
        confidence += 5
        conf_reasons.append("multi-source discovery")
    elif clean(row.get("source_url")):
        confidence += 3
        conf_reasons.append("public-source evidence")

    if clean(row.get("verified_website")):
        confidence += 5
        conf_reasons.append("website")
    if clean(row.get("verified_phone")) or clean(row.get("phone")):
        confidence += 4
        conf_reasons.append("phone")
    if clean(row.get("verified_email")) or clean(row.get("email")):
        confidence += 3
        conf_reasons.append("email")
    if clean(row.get("evidence_urls")):
        confidence += 3
        conf_reasons.append("web evidence")

    confidence = min(confidence, 20)

    total = service_fit + complexity + scale + confidence

    # Evidence coverage is separate from score.
    populated = sum(bool(clean(row.get(c))) for c in (
        "verified_website", "verified_phone", "verified_email",
        "service_lines", "size_signal"
    ))
    coverage = round(populated / 5 * 100)

    if coverage >= 80:
        evidence_tier = "High"
    elif coverage >= 40:
        evidence_tier = "Medium"
    else:
        evidence_tier = "Low"

    return {
        "score_service_fit": service_fit,
        "score_complexity": complexity,
        "score_scale": scale,
        "score_data_confidence": confidence,
        "icp_score": total,
        "evidence_coverage_pct": coverage,
        "evidence_tier": evidence_tier,
        "score_service_reason": service_reason,
        "score_scale_reason": scale_reason,
        "score_confidence_reason": " | ".join(conf_reasons) or "limited evidence",
    }

SCORE_FIELDS = [
    "score_service_fit","score_complexity","score_scale",
    "score_data_confidence","icp_score","evidence_coverage_pct",
    "evidence_tier","score_service_reason","score_scale_reason",
    "score_confidence_reason","icp_eligible","eligibility_reason","icp_rank"
]

def self_test():
    rich = {
        "service_lines":"roll-off | residential | commercial | recycling",
        "adjacent_service_lines":"portable toilet",
        "size_signal":"42 trucks",
        "source_count":"2",
        "source_url":"https://example.gov",
        "verified_website":"https://example.com",
        "verified_phone":"972-555-1212",
        "verified_email":"info@example.com",
        "evidence_urls":"https://example.com/services",
    }
    s = score_row(rich)
    assert s["score_service_fit"] == 40
    assert s["score_complexity"] == 20
    assert s["score_scale"] == 17
    assert s["score_data_confidence"] == 20
    assert s["icp_score"] == 97
    assert s["evidence_tier"] == "High"

    sparse = {
        "qualification_reason":"qualified waste hauler",
        "source_url":"https://example.gov",
        "phone":"972-555-1212",
    }
    q = score_row(sparse)
    assert q["score_service_fit"] == 30
    assert q["score_scale"] == 5
    assert q["icp_score"] > 0
    assert q["evidence_tier"] == "Low"
    assert eligibility({"company":"$(document).ready(function() {"})[0] is False
    assert eligibility({
        "company":"ABC Portable Toilet & Septic",
        "adjacent_service_lines":"portable toilet | septic"
    })[0] is True
    assert eligibility({
        "company":"ABC Used Oil Recycling",
        "service_hint":"used oil recycling"
    })[0] is False
    print("SELF-TEST PASSED")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="/content/data/enriched_candidates.csv")
    p.add_argument("--output", default="/content/data/scored_candidates.csv")
    p.add_argument("--final-output", default="/content/data/final_icp_150.csv")
    p.add_argument("--top", type=int, default=150)
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test:
        self_test()
        return

    with open(args.input, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames or [])

    scored = []
    for row in rows:
        out = dict(row)
        ok, why = eligibility(row)
        out["icp_eligible"] = "yes" if ok else "no"
        out["eligibility_reason"] = why
        out.update(score_row(row))
        scored.append(out)

    # Eligible candidates rank ahead of rejected records. Within each group,
    # score first, then evidence coverage, then company for stable output.
    scored.sort(key=lambda r: (
        0 if r["icp_eligible"] == "yes" else 1,
        -int(r["icp_score"]),
        -int(r["evidence_coverage_pct"]),
        clean(r.get("company")).lower()
    ))

    eligible = [r for r in scored if r["icp_eligible"] == "yes"]
    rejected = [r for r in scored if r["icp_eligible"] != "yes"]

    for i, row in enumerate(eligible, 1):
        row["icp_rank"] = i
    for row in rejected:
        row["icp_rank"] = ""

    out_fields = list(dict.fromkeys(fields + SCORE_FIELDS))

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(scored)

    # Assignment asks for 150+. Final output only contains eligible ICPs.
    final = eligible[:args.top]
    if len(final) < args.top:
        raise RuntimeError(
            f"Only {len(final)} eligible candidates remain; need {args.top}. "
            "Review eligibility rules rather than silently filling with rejected records."
        )
    with open(args.final_output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(final)

    tiers = {}
    for r in final:
        tiers[r["evidence_tier"]] = tiers.get(r["evidence_tier"], 0) + 1

    print("\nSCORING COMPLETE")
    print(f"Candidates scored: {len(scored)}")
    print(f"Eligible ICPs:     {len(eligible)}")
    print(f"Rejected guardrail:{len(rejected)}")
    print(f"Final ICP rows:    {len(final)}")
    print(f"Top score:         {final[0]['icp_score'] if final else 'n/a'}")
    print(f"Cutoff score:      {final[-1]['icp_score'] if final else 'n/a'}")
    print("Final evidence tiers:")
    for k in ("High","Medium","Low"):
        print(f"  {k}: {tiers.get(k,0)}")
    print(f"All scored: {args.output}")
    print(f"Final ICP:  {args.final_output}")

if __name__ == "__main__":
    main()
