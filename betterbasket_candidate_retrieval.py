#!/usr/bin/env python3
"""Compact in-memory candidate retrieval + SQLite detail store.

No FTS/BM25 in the hot loop. Store B is represented by a small (~10 MB for
this assessment data) pickle containing only normalized title/brand/core/spec
features and inverted postings. Full product details stay in SQLite and are
fetched only for the few candidates that reach deep scoring.
"""
from __future__ import annotations
import csv, html, math, os, pickle, re, sqlite3, unicodedata
from collections import Counter, defaultdict
from pathlib import Path

TOK_RE=re.compile(r"[a-z0-9]+")
NUM_RE=re.compile(r"\b\d+(?:\.\d+)?(?:\s*(?:fl\s*oz|oz|lb|g|kg|mg|mcg|ml|l|ct|count|pk|pack|%))?\b",re.I)
ALNUM_RE=re.compile(r"\b(?=[a-z0-9-]{3,24}\b)(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)[a-z0-9-]+\b",re.I)
STOP={"the","a","an","and","or","with","for","of","in","on","by","from","to","at","new","premium","classic","original","style","assorted","each","per","size","pack","count","ct","pk","oz","ounce","ounces","lb","lbs","g","kg","ml","l"}
A_PRIVATE_LABELS={"great value","marketside","equate","parent s choice","parents choice","ol roy","special kitty","sam s choice","sams choice","athletic works","mainstays","hyper tough","better homes and gardens","expert gardener","onn","george"}
B_PRIVATE_LABELS={"wegmans"}


def norm(x):
    s="" if x is None else str(x)
    if s.lower()=="nan":s=""
    s=html.unescape(s);s=unicodedata.normalize("NFKC",s).lower().replace("&"," and ").replace("×","x")
    s=re.sub(r"[^a-z0-9%+.\- ]+"," ",s)
    return re.sub(r"\s+"," ",s).strip()

def toks(x):return TOK_RE.findall(norm(x))
def core_tokens(name,brand=""):
    b=set(toks(brand));return {x for x in toks(name) if x not in STOP and x not in b and not x.isdigit()}
def numeric_tokens(*parts):
    s=" ".join(norm(x) for x in parts if x is not None)
    out={re.sub(r"\s+","",m.group(0)) for m in NUM_RE.finditer(s)}
    out.update(m.group(0) for m in ALNUM_RE.finditer(s));return out


def open_detail_db(path):
    uri='file:'+Path(path).resolve().as_posix()+'?mode=ro&immutable=1'
    con=sqlite3.connect(uri,uri=True,timeout=30)
    con.execute('PRAGMA query_only=ON');con.execute('PRAGMA cache_size=-4096')
    return con


def brand_index(con):
    d={}
    for (b,) in con.execute("SELECT DISTINCT brand_norm FROM products WHERE brand_norm<>''"):
        if not b:continue
        # Retrieval normalization preserves useful hyphens (Wish-Bone), while
        # title brand discovery uses punctuation-insensitive tokens. Index both
        # forms but retain the canonical retrieval-normalized brand as the value.
        keys={b.split()[0], re.sub(r'[^a-z0-9]+',' ',b).strip().split()[0]}
        for k in keys:
            if k:d.setdefault(k,[]).append(b)
    for k in d:
        d[k]=sorted(set(d[k]),key=len,reverse=True)
    return d


def build_index(csv_path,index_path,db_path):
    ip=Path(index_path);db=Path(db_path);ip.parent.mkdir(parents=True,exist_ok=True);db.parent.mkdir(parents=True,exist_ok=True)
    itmp=ip.with_suffix(ip.suffix+'.tmp');dtmp=db.with_suffix(db.suffix+'.tmp');itmp.unlink(missing_ok=True);dtmp.unlink(missing_ok=True)
    con=sqlite3.connect(dtmp);con.execute('PRAGMA journal_mode=OFF');con.execute('PRAGMA synchronous=OFF');con.execute('PRAGMA temp_store=MEMORY')
    con.execute('''CREATE TABLE products(
      b_index INTEGER PRIMARY KEY,item_id TEXT,name TEXT,brand_raw TEXT,description TEXT,item_info TEXT,sizing_comp TEXT,tags TEXT,title_norm TEXT,brand_norm TEXT)''')
    rows=[];items=[];df=Counter()
    with open(csv_path,'r',encoding='utf-8-sig',newline='') as f:
        for i,r in enumerate(csv.DictReader(f)):
            title=norm(r.get('name',''));brand=norm(r.get('brand_raw',''));core=core_tokens(title,brand);nums=numeric_tokens(title,r.get('sizing_comp',''))
            items.append((str(r.get('item_id','')),title,brand,tuple(sorted(core)),tuple(sorted(nums))))
            df.update(core)
            rows.append((i,str(r.get('item_id','')),r.get('name','') or '',r.get('brand_raw','') or '',r.get('description','') or '',r.get('item_info','') or '',r.get('sizing_comp','') or '',r.get('tags','') or '',title,brand))
            if len(rows)>=4000:con.executemany('INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?)',rows);rows=[]
    if rows:con.executemany('INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?)',rows)
    con.execute('CREATE INDEX idx_products_title ON products(title_norm)');con.execute('CREATE INDEX idx_products_brand ON products(brand_norm)');con.commit();con.close()
    post=defaultdict(list);npost=defaultdict(list);exact=defaultdict(list);bpost=defaultdict(list);ppost=defaultdict(list)
    for i,(iid,title,brand,core,nums) in enumerate(items):
        exact[title].append(i)
        if brand:bpost[brand].append(i)
        for w in core:
            post[w].append(i)
            if brand in B_PRIVATE_LABELS:ppost[w].append(i)
        for z in nums:npost[z].append(i)
    N=len(items);idf={w:math.log((N+1)/(c+1))+1.0 for w,c in df.items()}
    obj={'items':items,'df':dict(df),'idf':idf,'post':dict(post),'num_post':dict(npost),'exact':dict(exact),'brand_post':dict(bpost),'private_post':dict(ppost)}
    with open(itmp,'wb') as f:pickle.dump(obj,f,pickle.HIGHEST_PROTOCOL)
    os.replace(itmp,ip);os.replace(dtmp,db)
    return N


class CompactRetriever:
    def __init__(self,index_path,pool_k=60):
        with open(index_path,'rb') as f:z=pickle.load(f)
        self.items=z['items'];self.df=z['df'];self.idf=z['idf'];self.post=z['post'];self.num_post=z['num_post'];self.exact=z['exact'];self.brand_post=z.get('brand_post',{});self.private_post=z.get('private_post',{});self.pool_k=int(pool_k)

    def retrieve(self,row,k=20):
        title=norm(row.get('name',''));brand=norm(row.get('brand_raw',''));wc=core_tokens(title,brand);ns=numeric_tokens(title,row.get('sizing_comp',''))
        acc=defaultdict(float);protected=set(self.exact.get(title,()))
        # The rarest title tokens carry most identity information and keep candidate
        # generation O(tokens), not O(catalog). All postings are precomputed.
        terms=sorted((w for w in wc if w in self.df),key=lambda w:(self.df[w],-len(w),w))[:4]
        for w in terms:
            wt=self.idf.get(w,1.0)*1.8
            for i in self.post.get(w,()):acc[i]+=wt
        for z in ns:
            for i in self.num_post.get(z,()):acc[i]+=1.2
        for i in protected:acc[i]+=10.0
        base_ids=set(acc);base_pre=set(sorted(acc,key=acc.get,reverse=True)[:self.pool_k]);same_rescue=set();private_rescue=set()
        # Always give same-brand products a route into the local pool. v10 only
        # did this when the global sparse pool was tiny, which missed obvious
        # products for common terms (Snapple Apple, Simply Lemonade, Wish-Bone,
        # etc.). Most brands have very few B items, so this is cheap.
        if brand and brand in self.brand_post:
            ids=self.brand_post.get(brand,())
            if len(ids)<=600:
                for i in ids:
                    _,_,_,bc,bn=self.items[i];bc=set(bc);bn=set(bn)
                    sh=wc&bc
                    if sh or (ns and ns&bn):
                        if i not in base_pre:same_rescue.add(i)
                        acc[i]+=1.2+0.12*sum(self.idf.get(x,1.0) for x in sh)
            else:
                # Very large brands: use rare-token postings instead of scanning.
                bset=set(ids)
                for w in terms:
                    for i in self.post.get(w,()):
                        if i in bset:
                            if i not in base_pre:same_rescue.add(i)
                            acc[i]+=1.2

        # Cross-retailer private-label rescue. A-only labels such as Great Value,
        # Marketside and Equate are stripped before retrieval; shared product
        # tokens then search only B's retailer private-label inventory.
        if brand in A_PRIVATE_LABELS and brand not in B_PRIVATE_LABELS:
            for w in sorted((x for x in wc if x in self.df),key=lambda x:(self.df[x],-len(x),x))[:6]:
                wt=self.idf.get(w,1.0)*0.9
                for i in self.private_post.get(w,()):
                    if i not in base_pre:private_rescue.add(i)
                    acc[i]+=wt
        if not acc:return []
        pre=sorted(acc,key=acc.get,reverse=True)[:self.pool_k];sc=[]
        aw=sum(self.idf.get(z,1.0) for z in wc) or 1.0
        for i in pre:
            iid,bt,bb,bc0,bn0=self.items[i];bc=set(bc0);bn=set(bn0);inter=wc&bc;union=wc|bc
            wi=sum(self.idf.get(z,1.0) for z in inter);wu=sum(self.idf.get(z,1.0) for z in union) or 1.0;bw=sum(self.idf.get(z,1.0) for z in bc) or 1.0
            wsim=wi/wu;contain=wi/(min(aw,bw) or 1.0);same_brand=1.0 if brand and brand==bb else 0.0
            private_pair=1.0 if brand in A_PRIVATE_LABELS and bb in B_PRIVATE_LABELS else 0.0
            nsim=1.0 if ns and bn and ns==bn else (.5 if ns&bn else 0.0);exact=1.0 if title==bt else 0.0
            # Brand/private-label bonuses affect retrieval only. The independent
            # Match/Non-match engines still decide whether the candidate is safe.
            # Keep v10's same-brand scoring weight; v11 improves recall by
            # candidate inclusion, not by making same-brand evidence stronger.
            base_score=3.2*wsim+1.8*contain+1.0*same_brand+.6*nsim+4.0*exact
            score=base_score+1.0*private_pair
            rescue=''
            inferred=str(row.get('_brand_inferred',''))
            if private_pair and (i in private_rescue or inferred=='PRIVATE_LABEL'):rescue='PRIVATE_LABEL'
            elif same_brand and (i in same_rescue or inferred=='PACK_PREFIX_BRAND'):rescue='SAME_BRAND'
            sc.append((score,i,iid,i in protected,base_score,rescue))
        sc.sort(key=lambda x:(-x[0],x[1]));out=[]
        for rank,(score,i,iid,prot,base_score,rescue) in enumerate(sc[:int(k)],1):out.append({'item_id_B':iid,'b_index':i,'retrieval_rank':rank,'retrieval_score':round(score,6),'base_retrieval_score':round(base_score,6),'retrieval_rescue_type':rescue,'protected':prot})
        return out

    @staticmethod
    def deep_shortlist(candidates,deep_k=3,cap=5):
        keep=list(candidates[:deep_k]);have={x['item_id_B'] for x in keep}
        for x in candidates[deep_k:]:
            if x.get('protected') and x['item_id_B'] not in have:
                keep.append(x);have.add(x['item_id_B'])
                if len(keep)>=cap:break
        return keep[:cap]
