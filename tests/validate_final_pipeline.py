"""Fast offline invariants for BetterBasket AUDITED v17 final identity + count-uniqueness guard."""
from __future__ import annotations
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collections import defaultdict
import betterbasket_app as app
import betterbasket_final_certification as polish


def row(key,a,bid='1',b='Generic Product',policy='SAME'):
    return {
        '_row_key_A':str(key),'best_candidate_name_A':a,'best_candidate_name_B':b,
        'selected_item_id_B':str(bid),'best_candidate_item_id_B':str(bid),
        'brand_policy':policy,'pair_support_score':'0.40','functional_identity_similarity':'0.92',
        'runner_up_margin':'0.20','gpt_evidence':'CLEAR','retrieval_rank_of_best':'1',
        'final_verdict':'MATCH'
    }


def bm(name,brand='Brand',description='',size='',item_info='{}'):
    sizing_comp='{}' if not size else '{"size_user_friendly":"'+size+'"}'
    return {'name':name,'brand_raw':brand,'description':description,'item_info':item_info,'sizing_comp':sizing_comp,'tags':''}


def check(name,cond):
    if not cond: raise AssertionError(name)
    print('PASS',name)


def main():
    # Explicit national-brand contradictions.
    r=row(1,'Quaker Yellow Corn Meal 24 oz',b='Quaker Corn Meal, White')
    check('national color contradiction',app._v17_explicit_identity_conflict(r,bm('Quaker Corn Meal, White','Quaker','Quaker White Corn Meal 24 OZ','24 ounce'))=='V17_NATIONAL_COLOR_VARIANT')
    r2=row(2,'Ninja Foodi Air Fryer DZ100',b='Ninja Foodi Air Fryer')
    check('manufacturer model contradiction',app._v17_explicit_identity_conflict(r2,bm('Ninja Foodi Air Fryer','Ninja','DZ090 Ninja Air Fryer 1 EA'))=='V17_NATIONAL_MODEL_PART')
    r3=row(3,'Vicks SpeedRead Thermometer V912',b='Vicks SpeedRead Thermometer')
    check('model suffix compatibility',app._v17_explicit_identity_conflict(r3,bm('Vicks SpeedRead Thermometer','Vicks','V912USV5 Vicks Speed Read Thermometer 1 EA'))=='')
    r4=row(4,'Welch Concord Grape Zero Sugar Drink 64 fl oz',b='Welch Grape Juice')
    check('zero sugar cannot wildcard',app._v17_explicit_identity_conflict(r4,bm('Welch Grape Juice','Welch','Welch 100% Grape Juice','64 fl. oz.'))=='V17_NATIONAL_SUGAR_STATUS_UNRESOLVED')
    r5=row(5,'G Hughes Zero Sugar Ketchup 13 oz',b='G Hughes Sugar Free Ketchup')
    check('zero sugar/sugar free equivalence',app._v17_explicit_identity_conflict(r5,bm('G Hughes Sugar Free Ketchup','G Hughes','G Hughes Sugar Free Ketchup 13 OZ','13 ounce'))=='')
    r6=row(6,'Sunsweet Prunes with Pits 16 oz',b='Sunsweet Pitted Prunes')
    check('pitted contradiction',app._v17_explicit_identity_conflict(r6,bm('Sunsweet Pitted Prunes','Sunsweet','Pitted Prunes 16 OZ','16 ounce'))=='V17_NATIONAL_PIT_VARIANT')
    r7=row(7,'Tweezerman Baby Manicure Kit',b='Tweezerman Manicure Kit')
    check('baby product stage unresolved',app._v17_explicit_identity_conflict(r7,bm('Tweezerman Manicure Kit','Tweezerman','Tweezerman Essential Manicure Kit 1 EA'))=='V17_NATIONAL_LIFE_STAGE_UNRESOLVED')
    r8=row(8,'Badia Organic Cinnamon Powder 2 oz',b='Badia Cinnamon Powder')
    check('organic status unresolved',app._v17_explicit_identity_conflict(r8,bm('Badia Cinnamon Powder','Badia','Badia Cinnamon Powder 2 OZ','2 ounce'))=='V17_NATIONAL_ORGANIC_STATUS_UNRESOLVED')
    r9=row(9,'Campoverde Tropical Mix Sorbet Bar',b='Campoverde Tropical Mix')
    check('dessert versus frozen fruit role',app._v17_explicit_identity_conflict(r9,bm('Campoverde Tropical Mix','Campoverde','Campoverde Tropical Mix Fruit - Frozen 48 OZ','48 ounce'))=='V17_NATIONAL_PRODUCT_ROLE')
    r10=row(10,'Murphy Oil Soap 32 fl oz - 9 / Carton',b='Murphy Oil Soap Wood Cleaner')
    check('outer case configuration',app._v17_explicit_identity_conflict(r10,bm('Murphy Oil Soap Wood Cleaner','Murphy','Murphy Oil Soap 32 FO','32 fl. oz.'))=='V17_NATIONAL_OUTER_CASE')
    r12=row(12,'Head and Shoulders Dandruff Shampoo, Deep Scalp Cleanse, 12.5 fl oz',b='Head & Shoulders Dandruff Shampoo, Dry Scalp Care')
    check('named product-line contradiction',app._v17_explicit_identity_conflict(r12,bm('Head & Shoulders Dandruff Shampoo, Dry Scalp Care','Head & Shoulders','Dry Scalp Care Shampoo 12.5 OZ','12.5 ounce'))=='V17_NATIONAL_NAMED_VARIANT')
    r13=row(13,'Trolli Fruit Punch Sour Brite Crawlers 14 oz',b='Trolli Sour Brite Crawlers')
    check('named flavor cannot wildcard',app._v17_explicit_identity_conflict(r13,bm('Trolli Sour Brite Crawlers','Trolli','Sour Brite Crawlers Family Size 14 OZ','14 ounce'))=='V17_NATIONAL_NAMED_VARIANT_UNRESOLVED')
    r14=row(14,'Kraft Thousand Island Dressing 16 fl oz',b='Kraft Fat Free Thousand Island Dressing')
    check('fat-sensitive national variant',app._v17_explicit_identity_conflict(r14,bm('Kraft Fat Free Thousand Island Dressing','Kraft','Fat Free Thousand Island Dressing 16 FO','16 fl. oz.'))=='V17_NATIONAL_FAT_STATUS_UNRESOLVED')
    r15=row(15,'Sour Patch Kids Candy 8 oz',b='Sour Patch Kids Candy')
    check('incidental fat-free claim not overblocked',app._v17_explicit_identity_conflict(r15,bm('Sour Patch Kids Candy','Sour Patch Kids','Fat free candy 8 OZ','8 ounce'))=='')
    r16=row(16,'Original Donut Shop Coffee 24 Single Serve K-Cup Pods',b='Original Donut Shop K-Cup Pods')
    check('structured pod-count mismatch',app._metadata_pair_block(r16,bm('Original Donut Shop K-Cup Pods','Original Donut Shop','Coffee Capsules','10 ct.'))=='B_METADATA_COUNT')
    r17=row(17,'Everyone Hand Soap Meyer Lemon 2-Packs',b='Everyone Hand Soap Meyer Lemon')
    check('trailing multipack unresolved',app._v17_explicit_identity_conflict(r17,bm('Everyone Hand Soap Meyer Lemon','Everyone','Hand Soap 12.75 FO','12.75 fl. oz.'))=='V17_NATIONAL_MULTIPACK_UNCONFIRMED')
    r18=row(18,'Everyone Hand Soap Refil 1 Each 1-32 Fz',b='Everyone Hand Soap Meyer Lemon')
    check('retail fz size normalization',app._metadata_pair_block(r18,bm('Everyone Hand Soap Meyer Lemon','Everyone','Hand Soap 12.75 FO','12.75 fl. oz.'))=='B_METADATA_SIZE')
    # Private-label equivalents must not be subjected to national exact-SKU guard.
    rp=row(11,'Great Value Organic Tomato Sauce 8 oz',b='Wegmans Organic Tomato Sauce',policy='PRIVATE')
    check('private-label policy preserved',app._v17_explicit_identity_conflict(rp,bm('Wegmans Organic Tomato Sauce','Wegmans','Organic Tomato Sauce 8 OZ','8 ounce'))=='')
    # Generic national B cannot stand in for several incompatible explicit variants.
    rs=[row(20,'Brand Vanilla Product',bid='9'),row(21,'Brand Chocolate Product',bid='9'),row(22,'Brand Strawberry Product',bid='9')]
    g=defaultdict(list)
    for x in rs:g['9'].append(x)
    bad=app._catalog_collision_downgrades_v17(g,{'9':bm('Brand Product','Brand','Brand Product')})
    check('many-to-one variant collision',len(bad)>=2)

    # Frozen final-polish invariant: one national-brand B SKU cannot represent
    # incompatible explicit package counts simultaneously.
    cr=[row(30,'Kraft Singles American Slices, 16 Count Pack',bid='77',b='Kraft Singles American Cheese Slices'),
        row(31,'Kraft Singles American Slices, 24 Count Pack',bid='77',b='Kraft Singles American Cheese Slices')]
    cbad={}
    polish._collision_reasons(cr,{'77':bm('Kraft Singles American Cheese Slices','Kraft')},cbad)
    check('count-opaque B conflicting counts deferred',set(cbad)=={'30','31'} and set(cbad.values())=={'POLISH_COLLISION_AMBIGUOUS_COUNT'})

    cbad={}
    polish._collision_reasons(cr,{'77':bm('Kraft Singles American Cheese Slices','Kraft',size='16 ct.')},cbad)
    check('B-confirmed count keeps matching count only',set(cbad)=={'31'} and cbad['31']=='POLISH_COLLISION_COUNT_MISMATCH_TO_B')

    same=[row(32,'Mission Super Soft Yellow Corn Tortillas, 24 Count',bid='78',b='Mission Super Soft Yellow Corn Tortillas'),
          row(33,'Mission Super Soft Yellow Corn Tortillas, 24 Count (Pack of 1)',bid='78',b='Mission Super Soft Yellow Corn Tortillas')]
    cbad={}
    polish._collision_reasons(same,{'78':bm('Mission Super Soft Yellow Corn Tortillas','Mission')},cbad)
    check('duplicate same-count listings preserved',not cbad)
    print('v17_guard_validation_complete=true')

if __name__=='__main__':main()
