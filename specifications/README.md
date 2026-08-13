# BetterBasket specifications

This folder separates design/audit specifications from executable runtime code.

- `betterbasket_candidate_retrieval_spec_v1.json` — the original 12-channel candidate-retrieval design specification from the development process. The frozen runtime was later optimized into the compact retrieval implementation in `betterbasket_candidate_retrieval.py`, so this file is retained as design history rather than runtime configuration.
- `betterbasket_non_match_spec_final.json` — derived final view of the 140 contradiction / Non-match atomic criteria.
- `betterbasket_hard_veto_rules_final.json` — derived final view of the hard-veto IDs, attribute mapping, and special runtime vetoes.
- `betterbasket_router_policy_final.json` — final router thresholds extracted from `betterbasket_runtime_config.json`.
- `betterbasket_confidence_calibration_final.json` — final Match and Non-match calibration coefficients extracted from `betterbasket_runtime_config.json`.

The runtime source of truth remains the Python modules, `betterbasket_runtime_config.json`, and `product_match_criteria_v2_audited.json`.
