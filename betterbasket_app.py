#!/usr/bin/env python3
"""BetterBasket Audited v17 launcher. Standard library only."""
from __future__ import annotations
import concurrent.futures as cf
import csv, hashlib, json, os, re, sqlite3, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from collections import Counter, defaultdict
from pathlib import Path

BASE=Path(__file__).resolve().parent;WORKER=BASE/'betterbasket_pipeline_runner.py';CONFIG=BASE/'betterbasket_runtime_config.json'
def clean_input(s):
    s=(s or '').strip()
    if len(s)>=2 and s[0]==s[-1] and s[0] in {'"',"'"}:s=s[1:-1]
    return s.strip()
def yn(prompt,default=True):
    x=clean_input(input(prompt)).lower();return default if not x else x in {'y','yes','1','true'}
def loadj(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def atomic_json(x,p):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);t=p.with_suffix(p.suffix+'.tmp');t.write_text(json.dumps(x,indent=2),encoding='utf-8');os.replace(t,p)
def is_url(x):return urllib.parse.urlparse(x).scheme.lower() in {'http','https'}
def download(url,dst):
    dst=Path(dst);dst.parent.mkdir(parents=True,exist_ok=True);print('[download]',url);req=urllib.request.Request(url,headers={'User-Agent':'BetterBasketMatcher/8'})
    with urllib.request.urlopen(req,timeout=180) as r,open(dst,'wb') as f:
        for b in iter(lambda:r.read(1024*1024),b''):f.write(b)
    return dst
def resolve_dataset(x,root,label):
    x=clean_input(x)
    if is_url(x):
        p=Path(root)/'input_cache'/(Path(urllib.parse.urlparse(x).path).name or f'{label}.csv')
        if not p.exists():download(x,p)
        return p.resolve()
    p=Path(x).expanduser().resolve()
    if not p.exists():raise FileNotFoundError(p)
    return p
def file_sig(p):
    s=Path(p).stat();return [str(Path(p).resolve()),s.st_size,s.st_mtime_ns]
def run_signature(a,b):return hashlib.sha256(json.dumps([file_sig(a),file_sig(b),loadj(CONFIG)],sort_keys=True).encode()).hexdigest()
def worker_cmd(*args,env=None):
    cmd=[sys.executable,str(WORKER),*map(str,args)];print('[run]',' '.join(cmd[:12])+(' ...' if len(cmd)>12 else ''),flush=True);t=time.time();subprocess.run(cmd,check=True,env=env);print(f'[stage] {time.time()-t:.1f}s',flush=True)
def csv_count(path):
    try:
        with open(path,newline='',encoding='utf-8-sig') as f:return dict(Counter(r.get('final_verdict','') for r in csv.DictReader(f) if r.get('final_verdict')))
    except:return {}
def csv_nonempty(path):
    try:
        with open(path,newline='',encoding='utf-8-sig') as f:return next(csv.DictReader(f),None) is not None
    except:return False

def gpt_http(endpoint,key,model,messages,timeout=60,extra=None):
    url=endpoint.rstrip('/')+'/chat/completions';payload={'model':model,'messages':messages};payload.update(extra or {});body=json.dumps(payload).encode();req=urllib.request.Request(url,data=body,headers={'Content-Type':'application/json','Authorization':f'Bearer {key}','api-key':key},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:raise RuntimeError(f'HTTP {e.code}: '+e.read().decode('utf-8','replace')[:1200]) from None
def test_gpt(endpoint,key,model):
    schema={'type':'json_schema','json_schema':{'name':'bb_capability_test','strict':True,'schema':{'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}}}
    extra={'reasoning_effort':'low','response_format':schema,'max_completion_tokens':200}
    obj=gpt_http(endpoint,key,model,[{'role':'developer','content':'Return the requested structured capability-test result.'},{'role':'user','content':'Set ok=true.'}],timeout=60,extra=extra);c=obj['choices'][0]['message']['content']
    if isinstance(c,list):c=''.join(str(x.get('text','')) if isinstance(x,dict) else str(x) for x in c)
    try:z=json.loads(str(c))
    except Exception as e:raise RuntimeError('Structured-output capability test returned invalid JSON: '+str(c)[:400]) from e
    if z.get('ok') is not True:raise RuntimeError('Unexpected structured GPT response: '+str(c)[:400])
    print('[gpt] capability test passed: reasoning_effort=low + strict Structured Outputs')

def build_index(b,index,db):
    if Path(db).exists() and Path(index).exists() and Path(index).stat().st_size>1024*1024:
        print(f'[index] reuse {index} + {db}');return
    print('\n========== BUILD STORE-B COMPACT INDEX ==========')
    worker_cmd('--build-index','--b',b,'--index',index,'--db',db)
def write_chunk(rows,fields,path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);t=p.with_suffix(p.suffix+'.tmp')
    with open(t,'w',newline='',encoding='utf-8') as f:w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
    os.replace(t,p)
def prepare_jobs(a_path,root,chunk_size):
    detdir=root/'deterministic';qdir=root/'gpt_queue';tmp=root/'tmp'
    for d in (detdir,qdir,tmp):d.mkdir(parents=True,exist_ok=True)
    jobs=[];rows=[];chunk=0;row_key=0
    with open(a_path,newline='',encoding='utf-8-sig') as f:
        r=csv.DictReader(f);fields=list(r.fieldnames or [])+['_row_key_A']
        for rec in r:
            rec['_row_key_A']=str(row_key);row_key+=1;rows.append(rec)
            if len(rows)==chunk_size:
                out=detdir/f'chunk_{chunk:06d}.csv';q=qdir/f'chunk_{chunk:06d}.csv'
                if not (out.exists() and q.exists()):inp=tmp/f'A_chunk_{chunk:06d}.csv';write_chunk(rows,fields,inp);jobs.append((chunk,inp,out,q,len(rows)))
                else:print(f'[resume] chunk {chunk} | {csv_count(out)}')
                rows=[];chunk+=1
        if rows:
            out=detdir/f'chunk_{chunk:06d}.csv';q=qdir/f'chunk_{chunk:06d}.csv'
            if not (out.exists() and q.exists()):inp=tmp/f'A_chunk_{chunk:06d}.csv';write_chunk(rows,fields,inp);jobs.append((chunk,inp,out,q,len(rows)))
            else:print(f'[resume] chunk {chunk} | {csv_count(out)}')
    return jobs,row_key

def deterministic_pass(a_path,index,db,root,cfg):
    print('\n========== PASS 1: DETERMINISTIC MATCH/NON-MATCH ==========')
    jobs,total=prepare_jobs(a_path,root,int(cfg['worker_chunk_size']))
    max_conc=int(cfg['deterministic_worker_concurrency']);conc=max_conc
    print(f'[det] {total:,} A rows | adaptive worker concurrency up to {max_conc} | compact inverted retrieval')
    def one(j):
        i,inp,out,q,n=j;print(f'\n--- worker {i} | {n:,} A products ---',flush=True);t=time.time()
        try:worker_cmd('--det','--a',inp,'--index',index,'--db',db,'--out',out,'--queue',q)
        finally:Path(inp).unlink(missing_ok=True)
        qn=0
        if Path(q).exists():
            with open(q,newline='',encoding='utf-8-sig') as f:qn=sum(1 for _ in csv.DictReader(f))
        return i,csv_count(out),qn,time.time()-t
    pos=0;baseline=None
    while pos<len(jobs):
        wave=jobs[pos:pos+conc];times=[]
        with cf.ThreadPoolExecutor(max_workers=conc) as ex:
            for f in cf.as_completed([ex.submit(one,j) for j in wave]):
                i,c,qn,elapsed=f.result();times.append(elapsed);print(f'[det {i}] {c} | GPT queue {qn:,}',flush=True)
        pos+=len(wave)
        if times:
            med=sorted(times)[len(times)//2]
            if baseline is None:baseline=med
            else:baseline=min(baseline,med)
            if conc>2 and med>baseline*1.8:
                conc=2;print(f'[adaptive] sustained worker slowdown detected ({med:.1f}s vs {baseline:.1f}s baseline); reducing concurrency to 2 to limit CPU/thermal pressure',flush=True)
            elif conc>1 and med>baseline*2.8:
                conc=1;print(f'[adaptive] severe throttling detected; reducing concurrency to 1',flush=True)

def combine_queue(qdir,out,max_total=None):
    seen=set();rows=[];fields=None
    for q in sorted(Path(qdir).glob('chunk_*.csv')):
        if not csv_nonempty(q):continue
        with open(q,newline='',encoding='utf-8-sig') as fi:
            r=csv.DictReader(fi);fields=fields or r.fieldnames
            for row in r:
                k=row.get('_row_key_A','')
                if not k or k in seen:continue
                seen.add(k);rows.append(row)
    def pri(r):
        try:return float(r.get('_gpt_priority',0) or 0)
        except:return 0.0
    rows.sort(key=pri,reverse=True)
    if max_total is not None:rows=rows[:int(max_total)]
    p=Path(out);p.parent.mkdir(parents=True,exist_ok=True);t=p.with_suffix('.tmp')
    with open(t,'w',newline='',encoding='utf-8') as fo:
        w=csv.DictWriter(fo,fieldnames=fields or ['_row_key_A','item_id','_gpt_priority'],extrasaction='ignore');w.writeheader();w.writerows(rows)
    os.replace(t,p);return len(rows)
def gpt_pass(index,db,root,cfg,creds):
    if not creds:return
    endpoint,key,model=creds;print('\n========== PASS 2: REASONED STRUCTURED SEMANTIC ADJUDICATION ==========');od=root/'gpt_overrides';od.mkdir(parents=True,exist_ok=True);allq=root/'gpt_queue'/'all_queued_products.csv';out=od/'all.csv'
    if out.exists():print('[resume] GPT |',csv_count(out));return
    n=combine_queue(root/'gpt_queue',allq,cfg['gpt'].get('max_total_products'))
    if not n:out.write_text('_row_key_A,final_verdict\n',encoding='utf-8');print('[gpt] no queued products');return
    g=int(cfg['gpt']['group_size']);print(f'[gpt] selected {n:,} unresolved products | at most {(n+g-1)//g:,} structured requests | concurrency {cfg["gpt"]["concurrency"]} | reasoning {cfg["gpt"].get("reasoning_effort","low")}')
    env=os.environ.copy();env.update({'BB_ENDPOINT':endpoint,'BB_API_KEY':key,'BB_MODEL':model});worker_cmd('--gpt','--a',allq,'--index',index,'--db',db,'--out',out,env=env);print('[gpt]',csv_count(out))
def load_overrides(path):
    d={}
    if not csv_nonempty(path):return d
    with open(path,newline='',encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):d[r.get('_row_key_A','')]=r
    return d
# v16 final catalog-level uniqueness guard ------------------------------------
_COLLISION_GENERIC=set('the a an and or with for of to in on by from at each per brand product products item items packaging may vary not pictured made real value premium quality authentic traditional fresh'.split())
_COLLISION_UNITS=set('oz ounce ounces fl fluid lb lbs pound pounds g kg mg mcg ml l gal gallon gallons qt quart ct count pk pack packs bottle bottles box boxes can cans bag bags pouch pouches jar jars'.split())
_COLLISION_PRIVATE=['great value','equate','marketside','mainstays','better homes gardens','parent s choice','good gather','up up','kirkland signature','wegmans']
_COLLISION_STRONG=set('rc remote control playset bundle kit holiday halloween easter christmas valentine mini small medium large jumbo oversized xl xxl black white red blue green pink purple brown gold silver gray grey beige clear yellow vanilla chocolate strawberry blueberry raspberry lemon lime orange apple cherry peach mango honey cinnamon garlic ranch mint peppermint coconut pineapple banana caramel hazelnut pumpkin grape watermelon cucumber berry mocha latte lavender linen citrus floral ocean rose eucalyptus unscented fragrance leather smoke cedar sandalwood decaf peanut oregano jalapeno shea kiwi reduced'.split())

def _c_norm(s):return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9:]+',' ',str(s or '').lower())).strip()
def _c_strip_private(s):
    z=_c_norm(s)
    for p in _COLLISION_PRIVATE:
        bp=_c_norm(p)
        if z==bp:return ''
        if z.startswith(bp+' '):return z[len(bp):].strip()
    return z

def _c_counts(s):
    z=str(s or '').lower().replace('×','x');z=re.sub(r'\s+',' ',z);out=set()
    desc=r'(?:(?:double|triple|mega|mega\s+xl|mega\s+xxl|super\s+mega|jumbo|regular|standard|bonus|plus|flip[- ]?top)\s+){0,3}'
    noun=r'(?:waffles?|rolls?|wipes?|refills?|bottles?|boxes?|cans?|pods?|packets?|pouches?|bars?|sticks?|tablets?|caplets?|capsules?|gummies|diapers?|liners?|pads?|tampons?|cups?|bags?|pieces?|pcs?|filters?|razors?|blades?|toothbrushes?|brushes?|markers?|pens?|pencils?|batteries?|bulbs?)'
    for pat in (r'\b(\d+)\s*/\s*(?:pk|pack|ct|count)\b',r'\bpack\s+of\s+(\d+)\b',r'\b(\d+)\s*(?:ct|count)\b',r'\b(\d+)\s*(?:pack|pk)\b',r'\b(\d+)\s+(?:(?:single\s+serve|single\s+cup)\s+)?(?:(?:k[- ]?cup)\s+)?pods?\b',rf'\b(\d+)\s*{desc}{noun}\b'):
        for m in re.finditer(pat,z):
            try:
                n=int(m.group(1))
                if n>0:out.add(n)
            except:pass
    for m in re.finditer(r'\b(\d+)\s*x\s*(\d+)\s*(?:ct|count)\b',z):
        try:out.add(int(m.group(1))*int(m.group(2)))
        except:pass
    for m in re.finditer(r'\b(\d+)\s*x\s*\d+(?:\.\d+)?\s*(?:fl\.?\s*oz|oz|ml|l|g|kg|lb|lbs|qt|quart|gal|gallon)\b',z):
        try:out.add(int(m.group(1)))
        except:pass
    return out

def _c_roll_tier(s):
    z=' '+_c_norm(s)+' '
    if ' roll' not in z:return ''
    for x in ('mega xxl','mega xl','super mega','triple plus','double plus','triple','double','jumbo','mega','regular'):
        if (' '+x+' roll') in z or (' '+x+' rolls') in z:return x
    return ''

def _c_residual(a,b):
    A=_c_strip_private(a).split();B=set(_c_strip_private(b).split());out=set()
    for t in A:
        if t in B or t in _COLLISION_GENERIC or t in _COLLISION_UNITS:continue
        if re.fullmatch(r'\d+(?::\d+)?(?:\.\d+)?(?:oz)?',t):continue
        if len(t)<=1:continue
        out.add(t)
    return out

def _catalog_collision_downgrades(groups):
    """Return row_key -> audit reason for incompatible many-A-to-one-B MATCHes.

    This runs only once after GPT, over the ~10k proposed MATCH rows. It never
    performs retrieval, DB access, rescoring, fact extraction, or API calls.
    Exact/generic duplicate A listings are kept; only incompatible configuration
    or materially divergent A-specific residual variants are deferred to REVIEW.
    """
    bad={}
    for bid,rows in groups.items():
        if not bid or len(rows)<2:continue
        b=str(rows[0].get('best_candidate_name_B',''));bc=_c_counts(b);bt=_c_roll_tier(b)
        ac=[_c_counts(r.get('best_candidate_name_A','')) for r in rows]
        at=[_c_roll_tier(r.get('best_candidate_name_A','')) for r in rows]
        # If B states a count/tier, only explicitly compatible A configs survive.
        if bc:
            for r,x in zip(rows,ac):
                if x and x.isdisjoint(bc):bad[str(r.get('_row_key_A',''))]='B_COLLISION_COUNT_CONFIG'
        else:
            sig={tuple(sorted(x)) for x in ac if x}
            if len(sig)>1:
                for r,x in zip(rows,ac):
                    if x:bad[str(r.get('_row_key_A',''))]='B_COLLISION_COUNT_VARIANTS'
        if bt:
            for r,x in zip(rows,at):
                if x and x!=bt:bad[str(r.get('_row_key_A',''))]='B_COLLISION_ROLL_TIER'
        else:
            sig={x for x in at if x}
            if len(sig)>1:
                for r,x in zip(rows,at):
                    if x:bad[str(r.get('_row_key_A',''))]='B_COLLISION_ROLL_VARIANTS'

        # Dynamic variant names (scents, colors, seasonal/model names) are often
        # absent from B's generic title. Compare A-only residual identity tokens
        # within the same B group and remove wording shared by most A listings.
        rs=[_c_residual(r.get('best_candidate_name_A',''),b) for r in rows]
        freq=Counter(t for x in rs for t in x);common={t for t,n in freq.items() if n/len(rows)>=.65}
        rr=[x-common for x in rs];uniq={frozenset(x) for x in rr if x}
        divergent=False
        # A generic/exact A anchor plus a more specific strong A variant cannot
        # both be the same single Store-B SKU. Keep the anchor, defer the specific row.
        if any(not x for x in rr) and any(x&_COLLISION_STRONG for x in rr if x):divergent=True
        elif len(rows)>=3 and len(uniq)>=3:divergent=True
        elif len(uniq)>=2:
            uu=list(uniq)
            for i,x in enumerate(uu):
                for y in uu[i+1:]:
                    jac=len(x&y)/max(1,len(x|y))
                    if jac<.20 and ((x|y)&_COLLISION_STRONG):divergent=True;break
                if divergent:break
        if divergent:
            for r,x in zip(rows,rr):
                if not x:continue  # keep exact/generic anchor listings
                k=str(r.get('_row_key_A',''))
                # Strong explicit count/tier reasons remain more informative.
                bad.setdefault(k,'B_COLLISION_VARIANT_IDENTITY')
    return bad

# v16 metadata-aware final certification --------------------------------------
def _num(x,default=0.0):
    try:return float(x)
    except:return default

def _bmeta_load(db,bids):
    ids=[str(x) for x in bids if str(x)]
    out={}
    if not ids:return out
    con=sqlite3.connect(str(db),timeout=30)
    try:
        for i in range(0,len(ids),800):
            ch=ids[i:i+800];q=','.join('?' for _ in ch)
            for r in con.execute(f'SELECT item_id,name,brand_raw,description,item_info,sizing_comp,tags FROM products WHERE item_id IN ({q})',ch):
                out[str(r[0])]={'item_id':str(r[0]),'name':r[1] or '','brand_raw':r[2] or '','description':r[3] or '','item_info':r[4] or '','sizing_comp':r[5] or '','tags':r[6] or ''}
    finally:con.close()
    return out

def _bsize(m):
    try:z=json.loads(str((m or {}).get('sizing_comp','') or '{}'))
    except:z={}
    vals=[]
    for k in ('size_user_friendly','size'):
        v=z.get(k)
        if v not in (None,'') and str(v).lower()!='nan':vals.append(str(v).strip())
    return ' | '.join(dict.fromkeys(vals))

def _amt(s):
    z=str(s or '').lower().replace('×','x');z=re.sub(r'\b(\d+)\s+(\d+)\s*/\s*(\d+)\b',lambda m:str(int(m.group(1))+int(m.group(2))/max(1,int(m.group(3)))),z)
    out={'mass':[],'vol':[],'oz_any':[]}
    specs=[('vol',r'\b(\d+(?:\.\d+)?)\s*(?:fl\.?\s*oz\.?|fluid\s*ounces?|fz)\b',29.5735295625),('vol',r'\b(\d+(?:\.\d+)?)\s*(?:ml|milliliters?)\b',1.0),('vol',r'\b(\d+(?:\.\d+)?)\s*(?:l|liters?|litres?)\b',1000.0),('vol',r'\b(\d+(?:\.\d+)?)\s*(?:qt|quart|quarts)\b',946.352946),('vol',r'\b(\d+(?:\.\d+)?)\s*(?:gal|gallon|gallons)\b',3785.411784),('mass',r'\b(\d+(?:\.\d+)?)\s*(?:oz\.?|ounce|ounces)\b',28.349523125),('mass',r'\b(\d+(?:\.\d+)?)\s*(?:lb|lbs|pound|pounds)\b',453.59237),('mass',r'\b(\d+(?:\.\d+)?)\s*(?:kg|kilograms?)\b',1000.0),('mass',r'\b(\d+(?:\.\d+)?)\s*(?:g|grams?)\b',1.0)]
    occ=[]
    for dim,pat,mul in specs:
        for m in re.finditer(pat,z):
            if any(m.start()<e and m.end()>b for b,e in occ):continue
            if dim=='mass' and re.match(r'\s*(?:of\s+)?(?:protein|sugar|carbs?|fiber|fat)\b',z[m.end():m.end()+24]):continue
            try:v=float(m.group(1));out[dim].append(v*mul);occ.append((m.start(),m.end()))
            except:pass
    for m in re.finditer(r'\b(\d+(?:\.\d+)?)\s*(?:(?:fl\.?\s*)?oz\.?|ounces?)\b',z):
        try:out['oz_any'].append(float(m.group(1)))
        except:pass
    return out

def _near_any(a,b,tol=.05):
    return any(abs(x-y)<=tol*max(abs(x),abs(y),1e-9) for x in a for y in b)

def _a_amount_candidates(s):
    x=_amt(s);counts=sorted(v for v in _c_counts(s) if 1<v<=200)
    # Store-B structured size frequently stores total package content while A's
    # title states per-unit content + count. Compare both representations.
    if len(counts)==1:
        n=counts[0]
        for dim in ('mass','vol','oz_any'):
            base=list(x[dim])
            x[dim]=base+[v*n for v in base]
    return x

def _variant_tier(s):
    z=' '+_c_norm(s)+' '
    if any((' '+x+' ') in z for x in ('maximum strength','max strength','ultra strength')):return 'MAX'
    if ' extra strength ' in z:return 'EXTRA'
    if any((' '+x+' ') in z for x in ('original strength','regular strength')):return 'ORIGINAL'
    return ''

def _named_variant_conflict(a,b):
    a=' '+_c_norm(a)+' ';b=' '+_c_norm(b)+' '
    fams=(('mild mint','arctic mint','cool mint','fresh mint','spearmint','peppermint'),('buttermilk ranch','classic ranch','light ranch','fat free ranch'),('extra moisturizing','night'))
    for fam in fams:
        aa=[x for x in fam if (' '+x+' ') in a];bb=[x for x in fam if (' '+x+' ') in b]
        if aa and bb and aa[0]!=bb[0]:return True
    return False

def _metadata_pair_block(row,bm):
    """Use authoritative Store-B metadata that the short scraped title may omit."""
    if not bm:return ''
    a=str(row.get('best_candidate_name_A',''));bn=str(bm.get('name',''));bd=str(bm.get('description','') or '')[:1200];bs=_bsize(bm);bfull=bn+' '+bd
    # Structured Store-B size/count is stronger than omission in the B title.
    ac,bc=_c_counts(a),_c_counts(bs)
    if ac and bc and ac.isdisjoint(bc):return 'B_METADATA_COUNT'
    A,B=_a_amount_candidates(a),_amt(bs)
    for dim in ('mass','vol'):
        if A[dim] and B[dim] and not _near_any(A[dim],B[dim],.05):return 'B_METADATA_SIZE'
    # Retail feeds inconsistently use oz vs fl oz for HBA/household liquids. If
    # both are clearly ounce quantities for the same-brand product, compare the
    # displayed amount as a final backstop.
    if str(row.get('brand_policy','')) in {'SAME','SAME_INFERRED','PRIVATE'} and A['oz_any'] and B['oz_any'] and not _near_any(A['oz_any'],B['oz_any'],.05):return 'B_METADATA_OZ_SIZE'
    ta,tb=_variant_tier(a),_variant_tier(bfull)
    if ta and tb and ta!=tb:return 'B_METADATA_STRENGTH'
    if _named_variant_conflict(a,bn):return 'B_METADATA_VARIANT'
    an=' '+_c_norm(a)+' ';bf=' '+_c_norm(bfull)+' '
    heel=lambda x:any((' '+q+' ') in x for q in ('heel cushion','heel cushions','heel cup','heel cups'))
    ins=lambda x:' insole ' in x or ' insoles ' in x
    if (heel(an) and ins(bf)) or (heel(bf) and ins(an)):return 'B_METADATA_ROLE'
    if ((' bowl ' in an and ' bag ' in (' '+_c_norm(bn)+' ')) or (' bag ' in an and ' bowl ' in (' '+_c_norm(bn)+' '))):return 'B_METADATA_ROLE'
    # Explicit B format from description can reveal a generic-title near miss.
    bpod=any((' '+q+' ') in bf for q in ('k cup','k cups','k cup pods','coffee pod','coffee pods','capsule','capsules'))
    apod=any((' '+q+' ') in an for q in ('k cup','k cups','coffee pod','coffee pods','capsule','capsules'))
    if bpod and not apod and ((' canister ' in an) or (' ground ' in an) or bool(A['mass'])):return 'B_METADATA_FORM'
    return ''

# v17 final national-brand exact-SKU guard ------------------------------------
# The assessment allows broad consumer-equivalence matching for private labels
# and fresh commodities. A national-brand MATCH is different: it must represent
# the same sellable product, not merely the same brand/family. These helpers run
# only after all expensive retrieval/GPT work, over the small provisional MATCH
# set, and only use metadata already loaded by the final merge.
_V17_COLORS=set('white yellow pink red blue green black brown purple orange gold silver gray grey beige'.split())
_V17_FLAVOR=set('vanilla chocolate strawberry blueberry raspberry lemon lime orange apple cherry peach mango honey cinnamon garlic basil ranch bbq barbecue cheddar mozzarella mint peppermint coconut pineapple banana caramel hazelnut pumpkin grape watermelon cucumber berry mocha latte teriyaki barbacoa verde jalapeno cranberry mango peach tropical lavender freesia limon chicken beef steak vegetable aloe bacon'.split())
_V17_STRONG=set('mini reduced lean decaf unscented baby infant kids kid boys boy girls girl men women puppy kitten senior clinical advanced future restore retinol hyaluronic sensitive gentle power flexi keyed laminated weather aluminum steel pet xl xxl mega triple double jumbo holiday halloween christmas valentine platinum shimmer coffeehouse machiatto indulgence salted truffle soothing burst blended tamal shield electric flash nighttime daytime'.split())|_V17_COLORS|_V17_FLAVOR
_V17_STOP=set('the a an and or with for of to in on by from at each per brand product products item items packaging package may vary varies pictured made real premium quality authentic traditional new more select selection selected supplies supply teacher teachers school back child children family retail pegegable peg assorted asstd style styles color colors get helps help use using perfect includes including designed one single'.split())
_V17_UNIT=set('oz ounce ounces fl fluid lb lbs pound pounds g gram grams kg mg mcg ml l liter liters gal gallon gallons qt quart ct count pk pack packs bottle bottles box boxes can cans bag bags pouch pouches jar jars piece pieces pc pcs ea each unit units'.split())

def _v17_words(s):
    return {t for t in _c_norm(s).split() if len(t)>1 and not t.isdigit() and t not in _V17_STOP and t not in _V17_UNIT and not re.fullmatch(r'\d+(?:\.\d+)?',t)}

def _v17_brand_words(bm):return _v17_words((bm or {}).get('brand_raw',''))

def _v17_b_evidence_words(bm):
    if not bm:return set()
    # Keep the description bounded: this is a product-identity backstop, not a
    # second retrieval pass. The first 500 chars usually contain the actual SKU.
    return _v17_words(str(bm.get('name',''))+' '+str(bm.get('description',''))[:500]+' '+str(bm.get('item_info',''))[:250])-_v17_brand_words(bm)

def _v17_color_set(s):
    x=_v17_words(s)&_V17_COLORS
    if 'grey' in x:x.add('gray')
    return x

def _v17_group_value(s,groups):
    w=_v17_words(s);return {i for i,g in enumerate(groups) if w&g}

def _v17_model_ids(s):
    """Extract plausible manufacturer model/part IDs, excluding plain numerics."""
    out=set()
    for m in re.finditer(r'\b[A-Za-z0-9]{1,12}(?:[-/][A-Za-z0-9]{1,12})*\b',str(s or '')):
        x=m.group(0).upper().strip('-/')
        if len(x)<4 or not x[0].isalpha() or not re.search(r'[A-Z]',x) or not re.search(r'\d',x):continue
        if x in {'2IN1','3IN1','4IN1','5IN1','6IN1','7IN1','8IN1','9IN1'}:continue
        out.add(x)
    return out

def _v17_model_compatible(a,b):
    aa=re.sub(r'[-/]','',a);bb=re.sub(r'[-/]','',b)
    if aa==bb:return True
    # Retail descriptions often append a revision/suffix to the same base model
    # (e.g. V912 vs V912USV5). A true conflict needs incompatible bases.
    if min(len(aa),len(bb))>=4 and (aa.startswith(bb) or bb.startswith(aa)):return True
    return False

def _v17_zero_sugar(s):
    z=' '+_c_norm(s)+' '
    return any(x in z for x in (' zero sugar ',' sugar free ',' sugarfree ',' no sugar '))

def _v17_unscented(s):
    z=' '+_c_norm(s)+' '
    return any(x in z for x in (' unscented ',' fragrance free ',' fragrancefree ',' free clean '))

def _v17_explicit_identity_conflict(row,bm):
    """High-precision direct contradictions for national-brand exact SKU identity."""
    if str(row.get('brand_policy','')) not in {'SAME','SAME_INFERRED'} or not bm:return ''
    a=str(row.get('best_candidate_name_A',''));bn=str(bm.get('name',''));bshort=bn+' '+str(bm.get('description',''))[:220]
    # Reliable manufacturer model/part IDs are exact-SKU anchors. If both sides
    # expose incompatible identifiers, semantic similarity cannot override them.
    ma=_v17_model_ids(a);bd=str(bm.get('description','')).strip();lead=re.match(r'^([A-Za-z][A-Za-z0-9/-]{3,24})\b',bd);mb=_v17_model_ids(bn+' '+(lead.group(1) if lead else ''))
    same_prefix=[(x,y) for x in ma for y in mb if re.match(r'^[A-Z]+',x) and re.match(r'^[A-Z]+',y) and re.match(r'^[A-Z]+',x).group(0)==re.match(r'^[A-Z]+',y).group(0)]
    if same_prefix and not any(_v17_model_compatible(x,y) for x,y in same_prefix):return 'V17_NATIONAL_MODEL_PART'
    # Explicit zero/sugar-free status is a separate sellable national-brand SKU.
    # If A says it but all Store-B identity metadata omits it, identity is unresolved.
    if _v17_zero_sugar(a) and not _v17_zero_sugar(bshort):return 'V17_NATIONAL_SUGAR_STATUS_UNRESOLVED'
    # Likewise, unscented/fragrance-free is identity-bearing when A states it.
    if _v17_unscented(a) and not _v17_unscented(bshort):return 'V17_NATIONAL_SCENT_STATUS_UNRESOLVED'
    # Organic and tinted/foundation variants are distinct national-brand SKU
    # identities. Missing B identity is REVIEW, never a forced false match.
    if re.search(r'\borganic\b',a,re.I) and not re.search(r'\borganic\b',bshort+' '+str(bm.get('item_info',''))[:260],re.I):return 'V17_NATIONAL_ORGANIC_STATUS_UNRESOLVED'
    if re.search(r'\bfoundation\b',a,re.I) and not re.search(r'\b(?:foundation|tint|tinted)\b',bshort,re.I):return 'V17_NATIONAL_FOUNDATION_VARIANT_UNRESOLVED'
    # Colors such as white/yellow cornmeal, pink/white icing, black/white brush
    # heads are explicit SKU variants. "clear" is intentionally excluded because
    # it is frequently part of a product-line name (UltraClear) rather than color.
    ca,cb=_v17_color_set(a),_v17_color_set(bn)
    if ca and cb and ca.isdisjoint(cb):return 'V17_NATIONAL_COLOR_VARIANT'
    fams=(
      ({'boy','boys'},{'girl','girls'}),
      ({'men','mens','male'},{'women','womens','female'}),
      ({'baby','infant'},{'kid','kids','child','children'},{'adult','adults'}),
    )
    for fam in fams:
        aa=_v17_group_value(a,fam);bb=_v17_group_value(bn,fam)
        if aa and bb and aa.isdisjoint(bb):return 'V17_NATIONAL_AUDIENCE_VARIANT'
    # Some feeds put gender in the leading description while the title is generic
    # (GoodNites is a real v16 example). Only consult the short prefix for this
    # binary family; do not use arbitrary description adjectives as variant truth.
    if not _v17_group_value(bn,({'boy','boys'},{'girl','girls'})):
        bpre=str(bm.get('description',''))[:120]
        aa=_v17_group_value(a,({'boy','boys'},{'girl','girls'}));bb=_v17_group_value(bpre,({'boy','boys'},{'girl','girls'}))
        if aa and bb and aa.isdisjoint(bb):return 'V17_NATIONAL_GENDER_METADATA'
    # Roast is only compared when both titles explicitly say roast.
    wa,wb=_v17_words(a),_v17_words(bn)
    if 'roast' in wa and 'roast' in wb:
        aa=_v17_group_value(a,({'light'},{'medium'},{'dark'}));bb=_v17_group_value(bn,({'light'},{'medium'},{'dark'}))
        if aa and bb and aa.isdisjoint(bb):return 'V17_NATIONAL_ROAST_VARIANT'
    # Branded strength/tier words are sellable-SKU identity, not cosmetic wording.
    # Requiring explicit competing tier words on both sides keeps this fail-closed
    # without turning ordinary one-sided marketing omissions into false vetoes.
    tier_groups=({'clinical'},{'advanced'},{'maximum','max'},{'regular','original'})
    aa=_v17_group_value(a,tier_groups);bb=_v17_group_value(bn+' '+str(bm.get('description',''))[:140],tier_groups)
    if aa and bb and aa.isdisjoint(bb):return 'V17_NATIONAL_TIER_VARIANT'
    # A few dressing-style flavor labels are mutually exclusive named variants
    # when both are explicit. This catches a known generic-family collapse while
    # preserving private-label equivalence and one-sided missing metadata.
    dress_groups=({'balsamic'},{'sweet'},{'ranch'},{'italian'},{'caesar'})
    aa=_v17_group_value(a,dress_groups);bb=_v17_group_value(bn,dress_groups)
    if aa and bb and aa.isdisjoint(bb):return 'V17_NATIONAL_DRESSING_VARIANT'
    active_groups=({'retinol'},{'hyaluronic'},{'salicylic'},{'benzoyl'})
    aa=_v17_group_value(a,active_groups);bb=_v17_group_value(bshort,active_groups)
    if aa and bb and aa.isdisjoint(bb):return 'V17_NATIONAL_ACTIVE_VARIANT'
    timing_groups=({'express'},{'overnight'})
    aa=_v17_group_value(a,timing_groups);bb=_v17_group_value(bn,timing_groups)
    if aa and bb and aa.isdisjoint(bb):return 'V17_NATIONAL_TIMING_VARIANT'
    # Final audit backstop for named national-brand lines/flavors that the generic
    # lexical/GPT layer can otherwise collapse. These are deliberately narrow:
    # either both sides expose mutually exclusive concrete names, or one side
    # exposes a SKU-defining marker that the other's full identity metadata does
    # not establish. Unknown identity is REVIEW, never a wildcard.
    afull=' '+_c_norm(a)+' ';bident=' '+_c_norm(bn+' '+str(bm.get('description',''))[:760]+' '+str(bm.get('item_info',''))[:240])+' '
    named_groups=(
      (' deep scalp cleanse ',' dry scalp care '),
      (' bacteria protection ',' gum protection '),
      (' odor eliminators ',' white bright '),
      (' reggae medley ',' mango carrot '),
      (' new car ',' platinum '),
      (' disney princess ',' disney s moana '),
      (' platinum rinse aid ',' power dry '),
      (' ground dinner ',' minichunks '),
      (' lean ',' original '),
    )
    for fam in named_groups:
        av=[x for x in fam if x in afull];bv=[x for x in fam if x in bident]
        if av and bv and av[0]!=bv[0]:return 'V17_NATIONAL_NAMED_VARIANT'
    # A small set of especially SKU-defining named variants found in the final
    # audit must be positively present on both sides. This avoids accepting a
    # generic family listing as an exact national-brand SKU.
    for marker in (' fruit punch ',' retinol 24 ',' chocolate chip ',' minis ',' day night ',' sportsmen '):
        if (marker in afull)!=(marker in bident):return 'V17_NATIONAL_NAMED_VARIANT_UNRESOLVED'
    # Fat-free is commonly sold beside the regular national-brand version; one
    # side explicitly saying it while the other does not is not enough for exact
    # SKU certification.
    aff=bool(re.search(r'\bfat[- ]?free\b',str(a),re.I));bff=bool(re.search(r'\bfat[- ]?free\b',bident,re.I))
    fat_sensitive=bool(re.search(r'\b(?:dressing|yogurt|yoghurt|cheese|refried\s+beans?|milk|cream|mayonnaise|mayo)\b',str(a)+' '+bn,re.I))
    if fat_sensitive and aff!=bff:return 'V17_NATIONAL_FAT_STATUS_UNRESOLVED'
    # A few role/form identities are categorically different sellable products.
    an=' '+_c_norm(a)+' ';bh=' '+_c_norm(bshort)+' '
    if ((' ice cream ' in an or ' sorbet ' in an) and not any(x in bh for x in (' ice cream ',' sorbet ',' frozen dessert ')) and (' fruit ' in bh or ' frozen fruit ' in bh)):return 'V17_NATIONAL_PRODUCT_ROLE'
    if (' foaming oil ' in an or ' cleansing oil ' in an) and ' oil ' not in bh:return 'V17_NATIONAL_FORMULATION_VARIANT'
    # Time-of-day medicine formulas and sodium-reduced foods are distinct SKUs.
    for tag in ('nighttime','daytime'):
        if (' '+tag+' ') in an and (' '+tag+' ') not in bh:return 'V17_NATIONAL_TIME_FORMULA_UNRESOLVED'
    alow=bool(re.search(r'\b(?:low|reduced)\s+sodium\b|\breducido\s+en\s+sodio\b',str(a),re.I));blow=bool(re.search(r'\b(?:low|reduced)\s+sodium\b|\breducido\s+en\s+sodio\b',bshort,re.I))
    if alow and not blow:return 'V17_NATIONAL_SODIUM_STATUS_UNRESOLVED'
    # Product-role contradictions found in the final audit. These are semantic
    # category differences, not mere adjective omissions.
    if ' broth ' in an and ' vegetable ' in an and ' no chicken ' in bh:return 'V17_NATIONAL_PRODUCT_ROLE'
    if (' mac cheese ' in an or ' mac and cheese ' in an) and (' pasta sauce ' in bh or ' alfredo sauce ' in bh):return 'V17_NATIONAL_PRODUCT_ROLE'
    if re.search(r'\bwith\s+pits?\b',str(a),re.I) and re.search(r'\bpitted\b',bshort,re.I):return 'V17_NATIONAL_PIT_VARIANT'
    # Wholesale/marketplace outer packs are different sale units from a single
    # Store-B package even when the inner product is identical.
    oc=re.search(r'\b([2-9]\d*)\s*(?:/|per)\s*(?:carton|case)\b',str(a),re.I)
    if oc and not re.search(r'\b'+re.escape(oc.group(1))+r'\s*(?:/|per)?\s*(?:carton|case)\b',bshort,re.I):return 'V17_NATIONAL_OUTER_CASE'
    if re.search(r'\b(?:twin\s*pk|twin)\s*$',str(a).strip(),re.I) and not re.search(r'\b(?:twin|2\s*pack|2-pack)\b',bshort,re.I):return 'V17_NATIONAL_TWIN_CONFIG'
    # Life-stage words are exact-SKU identity for infant/pet products. Generic
    # demographic prose such as 'for men/women' is intentionally not used here.
    zbh=' '+_c_norm(bshort+' '+str(bm.get('item_info',''))[:180])+' '
    baby_identity=bool(re.search(r'\b(?:baby|infant|newborn)\b',str(a),re.I) and re.search(r'\b(?:food|formula|teether|wipe|wipes|diaper|lotion|wash|shampoo|sunscreen|pacifier|bottle|manicure|nail|medicine|underwear)\b',str(a),re.I))
    if baby_identity and not (re.search(r'\b(?:baby|infant|newborn)\b',zbh,re.I) or re.search(r'\bstage\s*[12]\b|\b\d+\+?\s*months?\b|\b\d+m\+\b',zbh,re.I)):return 'V17_NATIONAL_LIFE_STAGE_UNRESOLVED'
    if re.search(r'\bpuppy\b',str(a),re.I) and not re.search(r'\bpuppy\b',zbh,re.I):return 'V17_NATIONAL_LIFE_STAGE_UNRESOLVED'
    if re.search(r'\bkitten\b',str(a),re.I) and not re.search(r'\bkitten\b',zbh,re.I):return 'V17_NATIONAL_LIFE_STAGE_UNRESOLVED'
    if re.search(r'\bsenior\b',str(a),re.I) and not re.search(r'\b(?:senior|mature|7\+)\b',zbh,re.I):return 'V17_NATIONAL_LIFE_STAGE_UNRESOLVED'
    # Explicit total-count formulations that v16's permissive "any count anchor"
    # rule could miss (e.g. 168 total wipes versus Store-B 42 ct).
    at=_v17_explicit_total_count(a);bc=_v17_structured_count(bm)
    if at is not None and bc is not None and at!=bc:return 'V17_NATIONAL_TOTAL_COUNT'
    # Pellet counts are a common homeopathic package discriminator and may live
    # only in Store-B description rather than structured size.
    ap=_v17_named_unit_count(a,'pellets?');bp=_v17_named_unit_count(bn+' '+str(bm.get('description',''))[:450],'pellets?')
    if ap is not None and bp is not None and ap!=bp:return 'V17_NATIONAL_PELLET_COUNT'
    # Marketplace multipacks/bonus bundles are different sellable configurations
    # unless Store-B metadata positively carries the same package signal.
    am=_v17_leading_multipack(a)
    if am and not _v17_matching_multipack(am,bm):return 'V17_NATIONAL_MULTIPACK_UNCONFIRMED'
    if re.search(r'\bbonus\b',a,re.I) and not re.search(r'\bbonus\b',bn+' '+str(bm.get('description',''))[:180],re.I):return 'V17_NATIONAL_BONUS_CONFIG'
    # Small set of role-bearing extras that change what is actually sold.
    an=' '+_c_norm(a)+' ';bh=' '+_c_norm(bn+' '+str(bm.get('description',''))[:220])+' '
    for phrase in ('broom','faucet','starter kit','bundle','shower mount'):
        if (' '+phrase+' ') in an and (' '+phrase+' ') not in bh:return 'V17_NATIONAL_ROLE_BUNDLE'
    return ''

def _v17_named_unit_count(s,noun_regex):
    vals=[]
    for m in re.finditer(r'\b(\d+)\s*'+noun_regex+r'\b',str(s or '').lower()):
        try:vals.append(int(m.group(1)))
        except:pass
    return vals[-1] if vals else None

def _v17_explicit_total_count(s):
    z=str(s or '').lower().replace('×','x')
    pats=(
      r'\b(\d+)\s+total\s+(?:wipes?|tablets?|capsules?|gummies|diapers?|pads?|liners?|pieces?)\b',
      r'\b(\d+)\s+(?:wipes?|tablets?|capsules?|gummies|diapers?|pads?|liners?|pieces?)\s+total\b',
    )
    for pat in pats:
        m=re.search(pat,z)
        if m:return int(m.group(1))
    m=re.search(r'\b(\d+)\s+(?:flip[- ]?top\s+)?packs?\s+of\s+(\d+)\s+(?:wipes?|tablets?|capsules?|gummies|diapers?|pads?|liners?|pieces?)\b',z)
    if m:return int(m.group(1))*int(m.group(2))
    m=re.search(r'\b(\d+)\s+(?:flip[- ]?top\s+)?packs?.{0,60}?\b(\d+)\s+(?:wipes?|tablets?|capsules?|gummies|diapers?|pads?|liners?|pieces?)\s+per\s+pack\b',z)
    if m:return int(m.group(1))*int(m.group(2))
    m=re.search(r'\b(\d+)\s*x\s*(\d+)\s*\)?\s*(?:ct|count|wipes?|tablets?|capsules?|gummies|diapers?|pads?|liners?|pieces?)\b',z)
    return int(m.group(1))*int(m.group(2)) if m else None

def _v17_structured_count(bm):
    z=_bsize(bm)
    m=re.search(r'\b(\d+)\s*(?:ct|count)\b',str(z or '').lower())
    return int(m.group(1)) if m else None

def _v17_leading_multipack(s):
    z=str(s or '').lower().replace('×','x')
    for pat in (r'^\s*\(?\s*([2-9]\d*)\s*(?:pack|pk)\b',r'^\s*\(?\s*([2-9]\d*)\s*x\s*[-–—]',r'\bpack\s+of\s+([2-9]\d*)\b',r'(?:^|[\s(\-])([2-9]\d*)\s*[- ]?packs?\b'):
        m=re.search(pat,z)
        if m:return int(m.group(1))
    return None

def _v17_matching_multipack(n,bm):
    text=str((bm or {}).get('name',''))+' '+str((bm or {}).get('description',''))[:220]+' '+_bsize(bm)
    z=str(text).lower().replace('×','x')
    return bool(re.search(r'\b'+str(int(n))+r'\s*(?:pack|pk|ct|count)\b',z) or re.search(r'\bpack\s+of\s+'+str(int(n))+r'\b',z))

def _v17_roll_tier(s):
    z=' '+_c_norm(s)+' '
    for x in ('mega xxl','mega xl','super mega','triple plus','double plus','triple','double','jumbo','mega','regular'):
        if re.search(r'\b'+re.escape(x)+r'\s+rolls?\b',z):return x
    return ''

def _v17_roll_count(s):
    z=_c_norm(s);m=re.search(r'\b(\d+)\s+(?:(?:super\s+)?mega|triple|double|jumbo|regular)?\s*rolls?\b',z)
    return int(m.group(1)) if m else None

def _v17_sheets_per_roll(s):
    m=re.search(r'\b(\d+)\s+sheets?\s*(?:per|/)\s*roll\b',str(s or '').lower())
    return int(m.group(1)) if m else None

def _v17_package_signature(s):
    v=(_v17_explicit_total_count(s),_v17_roll_count(s),_v17_roll_tier(s),_v17_sheets_per_roll(s))
    return v if any(x not in (None,'') for x in v) else None

def _v17_b_package_signature(bm):
    # Description prefix only: later marketing often enumerates other available
    # pack sizes and must never be mistaken for the current SKU.
    t=str((bm or {}).get('name',''))+' '+str((bm or {}).get('description',''))[:160]+' '+_bsize(bm)
    return (_v17_explicit_total_count(t) or _v17_structured_count(bm),_v17_roll_count(t),_v17_roll_tier(t),_v17_sheets_per_roll(t))

def _v17_package_collision(rows,bm,bad):
    # General explicit unit-count collision (pods, packs, wipes, rolls, etc.).
    # If Store-B establishes a count, incompatible A counts cannot share it. If
    # B omits count but a collision group mixes counted and generic A listings,
    # the explicitly counted configurations are unresolved rather than wildcards.
    btxt=str((bm or {}).get('name',''))+' '+str((bm or {}).get('description',''))[:220]+' '+_bsize(bm)
    bc=_c_counts(btxt);acs=[_c_counts(r.get('best_candidate_name_A','')) for r in rows]
    if bc:
        for r,x in zip(rows,acs):
            if x and x.isdisjoint(bc):bad.setdefault(str(r.get('_row_key_A','')),'V17_COLLISION_GENERAL_COUNT')
    sig=[_v17_package_signature(r.get('best_candidate_name_A','')) for r in rows];bs=_v17_b_package_signature(bm)
    for j,label in enumerate(('TOTAL_COUNT','ROLL_COUNT','ROLL_TIER','SHEETS_PER_ROLL')):
        vals={s[j] for s in sig if s and s[j] not in (None,'')}
        if len(vals)>1:
            for r,s in zip(rows,sig):
                if not s or s[j] in (None,''):continue
                if bs[j] in (None,'') or s[j]!=bs[j]:bad.setdefault(str(r.get('_row_key_A','')),'V17_COLLISION_'+label)
        elif bs[j] not in (None,''):
            for r,s in zip(rows,sig):
                if s and s[j] not in (None,'') and s[j]!=bs[j]:bad.setdefault(str(r.get('_row_key_A','')),'V17_COLLISION_'+label)

def _v17_identity_coverage(a,bm):
    aw=_v17_words(a)-_v17_brand_words(bm);bw=_v17_b_evidence_words(bm)
    return len(aw&bw)/max(1,len(aw))

def _v17_jaccard(a,b,bm):
    x=_v17_words(a)-_v17_brand_words(bm);y=_v17_words(b)-_v17_brand_words(bm)
    return len(x&y)/max(1,len(x|y))

def _v17_collision_residuals(rows,bm):
    brand=_v17_brand_words(bm);sets=[_v17_words(r.get('best_candidate_name_A',''))-brand for r in rows]
    freq=Counter(t for s in sets for t in s);common={t for t,n in freq.items() if n/len(rows)>=.80}
    return sets,[s-common for s in sets]

def _v17_identity_collision(rows,bm,bad):
    # Private-label/commodity equivalence follows the assessment's broader rule;
    # this additional exact-SKU collision rule is national-brand only.
    active=[r for r in rows if str(r.get('brand_policy','')) in {'SAME','SAME_INFERRED'} and str(r.get('_row_key_A','')) not in bad]
    if len(active)<2:return
    if len(active)==2:
        c=[_v17_identity_coverage(r.get('best_candidate_name_A',''),bm) for r in active]
        j=_v17_jaccard(active[0].get('best_candidate_name_A',''),active[1].get('best_candidate_name_A',''),bm)
        best=0 if c[0]>=c[1] else 1;other=1-best
        # Two A listings can legitimately be wording duplicates; coverage alone
        # is never enough to choose one. Require an unsupported identity-bearing
        # variant below instead of auto-demoting the lower-coverage wording.
        # Two A listings can legitimately be wording duplicates. Defer only an
        # A-only identity-bearing variant when the competing row is materially
        # better supported by Store-B metadata. This preserves abbreviated/exact
        # duplicates while catching generic-SKU collapse (e.g. one named scent
        # or flavor mapped onto another concrete B variant).
        brand=_v17_brand_words(bm);sets=[_v17_words(r.get('best_candidate_name_A',''))-brand for r in active];common=sets[0]&sets[1];res=[x-common for x in sets];bev=_v17_b_evidence_words(bm)
        uns=[(x&_V17_STRONG)-bev for x in res]
        if bool(uns[other]) and not uns[best] and c[best]>=.72 and c[best]-c[other]>=.10 and j<.72:
            bad.setdefault(str(active[other].get('_row_key_A','')),'V17_COLLISION_UNSUPPORTED_VARIANT')
        return
    sets,res=_v17_collision_residuals(active,bm);uniq={frozenset(x) for x in res}
    if len(uniq)<=1:return
    bev=_v17_b_evidence_words(bm)
    for r,x in zip(active,res):
        if not x:continue
        cov=_v17_identity_coverage(r.get('best_candidate_name_A',''),bm);sup=len(x&bev)/max(1,len(x));unsupported=(x&_V17_STRONG)-bev
        if len(active)>=6:
            flag=(cov<.90 and sup<.50) or bool(unsupported)
        else:
            flag=(cov<.65 and sup<.20) or bool(unsupported)
        if flag:bad.setdefault(str(r.get('_row_key_A','')),'V17_COLLISION_NATIONAL_SKU_IDENTITY')

def _recovery_candidate(row):
    """Backstop for GPT MATCHes held only by soft omission guards.

    Fresh v17 workers normally promote these already; final certification still
    rechecks Store-B metadata and catalog consistency before any recovery survives.
    """
    if row.get('final_verdict')!='REVIEW' or str(row.get('gpt_reviewer_lean',''))!='MATCH':return False
    if str(row.get('gpt_blocker',''))!='NONE' or str(row.get('gpt_evidence','')) not in {'CLEAR','PROBABLE'}:return False
    try:choice=int(float(row.get('gpt_choice') or 0))
    except:choice=0
    if choice!=1:return False
    bp=str(row.get('brand_policy',''));mt=str(row.get('gpt_match_type',''));ev=str(row.get('gpt_evidence',''))
    if bp in {'SAME','SAME_INFERRED'} and mt!='SAME_BRAND_PRODUCT':return False
    if bp=='PRIVATE' and mt not in {'PRIVATE_LABEL_EQUIVALENT','SAME_BRAND_PRODUCT'}:return False
    if bp=='COMMODITY' and mt!='COMMODITY_EQUIVALENT':return False
    rank=int(_num(row.get('retrieval_rank_of_best'),99));ps=_num(row.get('pair_support_score'));fi=_num(row.get('functional_identity_similarity'))
    if bp in {'SAME_INFERRED','COMMODITY'}:return ev=='CLEAR' and rank==1 and ps>=.055 and fi>=.48
    if ev=='CLEAR':return bp in {'SAME','PRIVATE'} and rank<=2 and ps>=.055 and fi>=.48
    return bp in {'SAME','PRIVATE'} and rank<=2 and ps>=.075 and fi>=.58

def _a_size_signature(row):
    a=str(row.get('best_candidate_name_A',''));c=tuple(sorted(v for v in _c_counts(a) if v>1));m=_amt(a);oe=None
    if m['oz_any']:oe=m['oz_any'][0]
    elif m['mass']:oe=m['mass'][0]/28.349523125
    elif m['vol']:oe=m['vol'][0]/29.5735295625
    p=_v17_package_signature(a)
    if c or oe is not None or p:return (c,round(oe,2) if oe is not None else None,p)
    return None

def _row_strength(row):
    return (3.0 if str(row.get('gpt_evidence',''))=='CLEAR' else 1.5 if str(row.get('gpt_evidence',''))=='PROBABLE' else 1.0)+2*_num(row.get('pair_support_score'))+_num(row.get('functional_identity_similarity'))+.5*_num(row.get('runner_up_margin'))

def _structured_size_preference(rows,bm,bad):
    """Use B's structured size to select the closest A configuration in a collision."""
    bs=_bsize(bm);B=_a_amount_candidates(bs);bc=_c_counts(bs);scored=[]
    for r in rows:
        A=_a_amount_candidates(str(r.get('best_candidate_name_A','')));errs=[]
        for dim in ('mass','vol'):
            for x in A[dim]:
                for y in B[dim]:errs.append(abs(x-y)/max(abs(x),abs(y),1e-9))
        if str(r.get('brand_policy','')) in {'SAME','SAME_INFERRED','PRIVATE'}:
            for x in A['oz_any']:
                for y in B['oz_any']:errs.append(abs(x-y)/max(abs(x),abs(y),1e-9))
        ac=_c_counts(str(r.get('best_candidate_name_A','')))
        if ac and bc:errs.append(0.0 if not ac.isdisjoint(bc) else 1.0)
        scored.append((min(errs) if errs else None,r))
    comparable=[x for x in scored if x[0] is not None and x[0]<.20]
    if not comparable:return rows
    best=min(x[0] for x in comparable);keep={str(r.get('_row_key_A','')) for e,r in comparable if e<=max(.025,best+.012)};out=[]
    for e,r in scored:
        k=str(r.get('_row_key_A',''))
        if e is not None and k not in keep:bad.setdefault(k,'B_COLLISION_BEST_STRUCTURED_SIZE')
        else:out.append(r)
    return out

def _catalog_collision_downgrades_v17(groups,bmeta):
    """Final metadata + national exact-SKU audit over provisional MATCHes.

    Runtime is O(provisional matches) and uses the single metadata batch already
    loaded for final merge. It never performs retrieval, candidate rescoring, fact
    extraction, or GPT/API calls.
    """
    bad={}
    for bid,rows in groups.items():
        if not bid:continue
        bm=bmeta.get(str(bid),{});survivors=[]
        for r in rows:
            why=_metadata_pair_block(r,bm) or _v17_explicit_identity_conflict(r,bm)
            if why:bad[str(r.get('_row_key_A',''))]=why
            else:survivors.append(r)
        if len(survivors)<2:continue
        survivors=_structured_size_preference(survivors,bm,bad)
        if len(survivors)<2:continue
        _v17_package_collision(survivors,bm,bad)
        _v17_identity_collision(survivors,bm,bad)
    return bad

def _iter_final_rows(chunks,over):
    for p in chunks:
        with open(p,newline='',encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):yield over.get(row.get('_row_key_A',''),row)

def merge_results(root,cfg,db=None):
    print('\n========== FINAL MERGE ==========');over=load_overrides(root/'gpt_overrides'/'all.csv');chunks=sorted((root/'deterministic').glob('chunk_*.csv'));allp=root/'all_verdicts.csv';sub=root/'submission_matches.csv';rev=root/'manual_review.csv';counts=Counter();processed=0
    # Build a tiny provisional acceptance set: existing MATCHes plus GPT MATCHes
    # held only by soft omission guards. No retrieval/rescoring/API work occurs.
    provisional={};groups=defaultdict(list)
    for row in _iter_final_rows(chunks,over):
        k=str(row.get('_row_key_A',''));isrec=_recovery_candidate(row)
        if row.get('final_verdict')=='MATCH' or isrec:
            r=dict(row)
            if isrec:
                r['final_verdict']='MATCH';r['selected_item_id_B']=str(r.get('best_candidate_item_id_B',''));r['manual_review_required']=False;r['decision_reason']='V17_METADATA_RECOVERY_MATCH';r['semantic_soft_resolved']=str(r.get('final_safety_blocker',''));r['final_safety_blocker']=''
            provisional[k]=r;groups[str(r.get('selected_item_id_B',''))].append(r)
    bids=list(groups);bmeta=_bmeta_load(db,bids) if db else {}
    collision_bad=_catalog_collision_downgrades_v17(groups,bmeta)
    if collision_bad:print(f'[v17] final identity/catalog guard deferred {len(collision_bad):,} provisional MATCH row(s)')
    fields=None
    for p in chunks:
        with open(p,newline='',encoding='utf-8-sig') as f:fields=csv.DictReader(f).fieldnames;break
    fields=list(fields or [])
    for x in ['final_safety_blocker','semantic_soft_resolved','gpt_reviewer_lean','gpt_reviewer_confidence','gpt_reason','gpt_choice','gpt_match_type','gpt_blocker','gpt_evidence']:
        if x not in fields:fields.append(x)
    recovered=0;metadata_blocks=0
    with open(allp.with_suffix('.tmp'),'w',newline='',encoding='utf-8') as fa,open(sub.with_suffix('.tmp'),'w',newline='',encoding='utf-8') as fs,open(rev.with_suffix('.tmp'),'w',newline='',encoding='utf-8') as fr:
        wa=csv.DictWriter(fa,fieldnames=fields,extrasaction='ignore');wr=csv.DictWriter(fr,fieldnames=fields,extrasaction='ignore');ws=csv.DictWriter(fs,fieldnames=['item_id_A','item_id_B']);wa.writeheader();wr.writeheader();ws.writeheader()
        for original in _iter_final_rows(chunks,over):
            k=str(original.get('_row_key_A',''));row=dict(provisional.get(k,original))
            if k in collision_bad and row.get('final_verdict')=='MATCH':
                why=collision_bad[k];metadata_blocks+=1;row['final_verdict']='REVIEW';row['manual_review_required']=True;row['selected_item_id_B']='';row['final_safety_blocker']=why;row['decision_reason']='V17_FINAL_IDENTITY_CATALOG_REVIEW'
            elif k in provisional and original.get('final_verdict')!='MATCH' and row.get('final_verdict')=='MATCH':recovered+=1
            v=row.get('final_verdict','');wa.writerow(row);processed+=1;counts[v]+=1
            if v=='MATCH':ws.writerow({'item_id_A':row.get('item_id_A',''),'item_id_B':row.get('selected_item_id_B','')})
            elif v=='REVIEW':wr.writerow(row)
    for p in (allp,sub,rev):os.replace(p.with_suffix('.tmp'),p)
    s={'products_processed':processed,'counts':dict(counts),'certified_matches':counts['MATCH'],'minimum_required_by_assessment':cfg['minimum_submission_requirement'],'minimum_requirement_met':counts['MATCH']>=cfg['minimum_submission_requirement'],'assessment_complete_set_note':'assessment states the complete set would include more than 10000','v17_metadata_recovered_matches':recovered,'v17_final_identity_catalog_reviews':metadata_blocks};atomic_json(s,root/'run_summary.json');print(f'Processed: {processed:,}');print(f"MATCH: {counts['MATCH']:,} | NON_MATCH: {counts['NON_MATCH']:,} | REVIEW: {counts['REVIEW']:,}");print(f'[v17] metadata recovery +{recovered:,} | final identity/catalog reviews {metadata_blocks:,}');print('CSV:',sub);print(f"BetterBasket minimum ({cfg['minimum_submission_requirement']:,}) {'satisfied' if s['minimum_requirement_met'] else 'NOT satisfied'}.")

def find_a_product(path,q):
    q=clean_input(q);hits=[]
    with open(path,newline='',encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            if str(r.get('item_id',''))==q:return r
            if len(hits)<10 and q.lower() in str(r.get('name','')).lower():hits.append(r)
    if not hits:return None
    if len(hits)==1:return hits[0]
    print('Matches:');[print(f"  {i}. [{r.get('item_id','')}] {r.get('name','')}") for i,r in enumerate(hits,1)]
    try:return hits[int(clean_input(input('Choose number: ')))-1]
    except:return None
def compare_one(a_path,index,db,root,creds):
    row=find_a_product(a_path,input('Enter Dataset A item_id or part of product name: '))
    if not row:print('Product not found.');return
    td=root/'single_product';td.mkdir(exist_ok=True);row=dict(row);row['_row_key_A']='single';inp=td/'A.csv';write_chunk([row],list(row),inp);det=td/'det.csv';q=td/'q.csv';go=td/'gpt.csv';worker_cmd('--det','--a',inp,'--index',index,'--db',db,'--out',det,'--queue',q);final=det
    if creds and csv_nonempty(q):
        e,k,m=creds;env=os.environ.copy();env.update({'BB_ENDPOINT':e,'BB_API_KEY':k,'BB_MODEL':m});worker_cmd('--gpt','--a',q,'--index',index,'--db',db,'--out',go,env=env);final=go if csv_nonempty(go) else det
    with open(final,newline='',encoding='utf-8-sig') as f:v=next(csv.DictReader(f));print(f"\nA: {row.get('name','')}\nBest B: {v.get('best_candidate_name_B','')} [{v.get('best_candidate_item_id_B','')}]\nVerdict: {v.get('final_verdict','')}\nMatch confidence: {float(v.get('match_confidence') or 0):.3f}\nNon-match confidence: {float(v.get('nonmatch_confidence') or 0):.3f}")
    if v.get('final_verdict')=='REVIEW':print('Educated guess:',v.get('educated_guess',''))
def main():
    print('='*70);print(' BetterBasket Product Matcher — Audited v17 FINAL SUBMISSION');print(' Recall-Rescue Retrieval -> Match/Non-match -> Reasoned Structured GPT Adjudication -> Router');print('='*70)
    a_in=clean_input(input('Dataset A URL/path: '));b_in=clean_input(input('Dataset B URL/path: '));out_in=clean_input(input('Output folder [betterbasket_run_v17]: ')) or 'betterbasket_run_v17';root=Path(out_in).expanduser().resolve();root.mkdir(parents=True,exist_ok=True);a=resolve_dataset(a_in,root,'store_a');b=resolve_dataset(b_in,root,'store_b');cfg=loadj(CONFIG);creds=None
    if yn('Use BetterBasket GPT-5 nano for narrow REVIEW cases? [Y/n]: ',True):
        endpoint=clean_input(input('GPT deployment endpoint/base_url: '));model=clean_input(input('GPT deployment name/model: '));key=clean_input(input('GPT API key (visible): '))
        try:test_gpt(endpoint,key,model);creds=(endpoint,key,model)
        except Exception as e:
            print('[gpt] connection test FAILED:',e)
            if not yn('Continue without GPT? [Y/n]: ',True):return
    print('\nConfiguration:');print('  A:',a);print('  B:',b);print(f"  Worker chunk: {cfg['worker_chunk_size']:,} A products");print(f"  Deterministic concurrency: {cfg['deterministic_worker_concurrency']}");print(f"  Retrieval: compact rare-token/spec index top {cfg['retrieval']['candidate_k']}");print(f"  Full scoring: top {cfg['retrieval']['deep_k']} + exact-title protected (cap {cfg['retrieval']['deep_cap']})");print(f"  GPT review: up to {cfg['gpt'].get('max_total_products',0)} unresolved products; {cfg['gpt']['group_size']}/request; reasoning={cfg['gpt'].get('reasoning_effort','low')} + structured output");print('  GPT:','ON' if creds else 'OFF');print('  Output:',root)
    if not yn('Start full-dataset pipeline? [Y/n]: ',True):return
    sig=run_signature(a,b);state=root/'run_state.json'
    if state.exists() and loadj(state).get('signature')!=sig:raise SystemExit('Output folder contains checkpoints from another dataset/config. Use a fresh folder.')
    if not state.exists():atomic_json({'signature':sig,'a':str(a),'b':str(b),'version':cfg['version']},state)
    bkey=hashlib.sha256(json.dumps(file_sig(b)).encode()).hexdigest()[:16];index=root/'cache'/f'store_b_compact_{bkey}.pkl';db=root/'cache'/f'store_b_details_{bkey}.sqlite';build_index(b,index,db);deterministic_pass(a,index,db,root,cfg);gpt_pass(index,db,root,cfg,creds);merge_results(root,cfg,db)
    print('\n========== FINAL PRECISION POLISH ==========')
    subprocess.run([sys.executable,str(BASE/'betterbasket_final_certification.py'),str(root),'--no-backup'],check=True)
    while yn('\nCompare a specific Dataset A product against B? [y/N]: ',False):compare_one(a,index,db,root,creds)
if __name__=='__main__':main()
