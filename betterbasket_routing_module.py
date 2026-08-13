#!/usr/bin/env python3
"""BetterBasket routing/adjudication module. This is the final automatic verdict router."""
from __future__ import annotations
import math
from typing import Dict, List, Any

def sigmoid(z):
    if z>=0:
        e=math.exp(-z);return 1/(1+e)
    e=math.exp(z);return e/(1+e)

def calibrate(raw,params):return sigmoid(float(params["coefficient"])*float(raw)+float(params["intercept"]))

def route_group(rows:List[Dict[str,Any]],cfg:dict)->Dict[str,Any]:
    """Aggressive three-zone adjudication.

    MATCH and NON_MATCH are normal outcomes. REVIEW is deliberately narrow and
    reserved for candidates genuinely near the boundary. Hard vetoes always win.
    """
    cal=cfg["calibration"]
    t=cfg.get("router",{})
    base_nm=sigmoid(float(cal["nonmatch"]["intercept"]))
    for r in rows:
        r["match_confidence"]=calibrate(r["match_evidence_score"],cal["match"])
        r["nonmatch_confidence"]=calibrate(r["nonmatch_evidence_score"],cal["nonmatch"])
        pen=max(0,min(1,(r["nonmatch_confidence"]-base_nm)/(1-base_nm)))
        r["pair_support_score"]=r["match_confidence"]*(1-pen)
        r["automatic_support_score"]=0 if r["hard_veto"] else r["pair_support_score"]
    rows.sort(key=lambda r:(-r["automatic_support_score"],r["retrieval_rank"],str(r["item_id_B"])))
    best=rows[0];second=rows[1] if len(rows)>1 else None
    second_score=second["automatic_support_score"] if second else 0.0
    margin=best["automatic_support_score"]-second_score

    raw=float(best["match_evidence_score"]);fs=float(best["functional_identity_similarity"])
    ps=float(best["pair_support_score"]);nm=float(best["nonmatch_confidence"])
    pe=int(best["atomic_positive_evidence_count"]);ia=int(best["identity_anchor_count"])
    same=bool(best.get("same_brand",False));priv=bool(best.get("private_label_pair",False))
    brand_policy=str(best.get("brand_policy","UNKNOWN"))
    exact_title=bool(best.get("exact_title_match",False))
    brand_ok=bool(same or priv or brand_policy in {"SAME","SAME_INFERRED","PRIVATE","COMMODITY"})
    commodity=brand_policy=="COMMODITY"
    qsim=float(best.get("quantity_similarity",-1.0))
    q_known=qsim>=0
    q_ok=(qsim>=float(t.get("min_quantity_similarity",.72))) if q_known else True
    rank=int(best.get("retrieval_rank",999))
    asym=int(best.get("modifier_asymmetry_count",0))

    # Strongest deterministic lanes. Same-brand national products and private-label
    # equivalents are allowed at materially lower textual overlap because retailer
    # titles routinely add/drop marketing words.
    rank_ok=(rank==1)
    match_same=((same or brand_policy in {"SAME","SAME_INFERRED"}) and rank_ok
                and raw>=float(t.get("same_brand_min_raw",.52))
                and fs>=float(t.get("same_brand_min_functional",.82))
                and ps>=float(t.get("same_brand_min_support",.10))
                and margin>=float(t.get("same_brand_min_margin",.01))
                and pe>=3 and ia>=2 and q_ok and asym==0
                and (q_known or (raw>=float(t.get("unknown_quantity_min_raw",.60))
                                 and fs>=float(t.get("unknown_quantity_min_functional",.90))
                                 and margin>=float(t.get("unknown_quantity_min_margin",.03))
                                 and pe>=4)))
    match_private=(priv and rank_ok and raw>=float(t.get("private_min_raw",.52))
                   and fs>=float(t.get("private_min_functional",.88))
                   and ps>=float(t.get("private_min_support",.10))
                   and margin>=float(t.get("private_min_margin",.015))
                   and pe>=3 and ia>=2 and q_ok and asym==0
                   and (q_known or (raw>=.58 and fs>=.92 and margin>=.03)))
    match_general=(commodity and rank_ok and raw>=float(t.get("general_min_raw",.68))
                   and fs>=float(t.get("general_min_functional",.90))
                   and ps>=float(t.get("general_min_support",.20))
                   and margin>=float(t.get("general_min_margin",.03))
                   and pe>=4 and ia>=2 and q_ok and asym==0)

    exact_lane=(exact_title and brand_ok and asym==0 and (not q_known or q_ok))
    if not best["hard_veto"] and brand_policy!="CONFLICT" and nm<=float(t.get("match_max_nonmatch_confidence",.35)) and (exact_lane or match_same or match_private or match_general):
        verdict="MATCH";reason="AGGRESSIVE_MATCH_LANE"
    else:
        # A clearly weak best candidate is a normal NON_MATCH, not an abstention.
        all_veto=all(bool(r["hard_veto"]) for r in rows)
        weak=(raw<float(t.get("nonmatch_raw_below",.40))
              or ps<float(t.get("nonmatch_support_below",.055))
              or fs<float(t.get("nonmatch_functional_below",.38)))
        contrad=best["hard_veto"] or brand_policy=="CONFLICT" or nm>=float(t.get("nonmatch_confidence_above",.70))
        # The middle band is the only place where GPT/human review is justified.
        review_band=(not contrad and not weak
                     and raw>=float(t.get("review_raw_min",.46))
                     and raw<=float(t.get("review_raw_max",.78))
                     and fs>=float(t.get("review_functional_min",.52))
                     and ps>=float(t.get("review_support_min",.08))
                     and (rank<=3 or margin>=.04))
        if all_veto or contrad or weak:
            verdict="NON_MATCH";reason="CLEAR_NONMATCH"
        elif review_band:
            verdict="REVIEW";reason="NARROW_DECISION_BAND"
        else:
            # Outside the ambiguity band, resolve using the stronger side rather than
            # defaulting to REVIEW. This is intentionally more aggressive than v5.
            if brand_ok and rank==1 and raw>=.62 and fs>=.88 and q_known and q_ok and nm<.35 and asym==0 and margin>=.03:
                verdict="MATCH";reason="BEST_EVIDENCE_MATCH"
            else:
                verdict="NON_MATCH";reason="BEST_EVIDENCE_NONMATCH"

    guess="MATCH" if (not best["hard_veto"] and ps>=.10 and fs>=.50) else "NON_MATCH"
    return {
        "item_id_A":best["item_id_A"],
        "selected_item_id_B":best["item_id_B"] if verdict=="MATCH" else "",
        "best_candidate_item_id_B":best["item_id_B"],
        "best_candidate_name_A":best.get("name_A",""),
        "best_candidate_name_B":best.get("name_B",""),
        "final_verdict":verdict,
        "manual_review_required":verdict=="REVIEW",
        "match_confidence":round(best["match_confidence"],6),
        "nonmatch_confidence":round(best["nonmatch_confidence"],6),
        "raw_match_evidence_score":best["match_evidence_score"],
        "raw_nonmatch_evidence_score":best["nonmatch_evidence_score"],
        "hard_veto":best["hard_veto"],
        "hard_veto_rules":"|".join(best["hard_veto_rules"]),
        "functional_identity_similarity":best["functional_identity_similarity"],
        "identity_anchor_count":best["identity_anchor_count"],
        "strong_support_count":best["strong_support_count"],
        "positive_evidence_count":best["atomic_positive_evidence_count"],
        "conflict_count":best["atomic_conflict_count"],
        "pair_support_score":round(best["pair_support_score"],6),
        "runner_up_item_id_B":second["item_id_B"] if second else "",
        "runner_up_support_score":round(second_score,6),
        "runner_up_margin":round(margin,6),
        "retrieval_rank_of_best":best["retrieval_rank"],
        "candidate_count":len(rows),
        "modifier_asymmetry_count":asym,
        "same_brand":same,
        "private_label_pair":priv,
        "brand_policy":brand_policy,
        "quantity_similarity":qsim,
        "educated_guess":guess,
        "educated_guess_item_id_B":best["item_id_B"],
        "decision_reason":reason,
    }
