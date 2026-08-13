# Better Basket Assessment

Product-matching pipeline for the BetterBasket Engineering Technical Assessment.

The matcher maps products from **Store A** to the closest product in **Store B** using a precision-first combination of compact retrieval, deterministic evidence scoring, structured GPT-5 nano review for unresolved cases, and a final SKU/catalog certification pass.

## Final result

The frozen final run processed **233,199 Store A products** against **55,516 Store B products**.

| Result | Count |
|---|---:|
| Certified matches | **8,469** |
| Manual review | 8,564 |
| Non-match | 216,166 |
| Total Store A rows | 233,199 |
| Final-polish demotions | 197 |

The assessment requires at least 4,000 submitted matches. The final output exceeds that threshold while deliberately prioritizing precision over forcing the theoretical 10,000+ complete-set count.

The final certification pass specifically protects against national-brand SKU mistakes such as incompatible package counts, pack-vs-single products, incompatible net sizes, product-line/variant differences, flavor or scent differences, and other identity-critical contradictions.

## Repository contents

The filenames mirror the pipeline design used during the assessment:

```text
Better-Basket-Assessment/
├── betterbasket_app.py                    # user-facing launcher / full pipeline entry point
├── betterbasket_candidate_retrieval.py    # Step 1: Candidate Retrieval
├── betterbasket_product_evidence.py       # shared product evidence extraction
├── betterbasket_match_algorithm.py        # Step 2: independent MATCH algorithm
├── betterbasket_non_match_algorithm.py    # Step 3: independent NON_MATCH algorithm
├── betterbasket_routing_module.py          # Step 4: calibrated routing/adjudication
├── betterbasket_pipeline_runner.py         # deterministic + GPT execution worker
├── betterbasket_final_certification.py     # Step 5: final SKU/count precision certification
├── betterbasket_benchmark.py               # single final benchmark/database comparison utility
├── betterbasket_runtime_config.json        # frozen runtime/calibration/router configuration
├── product_match_criteria_v2_audited.json  # canonical 140-attribute + contradiction/veto constitution
├── specifications/
│   ├── betterbasket_candidate_retrieval_spec_v1.json
│   ├── betterbasket_non_match_spec_final.json
│   ├── betterbasket_hard_veto_rules_final.json
│   ├── betterbasket_router_policy_final.json
│   └── betterbasket_confidence_calibration_final.json
├── tests/
│   └── validate_final_pipeline.py           # regression/invariant tests
└── outputs/
    ├── submission_matches.csv               # final assessment deliverable
    ├── all_verdicts.csv                     # full 233,199-row audit trail
    ├── manual_review.csv                    # intentionally unresolved rows
    ├── final_polish_demotions.csv           # final precision demotions
    ├── run_summary.json
    ├── run_state.json
    └── final_run.log
```

There is **one benchmark script**: `betterbasket_benchmark.py`. The regression validator lives under `tests/` and is not a second benchmark implementation.


### Specification JSONs

The original design work used JSON specifications before the production code was frozen.

- `product_match_criteria_v2_audited.json` is the canonical criteria constitution. It contains the 140 positive Match criteria, 140 independent contradiction/Non-match criteria, 140 evidence-quality criteria, and the original explicit hard-veto inventory.
- `specifications/betterbasket_candidate_retrieval_spec_v1.json` is the original 12-channel retrieval design specification. The production retrieval path was later optimized into the compact rare-token/spec implementation, so the JSON is retained as an architectural/design artifact rather than the final runtime settings.
- `specifications/betterbasket_non_match_spec_final.json` is a clean machine-readable view of the final 140 contradiction criteria and Non-match scoring semantics.
- `specifications/betterbasket_hard_veto_rules_final.json` documents the final runtime veto mapping, including the post-v2 V065-V067 additions and special cross-field count logic.
- `specifications/betterbasket_router_policy_final.json` and `specifications/betterbasket_confidence_calibration_final.json` are extracted directly from the frozen runtime configuration for easier audit/review.

The runtime source of truth remains the Python modules plus `betterbasket_runtime_config.json` and `product_match_criteria_v2_audited.json`; the derived specification files do not change execution.

## Requirements

- Python 3.10+
- No third-party Python packages are required by the matcher itself.
- Store A and Store B CSV files supplied with the assessment.
- BetterBasket GPT-5 nano endpoint, deployment/model name, and API key if GPT review is enabled.

The algorithm uses only the Python standard library. Store B details are cached locally in SQLite during a run.

## Run the full matcher

From the repository root:

```bash
python betterbasket_app.py
```

The launcher prompts for:

1. Store A CSV path or URL
2. Store B CSV path or URL
3. output folder
4. whether to enable BetterBasket GPT-5 nano
5. GPT endpoint, deployment/model name, and API key when enabled

Example:

```text
Dataset A URL/path: C:\data\grocery_store_a_items_final.csv
Dataset B URL/path: C:\data\grocery_store_b_items_final.csv
Output folder [betterbasket_run_v17]: C:\data\betterbasket_final_run
Use BetterBasket GPT-5 nano for narrow REVIEW cases? [Y/n]: y
```

The run performs:

```text
Store B compact index
        ↓
rare-token/spec candidate retrieval (top 20)
        ↓
deterministic evidence + non-match screening
        ↓
deep scoring on top candidates
        ↓
GPT-5 nano structured adjudication for bounded unresolved cases
        ↓
final national-SKU identity/catalog guard
        ↓
final MATCH-only precision polish
        ↓
submission_matches.csv
```

The final polish is invoked **automatically** by `betterbasket_app.py`; it does not require a second manual command.

### Main generated files

- `submission_matches.csv` — two columns: `item_id_A,item_id_B`; this is the main assessment submission.
- `all_verdicts.csv` — complete audit trail for every Store A row.
- `manual_review.csv` — unresolved rows intentionally excluded from the submission.
- `final_polish_demotions.csv` — rows removed from MATCH by the last precision layer and the reason for each removal.
- `run_summary.json` — final counts and polish statistics.

## Validate the frozen guards

Run the offline invariant suite before submission or after modifying code:

```bash
python tests/validate_final_pipeline.py
```

A successful run ends with:

```text
v17_guard_validation_complete=true
```

The checks cover national-brand identity contradictions, product models, sugar/fat/organic status, private-label policy, package size, K-Cup counts, outer cases, many-to-one variants, and the final national-brand package-count uniqueness rule.

## Benchmark script

`betterbasket_benchmark.py` has three modes.

### 1. Compare the two source databases

Use `profile` to compare Store A and Store B at the dataset level:

```bash
python betterbasket_benchmark.py profile \
  --a grocery_store_a_items_final.csv \
  --b grocery_store_b_items_final.csv \
  --out database_profile.json
```

It reports:

- row count
- column names/count
- shared and retailer-specific columns
- detected ID field
- ID uniqueness/duplicates
- blank-field fractions
- file sizes

This is useful before matching to verify that the expected datasets were supplied and to understand schema differences between the two retailers.

### 2. Benchmark deterministic matching speed

```bash
python betterbasket_benchmark.py speed \
  --a grocery_store_a_items_final.csv \
  --b grocery_store_b_items_final.csv \
  --sample 2500 \
  --out benchmark_speed.json
```

This builds a temporary Store B index, takes the first `--sample` Store A rows, and executes the same deterministic retrieval/scoring path used by production. It reports index time, deterministic execution time, rows/second, and verdict counts.

GPT is intentionally excluded from this benchmark because API/network latency is external and variable.

### 3. Compare two matcher runs

To compare two versions or two generated submissions:

```bash
python betterbasket_benchmark.py compare \
  --left path/to/old/submission_matches.csv \
  --right path/to/new/submission_matches.csv \
  --out comparison.json
```

The report includes:

- match count in each run
- shared Store A IDs
- Store A IDs whose selected B item changed
- added/removed A matches
- pair-level Jaccard similarity
- examples of changed assignments

This is the quickest way to quantify the effect of a matcher change without manually diffing CSVs.

## Comparing an individual Store A product against Store B

After the full pipeline finishes, the launcher asks:

```text
Compare a specific Dataset A product against B? [y/N]:
```

Choose `y`, then enter either a Store A `item_id` or part of a product name. The matcher runs the production retrieval/scoring logic for that product and prints the best Store B candidate, verdict, and match/non-match confidence. If GPT credentials are active and the row needs review, the same structured GPT review path is used.

This is useful for debugging individual matches after inspecting `all_verdicts.csv` or `manual_review.csv`.

## Matching strategy

### National brands

National-brand products are treated as exact-SKU matching. Same brand and product family are not sufficient when an identity-critical attribute conflicts. The pipeline checks, among other signals:

- model/part identifiers
- package count and multipack configuration
- net size
- flavor/scent/color
- strength or formula
- product line
- container/form
- product role

### Private label / fresh products

Private-label matching is handled as consumer-equivalence matching rather than national-brand exact SKU matching. Available attributes such as product identity, size, form, cut, fat/sodium status, and other modifiers are used to decide whether a shopper would reasonably regard the products as essentially the same.

### Abstention

The matcher intentionally supports `REVIEW`. Ambiguous cases are excluded rather than forced into the submission. This is especially important when Store B titles omit package size or variant information.

## Final run summary

The final frozen configuration is:

```text
audited_v17_final_submission_count_uniqueness_guard
```

Key runtime settings:

- deterministic worker concurrency: 4
- worker chunk size: 2,500
- compact retrieval: top 20
- deep scoring: top 3 plus protected exact-title candidates (cap 5)
- GPT group size: 24
- GPT concurrency: 12
- GPT unresolved-product cap: 12,000
- primary reasoning: low
- bounded secondary reasoning: medium

Final run:

```text
Products processed: 233,199
Certified MATCH:      8,469
REVIEW:               8,564
NON_MATCH:          216,166
Final polish:           197 rows demoted
Final polish runtime:  9.661 s
```

The 197 precision demotions included package-count collisions, pack-vs-single configurations, incompatible net sizes, and explicit national-brand/private-label variant contradictions. The final polish is MATCH-only and does not perform another full-catalog retrieval, rescoring pass, or GPT pass.

## Reproducibility notes

The deterministic stages are reproducible for the same datasets/configuration. GPT adjudication can vary slightly between full executions because it is a hosted model call. The final identity/catalog and precision-polish layers are deterministic and are intended to constrain any such variation before `submission_matches.csv` is emitted.

Do not reuse an output folder created with a different dataset/config signature. The launcher checks `run_state.json` and asks for a fresh output directory when the signature differs.
