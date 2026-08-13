#!/usr/bin/env python3
"""Final precision certification for a completed BetterBasket run.

This script DOES NOT rerun retrieval, deterministic scoring, or GPT.  It reads
only the completed all_verdicts.csv plus the already-built Store-B SQLite detail
cache, rechecks the small certified MATCH set, and demotes high-risk exact-SKU
collisions to REVIEW, including incompatible national-brand package counts
that collapse onto one Store-B SKU.  Runtime is O(final matches), not O(all 233k products).
"""
from __future__ import annotations
import argparse, csv, json, os, re, shutil, sys, time
from collections import Counter, defaultdict
from pathlib import Path

BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE))
import betterbasket_app as bb

CONTAINER_NOUNS=r'(?:bottles?|boxes?|cans?|packets?|pouches?|tins?|cups?)'
COUNT_NOUNS=(r'tests?',r'k[- ]?cup\s+pods?',r'(?:keurig\s+)?k[- ]?cup\s+pods?',r'pods?',r'bottles?',r'packets?',r'wipes?',r'rolls?',r'lighters?',r'pads?')

def _text(x): return '' if x is None else str(x)
def _f(x,d=0.0):
    try:return float(x)
    except:return d

def _row_bid(row):
    x=_text(row.get('selected_item_id_B','')).strip()
    if not x:return ''
    if re.fullmatch(r'\d+\.0',x):return x[:-2]
    return x

def _bfull(bm):
    return ' '.join((_text(bm.get('name','')),_text(bm.get('description',''))[:1200],_text(bm.get('item_info',''))[:400]))

def _pkg_count(s):
    """Best explicit sellable-unit count from a title; avoids serving-size math."""
    z=_text(s).lower().replace('×','x')
    m=re.search(r'\b(\d+)\s*\+\s*(\d+)\s*(?:ct|count)\b',z)
    if m:return int(m.group(1))+int(m.group(2))
    ms=list(re.finditer(r'\b(\d+)\s*(?:ct|count)\b',z))
    if ms:return int(ms[0].group(1))
    ms=list(re.finditer(r'\b(\d+)\s+(?:(?:individually|individual|wrapped)\s+){0,2}packs?\b',z))
    if ms:return int(ms[0].group(1))
    m=re.search(r'\bpack\s+of\s+(\d+)\b',z)
    if m:return int(m.group(1))
    for noun in COUNT_NOUNS:
        m=re.search(r'\b(\d+)\s+'+noun+r'\b',z)
        if m:return int(m.group(1))
    return None

def _structured_json(bm):
    try:return json.loads(_text((bm or {}).get('sizing_comp','')) or '{}')
    except:return {}

def _b_count(bm):
    n=_pkg_count((bm or {}).get('name',''))
    if n:return n
    z=bb._bsize(bm).lower().replace('×','x')
    # Structured B sizing frequently expresses retail multipacks as "8 x 20 fl oz".
    m=re.search(r'\b([2-9]\d*)\s*x\s*\d+(?:\.\d+)?\s*(?:fl\.?\s*oz|oz|ml|l|g|kg|lb|lbs)\b',z)
    if m:return int(m.group(1))
    m=re.search(r'\b(\d+)\s*(?:ct|count)\b',z)
    if m:return int(m.group(1))
    # N each with N>1 is an actual package count.  "1 each" is usually one box/set.
    m=re.search(r'\b([2-9]\d*)\s*(?:each|ea)\b',z)
    if m:return int(m.group(1))
    d=_text((bm or {}).get('description',''))[:450]
    for pat in (r'\b(\d+)\s*[- ]?pack\b',r'\bpack\s+of\s+(\d+)\b',r'\bcontains\s+(\d+)\s+(?:individually\s+)?(?:wrapped\s+)?(?:packs?|sandwiches?|bars?|bottles?|tests?)\b'):
        m=re.search(pat,d,re.I)
        if m:return int(m.group(1))
    return None

def _b_confirms_count(bm,n):
    if not n:return False
    if _b_count(bm)==n:return True
    z=_text((bm or {}).get('name',''))+' '+_text((bm or {}).get('description',''))[:850]
    pats=(
      r'\b'+str(n)+r'\s*[- ]?(?:ct|count|pack|pk)\b',
      r'\b'+str(n)+r'\s+(?:(?:individually|individual|wrapped|single[- ]serve|single)\s+){0,3}(?:packs?|bars?|cups?|sandwiches?|packets?|pods?|bottles?|cans?|pouches?|servings?|tests?|wipes?|rolls?)\b',
      r'\bcontains\s+'+str(n)+r'\s+(?:[^.;]{0,35}\s+)?(?:packs?|bars?|cups?|sandwiches?|packets?|pods?|bottles?|cans?|pouches?|servings?|tests?)\b',
    )
    if any(re.search(p,z,re.I) for p in pats):return True
    # Scraped descriptions often say e.g. "includes 4 total baby food pouches".
    if re.search(r'\b(?:includes?|contains?)\s+'+str(n)+r'\b.{0,70}\b(?:packs?|bars?|cups?|sandwiches?|packets?|pods?|bottles?|cans?|pouches?|servings?|tests?|wraps?|pieces?)\b',z,re.I):return True
    sj=_structured_json(bm);sv=sj.get('num_servings_nutrition')
    try:sv=float(sv)
    except:sv=None
    # Servings are usable as a package-count backstop only when the current B item
    # is explicitly a set of discrete singles (not for arbitrary food servings).
    if sv is not None and abs(sv-n)<1e-9 and re.search(r'\b(?:singles|single[- ]serve|packets?|pods?|cups?|sandwiches?|bars?)\b',z,re.I):return True
    return False

def _first_amount(s):
    z=_text(s).lower().replace('×','x')
    specs=(
      ('vol',r'(\d+(?:\.\d+)?)\s*(?:fl\.?\s*oz\.?|fluid\s*ounces?)',29.5735295625),
      ('vol',r'(\d+(?:\.\d+)?)\s*(?:ml|milliliters?)\b',1.0),
      ('vol',r'(\d+(?:\.\d+)?)\s*(?:l|liters?|litres?)\b',1000.0),
      ('mass',r'(\d+(?:\.\d+)?)\s*(?:oz\.?|ounce|ounces)\b',28.349523125),
      ('mass',r'(\d+(?:\.\d+)?)\s*(?:lb|lbs|pounds?)\b',453.59237),
      ('mass',r'(\d+(?:\.\d+)?)\s*(?:kg|kilograms?)\b',1000.0),
      ('mass',r'(\d+(?:\.\d+)?)\s*(?:g|grams?)\b',1.0),
    )
    hits=[]
    for dim,pat,mul in specs:
        for m in re.finditer(pat,z):
            if dim=='mass' and re.match(r'\s*(?:of\s+)?(?:protein|sugar|carbs?|fiber|fat)\b',z[m.end():m.end()+24]):continue
            hits.append((m.start(),dim,float(m.group(1))*mul,m.start(),m.end()))
    return min(hits,key=lambda x:x[0]) if hits else None

def _package_net(s):
    """(dimension,total,displayed,mode). Only multiplies unambiguous outer packs."""
    z=_text(s).lower().replace('×','x');h=_first_amount(s)
    if not h:return None
    _,dim,raw,st,en=h
    m=re.search(r'\b([2-9]\d*)\s*x\s*\d+(?:\.\d+)?\s*(?:fl\.?\s*oz|fluid\s*ounces?|oz|ounce|ounces|ml|l|g|kg|lb|lbs)\b',z)
    if m:return dim,raw*int(m.group(1)),raw,'mult'
    m=re.search(r'^\s*\(?\s*([2-9]\d*)\s*(?:pack|pk)\b',z)
    if m:return dim,raw*int(m.group(1)),raw,'mult'
    # Explicit leading physical-container multipack: "(6 Bottles) ... 375 mL".
    m=re.search(r'^\s*\(?\s*([2-9]\d*)\s*(?:bottles?|cans?|tins?|pouches?|packets?)\b',z)
    if m:return dim,raw*int(m.group(1)),raw,'mult'
    n=_pkg_count(s)
    if n and n>1:
        unitpat=r'(?:fl\.?\s*oz\.?|fluid\s*ounces?|oz\.?|ounce|ounces|ml|l|g|kg|lb|lbs)'
        # "12 ct pack, 20 oz Bottles" / "4 ct Pack, 3 oz Boxes".
        if re.search(r'\b'+str(n)+r'\s*(?:ct|count)?\s*(?:pack|pk)\b.{0,55}?\d+(?:\.\d+)?\s*'+unitpat+r'\b.{0,30}?\b(?:bottles?|boxes?|cans?|tins?|pouches?|packets?)\b',z):
            return dim,raw*n,raw,'perunit'
        # "13.7 fl oz, 12 Bottles" (no explicit word Count).
        tail=z[en:en+100]
        if re.search(r'\b'+str(n)+r'\s+(?:bottles?|cans?|tins?|pouches?)\b',tail):
            return dim,raw*n,raw,'perunit'
    if n and n>1:
        # A net size followed by a counted physical container: "16 fl oz, 12 count bottle"
        after=z[en:en+120];cm=re.search(r'\b'+str(n)+r'\s*(?:ct|count)\b',after)
        if cm:
            around=after[:min(len(after),cm.end()+35)]
            if re.search(r'\b'+CONTAINER_NOUNS+r'\b',around):return dim,raw*n,raw,'perunit'
        # Or the container directly precedes its per-unit size: "tins 1.5 oz, 2 count"
        before=z[max(0,st-35):st]
        if re.search(r'\b'+CONTAINER_NOUNS+r'\b[\s,:;/-]*$',before) and not re.search(r'^\s*(?:bag|box|carton)\b',z[en:en+28]):return dim,raw*n,raw,'perunit'
    return dim,raw,raw,'package'

def _b_net(bm):
    q=_package_net(bb._bsize(bm))
    if q:return q[0],q[1]
    d=_text((bm or {}).get('description',''))[:900].lower()
    # Example: "This box contains 20 individually portioned, 1-ounce ... packs."
    m=re.search(r'contains\s+(\d+)\s+(?:individually\s+)?(?:portioned\s*)?,?\s*([0-9]+(?:\.[0-9]+)?)\s*[- ]?(?:ounce|oz)\b',d)
    if m:return 'mass',int(m.group(1))*float(m.group(2))*28.349523125
    m=re.search(r'\b(\d+)\s+(?:individually\s+)?(?:wrapped\s+)?(?:packs?|cups?|bottles?).{0,60}?\b([0-9]+(?:\.[0-9]+)?)\s*[- ]?(?:ounce|oz)\b',d)
    if m:return 'mass',int(m.group(1))*float(m.group(2))*28.349523125
    return None

def _near(a,b,t=.08):return abs(a-b)<=t*max(abs(a),abs(b),1e-9)

def _a_has_container_count(a,n):
    """True only when the count clearly enumerates retail containers, not pieces."""
    if not n:return False
    z=_text(a).lower()
    # "12 count bottles", "4 count cans", "4 pouches", etc.
    if re.search(r'\b'+str(n)+r'\s*(?:ct|count)?\s*(?:bottles?|cans?|pouches?|tins?|packets?)\b',z):return True
    if re.search(r'\b'+str(n)+r'\s+(?:bottles?|cans?|pouches?|tins?|packets?)\b',z):return True
    # Plural physical container directly carries the displayed per-unit size:
    # "tins 1.5 oz, 2 count", "8 fl oz milk boxes, 12 count".
    h=_first_amount(a)
    if h:
        _,_,_,st,en=h
        around=z[max(0,st-45):min(len(z),en+45)]
        if re.search(r'\b(?:bottles|cans|pouches|tins|packets|boxes)\b',around):return True
    return False

def _b_singular_unit(bm):
    name=' '+bb._c_norm((bm or {}).get('name',''))+' '
    # Clear single retail units. Avoid internal-piece words such as egg/waffle
    # unless the B title itself is plainly a single sellable unit.
    if re.search(r'\b(?:bar|pouch|bottle|can|tin|packet|spray)\b',name) and not re.search(r'\b(?:bars|pouches|bottles|cans|tins|packets|sprays)\b',name):return True
    if re.search(r'\bwaffle\b',name) and not re.search(r'\b(?:waffle (?:bowls|cones|bites)|waffles)\b',name):return True
    if re.search(r'\bcreme egg\b',name) and not re.search(r'\beggs\b',name):return True
    return False

def _single_item_b(bm):
    txt=_text((bm or {}).get('name',''))+' '+_text((bm or {}).get('description',''))[:420]+' '+_text((bm or {}).get('item_info',''))[:300]
    return bool(re.search(r'\bsingle[- ]serve\b|\bsingle\s+serve\b|"category_2"\s*:\s*"Single Serve"',txt,re.I))

def _pregnancy_count(a):
    if not re.search(r'\bpregnancy\s+test\b',_text(a),re.I):return None
    m=re.search(r'\b(\d+)\s*(?:tests?|ea|each)\b',_text(a),re.I)
    return int(m.group(1)) if m else None

def _direct_reason(row,bm):
    a=_text(row.get('best_candidate_name_A',''));bn=_text((bm or {}).get('name',''));bf=_bfull(bm);bp=_text(row.get('brand_policy',''))
    ac=_pkg_count(a);bc=_b_count(bm)
    pc=_pregnancy_count(a)
    if pc is not None and bc and pc!=bc:return f'POLISH_PREGNANCY_COUNT_{pc}_VS_{bc}'
    if ac and bc and ac!=bc:return f'POLISH_COUNT_MISMATCH_{ac}_VS_{bc}'

    if bp in {'SAME','SAME_INFERRED'}:
        # Inverse outer-pack mismatch: A is a clearly single inner unit while
        # structured B is explicitly N x the same unit (e.g. one 20 fl oz
        # Powerade bottle versus an 8 x 20 fl oz B pack).
        bsz=bb._bsize(bm).lower().replace('×','x'); bpack=_package_net(bsz); apack=_package_net(a)
        if not ac and bpack and apack and bpack[3]=='mult' and apack[0]==bpack[0] and _near(apack[1],bpack[2],.06) and not _near(apack[1],bpack[1],.10):
            return 'POLISH_B_MULTIPACK_VS_A_SINGLE'
        aq,bq=_package_net(a),_b_net(bm)
        # Unambiguous N×size packaging versus one B inner unit.
        if aq and bq and aq[0]==bq[0] and aq[3]=='mult' and _near(aq[2],bq[1],.06) and not _near(aq[1],bq[1],.08):
            return 'POLISH_OUTER_PACK_NET_MISMATCH'
        # Counted physical containers where B size equals the A per-unit size and
        # B metadata never confirms the same count.
        if ac and ac>1 and not bc and aq and bq and aq[0]==bq[0] and _near(aq[2],bq[1],.06) and not _b_confirms_count(bm,ac):
            if aq[3]=='perunit' or _single_item_b(bm) or _b_singular_unit(bm):
                return 'POLISH_OUTER_COUNT_VS_SINGLE'
        # K-cup counts are exact package identity; Store B exposes a structured count.
        if re.search(r'\bk[- ]?cup\s+pods?\b',a,re.I) and bc:
            m=re.search(r'\b(\d+)\s+(?:(?:keurig\s+)?k[- ]?cup\s+pods?)\b',a,re.I)
            if m and int(m.group(1))!=bc:return f'POLISH_KCUP_COUNT_{m.group(1)}_VS_{bc}'

        # Narrow, evidence-backed SKU variants that survived v17.
        if re.search(r'\bunsweet(?:ened)?\b',a,re.I) and not re.search(r'\bunsweet(?:ened)?\b',bf,re.I):return 'POLISH_NATIONAL_UNSWEET_VARIANT'
        if re.search(r'\bhot\s*(?:&|and)?\s*spicy\b',a,re.I) and not re.search(r'\b(?:hot\s*(?:&|and)?\s*spicy|hot\s+with|hot\s+flavor)\b',bf,re.I):return 'POLISH_NATIONAL_HOT_SPICY_VARIANT'
        if re.search(r'\bmaple\b',a,re.I) and re.search(r'\b(?:sausage|breakfast|biscuit|pancake|waffle)\b',a,re.I) and not re.search(r'\bmaple\b',bf,re.I):return 'POLISH_NATIONAL_MAPLE_VARIANT'
        if re.search(r'\bmushroom(?:s)?\b',a,re.I) and re.search(r'\b(?:sauce|alfredo|pasta)\b',a,re.I) and not re.search(r'\bmushroom(?:s)?\b',bn+' '+_text((bm or {}).get('description',''))[:250],re.I):return 'POLISH_NATIONAL_MUSHROOM_VARIANT'
        if re.search(r'\bgreen\s+tea\b',a,re.I) and re.search(r'\btea\b',a,re.I) and not re.search(r'\bgreen\s+tea\b',bf,re.I) and re.search(r'\b(?:herbal\s+tea|lemon\s+ginger)\b',bf,re.I):return 'POLISH_NATIONAL_TEA_TYPE'
        if re.search(r'\bfresh\b',a,re.I) and re.search(r'\bdentastix\b',a,re.I) and re.search(r'\boriginal\b',bf,re.I):return 'POLISH_NATIONAL_DENTASTIX_FLAVOR'
        if re.search(r'\bstitch\b',a,re.I) and re.search(r'\bmoana\b',bf,re.I):return 'POLISH_NATIONAL_CHARACTER_VARIANT'
        if re.search(r'\bpine\s+wonderland\b',a,re.I) and not re.search(r'\bpine\s+wonderland\b',bf,re.I):return 'POLISH_NATIONAL_SCENT_VARIANT'
        if re.search(r'\bsingle\s+serve\s+packets?\b',a,re.I) and re.search(r'\bjar\b',bf,re.I):return 'POLISH_NATIONAL_CONTAINER_FORMAT'
        if re.search(r'\bwhitening\s+protection\b',a,re.I) and not re.search(r'\bwhitening\s+protection\b',bf,re.I):return 'POLISH_NATIONAL_WHITENING_VARIANT'
        if re.search(r'\b(?:less|reduced|low)\s+sodium\b',a,re.I) and not re.search(r'\b(?:less|reduced|low)\s+sodium\b',bf,re.I):return 'POLISH_NATIONAL_SODIUM_VARIANT'
        if re.search(r'\bbuttermilk\s+ranch\b',a,re.I) and re.search(r'\bclassic\s+ranch\b',bn,re.I):return 'POLISH_NATIONAL_RANCH_VARIANT'
        if re.search(r'\bdeep\s+conditioner\b',a,re.I) and not re.search(r'\bdeep\s+conditioner\b',bn,re.I):return 'POLISH_NATIONAL_DEEP_CONDITIONER_ROLE'
        # Explicit two-sided variant contradictions are safe to block even when
        # the rest of the scraped title is identical.
        mint_variants=('mild mint','arctic mint','radiant mint','cool mint','coolmint','spearmint','wintergreen','peppermint')
        am={x for x in mint_variants if x in a.lower()}; bmints={x for x in mint_variants if x in bf.lower()}
        if am and bmints and am.isdisjoint(bmints):return 'POLISH_NATIONAL_EXPLICIT_MINT_VARIANT'
        if re.search(r'\btan\b',a,re.I) and re.search(r'\bclear\b',bf,re.I) and re.search(r'\bnasal\s+strips?\b',a+' '+bf,re.I):return 'POLISH_NATIONAL_COLOR_VARIANT'
        if re.search(r'\bclear\b',a,re.I) and re.search(r'\btan\b',bf,re.I) and re.search(r'\bnasal\s+strips?\b',a+' '+bf,re.I):return 'POLISH_NATIONAL_COLOR_VARIANT'

    elif bp=='PRIVATE':
        groups=(({'half','halves'},'HALVES'),({'sliced','slice'},'SLICED'),({'diced','dice'},'DICED'),({'whole'},'WHOLE'),({'chunks','chunk'},'CHUNKS'))
        wa=set(bb._c_norm(a).split());wb=set(bb._c_norm(bn).split());ga={lab for g,lab in groups if wa&g};gb={lab for g,lab in groups if wb&g}
        if ga and gb and ga.isdisjoint(gb) and re.search(r'\b(?:pear|pears|peach|peaches|tomato|tomatoes|potato|potatoes|fruit)\b',a+' '+bn,re.I):return 'POLISH_PRIVATE_CUT_FORM'
        if re.search(r'\b(?:98\s*%\s*)?fat[- ]?free\b',a,re.I) and re.search(r'\b(?:soup|sherbet|yogurt|milk|cream|cheese|dressing|mayo|mayonnaise)\b',a,re.I) and not re.search(r'\bfat[- ]?free\b',bf,re.I):return 'POLISH_PRIVATE_FAT_STATUS'
        if re.search(r'\b(?:\d+\s*%\s*)?(?:less|reduced|low)\s+sodium\b',a,re.I) and not re.search(r'\b(?:less|reduced|low)\s+sodium\b',bf,re.I):return 'POLISH_PRIVATE_SODIUM_STATUS'
    return ''

def _collision_reasons(matches,bmeta,bad):
    groups=defaultdict(list)
    for r in matches:
        if _text(r.get('brand_policy','')) in {'SAME','SAME_INFERRED'} and _text(r.get('_row_key_A','')) not in bad:
            groups[_row_bid(r)].append(r)
    for bid,rows in groups.items():
        if not bid or len(rows)<2:continue
        bm=bmeta.get(bid,{});bn=_b_net(bm)

        # FINAL count-uniqueness guard.  A fixed national-brand Store-B item_id
        # cannot be the 16-count and 24-count (or 2-refill and 4-refill) SKU at
        # the same time.  If B metadata confirms exactly one observed count,
        # retain only that count.  If B is count-opaque, defer every conflicting
        # explicitly-counted A row rather than guessing which package B sells.
        counted=[]
        for r in rows:
            n=_pkg_count(r.get('best_candidate_name_A',''))
            if n is not None and n>0:counted.append((r,n))
        distinct=sorted({n for _,n in counted})
        if len(distinct)>1:
            bc=_b_count(bm)
            confirmed=set()
            if bc in distinct:confirmed.add(bc)
            for n in distinct:
                if _b_confirms_count(bm,n):confirmed.add(n)
            if len(confirmed)==1:
                keep=next(iter(confirmed))
                for r,n in counted:
                    if n!=keep:
                        bad.setdefault(_text(r.get('_row_key_A','')),'POLISH_COLLISION_COUNT_MISMATCH_TO_B')
            else:
                for r,n in counted:
                    bad.setdefault(_text(r.get('_row_key_A','')),'POLISH_COLLISION_AMBIGUOUS_COUNT')

        for dim in ('mass','vol'):
            rr=[]
            for r in rows:
                q=_package_net(r.get('best_candidate_name_A',''))
                if q and q[0]==dim:rr.append((r,q))
            if len(rr)<2:continue
            clusters=[]
            for rq in rr:
                v=rq[1][1]
                for c in clusters:
                    if _near(v,c[0][1][1],.08):c.append(rq);break
                else:clusters.append([rq])
            if len(clusters)<2:continue
            if bn and bn[0]==dim:
                for c in clusters:
                    for r,q in c:
                        if not _near(q[1],bn[1],.09):bad.setdefault(_text(r.get('_row_key_A','')),'POLISH_COLLISION_NET_SIZE')
            elif _b_count(bm):
                # One counted B SKU cannot simultaneously be several materially
                # different national-brand net-size SKUs when B supplies no size
                # evidence to choose among them. Defer all such ambiguous sizes.
                for c in clusters:
                    for r,q in c:bad.setdefault(_text(r.get('_row_key_A','')),'POLISH_COLLISION_AMBIGUOUS_SIZE')

def _load_matches(allp):
    out=[]
    with open(allp,newline='',encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            if r.get('final_verdict')=='MATCH':out.append(r)
    return out

def _find_db(root):
    xs=sorted((root/'cache').glob('store_b_details_*.sqlite'))
    xs=[x for x in xs if not x.name.endswith('_factcache.sqlite')]
    if len(xs)!=1:raise SystemExit(f'Expected exactly one Store-B detail SQLite cache under {root/"cache"}; found {len(xs)}')
    return xs[0]

def _backup(root):
    for name in ('all_verdicts.csv','submission_matches.csv','manual_review.csv','run_summary.json'):
        p=root/name;b=root/(p.stem+'.pre_final_polish'+p.suffix)
        if p.exists() and not b.exists():shutil.copy2(p,b)

def polish(root,backup=True,dry_run=False):
    t=time.time();root=Path(root).expanduser().resolve();allp=root/'all_verdicts.csv'
    if not allp.exists():raise SystemExit(f'Missing {allp}')
    db=_find_db(root);matches=_load_matches(allp);bids=sorted({_row_bid(r) for r in matches if _row_bid(r)})
    bmeta=bb._bmeta_load(db,bids);bad={}
    for r in matches:
        k=_text(r.get('_row_key_A',''));why=_direct_reason(r,bmeta.get(_row_bid(r),{}))
        if why:bad[k]=why
    _collision_reasons(matches,bmeta,bad)
    byreason=Counter(bad.values())
    print(f'[v17-polish] inspected {len(matches):,} certified MATCH rows; high-risk demotions={len(bad):,}')
    for k,n in byreason.most_common():print(f'  {k}: {n}')
    if dry_run:return bad
    if backup:_backup(root)
    # demotion audit file before mutating rows
    audit=root/'final_polish_demotions.csv'
    af=['item_id_A','item_id_B','name_A','name_B','reason','prior_decision_reason','gpt_evidence']
    with open(audit,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=af);w.writeheader()
        for r in matches:
            k=_text(r.get('_row_key_A',''))
            if k in bad:w.writerow({'item_id_A':r.get('item_id_A',''),'item_id_B':_row_bid(r),'name_A':r.get('best_candidate_name_A',''),'name_B':r.get('best_candidate_name_B',''),'reason':bad[k],'prior_decision_reason':r.get('decision_reason',''),'gpt_evidence':r.get('gpt_evidence','')})
    sub=root/'submission_matches.csv';rev=root/'manual_review.csv';tmpa=allp.with_suffix('.polish.tmp');tmps=sub.with_suffix('.polish.tmp');tmpr=rev.with_suffix('.polish.tmp');counts=Counter();processed=0
    with open(allp,newline='',encoding='utf-8-sig') as fi:
        rd=csv.DictReader(fi);fields=list(rd.fieldnames or [])
        with open(tmpa,'w',newline='',encoding='utf-8') as fa,open(tmps,'w',newline='',encoding='utf-8') as fs,open(tmpr,'w',newline='',encoding='utf-8') as fr:
            wa=csv.DictWriter(fa,fieldnames=fields,extrasaction='ignore');ws=csv.DictWriter(fs,fieldnames=['item_id_A','item_id_B']);wr=csv.DictWriter(fr,fieldnames=fields,extrasaction='ignore');wa.writeheader();ws.writeheader();wr.writeheader()
            for row in rd:
                k=_text(row.get('_row_key_A',''))
                if row.get('final_verdict')=='MATCH' and k in bad:
                    row['final_verdict']='REVIEW';row['manual_review_required']='True';row['selected_item_id_B']='';row['final_safety_blocker']=bad[k];row['decision_reason']='V17_FINAL_POLISH_REVIEW'
                v=row.get('final_verdict','');wa.writerow(row);processed+=1;counts[v]+=1
                if v=='MATCH':ws.writerow({'item_id_A':row.get('item_id_A',''),'item_id_B':_row_bid(row)})
                elif v=='REVIEW':wr.writerow(row)
    os.replace(tmpa,allp);os.replace(tmps,sub);os.replace(tmpr,rev)
    summary={}
    sp=root/'run_summary.json'
    if sp.exists():
        try:summary=json.loads(sp.read_text(encoding='utf-8'))
        except:summary={}
    summary.update({'products_processed':processed,'counts':dict(counts),'certified_matches':counts['MATCH'],'minimum_required_by_assessment':4000,'minimum_requirement_met':counts['MATCH']>=4000,'v17_final_polish_reviews':len(bad),'v17_final_polish_reason_counts':dict(byreason),'v17_final_polish_runtime_seconds':round(time.time()-t,3),'v17_final_polish_scope':'MATCH-only post-run metadata/catalog certification including national-brand count uniqueness; no retrieval, rescoring, or GPT'})
    sp.write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(f"[v17-polish] final MATCH={counts['MATCH']:,} NON_MATCH={counts['NON_MATCH']:,} REVIEW={counts['REVIEW']:,}")
    print(f'[v17-polish] completed in {time.time()-t:.2f}s; submission rewritten: {sub}')
    return bad

def main():
    ap=argparse.ArgumentParser(description='Precision-only finalizer for a completed BetterBasket v17 output folder.')
    ap.add_argument('output_folder',nargs='?',default=str(BASE.parent),help='Completed v17 output folder (default: package parent).')
    ap.add_argument('--dry-run',action='store_true',help='Audit only; do not rewrite output files.')
    ap.add_argument('--no-backup',action='store_true',help='Do not create .pre_final_polish backups.')
    a=ap.parse_args();polish(a.output_folder,backup=not a.no_backup,dry_run=a.dry_run)
if __name__=='__main__':main()
