# Better Basket Assessment


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


The final polish is invoked **automatically** by `betterbasket_app.py`; it does not require a second manual command.





Compare a specific Dataset A product against B? [y/N]:
```

Choose `y`, then enter either a Store A `item_id` or part of a product name. The matcher runs the production retrieval/scoring logic for that product and prints the best Store B candidate, verdict, and match/non-match confidence. If GPT credentials are active and the row needs review, the same structured GPT review path is used.



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

Do not reuse an output folder created with a different dataset/config signature. The launcher checks `run_state.json` and asks for a fresh output directory when the signature differs.
