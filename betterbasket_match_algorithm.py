#!/usr/bin/env python3
"""Independent BetterBasket MATCH evidence algorithm."""
from __future__ import annotations
import math
from itertools import combinations
from betterbasket_product_evidence import (
    value_positive_similarity, criticality_weight, applicable, fact_quality_pair
)

def _mcluster(n):
    if n in {"net_weight","net_volume","normalized_total_quantity"}:return "net_quantity"
    if n in {"declared_count","pack_structure","component_piece_count"}:return "pack_count"
    if n in {"product_family","canonical_category"}:return "broad_product_family"
    if n in {"functional_name","canonical_subcategory"}:return "functional_identity"
    if n in {"brand","sub_brand","product_line"}:return "brand_identity"
    if n in {"storage_state","physical_state","preparation_state"}:return "state_form"
    if n in {"flavor","variant_name"}:return "flavor_variant"
    if n in {"scent","variant_name"}:return "scent_variant"
    if n in {"active_ingredient","active_strength","dosage_form","route_of_administration","release_type"}:return "medicine_identity"
    if n in {"light_bulb_base","light_bulb_shape","light_output_lumens","light_color_temperature","light_watt_equivalent"}:return "bulb_identity"
    return n

def _pth(n):
    if n in {"functional_name","ingredient_signature"}:return .35
    if n=="model_or_part_identifier":return .55
    if n=="canonical_subcategory":return .45
    if n in {"net_weight","net_volume","declared_count","pack_structure","normalized_total_quantity",
             "active_strength","spf","alcohol_strength_abv","electrical_voltage","wattage",
             "storage_capacity","paper_ply","rated_use_count","light_output_lumens",
             "light_color_temperature","light_watt_equivalent","dimension_signature"}:return .88
    return .70

def _mweight(a):
    w=criticality_weight(a.get("criticality"))
    if a["name"]=="brand_type":w=min(w,1.5)
    if a["name"] in {"product_family","canonical_category"}:w=min(w,1.5)
    if a["group"]=="routing":w=min(w,1.0)
    if a["name"] in {"billed_by_weight","ordered_by_weight","variable_weight_flag"}:w=min(w,.5)
    return w

def match_score(fa,fb,criteria):
    best={}
    for a in criteria["attributes"]:
        n=a["name"]
        if not applicable(a,fa,fb):continue
        A,B=fa.get(n),fb.get(n)
        if not A or not B:continue
        if isinstance(A.value,bool) and isinstance(B.value,bool) and not A.value and not B.value:continue
        sim=value_positive_similarity(n,A.value,B.value);th=_pth(n)
        if sim<th:continue
        q=fact_quality_pair(A,B);w=_mweight(a)
        strength=max(.20,min(1,(sim-th)/(1-th) if th<1 else 1))
        e={"attribute":n,"group":a["group"],"similarity":sim,"quality":q,"weight":w,
           "contribution":w*q*strength,"cluster":_mcluster(n)}
        if e["cluster"] not in best or e["contribution"]>best[e["cluster"]]["contribution"]:best[e["cluster"]]=e
    atom=list(best.values());mass=sum(x["contribution"] for x in atom)
    inter=0.0
    for x,y in combinations(atom,2):
        if x["cluster"]==y["cluster"]:continue
        s=.06*math.sqrt(x["weight"]*y["weight"])*min(x["similarity"],y["similarity"])*min(x["quality"],y["quality"])
        if s>=.06:inter+=s
    inter=min(inter,.25*mass);total=mass+inter
    groups={x["group"] for x in atom}
    ids={"brand","functional_name","product_family","model_or_part_identifier","canonical_category","canonical_subcategory"}
    anchors=sum(x["attribute"] in ids for x in atom)
    strong=sum(x["weight"]>=3.5 for x in atom)
    fnA,fnB=fa.get("functional_name"),fb.get("functional_name")
    fs=value_positive_similarity("functional_name",fnA.value,fnB.value) if fnA and fnB else 0.0
    moA,moB=fa.get("model_or_part_identifier"),fb.get("model_or_part_identifier")
    ms=value_positive_similarity("model_or_part_identifier",moA.value,moB.value) if moA and moB else 0.0
    score=(1-math.exp(-total/10))*(.45+.20*min(1,len(groups)/4)+.20*min(1,anchors/2)+.15*min(1,strong/3))
    gate=.15+.85*max(0,min(1,(fs-.20)/.75))
    if ms>=.90:gate=max(gate,.95)
    score*=gate
    if fs<.45 and ms<.90:score=min(score,.45)
    elif fs<.65 and ms<.90:score=min(score,.62)
    elif fs<.75 and ms<.90:score=min(score,.78)
    ba=fa.get("brand");bb=fb.get("brand");ta=fa.get("brand_type");tb=fb.get("brand_type")
    bav=ba.value if ba else "";bbv=bb.value if bb else ""
    tav=ta.value if ta else "";tbv=tb.value if tb else ""
    same_brand=bool(bav and bbv and bav==bbv)
    private_pair=bool(tav=="private_label" and tbv=="private_label")
    national_pair=bool(tav=="national_or_manufacturer" and tbv=="national_or_manufacturer")
    qa,qb=fa.get("normalized_total_quantity"),fb.get("normalized_total_quantity")
    qsim=-1.0
    if qa and qb:
        av,bv=qa.value,qb.value
        if isinstance(av,(list,tuple)) and isinstance(bv,(list,tuple)) and len(av)==2 and len(bv)==2 and av[0]==bv[0]:
            qsim=value_positive_similarity("normalized_total_quantity",av,bv)
    return {
        "match_evidence_score":round(max(0,min(1,score)),6),
        "functional_identity_similarity":round(fs,6),
        "model_identifier_similarity":round(ms,6),
        "atomic_positive_evidence_count":len(atom),
        "identity_anchor_count":anchors,
        "strong_support_count":strong,
        "same_brand":same_brand,
        "private_label_pair":private_pair,
        "national_brand_pair":national_pair,
        "quantity_similarity":round(qsim,6),
    }
