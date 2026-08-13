#!/usr/bin/env python3
"""Independent BetterBasket NON_MATCH evidence algorithm."""
from __future__ import annotations
import math
from itertools import combinations
from betterbasket_product_evidence import (
    value_positive_similarity, criticality_weight, applicable, fact_quality_pair,
    values_conflict, VETO_BY_ATTR, DIET_ATTRS
)

def _ncluster(n):
    if n in {"net_weight","net_volume","normalized_total_quantity"}:return "net_quantity"
    if n in {"product_family","canonical_category"}:return "broad_product_family"
    if n in {"functional_name","canonical_subcategory"}:return "functional_identity"
    if n in {"flavor","variant_name"}:return "flavor_variant"
    if n in {"storage_state","physical_state","preparation_state"}:return "state_form"
    if n in {"active_ingredient","active_strength","dosage_form","route_of_administration","release_type"}:return "medicine_identity"
    return n

def _hard_veto(n,a,q,severity,fa,fb):
    if q<.84 or severity<.55:return False
    if n in {"canonical_category","product_family","functional_name","canonical_subcategory","model_or_part_identifier"}:return False
    if n=="brand":
        ta,tb=fa.get("brand_type"),fb.get("brand_type")
        if ta and tb and ta.value=="private_label" and tb.value=="private_label":return False
        return bool(ta and tb and ta.value=="national_or_manufacturer" and tb.value=="national_or_manufacturer")
    if n in DIET_ATTRS:return False
    return n in VETO_BY_ATTR

def nonmatch_score(fa,fb,criteria):
    best={};veto=set()
    for a in criteria["attributes"]:
        n=a["name"]
        if not applicable(a,fa,fb):continue
        A,B=fa.get(n),fb.get(n)
        if not A or not B:continue
        if n=="canonical_subcategory":continue
        if n in {"net_weight","net_volume"} and fa.get("normalized_total_quantity") and fb.get("normalized_total_quantity"):continue
        if n in {"product_family","canonical_category"}:
            fnA,fnB=fa.get("functional_name"),fb.get("functional_name")
            if A.source!="title_family_rules" or B.source!="title_family_rules":continue
            if fnA and fnB and value_positive_similarity("functional_name",fnA.value,fnB.value)>=.45:continue
        if n=="brand":
            ta,tb=fa.get("brand_type"),fb.get("brand_type")
            if ta and tb and ta.value=="private_label" and tb.value=="private_label":continue
        conflict,severity=values_conflict(n,A.value,B.value)
        if not conflict:continue
        q=fact_quality_pair(A,B)
        if q<.72:continue
        w=criticality_weight(a.get("criticality"));e={"attribute":n,"group":a["group"],"severity":severity,
            "quality":q,"weight":w,"contribution":w*q*max(.25,severity),"cluster":_ncluster(n)}
        if e["cluster"] not in best or e["contribution"]>best[e["cluster"]]["contribution"]:best[e["cluster"]]=e
        if _hard_veto(n,a,q,severity,fa,fb):veto.add(VETO_BY_ATTR[n])

    def vals(f):
        if not f:return set()
        v=f.value
        if isinstance(v,(list,set,tuple)):return {str(x).lower() for x in v}
        return {str(v).lower()}
    aw,bw=vals(fa.get("washable_longwear_status")),vals(fb.get("washable_longwear_status"))
    ap,bp=vals(fa.get("waterproof_status")),vals(fb.get("waterproof_status"))
    if (("washable" in aw and "waterproof" in bp and "waterproof" not in ap and "washable" not in bw) or
        ("washable" in bw and "waterproof" in ap and "waterproof" not in bp and "washable" not in aw)):
        best["water_resistance_variant"]={"attribute":"washable_vs_waterproof_variant","group":"health_personal_care",
            "severity":1,"quality":.94,"weight":7,"contribution":6.58,"cluster":"water_resistance_variant"}
        veto.add("V065")

    # Cross-field package count comparison: retailer feeds often encode the same concept
    # as pack_structure on one side and declared_count on the other.
    def num(f):
        try:return float(f.value) if f else None
        except:return None
    ac=num(fa.get("pack_structure"));bc=num(fb.get("declared_count"))
    ad=num(fa.get("declared_count"));bd=num(fb.get("pack_structure"))
    for x,y in ((ac,bc),(ad,bd)):
        if x is not None and y is not None and x>1 and y>1 and abs(x-y)>1e-9:
            best["cross_pack_count"]={"attribute":"cross_pack_count","group":"quantity_packaging",
                "severity":1.0,"quality":.94,"weight":7.0,"contribution":6.58,"cluster":"pack_count"}
            veto.add("V009")
            break

    modifier_attrs={"flavor","scent","shade","waterproof_status","washable_longwear_status",
                    "meat_species","meat_cut","produce_species","protein_source","sugar_status",
                    "caffeine_status","coffee_format","dosage_form","release_type","spf",
                    "pet_species","pet_life_stage","formula_base","diaper_size","absorbency",
                    "refill_vs_starter_kit","product_line",
                    "light_bulb_base","light_bulb_shape","model_or_part_identifier",
                    "bundle_flag","assortment_flag"}
    asym=[]
    for n in modifier_attrs:
        A,B=fa.get(n),fb.get(n)
        if bool(A) != bool(B):asym.append(n)

    # One-sided EXPLICIT multipack is unsafe, while inferred single/unknown packaging
    # should not penalize otherwise identical listings.
    pa,pb=fa.get("pack_structure"),fb.get("pack_structure")
    pav=float(pa.value) if pa else None;pbv=float(pb.value) if pb else None
    pa_exp=bool(pa and pa.source=="explicit_multipack" and pav>1)
    pb_exp=bool(pb and pb.source=="explicit_multipack" and pbv>1)
    if pa_exp != pb_exp:asym.append("explicit_multipack")

    # Partial flavor overlap is not a hard contradiction, but it is unsafe for automatic
    # matching: strawberry+chocolate is not automatically the same SKU as strawberry.
    def _vset(f):
        if not f:return set()
        v=f.value
        if isinstance(v,(list,set,tuple)):return {str(x).lower() for x in v}
        return {str(v).lower()}
    aflv,bflv=_vset(fa.get("flavor")),_vset(fb.get("flavor"))
    if aflv and bflv and aflv!=bflv and not aflv.isdisjoint(bflv):
        choc={"chocolate","milk chocolate","dark chocolate","white chocolate"}
        onlya,onlyb=aflv-bflv,bflv-aflv
        # Generic chocolate versus one specific chocolate style is compatible omission.
        compatible=(onlya|onlyb)<=choc and bool((aflv&choc) and (bflv&choc))
        if not compatible:asym.append("flavor_partial")

    # Organic/non-organic is especially SKU-defining across retailer private labels.
    ta,tb=fa.get("brand_type"),fb.get("brand_type")
    private_pair=bool(ta and tb and ta.value=="private_label" and tb.value=="private_label")
    if private_pair and bool(fa.get("organic_status")) != bool(fb.get("organic_status")):
        asym.append("organic_status_private")

    atom=list(best.values());mass=sum(x["contribution"] for x in atom);inter=0.0
    for x,y in combinations(atom,2):
        if x["cluster"]==y["cluster"]:continue
        s=.07*math.sqrt(x["weight"]*y["weight"])*min(x["severity"],y["severity"])*min(x["quality"],y["quality"])
        if s>=.07:inter+=s
    inter=min(inter,.25*mass);total=mass+inter
    score=(1-math.exp(-total/8))*(.70+.30*min(1,len({x["group"] for x in atom})/3))
    if veto:score=max(score,.88)
    return {"nonmatch_evidence_score":round(max(0,min(1,score)),6),"hard_veto":bool(veto),
            "hard_veto_rules":sorted(veto),"atomic_conflict_count":len(atom),
            "modifier_asymmetry_count":len(asym),"modifier_asymmetries":sorted(asym)}
