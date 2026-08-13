#!/usr/bin/env python3
"""BetterBasket v17 worker: v16 runtime + final exact-SKU identity hardening."""
from __future__ import annotations
import argparse, concurrent.futures as cf, csv, json, os, pickle, re, sqlite3, sys, time, urllib.error, urllib.request
from pathlib import Path
from collections import OrderedDict, Counter

from betterbasket_candidate_retrieval import CompactRetriever, build_index, open_detail_db, brand_index
from betterbasket_product_evidence import load_criteria, extract_facts, fact, Fact, PRIVATE_LABELS
from betterbasket_match_algorithm import match_score
from betterbasket_non_match_algorithm import nonmatch_score
from betterbasket_routing_module import route_group

BASE=Path(__file__).resolve().parent
CRITERIA=BASE/'product_match_criteria_v2_audited.json';CONFIG=BASE/'betterbasket_runtime_config.json'
GENERIC_LEADS={'fresh','organic','pack','value','premium','classic','original','the','a','an','all','new','family','size','frozen'}

def loadj(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def read_csv(p):
    with open(p,newline='',encoding='utf-8-sig') as f:return list(csv.DictReader(f))
def atomic_csv(rows,p,fields=None):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);t=p.with_suffix(p.suffix+'.tmp');rows=list(rows)
    if fields is None:
        fields=[];seen=set()
        for r in rows:
            for k in r:
                if k not in seen:seen.add(k);fields.append(k)
        if not fields:fields=['_row_key_A','item_id']
    with open(t,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
    os.replace(t,p)
def bnorm(x):
    s='' if x is None else str(x)
    if s.lower()=='nan':s=''
    return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9]+',' ',s.lower())).strip()
def btoks(x):return bnorm(x).split()
def related_brand(a,b):
    a,b=bnorm(a),bnorm(b)
    if not a or not b:return False
    ta,tb=set(a.split()),set(b.split());junk={'llc','inc','co','company','foods','food','organic'};ta-=junk;tb-=junk
    return bool(ta and tb and (ta<=tb or tb<=ta or len(ta&tb)/min(len(ta),len(tb))>=.6))
def infer_brand(row,idx):
    r=dict(row)
    if bnorm(r.get('brand_raw')):return r
    n=bnorm(r.get('name'))
    if not n:return r
    # Marketplace listings often put quantity before the real brand, e.g.
    # "(2 pack) Snapple ...", "3X Wish-Bone ...", or "Pack of 4 Goya ...".
    # Strip only a leading quantity wrapper for brand discovery; the original
    # title remains untouched so pack/quantity contradiction logic still sees it.
    lead=n
    for _ in range(2):
        z=re.sub(r'^(?:\d+\s*(?:x|pack|pk)\s+|pack\s+of\s+\d+\s+|\d+\s+pack\s+of\s+)', '', lead).strip()
        if z==lead:break
        lead=z
    # v10 only knew brands present in Store B. That left A-only retailer labels
    # (Great Value, Equate, Marketside, etc.) inside the title core and poisoned
    # rare-token retrieval. Recognize them before candidate generation.
    for p in sorted(PRIVATE_LABELS,key=len,reverse=True):
        bp=bnorm(p)
        if lead==bp or lead.startswith(bp+' '):
            r['brand_raw']=p;r['_brand_inferred']='PRIVATE_LABEL';return r
    if lead:
        for b in idx.get(lead.split()[0],()):
            bb=bnorm(b)
            if lead==bb or lead.startswith(bb+' '):r['brand_raw']=b;r['_brand_inferred']='PACK_PREFIX_BRAND' if lead!=n else 'TITLE_BRAND';break
    return r
def shared_distinctive_phrase(a,b):
    A=btoks(a);B=btoks(b)
    while A and A[0] in GENERIC_LEADS:A=A[1:]
    while B and B[0] in GENERIC_LEADS:B=B[1:]
    if not A or not B:return False
    for n in (3,2):
        if len(A)>=n and ' '+' '.join(A[:n])+' ' in ' '+' '.join(B)+' ':return True
        if len(B)>=n and ' '+' '.join(B[:n])+' ' in ' '+' '.join(A)+' ':return True
    return A[0]==B[0] and A[0] not in GENERIC_LEADS and len(A[0])>=4
def pair_brand_policy(arow,brow,fa,fb):
    ta=fa.get('brand_type');tb=fb.get('brand_type');ba=fa.get('brand');bb=fb.get('brand');tav=ta.value if ta else '';tbv=tb.value if tb else '';av=ba.value if ba else '';bv=bb.value if bb else ''
    if tav=='private_label' and tbv=='private_label':return 'PRIVATE'
    if av and bv:return 'SAME' if related_brand(av,bv) else 'CONFLICT'
    na,nb=arow.get('name',''),brow.get('name','')
    if av and (' '+bnorm(av)+' ') in (' '+bnorm(nb)+' '):return 'SAME_INFERRED'
    if bv and (' '+bnorm(bv)+' ') in (' '+bnorm(na)+' '):return 'SAME_INFERRED'
    commodity=any(fa.get(x) and fb.get(x) for x in ('produce_species','meat_species','seafood_species'))
    return 'COMMODITY' if commodity and not av and not bv else 'UNKNOWN'

def fact_cache_path(db_path):
    p=Path(db_path);return p.with_name(p.stem+'_factcache.sqlite')

def init_fact_cache(db_path):
    p=fact_cache_path(db_path);con=sqlite3.connect(p,timeout=30)
    con.execute('PRAGMA journal_mode=WAL');con.execute('PRAGMA synchronous=NORMAL')
    con.execute('CREATE TABLE IF NOT EXISTS facts(b_index INTEGER PRIMARY KEY, blob BLOB NOT NULL)');con.commit();con.close();return p

def pack_sparse_facts(facts):
    return pickle.dumps({k:(v.value,v.quality,v.source) for k,v in facts.items() if v is not None},pickle.HIGHEST_PROTOCOL)

def unpack_sparse_facts(blob):
    if not blob:return None
    z=pickle.loads(blob);return {k:Fact(v[0],float(v[1]),v[2]) for k,v in z.items()}

def load_cached_fact(cache_conn,i):
    r=cache_conn.execute('SELECT blob FROM facts WHERE b_index=?',(int(i),)).fetchone()
    return unpack_sparse_facts(r[0]) if r else None

def flush_fact_cache(cache_path,pending):
    if not pending:return
    con=sqlite3.connect(cache_path,timeout=30);con.execute('PRAGMA journal_mode=WAL');con.execute('PRAGMA synchronous=NORMAL')
    con.executemany('INSERT OR IGNORE INTO facts(b_index,blob) VALUES (?,?)',[(int(i),sqlite3.Binary(b)) for i,b in pending.items()]);con.commit();con.close()

def fetch_brows(conn,indices):
    out={};inds=sorted(set(int(x) for x in indices))
    for i in range(0,len(inds),800):
        ch=inds[i:i+800];q=','.join('?'*len(ch))
        for r in conn.execute(f'SELECT b_index,item_id,name,brand_raw,description,item_info,sizing_comp,tags FROM products WHERE b_index IN ({q})',ch):
            out[int(r[0])]={'item_id':r[1],'name':r[2] or '','brand_raw':r[3] or '','description':(r[4] or '')[:2000],'item_info':r[5] or '','sizing_comp':r[6] or '','tags':r[7] or ''}
    return out

def nonmatch_stub(r,reason='WEAK_RETRIEVAL_NONMATCH'):
    return {'_row_key_A':str(r.get('_row_key_A','')),'item_id_A':str(r.get('item_id','')),'selected_item_id_B':'','best_candidate_item_id_B':'','best_candidate_name_A':r.get('name',''),'best_candidate_name_B':'','final_verdict':'NON_MATCH','manual_review_required':False,'match_confidence':0.0,'nonmatch_confidence':1.0,'raw_match_evidence_score':0.0,'raw_nonmatch_evidence_score':1.0,'hard_veto':False,'hard_veto_rules':'','functional_identity_similarity':0.0,'identity_anchor_count':0,'strong_support_count':0,'positive_evidence_count':0,'conflict_count':0,'pair_support_score':0.0,'runner_up_item_id_B':'','runner_up_support_score':0.0,'runner_up_margin':0.0,'retrieval_rank_of_best':0,'candidate_count':0,'modifier_asymmetry_count':0,'same_brand':False,'private_label_pair':False,'brand_policy':'UNKNOWN','quantity_similarity':-1.0,'educated_guess':'NON_MATCH','educated_guess_item_id_B':'','decision_reason':reason,'gpt_reviewed':False}

def score_candidates(arow,af,cands,brows,bfacts,criteria):
    out=[]
    for c in cands:
        i=int(c['b_index']);b=brows[i];m=match_score(af,bfacts[i],criteria);n=nonmatch_score(af,bfacts[i],criteria);m['brand_policy']=pair_brand_policy(arow,b,af,bfacts[i]);m['exact_title_match']=bnorm(arow.get('name',''))==bnorm(b.get('name',''))
        out.append({'_row_key_A':str(arow.get('_row_key_A','')),'item_id_A':str(arow.get('item_id','')),'item_id_B':str(b['item_id']),'name_A':arow.get('name',''),'name_B':b.get('name',''),'retrieval_rank':c['retrieval_rank'],'retrieval_score':c['retrieval_score'],'base_retrieval_score':c.get('base_retrieval_score',c['retrieval_score']),'retrieval_rescue_type':c.get('retrieval_rescue_type',''),**m,**n})
    return out


# v12 deterministic semantic promotion ---------------------------------------
PROMO_DANGEROUS_ASYM={
    'model_or_part_identifier','refill_vs_starter_kit',
    'coffee_format','pet_species','pet_life_stage','meat_species','meat_cut',
    'diaper_size','formula_base','absorbency','spf','flavor_partial',
    'organic_status_private','bundle_flag','assortment_flag','shade',
    'waterproof_status','washable_longwear_status','sugar_status',
    'caffeine_status','release_type','protein_source','produce_species'
}
PROMO_SOFT_ASYM={'flavor','scent','product_line','dosage_form'}
TITLE_STOP=set('the a an and or with for of in on by from to at new premium classic original style assorted each per size pack count ct pk oz ounce ounces lb lbs pound pounds g kg mg mcg ml l fl fluid bottle bottles bag bags box boxes can cans carton cartons tub tubs jar jars pouch pouches package packages value family fresh made plus food foods brand recipe delicious natural naturally real great best quality extra super ultimate select signature traditional authentic'.split())
ROLE_GROUPS=[
    {'shampoo'},{'conditioner'},{'body','wash'},{'bar','soap'},{'deodorant'},{'antiperspirant'},
    {'lotion'},{'serum'},{'cleanser'},{'toothpaste'},{'mouthwash'},{'toothbrush'},{'floss'},
    {'detergent'},{'fabric','softener'},{'dish','soap'},{'trash','bags'},
    {'coffee'},{'tea'},{'juice'},{'soda'},{'water'},{'yogurt'},{'milk'},{'cheese'},
    {'cereal'},{'granola'},{'crackers'},{'chips'},{'cookies'},{'pasta'},{'rice'},
    {'ketchup'},{'mustard'},{'mayonnaise'},{'salsa'},{'hummus'}
]
STATUS_PHRASES=['organic','lactose free','sugar free','zero sugar','decaf','caffeine free','low sodium','reduced sodium','fat free','low fat','whole milk','skim milk','waterproof','washable','unscented','fragrance free','sensitive skin']
FORMAT_GROUPS=[
    ('ground coffee','whole bean','coffee pods','coffee pod','capsules','capsule','instant coffee'),
    ('liquid detergent','detergent pods','detergent pacs','laundry pods','laundry pacs','laundry sheets','powder detergent'),
]
FLAVOR_WORDS=set('vanilla chocolate strawberry blueberry raspberry lemon lime orange apple cherry peach mango honey cinnamon garlic basil ranch bbq barbecue cheddar mozzarella mint peppermint coconut pineapple banana caramel hazelnut pumpkin grape watermelon cucumber berry mocha latte teriyaki barbacoa verde original'.split())
SAFE_OMISSION=set('daily refreshing nourishing moisturizing natural naturally classic traditional premium signature adult men mens women womens kids kid baby family frozen refrigerated snack snacks flavored flavor style instant ready eat baked creamy rich pure gentle original broad spectrum with real made whole grain gluten free no added quick dry'.split())

def _title_words(s):
    z=bnorm(s)
    z=re.sub(r'\b\d+(?:\.\d+)?\s*(?:fl\s*oz|oz|ounce|ounces|lb|lbs|pound|pounds|g|kg|mg|mcg|ml|l|ct|count|pk|pack|gal|gallon|gallons)\b',' ',z)
    z=re.sub(r'\b\d+(?:\.\d+)?\b',' ',z)
    return [w for w in z.split() if w not in TITLE_STOP and len(w)>1]

def _strip_private_prefix(words):
    z=list(words)
    labels=[bnorm(x).split() for x in PRIVATE_LABELS]
    for pp in sorted(labels,key=len,reverse=True):
        if z[:len(pp)]==pp:return z[len(pp):]
    return z

def _core_similarity(a,b):
    A=_strip_private_prefix(_title_words(a));B=_strip_private_prefix(_title_words(b))
    k=0
    while k<min(3,len(A),len(B)) and A[k]==B[k]:k+=1
    if k:A=A[k:];B=B[k:]
    sa,sb=set(A),set(B)
    if not sa or not sb:return 0.0,0.0
    inter=len(sa&sb)
    return inter/len(sa|sb),inter/min(len(sa),len(sb))

def _core_sets(a,b):
    A=_strip_private_prefix(_title_words(a));B=_strip_private_prefix(_title_words(b))
    k=0
    while k<min(3,len(A),len(B)) and A[k]==B[k]:k+=1
    if k:A=A[k:];B=B[k:]
    return set(A),set(B)

def _only_safe_wording_difference(a,b):
    A,B=_core_sets(a,b);oa=A-B;ob=B-A
    # Exact content sets are safest. Otherwise every unmatched token must be a
    # known retailer/marketing omission, never an arbitrary variant descriptor.
    return not ((oa|ob)-SAFE_OMISSION)

def _phrase_present(s,p):return (' '+p+' ') in (' '+bnorm(s)+' ')

def _title_role_conflict(a,b):
    A=set(_title_words(a));B=set(_title_words(b));ra=[];rb=[]
    for i,g in enumerate(ROLE_GROUPS):
        if g<=A:ra.append(i)
        if g<=B:rb.append(i)
    ash='shampoo' in A;aco='conditioner' in A;bsh='shampoo' in B;bco='conditioner' in B
    na,nb=' '+bnorm(a)+' ',' '+bnorm(b)+' ';a2=(' 2 in 1 ' in na or ' 2in1 ' in na);b2=(' 2 in 1 ' in nb or ' 2in1 ' in nb)
    # Retailers often abbreviate the same 2-in-1 SKU as "2 in 1 shampoo" on one
    # side and "2 in 1 shampoo and conditioner" on the other.
    if not (a2 and b2 and ash and bsh):
        if (ash,aco)!=(bsh,bco) and (ash or aco) and (bsh or bco):return True
    return bool(ra and rb and not set(ra)&set(rb))

def _title_status_conflict(a,b):
    return any(_phrase_present(a,p)!=_phrase_present(b,p) for p in STATUS_PHRASES)

def _title_format_conflict(a,b):
    na,nb=' '+bnorm(a)+' ',' '+bnorm(b)+' '
    for grp in FORMAT_GROUPS:
        aa=[x for x in grp if (' '+x+' ') in na];bb=[x for x in grp if (' '+x+' ') in nb]
        if aa and bb and aa[0]!=bb[0]:return True
    forms=('gummy','gummies','tablet','tablets','capsule','capsules','softgel','softgels','lozenge','lozenges','powder','liquid')
    aa={x for x in forms if (' '+x+' ') in na};bb={x for x in forms if (' '+x+' ') in nb}
    return bool(aa and bb and aa.isdisjoint(bb))

def _title_flavor_conflict(a,b):
    A=set(_title_words(a))&FLAVOR_WORDS;B=set(_title_words(b))&FLAVOR_WORDS
    return bool(A and B and A.isdisjoint(B))

def _title_pack_values(s):
    z=bnorm(s);out=[]
    for pat in (r'\b(\d+)\s*(?:x|pack|pk)\b',r'\bpack\s+of\s+(\d+)\b',r'\b(\d+)\s*(?:ct|count)\b'):
        for m in re.finditer(pat,z):
            try:
                n=int(m.group(1))
                if n>1:out.append(n)
            except:pass
    return out

def _explicit_pack_conflict(a,b):
    A,B=_title_pack_values(a),_title_pack_values(b)
    if A and B and max(A)!=max(B):return True
    return bool(A)!=bool(B)

def title_guard(pair,strict_unknown=False):
    if bool(pair.get('hard_veto')):return False,0.0,0.0
    a,b=str(pair.get('name_A','')),str(pair.get('name_B',''))
    if obvious_title_conflict(pair) or _title_role_conflict(a,b) or _title_status_conflict(a,b) or _title_format_conflict(a,b) or _title_flavor_conflict(a,b):return False,0.0,0.0
    asym=set(pair.get('modifier_asymmetries') or [])
    # Retail feeds often encode the same 8-count product as declared_count on one
    # side and explicit_multipack on the other. If both titles explicitly agree on
    # the count, that parser asymmetry is not a real contradiction.
    if 'explicit_multipack' in asym:
        pa,pb=_title_pack_values(a),_title_pack_values(b)
        if pa and pb and max(pa)==max(pb):asym.discard('explicit_multipack')
        else:return False,0.0,0.0
    if asym:return False,0.0,0.0
    if _explicit_pack_conflict(a,b):return False,0.0,0.0
    j,c=_core_similarity(a,b)
    if not _only_safe_wording_difference(a,b):return False,j,c
    if strict_unknown and c<.78:return False,j,c
    return True,j,c

def safe_review_promotion(pair,cfg):
    if int(pair.get('retrieval_rank',99) or 99)!=1:return False,''
    bp=str(pair.get('brand_policy','UNKNOWN'))
    if bp not in {'SAME','PRIVATE','COMMODITY'}:return False,''
    if float(pair.get('nonmatch_confidence',1) or 1)>.28:return False,''
    raw=float(pair.get('match_evidence_score',0) or 0);fi=float(pair.get('functional_identity_similarity',0) or 0)
    ps=float(pair.get('pair_support_score',0) or 0);margin=float(pair.get('_runner_margin',0) or 0)
    pe=int(pair.get('atomic_positive_evidence_count',0) or 0);ia=int(pair.get('identity_anchor_count',0) or 0)
    q=float(pair.get('quantity_similarity',-1) or -1);qknown=q>=0
    if qknown and q<float(cfg['router'].get('min_quantity_similarity',.72)):return False,''
    ok,j,contain=title_guard(pair,strict_unknown=not qknown)
    if not ok:return False,''
    if bp=='SAME':
        if qknown:
            good=(raw>=.50 and fi>=.55 and ps>=.075 and margin>=.005 and pe>=3 and ia>=2 and contain>=.78 and j>=.52)
            return good,'V12_SAFE_SAME_BRAND_KNOWN_Q' if good else ''
        good=(raw>=.58 and fi>=.68 and ps>=.10 and margin>=.018 and pe>=4 and ia>=2 and contain>=.90 and j>=.66)
        return good,'V12_SAFE_SAME_BRAND_UNKNOWN_Q' if good else ''
    if bp=='PRIVATE':
        if not qknown:return False,''
        good=(raw>=.50 and fi>=.60 and ps>=.08 and margin>=.008 and pe>=3 and ia>=2 and contain>=.76 and j>=.46)
        return good,'V12_SAFE_PRIVATE_LABEL' if good else ''
    good=(qknown and raw>=.62 and fi>=.80 and ps>=.16 and margin>=.025 and pe>=4 and ia>=2 and contain>=.80)
    return good,'V12_SAFE_COMMODITY' if good else ''

def deterministic(rows,ret,conn,bidx,criteria,cfg):
    floor=float(cfg['retrieval']['min_deep_retrieval_score']);verdicts={};ctx={};cache=OrderedDict();cache_max=128;fcpath=init_fact_cache(conn.execute('PRAGMA database_list').fetchone()[2]);fc=sqlite3.connect(f'file:{fcpath.as_posix()}?mode=ro',uri=True,timeout=30);pending={}
    def get_b(i):
        i=int(i)
        if i in cache:
            x=cache.pop(i);cache[i]=x;return x
        z=fetch_brows(conn,[i]).get(i)
        if z is None:return None,None
        bf=load_cached_fact(fc,i)
        if bf is None:
            bf=extract_facts(z,criteria);pending[i]=pack_sparse_facts(bf)
        x=(z,bf);cache[i]=x
        if len(cache)>cache_max:cache.popitem(last=False)
        return x
    for r0 in rows:
        r=infer_brand(r0,bidx);key=str(r.get('_row_key_A',''));broad=ret.retrieve(r,cfg['retrieval']['candidate_k'])
        if not broad or float(broad[0].get('retrieval_score',0))<floor:verdicts[key]=nonmatch_stub(r);continue
        deep=ret.deep_shortlist(broad,cfg['retrieval']['deep_k'],cfg['retrieval']['deep_cap'])
        if not deep:verdicts[key]=nonmatch_stub(r,'NO_DEEP_CANDIDATE');continue
        af=extract_facts(r,criteria);brows={};bfacts={};usable=[]
        for c in deep:
            i=int(c['b_index']);b,bf=get_b(i)
            if b is not None:brows[i]=b;bfacts[i]=bf;usable.append(c)
        pairs=score_candidates(r,af,usable,brows,bfacts,criteria) if usable else []
        if not pairs:verdicts[key]=nonmatch_stub(r,'NO_DEEP_CANDIDATE');continue
        v=route_group(pairs,cfg);v['_row_key_A']=key;v['gpt_reviewed']=False
        for _p in pairs:_p['_runner_margin']=float(v.get('runner_up_margin',0) or 0)
        # Retrieval rescue expands recall, but it must not silently weaken the
        # audited deterministic decision boundary. If the selected MATCH exists
        # only because v11's title-brand/private-label rescue surfaced it, route
        # it through semantic adjudication instead of certifying it immediately.
        if v.get('final_verdict')=='MATCH':
            bp=next((p for p in pairs if str(p.get('item_id_B'))==str(v.get('best_candidate_item_id_B'))),None)
            if bp is not None and str(bp.get('retrieval_rescue_type','')):
                v['final_verdict']='REVIEW';v['selected_item_id_B']='';v['manual_review_required']=True;v['decision_reason']='RECALL_RESCUE_REVIEW';v['educated_guess']='MATCH';v['educated_guess_item_id_B']=str(bp.get('item_id_B',''))
        if v.get('final_verdict')=='REVIEW':
            bp=next((p for p in pairs if str(p.get('item_id_B'))==str(v.get('best_candidate_item_id_B'))),None)
            if bp is not None:
                promote,why=safe_review_promotion(bp,cfg)
                if promote:
                    v['final_verdict']='MATCH';v['selected_item_id_B']=str(bp.get('item_id_B',''));v['manual_review_required']=False;v['decision_reason']=why;v['educated_guess']='MATCH';v['educated_guess_item_id_B']=str(bp.get('item_id_B',''))
        # v17 final firewall: every deterministic MATCH, regardless of
        # which lane produced it, must survive the same explicit contradiction gate
        # used after GPT. This is O(1) and reuses the already-scored best pair.
        v['final_safety_blocker']=''
        if v.get('final_verdict')=='MATCH':
            bp=next((p for p in pairs if str(p.get('item_id_B'))==str(v.get('selected_item_id_B') or v.get('best_candidate_item_id_B'))),None)
            if bp is not None:
                action,block=final_match_firewall(bp,cfg)
                if action=='REVIEW' and block=='ONE_SIDED_EXPLICIT_COUNT':
                    c=next((x for x in broad if str(x.get('item_id_B'))==str(bp.get('item_id_B'))),None)
                    br=brows.get(int(c['b_index'])) if c is not None and str(c.get('b_index','')).strip() else None
                    if br is not None and _metadata_quantity_confirm_v16(r,br):
                        action,block='MATCH','';v['decision_reason']=str(v.get('decision_reason',''))+'_B_METADATA_QCONF'
                v['final_safety_blocker']=block
                if action!='MATCH':
                    v['final_verdict']=action;v['selected_item_id_B']='';v['manual_review_required']=action=='REVIEW';v['decision_reason']='V17_FINAL_SAFETY_'+action;v['educated_guess']='NON_MATCH' if action=='NON_MATCH' else 'MATCH';v['educated_guess_item_id_B']=str(bp.get('item_id_B',''))
        verdicts[key]=v
        # Keep compact context not only for REVIEW but also for plausible
        # evidence-bearing NON_MATCH rows. v11's semantic second pass may rescue
        # these, but hard veto/brand/quantity guards still remain authoritative.
        semantic_ok=(not bool(v.get('hard_veto')) and str(v.get('brand_policy')) in {'SAME','SAME_INFERRED','PRIVATE','COMMODITY'}
                     and float(v.get('nonmatch_confidence',1))<.35 and float(v.get('pair_support_score',0))>=.055
                     and int(float(v.get('retrieval_rank_of_best',99) or 99))<=3)
        if v['final_verdict']=='REVIEW' or semantic_ok:ctx[key]=(r,af,broad,pairs)
    fc.close();flush_fact_cache(fcpath,pending)
    return [verdicts[str(r.get('_row_key_A',''))] for r in rows],ctx

def queue_from_verdicts(rows,verdicts,cfg):
    reviews=[]
    allowed={'SAME','SAME_INFERRED','PRIVATE','COMMODITY'}
    for v in verdicts:
        verdict=str(v.get('final_verdict',''))
        if verdict!='REVIEW' or bool(v.get('hard_veto')):continue
        # v17 distinguishes explicit contradictions (already NON_MATCH) from
        # one-sided/omitted-attribute ambiguity. Soft firewall REVIEWs are worth
        # sending to GPT because GPT also sees Store-B structured size/category/
        # description and can resolve harmless retailer omissions. This recovers
        # recall without allowing GPT to override an explicit contradiction.
        if str(v.get('brand_policy','UNKNOWN')) not in allowed:continue
        q=float(v.get('quantity_similarity',-1) or -1)
        if q>=0 and q<float(cfg['router'].get('min_quantity_similarity',.72)):continue
        nm=float(v.get('nonmatch_confidence',1) or 1);ps=float(v.get('pair_support_score',0) or 0);raw=float(v.get('raw_match_evidence_score',0) or 0);fi=float(v.get('functional_identity_similarity',0) or 0)
        if nm>=.35 or ps<.055 or fi<.48:continue
        # Only unresolved REVIEW cases consume semantic API budget.
        pr=ps+.35*raw+.22*fi-.25*nm + (.08 if verdict=='REVIEW' else 0) + (.06 if str(v.get('brand_policy'))=='PRIVATE' else 0)
        reviews.append((pr,str(v.get('_row_key_A',''))))
    reviews.sort(reverse=True)
    lim=int(cfg['gpt']['max_products_per_batch']);score={k:pr for pr,k in reviews[:lim]}
    out=[]
    for r in rows:
        k=str(r.get('_row_key_A',''))
        if k in score:
            z=dict(r);z['_gpt_priority']=f"{score[k]:.9f}";out.append(z)
    return out

# GPT v17: metadata-aware evidence-grounded structured adjudication -----------

def obvious_title_conflict(pair):
    a=' '+bnorm(pair.get('name_A',''))+' ';b=' '+bnorm(pair.get('name_B',''))+' '
    def has(s,p):return (' '+p+' ') in s
    ash=has(a,'shampoo');aco=has(a,'conditioner');bsh=has(b,'shampoo');bco=has(b,'conditioner')
    a2=(' 2 in 1 ' in a or ' 2in1 ' in a);b2=(' 2 in 1 ' in b or ' 2in1 ' in b)
    if (ash and not aco and bco and not bsh) or (aco and not ash and bsh and not bco):return True
    if a2!=b2 and ((ash or aco) and (bsh or bco)):return True
    if (has(a,'body wash') and (' epsom salt ' in b or ' bath salt ' in b)) or (has(b,'body wash') and (' epsom salt ' in a or ' bath salt ' in a)):return True
    if (has(a,'toothpaste') and has(b,'mouthwash')) or (has(b,'toothpaste') and has(a,'mouthwash')):return True
    choc=lambda s:'dark' if ' dark chocolate ' in s else ('milk' if ' milk chocolate ' in s else '')
    ca,cb=choc(a),choc(b)
    if ca and cb and ca!=cb:return True
    heat=lambda s:next((x for x in ('mild','medium','hot') if (' '+x+' ') in s),'')
    ha,hb=heat(a),heat(b)
    if ha and hb and ha!=hb:return True
    return False

def _json_obj(x):
    if not x:return {}
    try:return json.loads(str(x))
    except:return {}

def _category_path(row):
    z=_json_obj(row.get('item_info',''))
    return ' > '.join(str(z.get(f'category_{i}','')).strip() for i in range(4) if str(z.get(f'category_{i}','')).strip() and str(z.get(f'category_{i}','')).lower()!='none')[:180]

def _friendly_size(row):
    z=_json_obj(row.get('sizing_comp',''))
    # Wegmans feed has two schemas. Most rows use size_user_friendly, while a
    # smaller Instacart-derived slice uses `size` (e.g. 149 fl oz). v15 ignored
    # the second schema, hiding authoritative B quantity from both scoring/GPT.
    vals=[]
    for k in ('size_user_friendly','size'):
        v=z.get(k)
        if v not in (None,'') and str(v).lower()!='nan':vals.append(str(v).strip())
    return ' | '.join(dict.fromkeys(vals))[:120]

def _clean_description(row):
    s=str(row.get('description','') or '')
    if s.lower()=='nan':return ''
    s=re.sub(r'<[^>]+>',' ',s)
    s=re.sub(r'\s+',' ',s).strip()
    return s[:220]

def gpt_schema():
    item={
      'type':'object',
      'properties':{
        'id':{'type':'string'},
        'choice':{'type':'integer','enum':[0,1,2]},
        'decision':{'type':'string','enum':['MATCH','NON_MATCH','UNCERTAIN']},
        'match_type':{'type':'string','enum':['SAME_BRAND_PRODUCT','PRIVATE_LABEL_EQUIVALENT','COMMODITY_EQUIVALENT','NONE']},
        'blocker':{'type':'string','enum':['NONE','BRAND','ROLE','VARIANT','FLAVOR_SCENT_SHADE','QUANTITY','FORM','ORGANIC_STATUS','MODEL','PET_STAGE','MEAT_CUT','OTHER']},
        'evidence':{'type':'string','enum':['CLEAR','PROBABLE','INSUFFICIENT']}
      },
      'required':['id','choice','decision','match_type','blocker','evidence'],
      'additionalProperties':False
    }
    return {'type':'json_schema','json_schema':{'name':'betterbasket_product_adjudication','strict':True,'schema':{
      'type':'object','properties':{'results':{'type':'array','items':item}},'required':['results'],'additionalProperties':False
    }}}

GPT_DEVELOPER_PROMPT='''You are BetterBasket's final retail-product identity adjudicator. The candidate generator has already found the closest Store-B products. Your job is to decide whether one shown candidate is genuinely the same sellable product, or an allowed retailer-private-label/fresh-commodity equivalent.

DECISION POLICY
1. UNKNOWN IS NOT A CONTRADICTION, but national-brand MATCH means the same sellable SKU. If a title omits an attribute, inspect the supplied structured size/category/description before treating it as unknown. Reject whenever those Store-B fields reveal an incompatible size, count, strength, role, form, or variant.
2. Do not default to UNCERTAIN merely because one title is shorter. Use UNCERTAIN only when genuinely missing information is necessary to distinguish between plausible incompatible SKUs.
2A. NATIONAL-BRAND GENERIC-CANDIDATE RULE: if Store A names a specific product line/variant/configuration but Store B only establishes a generic family that could correspond to multiple incompatible same-brand SKUs, the missing Store-B identity IS necessary. Return UNCERTAIN, not MATCH. Never let one generic Store-B SKU stand in for multiple named medicines, scents, colors, age/gender variants, models, or package configurations.
3. MATCH when one candidate has the same product identity and there is no explicit SKU-changing contradiction.
4. NON_MATCH when every candidate is clearly a different product or has an explicit contradiction.
5. The candidates are deliberately near-neighbors. Same brand plus similar core words is NOT enough when both sides explicitly name different variants. Example: a rolling-ball pen is not the same SKU as a gel-ink pen; Butter Masala is not Tikka Masala.

BRAND POLICY
- National/manufacturer brand: require the same actual manufacturer brand/product identity. Different national brands are NOT interchangeable.
- Retailer private label: different retailer brands are expected. Great Value/Marketside/Equate/etc. may match Wegmans private-label products when the consumer product is equivalent.
- Fresh/unbranded commodity: brand can be irrelevant; species/cut/form/variety must still agree.

EXPLICIT SKU-CHANGING DIFFERENCES
Different product role, model/part, flavor/scent/shade when both explicit, dosage/form, coffee format, pet species/life stage, meat species/cut, organic vs explicitly non-organic, or explicitly incompatible quantity/pack/size => NON_MATCH.
A value stated only in one TITLE is not automatically a conflict; the other side's supplied size/description may make it explicit.

IMPORTANT EXAMPLES
- Great Value Organic Tomato Sauce vs Wegmans Organic Tomato Sauce => MATCH, PRIVATE_LABEL_EQUIVALENT.
- Chobani Whole Milk Greek Yogurt Honey Blended vs Chobani Greek Honey Blended => MATCH, SAME_BRAND_PRODUCT. The shorter title does not create a contradiction.
- V8 Original Vegetable Juice 11.5 fl oz, 6 Count vs V8 Original Vegetable Juice, with Store B count omitted => MATCH if no Store-B field explicitly contradicts the 6-count. Missing count is UNKNOWN.
- Native Gumdrop Deodorant vs Native Gumdrop Body Wash => NON_MATCH, ROLE.
- Starbucks ground coffee vs Starbucks capsules/pods => NON_MATCH, FORM.
- Same product explicitly labeled Pack of 6 vs Pack of 12 => NON_MATCH, QUANTITY.
- Original/regular strength vs Extra/Maximum Strength => NON_MATCH, VARIANT.
- Mild Mint vs Arctic Mint, Buttermilk Ranch vs Classic Ranch, or Night vs Extra Moisturizing => NON_MATCH when both variants are explicit.
- Heel cushions vs full work insoles, or K-Cup/pod format vs canister/powder mix => NON_MATCH, ROLE/FORM.

INPUT SIGNALS
brand_relation and quantity_relation are deterministic evidence summaries. quantity_relation=UNKNOWN means one or both sides lack explicit comparable quantity; it does NOT mean mismatch. Candidate rank/support are hints only. Decide from product identity and explicit evidence.

OUTPUT RULES
For MATCH: choice must be 1 or 2, blocker=NONE, and match_type must describe why it is allowed.
For NON_MATCH: choice=0 and blocker should identify the clearest explicit reason.
For UNCERTAIN: choice=0. Use this only for real unresolved ambiguity, not ordinary missing retailer wording.
Evidence=CLEAR when identity/contradiction is directly supported; PROBABLE when the identity is well-supported with harmless omissions; INSUFFICIENT only when a necessary distinction cannot be resolved.'''

def gpt_http(endpoint,api_key,model,messages,cfg,effort='low',timeout=45):
    url=endpoint.rstrip('/')+'/chat/completions'
    body={
      'model':model,
      'messages':messages,
      'reasoning_effort':effort,
      'response_format':gpt_schema(),
      'max_completion_tokens':int(cfg['gpt'].get('max_completion_tokens',5000))
    }
    req=urllib.request.Request(url,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':f'Bearer {api_key}','api-key':api_key},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:obj=json.loads(r.read().decode())
    except urllib.error.HTTPError as e:raise RuntimeError(f'HTTP {e.code}: '+e.read().decode('utf-8','replace')[:1200]) from None
    c=obj['choices'][0]['message'].get('content','')
    if isinstance(c,list):c=''.join(str(x.get('text','')) if isinstance(x,dict) else str(x) for x in c)
    return str(c)

def _quantity_relation(pair):
    q=float(pair.get('quantity_similarity',-1) or -1)
    if q<0:return 'UNKNOWN'
    return 'COMPATIBLE' if q>=.72 else 'CONFLICT'

def _candidate_packet(n,x):
    p=x['pair'];b=x['b']
    return {
      'choice':n,
      'title':str(b.get('name',''))[:260],
      'brand':str(b.get('brand_raw',''))[:90],
      'size':_friendly_size(b),
      'category':_category_path(b),
      'description':_clean_description(b),
      'brand_relation':str(p.get('brand_policy','UNKNOWN')),
      'quantity_relation':_quantity_relation(p),
      'retrieval_rank':int(p.get('retrieval_rank',99) or 99),
      'support':round(float(p.get('pair_support_score',0) or 0),3),
      'functional_identity':round(float(p.get('functional_identity_similarity',0) or 0),3)
    }

def _case_packet(j):
    r=j['r']
    return {
      'id':str(j['key']),
      'store_a':{
        'title':str(r.get('name',''))[:260],
        'brand':str(r.get('brand_raw',''))[:90],
        'size':_friendly_size(r),
        'category':_category_path(r),
        'description':_clean_description(r)
      },
      'candidates':[_candidate_packet(i,x) for i,x in enumerate(j['pairs'],1)]
    }

def gpt_call(endpoint,api_key,model,jobs,cfg,effort='low'):
    payload={'cases':[_case_packet(j) for j in jobs]}
    timeout=int(cfg['gpt'].get('request_timeout_seconds',35))
    messages=[{'role':'developer','content':GPT_DEVELOPER_PROMPT},{'role':'user','content':json.dumps(payload,ensure_ascii=False,separators=(',',':'))}]
    return gpt_http(endpoint,api_key,model,messages,cfg,effort=effort,timeout=timeout)

def parse_semantic_json(text,valid_keys):
    valid=set(map(str,valid_keys));out={}
    try:obj=json.loads(str(text or ''))
    except Exception:return out
    for z in obj.get('results',[]) if isinstance(obj,dict) else []:
        if not isinstance(z,dict):continue
        key=str(z.get('id',''))
        if key not in valid:continue
        try:choice=int(z.get('choice',0) or 0)
        except:choice=0
        decision=str(z.get('decision','UNCERTAIN')).upper()
        if decision not in {'MATCH','NON_MATCH','UNCERTAIN'}:decision='UNCERTAIN'
        if decision!='MATCH':choice=0
        out[key]={
          'choice':choice,
          'verdict':decision,
          'match_type':str(z.get('match_type','NONE')).upper(),
          'blocker':str(z.get('blocker','OTHER')).upper(),
          'evidence':str(z.get('evidence','INSUFFICIENT')).upper()
        }
    return out

def _v13_pack_values(s):
    z=bnorm(s);out=[]
    for pat in (r'\b(\d+)\s*(?:x|pack|pk)\b',r'\bpack\s+of\s+(\d+)\b',r'\b(\d+)\s*(?:ct|count)\b'):
        for m in re.finditer(pat,z):
            try:
                n=int(m.group(1))
                if n>=1:out.append(n)
            except:pass
    return out

def _explicit_pack_conflict_v13(a,b):
    # One-sided count is UNKNOWN, not a contradiction. Slash-pack notation such
    # as 24/PK was a v13 blind spot, so v14 handles it in the stronger parser below.
    A,B=_v13_pack_values(a),_v13_pack_values(b)
    return bool(A and B and max(A)!=max(B))

def _v14_pack_total(s):
    z=str(s or '').lower().replace('×','x')
    z=re.sub(r'\s+',' ',z)
    # Marketplace nesting: "4 pack ... 2/pack" means 8 sellable units, not 4.
    m=re.search(r'\b(\d+)\s*pack\b.*?\b(\d+)\s*/\s*(?:pk|pack)\b',z)
    if m:
        try:return int(m.group(1))*int(m.group(2))
        except:return None
    vals=[]
    pats=(r'\b(\d+)\s*/\s*(?:pk|pack|ct|count)\b',r'\bpack\s+of\s+(\d+)\b',
          r'\b(\d+)\s*(?:ct|count|refills?|bottles?|boxes?|cans?)\b',r'\b(\d+)\s*(?:pack|pk)\b',
          r'\b(\d+)\s*x\s*\d+(?:\.\d+)?\s*(?:fl\.?\s*oz|oz|ml|l|g|kg|lb|lbs|qt|quart|gal|gallon)\b')
    for pat in pats:
        for m in re.finditer(pat,z):
            try:
                n=int(m.group(1))
                if n>=1:vals.append(n)
            except:pass
    return max(vals) if vals else None

def _v16_count_values(s):
    """Return plausible retail unit/count anchors from a title.

    Multiple values are retained because paper goods often state both physical
    and equivalent roll counts (e.g. 6 Mega Rolls = 24 Regular Rolls).  A pair
    is contradictory only when both sides have explicit anchors and no anchor
    can agree.  This also understands count nouns such as "8 waffles" which
    v14's ct/pack-only parser missed.
    """
    z=str(s or '').lower().replace('×','x')
    z=re.sub(r'\s+',' ',z)
    vals=set()
    desc=r'(?:(?:double|triple|mega|mega\s+xl|mega\s+xxl|super\s+mega|jumbo|regular|standard|bonus|plus|flip[- ]?top)\s+){0,3}'
    noun=r'(?:waffles?|rolls?|wipes?|refills?|bottles?|boxes?|cans?|pods?|packets?|pouches?|bars?|sticks?|tablets?|caplets?|capsules?|gummies|diapers?|liners?|pads?|tampons?|cups?|bags?|pieces?|pcs?|filters?|razors?|blades?|toothbrushes?|brushes?|markers?|pens?|pencils?|batteries?|bulbs?)'
    pats=(r'\b(\d+)\s*/\s*(?:pk|pack|ct|count)\b',
          r'\bpack\s+of\s+(\d+)\b',r'\b(\d+)\s*(?:ct|count)\b',
          r'\b(\d+)\s*(?:pack|pk)\b',rf'\b(\d+)\s*{desc}{noun}\b')
    for pat in pats:
        for m in re.finditer(pat,z):
            try:
                n=int(m.group(1))
                if n>0:vals.add(n)
            except:pass
    # Explicit multiplication, including 2x80 count -> 160 count.
    for m in re.finditer(r'\b(\d+)\s*x\s*(\d+)\s*(?:ct|count|'+noun[3:-1]+r')\b',z):
        try:
            n=int(m.group(1))*int(m.group(2))
            if n>0:vals.add(n)
        except:pass
    # Leading multipack × internal unit count ("(2 pack) ... 4 Count" -> 8).
    lead=re.search(r'^\s*\(?\s*(\d+)\s*(?:pack|pk)\s*\)?',z)
    if lead:
        try:
            mult=int(lead.group(1))
            intern=[v for v in vals if v!=mult]
            for v in intern:vals.add(mult*v)
        except:pass
    return vals

def _v16_roll_tier(s):
    z=' '+bnorm(s)+' '
    if ' roll' not in z:return ''
    for x in ('mega xxl','mega xl','super mega','triple plus','double plus','triple','double','jumbo','mega','regular'):
        if (' '+x+' roll') in z or (' '+x+' rolls') in z:return x
    return ''

def _explicit_pack_conflict_v14(a,b):
    # v16 count anchors understand multiplied totals (2 x 80 Count == 160) and
    # nested marketplace packs. If both sides expose count anchors, agreement on
    # *any* plausible total is sufficient; the older single-value parser can
    # otherwise misread the internal 80 as the package total.
    ca,cb=_v16_count_values(a),_v16_count_values(b)
    if ca and cb:
        if ca.isdisjoint(cb):return True
    else:
        A,B=_v14_pack_total(a),_v14_pack_total(b)
        if A is not None and B is not None and A!=B:return True
    ta,tb=_v16_roll_tier(a),_v16_roll_tier(b)
    if ta and tb and ta!=tb:return True
    return False

def _one_sided_explicit_count_v16(a,b):
    ca,cb=_v16_count_values(a),_v16_count_values(b)
    return bool(ca) != bool(cb)

def _v14_amounts(s):
    z=str(s or '').lower().replace('×','x')
    z=re.sub(r'\b(\d+)\s+(\d+)\s*/\s*(\d+)\b',lambda m:str(int(m.group(1))+int(m.group(2))/max(1,int(m.group(3)))),z)
    z=re.sub(r'\s+',' ',z)
    out={'mass':[],'volume':[],'dimension':[]};occupied=[]
    # Parse volume first so the "oz" inside "fl oz" cannot be counted as mass.
    specs=[
      ('volume',r'\b(\d+(?:\.\d+)?)\s*(?:fl\.?\s*oz\.?|fluid\s*ounces?)\b',29.5735295625),
      ('volume',r'\b(\d+(?:\.\d+)?)\s*(?:ml|milliliters?)\b',1.0),
      ('volume',r'\b(\d+(?:\.\d+)?)\s*(?:l|liters?|litres?)\b',1000.0),
      ('volume',r'\b(\d+(?:\.\d+)?)\s*(?:qt|quart|quarts)\b',946.352946),
      ('volume',r'\b(\d+(?:\.\d+)?)\s*(?:pt|pint|pints)\b',473.176473),
      ('volume',r'\b(\d+(?:\.\d+)?)\s*(?:gal|gallon|gallons)\b',3785.411784),
      ('mass',r'\b(\d+(?:\.\d+)?)\s*(?:oz\.?|ounce|ounces)\b',28.349523125),
      ('mass',r'\b(\d+(?:\.\d+)?)\s*(?:lb|lbs|pound|pounds)\b',453.59237),
      ('mass',r'\b(\d+(?:\.\d+)?)\s*(?:kg|kilograms?)\b',1000.0),
      ('mass',r'\b(\d+(?:\.\d+)?)\s*(?:g|grams?)\b',1.0),
      ('dimension',r'\b(\d+(?:\.\d+)?)\s*(?:inch|inches|in)\b',1.0),
    ]
    for dim,pat,mult in specs:
        for m in re.finditer(pat,z):
            if any(m.start()<e and m.end()>b for b,e in occupied):continue
            # "40+ lb" in a dog-treat title is an intended-animal threshold, not
            # package weight; similarly ignore other plus-suffixed ranges.
            if dim=='mass' and m.end()-m.start()>0:
                pre=z[max(0,m.start()-1):m.start()+len(m.group(1))+2]
                if '+' in pre:continue
            # Nutrition/formulation amounts such as "50g protein" or "5g sugar"
            # are not package quantity. The evidence engine handles them separately.
            if dim=='mass':
                tail=z[m.end():m.end()+18]
                if re.match(r'\s*(?:protein|sugar|carbs?|fiber|fat)\b',tail):continue
            try:
                v=float(m.group(1))*mult
                if v>0:out[dim].append(v);occupied.append((m.start(),m.end()))
            except:pass
    return out

def _metadata_quantity_confirm_v16(arow,brow):
    """Confirm an omitted count/pack from authoritative structured size metadata.

    This is a positive-only rescue: it never creates a contradiction. It handles
    per-unit A content x count versus Store-B total package content.
    """
    a=str(arow.get('name',''))+' '+_friendly_size(arow);b=str(brow.get('name',''))+' '+_friendly_size(brow)
    ca={x for x in _v16_count_values(a) if x>1};cb={x for x in _v16_count_values(b) if x>1}
    if ca and cb and not ca.isdisjoint(cb):return True
    A=_v14_amounts(a);B=_v14_amounts(b)
    if len(ca)==1:
        n=next(iter(ca))
        for dim in ('mass','volume'):
            A[dim]=A[dim]+[x*n for x in list(A[dim])]
    if len(cb)==1:
        n=next(iter(cb))
        for dim in ('mass','volume'):
            B[dim]=B[dim]+[x*n for x in list(B[dim])]
    for dim in ('mass','volume'):
        for x in A[dim]:
            for y in B[dim]:
                if abs(x-y)<=.05*max(x,y,1e-9):return True
    # Retailers inconsistently label HBA liquids as oz vs fl oz.
    def ozs(x):
        return [float(m.group(1)) for m in re.finditer(r'\b(\d+(?:\.\d+)?)\s*(?:(?:fl\.?\s*)?oz\.?|ounces?)\b',str(x or '').lower())]
    aa,bb=ozs(a),ozs(b)
    return any(abs(x-y)<=.05*max(x,y,1e-9) for x in aa for y in bb)

def _explicit_size_conflict_v14(a,b):
    A,B=_v14_amounts(a),_v14_amounts(b)
    for dim in ('mass','volume','dimension'):
        if not A[dim] or not B[dim]:continue
        # Multiple explicit sizes can encode each-unit + total quantity. Any close
        # compatible anchor is enough; otherwise the explicit quantities conflict.
        ok=False
        for x in A[dim]:
            for y in B[dim]:
                if abs(x-y)<=max(.03*max(x,y),1e-6):ok=True;break
            if ok:break
        if not ok:return True
    return False

def _explicit_variant_conflict_v14(a,b):
    a=' '+bnorm(a)+' ';b=' '+bnorm(b)+' '
    def has(s,p):return (' '+p+' ') in s
    # Only use explicit opposites that are unambiguous across categories.
    # "regular" is intentionally excluded because in diapers/tampons it is a size/
    # absorbency label, not the opposite of unscented/zero-sugar/decaf.
    opposites=((('decaf','caffeine free'),('original','caffeinated')),
               (('zero sugar','sugar free'),('original',)))
    for grp,regular in opposites:
        aa=any(has(a,x) for x in grp);bb=any(has(b,x) for x in grp)
        if aa==bb:continue
        other=b if aa else a
        if any(has(other,x) for x in regular):return True
    if has(a,'fresh flavor') and has(b,'original'):return True
    if has(b,'fresh flavor') and has(a,'original'):return True
    return False

def _explicit_flavor_sets_v14(s):
    z=' '+bnorm(s)+' '
    return {x for x in FLAVOR_WORDS if (' '+x+' ') in z}

def _partial_variant_ambiguity_v14(a,b):
    A,B=_explicit_flavor_sets_v14(a),_explicit_flavor_sets_v14(b)
    # Both sides explicitly name flavor/scent tokens, share at least one, but one
    # has an additional named variant token. This is unsafe to auto-certify but
    # can be manually/semantically reviewed rather than hard-rejected.
    return bool(A and B and A!=B and A&B)

def _explicit_bundle_conflict_v14(a,b):
    a=' '+bnorm(a)+' ';b=' '+bnorm(b)+' '
    # "starter kit/device + refill" and "warmer + refill" describe the same bundle role.
    def bundle(s):return (' refill' in s) and any(x in s for x in (' warmer ',' starter kit ',' device '))
    aw,bw=bundle(a),bundle(b);ar=(' refill' in a);br=(' refill' in b)
    if aw and br and not bw:return True
    if bw and ar and not aw:return True
    return False

def _explicit_status_conflict_v14(a,b):
    # Only explicit opposite states conflict. One-sided wording is UNKNOWN.
    a=' '+bnorm(a)+' ';b=' '+bnorm(b)+' '
    def anyp(s,phrases):return any((' '+p+' ') in s for p in phrases)
    if anyp(a,['non organic']) and anyp(b,['organic']):return True
    if anyp(b,['non organic']) and anyp(a,['organic']):return True
    decaf=['decaf','caffeine free'];caf=['caffeinated']
    if (anyp(a,decaf) and anyp(b,caf)) or (anyp(b,decaf) and anyp(a,caf)):return True
    # Milk fat levels are explicit mutually-exclusive variants when both are stated.
    fat_groups=[['fat free','nonfat','skim milk'],['low fat','lowfat'],['reduced fat','2 milk','2 percent milk'],['whole milk']]
    ia=[i for i,g in enumerate(fat_groups) if anyp(a,g)];ib=[i for i,g in enumerate(fat_groups) if anyp(b,g)]
    if ia and ib and ia[0]!=ib[0]:return True
    return False

def _explicit_strength_conflict_v16(a,b):
    a=' '+bnorm(a)+' ';b=' '+bnorm(b)+' '
    def tier(x):
        if any((' '+p+' ') in x for p in ('maximum strength','max strength','ultra strength')):return 'MAX'
        if ' extra strength ' in x:return 'EXTRA'
        if any((' '+p+' ') in x for p in ('original strength','regular strength')):return 'ORIGINAL'
        return ''
    x,y=tier(a),tier(b)
    return bool(x and y and x!=y)

def _explicit_named_variant_conflict_v16(a,b):
    """Small, high-precision families whose members are mutually exclusive SKUs.

    These are deliberately phrase-level, not arbitrary adjective differences.
    """
    a=' '+bnorm(a)+' ';b=' '+bnorm(b)+' '
    families=(
      ('mild mint','arctic mint','cool mint','fresh mint','spearmint','peppermint'),
      ('buttermilk ranch','classic ranch','light ranch','fat free ranch'),
      ('extra moisturizing','night'),
    )
    for fam in families:
        aa=[x for x in fam if (' '+x+' ') in a];bb=[x for x in fam if (' '+x+' ') in b]
        if aa and bb and aa[0]!=bb[0]:return True
    return False

def _one_sided_subtype_risk_v16(a,b):
    a=' '+bnorm(a)+' ';b=' '+bnorm(b)+' '
    # A deep-treatment conditioner and a normal rinse-out conditioner are distinct
    # retail SKUs even when a short title drops the word "deep". Defer to GPT/B
    # metadata rather than auto-certifying the generic side.
    if (' conditioner ' in a and ' conditioner ' in b) and ((' deep conditioner ' in a)!=(' deep conditioner ' in b)):return True
    # Heel cushions/heel cups are not interchangeable with full-length work insoles.
    heel=lambda x:any((' '+p+' ') in x for p in ('heel cushion','heel cushions','heel cup','heel cups'))
    ins=lambda x:' insole ' in x or ' insoles ' in x
    if (heel(a) and ins(b)) or (heel(b) and ins(a)):return True
    return False

def authoritative_blocker(pair,cfg):
    if bool(pair.get('hard_veto')):return 'HARD_VETO'
    bp=str(pair.get('brand_policy','UNKNOWN'))
    if bp not in {'SAME','SAME_INFERRED','PRIVATE','COMMODITY'}:return 'BRAND'
    a,b=str(pair.get('name_A','')),str(pair.get('name_B',''))
    if obvious_title_conflict(pair) or _title_role_conflict(a,b) or _explicit_bundle_conflict_v14(a,b):return 'ROLE'
    if _title_format_conflict(a,b):return 'FORM'
    Afl,Bfl=_explicit_flavor_sets_v14(a),_explicit_flavor_sets_v14(b)
    if (Afl and Bfl and Afl.isdisjoint(Bfl)) or _explicit_variant_conflict_v14(a,b) or _explicit_strength_conflict_v16(a,b) or _explicit_named_variant_conflict_v16(a,b):return 'FLAVOR_SCENT_SHADE'
    if _explicit_pack_conflict_v14(a,b) or _explicit_size_conflict_v14(a,b):return 'QUANTITY'
    if _explicit_status_conflict_v14(a,b):return 'ORGANIC_STATUS'
    q=float(pair.get('quantity_similarity',-1) or -1)
    if q>=0 and q<float(cfg['router'].get('min_quantity_similarity',.72)):return 'QUANTITY'
    return ''

def final_match_firewall(pair,cfg):
    """Constant-time final gate shared by deterministic and GPT MATCH paths.

    No retrieval, DB access, fact extraction, candidate rescoring, or API call occurs
    here. It only examines the already-scored pair plus its two titles.
    """
    hard=authoritative_blocker(pair,cfg)
    if hard:return 'NON_MATCH',hard
    asym=set(pair.get('modifier_asymmetries') or [])
    # These signals are too SKU-defining to auto-certify when present on only one
    # side. We defer rather than reject because the other retailer may have omitted it.
    risky={'model_or_part_identifier','refill_vs_starter_kit','coffee_format','pet_species','pet_life_stage',
           'meat_species','meat_cut','diaper_size','formula_base','absorbency','spf','shade',
           'waterproof_status','washable_longwear_status','release_type','protein_source','produce_species',
           'bundle_flag','assortment_flag','flavor_partial','organic_status_private','explicit_multipack',
           'flavor','scent','product_line','color','occasion_or_holiday'}
    hit=sorted(asym&risky)
    if hit:return 'REVIEW','ATTRIBUTE_ASYMMETRY:'+','.join(hit)
    q=float(pair.get('quantity_similarity',-1) or -1)
    if q<0 and _one_sided_explicit_count_v16(str(pair.get('name_A','')),str(pair.get('name_B',''))):
        return 'REVIEW','ONE_SIDED_EXPLICIT_COUNT'
    if _partial_variant_ambiguity_v14(str(pair.get('name_A','')),str(pair.get('name_B',''))):return 'REVIEW','PARTIAL_FLAVOR_SCENT_VARIANT'
    if _one_sided_subtype_risk_v16(str(pair.get('name_A','')),str(pair.get('name_B',''))):return 'REVIEW','ONE_SIDED_PRODUCT_SUBTYPE'
    return 'MATCH',''

def semantic_match_allowed_v17(pair,z,cfg):
    action,soft=final_match_firewall(pair,cfg)
    # GPT never overrides an explicit deterministic contradiction.
    if action=='NON_MATCH':return False
    if str(z.get('verdict'))!='MATCH' or int(z.get('choice',0) or 0)<1:return False
    if str(z.get('blocker','OTHER'))!='NONE':return False
    ev=str(z.get('evidence','INSUFFICIENT'))
    if ev not in {'CLEAR','PROBABLE'}:return False
    bp=str(pair.get('brand_policy','UNKNOWN'));mt=str(z.get('match_type','NONE'))
    if bp in {'SAME','SAME_INFERRED'} and mt!='SAME_BRAND_PRODUCT':return False
    if bp=='PRIVATE' and mt not in {'PRIVATE_LABEL_EQUIVALENT','SAME_BRAND_PRODUCT'}:return False
    if bp=='COMMODITY' and mt!='COMMODITY_EQUIVALENT':return False
    rank=int(pair.get('retrieval_rank',99) or 99);ps=float(pair.get('pair_support_score',0) or 0);fi=float(pair.get('functional_identity_similarity',0) or 0)
    if bp in {'SAME_INFERRED','COMMODITY'} and (ev!='CLEAR' or rank!=1):return False
    if action=='REVIEW':
        # One-sided metadata is not a contradiction. Let CLEAR semantic evidence
        # resolve it when the candidate is strong; allow PROBABLE only for the
        # common retailer-omission classes. The final catalog/B-metadata guard is
        # still authoritative after this promotion.
        low=('ONE_SIDED_EXPLICIT_COUNT','PARTIAL_FLAVOR_SCENT_VARIANT','ONE_SIDED_PRODUCT_SUBTYPE')
        lowrisk=(soft in low or soft.startswith('ATTRIBUTE_ASYMMETRY:'))
        if ev=='CLEAR':return lowrisk and rank<=2 and ps>=.055 and fi>=.48
        return lowrisk and bp in {'SAME','PRIVATE'} and rank<=2 and ps>=.075 and fi>=.58
    if ev=='PROBABLE':
        if rank>2 or ps<.055:return False
    return True

def gpt_refine(rows,ret,conn,bidx,criteria,cfg,endpoint,api_key,model):
    verdicts,ctx=deterministic(rows,ret,conn,bidx,criteria,cfg)
    jobs=[];need=[];topk=int(cfg['gpt'].get('top_k',2))
    for key,(r,af,broad,pairs) in ctx.items():
        vv=next((x for x in verdicts if str(x.get('_row_key_A'))==str(key)),None)
        if vv is None or vv.get('final_verdict')!='REVIEW':continue
        ranked=sorted([p for p in pairs if not bool(p.get('hard_veto'))],key=lambda p:(-float(p.get('pair_support_score',0) or 0),int(p.get('retrieval_rank',99))))[:topk]
        ps=[]
        for p in ranked:
            c=next((x for x in broad if str(x.get('item_id_B'))==str(p.get('item_id_B'))),None)
            if c is None:continue
            need.append(int(c['b_index']));ps.append((p,c))
        if ps:jobs.append((key,r,ps))
    brows=fetch_brows(conn,need);jj=[]
    for key,r,ps in jobs:
        arr=[]
        for p,c in ps:
            i=int(c['b_index'])
            if i in brows:arr.append({'pair':p,'b':brows[i]})
        if arr:jj.append({'key':str(key),'r':r,'pairs':arr})
    out={str(v['_row_key_A']):v for v in verdicts}
    if not jj:return verdicts

    def run_batches(items,effort,size,workers):
        chunks=[items[i:i+size] for i in range(0,len(items),size)]
        def request(ch):
            def call(part,content_depth=0):
                keys=[j['key'] for j in part]
                try:return parse_semantic_json(gpt_call(endpoint,api_key,model,part,cfg,effort),keys)
                except Exception as e:
                    msg=str(e);is_filter=('content_filter' in msg or 'ResponsibleAIPolicyViolation' in msg)
                    # Azure can reject a whole 24-product request because one retail
                    # title trips a content filter. Isolate only that case so unrelated
                    # products are not silently lost. Depth is bounded and singleton
                    # offenders remain REVIEW. Normal transport failures retain the inherited bounded
                    # one-split behavior to protect runtime.
                    if is_filter and len(part)>1 and content_depth<6:
                        mid=len(part)//2;got={}
                        got.update(call(part[:mid],content_depth+1));got.update(call(part[mid:],content_depth+1));return got
                    if (not is_filter) and content_depth==0 and len(part)>4:
                        mid=len(part)//2;got={}
                        for sub in (part[:mid],part[mid:]):
                            try:got.update(parse_semantic_json(gpt_call(endpoint,api_key,model,sub,cfg,effort),[j['key'] for j in sub]))
                            except Exception as ee:print(f'[gpt][warn] {effort} subgroup skipped:',ee,flush=True)
                        return got
                    print(f'[gpt][warn] {effort} '+('content-filter case' if is_filter else 'batch')+' skipped:',e,flush=True);return {}
            return call(ch)
        got={}
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            for z in ex.map(request,chunks):got.update(z)
        return got

    low_size=int(cfg['gpt']['group_size']);workers=int(cfg['gpt']['concurrency'])
    got=run_batches(jj,str(cfg['gpt'].get('reasoning_effort','low')),low_size,workers)

    # Escalate only genuinely ambiguous low-reasoning results. This spends extra
    # thinking time where it can change a decision instead of on obvious cases.
    sec_max=int(cfg['gpt'].get('secondary_max_products',0) or 0)
    if sec_max>0:
        amb=[]
        for j in jj:
            z=got.get(j['key'])
            if z is None or z.get('verdict')=='UNCERTAIN' or str(z.get('evidence'))=='INSUFFICIENT':amb.append(j)
        amb=amb[:sec_max]
        if amb:
            print(f'[gpt] escalating {len(amb):,} ambiguous case(s) to medium reasoning',flush=True)
            got.update(run_batches(amb,str(cfg['gpt'].get('secondary_reasoning_effort','medium')),int(cfg['gpt'].get('secondary_group_size',12)),max(1,int(cfg['gpt'].get('secondary_concurrency',8)))))

    for j in jj:
        base=out.get(j['key']);z=got.get(j['key'])
        if base is None or z is None:continue
        verdict=str(z['verdict']);choice=int(z['choice']);pair=j['pairs'][choice-1]['pair'] if verdict=='MATCH' and 1<=choice<=len(j['pairs']) else None
        base['gpt_reviewed']=True;base['gpt_reviewer_lean']=verdict;base['gpt_reviewer_confidence']='';base['gpt_reason']='V17_STRUCTURED_EVIDENCE';base['gpt_choice']=choice;base['gpt_match_type']=z.get('match_type','NONE');base['gpt_blocker']=z.get('blocker','OTHER');base['gpt_evidence']=z.get('evidence','INSUFFICIENT')
        if verdict=='MATCH' and pair is not None:
            action,block=final_match_firewall(pair,cfg);base['final_safety_blocker']=block
            if action=='NON_MATCH':
                base['final_verdict']='NON_MATCH';base['selected_item_id_B']='';base['manual_review_required']=False;base['decision_reason']='V17_FINAL_SAFETY_NON_MATCH';base['educated_guess']='NON_MATCH';base['educated_guess_item_id_B']=str(pair.get('item_id_B',''))
            elif action in {'MATCH','REVIEW'} and semantic_match_allowed_v17(pair,z,cfg):
                nv=route_group([pair],cfg);nv['_row_key_A']=j['key'];nv['final_verdict']='MATCH';nv['selected_item_id_B']=pair['item_id_B'];nv['manual_review_required']=False;nv['decision_reason']='GPT_V17_STRUCTURED_MATCH';nv['semantic_soft_resolved']=block if action=='REVIEW' else '';nv['final_safety_blocker']='';nv['gpt_reviewed']=True;nv['gpt_reviewer_lean']='MATCH';nv['gpt_reviewer_confidence']='';nv['gpt_reason']='V17_STRUCTURED_EVIDENCE';nv['gpt_choice']=choice;nv['gpt_match_type']=z.get('match_type','NONE');nv['gpt_blocker']=z.get('blocker','NONE');nv['gpt_evidence']=z.get('evidence','PROBABLE');out[j['key']]=nv
        # GPT NON_MATCH remains REVIEW unless the deterministic layer already had
        # an authoritative explicit blocker. We optimize recall without letting a
        # semantic opinion erase a potentially valid match.
    return [out[str(r.get('_row_key_A'))] for r in rows]

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--build-index',action='store_true');ap.add_argument('--det',action='store_true');ap.add_argument('--gpt',action='store_true');ap.add_argument('--b');ap.add_argument('--db',required=True);ap.add_argument('--index',required=True);ap.add_argument('--a');ap.add_argument('--out');ap.add_argument('--queue');a=ap.parse_args();cfg=loadj(CONFIG);criteria=load_criteria(CRITERIA)
    if a.build_index:
        n=build_index(a.b,a.index,a.db);init_fact_cache(a.db);print(f'[index] {n:,} B products -> compact inverted index + SQLite detail store + lazy fact cache',flush=True);return
    conn=open_detail_db(a.db);ret=CompactRetriever(a.index,cfg['retrieval']['pool_k']);bidx=brand_index(conn);fast=False
    rows=read_csv(a.a)
    try:
        if a.det:
            t0=time.time();v,_=deterministic(rows,ret,conn,bidx,criteria,cfg);q=queue_from_verdicts(rows,v,cfg);atomic_csv(v,a.out);atomic_csv(q,a.queue,fields=(list(rows[0].keys())+['_gpt_priority']) if rows else ['_row_key_A','item_id','_gpt_priority']);deep=sum(1 for x in v if int(float(x.get('candidate_count',0) or 0))>0);print('[worker]',dict(Counter(x['final_verdict'] for x in v)),f'| deep {deep:,}/{len(v):,} | {time.time()-t0:.1f}s',flush=True);fast=True
        elif a.gpt:
            v=gpt_refine(rows,ret,conn,bidx,criteria,cfg,os.environ['BB_ENDPOINT'],os.environ['BB_API_KEY'],os.environ['BB_MODEL']);atomic_csv(v,a.out);fast=True
        else:raise SystemExit('choose --build-index, --det, or --gpt')
    finally:
        if not fast: conn.close()
    if fast:sys.stdout.flush();sys.stderr.flush();os._exit(0)
if __name__=='__main__':main()
