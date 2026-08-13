#!/usr/bin/env python3
"""Final BetterBasket benchmark / database-comparison utility (standard library only).

Modes
-----
1) Dataset profile comparison:
   python betterbasket_benchmark.py profile --a store_a.csv --b store_b.csv
2) Compare two submission CSVs:
   python betterbasket_benchmark.py compare --left old_submission.csv --right new_submission.csv
3) Time a deterministic sample through the production retrieval/scoring path:
   python betterbasket_benchmark.py speed --a store_a.csv --b store_b.csv --sample 2500
"""
from __future__ import annotations
import argparse, csv, hashlib, json, os, shutil, subprocess, sys, tempfile, time
from collections import Counter
from pathlib import Path

BASE=Path(__file__).resolve().parent
WORKER=BASE/'betterbasket_pipeline_runner.py'
CONFIG=BASE/'betterbasket_runtime_config.json'

def _count_rows(path):
    with open(path,newline='',encoding='utf-8-sig') as f:
        r=csv.reader(f); next(r,None); return sum(1 for _ in r)

def _profile(path):
    p=Path(path)
    with open(p,newline='',encoding='utf-8-sig') as f:
        r=csv.DictReader(f); fields=r.fieldnames or []; n=0; blanks=Counter(); ids=[]
        idcol=next((x for x in ('item_id','item_id_A','item_id_B','id') if x in fields),None)
        for row in r:
            n+=1
            for k in fields:
                if not str(row.get(k,'')).strip(): blanks[k]+=1
            if idcol: ids.append(str(row.get(idcol,'')).strip())
    return {
        'path':str(p.resolve()),'rows':n,'columns':fields,'column_count':len(fields),
        'id_column':idcol,'unique_ids':len(set(x for x in ids if x)) if idcol else None,
        'duplicate_ids':(len([x for x in ids if x])-len(set(x for x in ids if x))) if idcol else None,
        'blank_fraction':{k:round(v/n,6) if n else 0 for k,v in blanks.items() if v},
        'size_bytes':p.stat().st_size,
    }

def profile_mode(a,b,out=None):
    pa,pb=_profile(a),_profile(b)
    common=sorted(set(pa['columns'])&set(pb['columns']))
    report={'dataset_A':pa,'dataset_B':pb,'common_columns':common,
            'only_A_columns':sorted(set(pa['columns'])-set(pb['columns'])),
            'only_B_columns':sorted(set(pb['columns'])-set(pa['columns']))}
    print(json.dumps(report,indent=2))
    if out: Path(out).write_text(json.dumps(report,indent=2),encoding='utf-8')

def _load_pairs(path):
    d={}
    with open(path,newline='',encoding='utf-8-sig') as f:
        r=csv.DictReader(f)
        a_col='item_id_A' if 'item_id_A' in (r.fieldnames or []) else (r.fieldnames or [''])[0]
        b_col='item_id_B' if 'item_id_B' in (r.fieldnames or []) else (r.fieldnames or ['', ''])[1]
        for row in r:
            a=str(row.get(a_col,'')).strip(); b=str(row.get(b_col,'')).strip()
            if a and b:d[a]=b
    return d

def compare_mode(left,right,out=None):
    l,r=_load_pairs(left),_load_pairs(right); lk,rk=set(l),set(r); common=lk&rk
    changed=sorted(a for a in common if l[a]!=r[a])
    same=len(common)-len(changed)
    pair_l={(a,b) for a,b in l.items()}; pair_r={(a,b) for a,b in r.items()}
    union=len(pair_l|pair_r)
    report={
        'left_matches':len(l),'right_matches':len(r),'shared_A_ids':len(common),
        'same_B_for_shared_A':same,'changed_B_for_shared_A':len(changed),
        'added_A_ids':len(rk-lk),'removed_A_ids':len(lk-rk),
        'pair_jaccard':round(len(pair_l&pair_r)/union,6) if union else 1.0,
        'changed_examples':[{'item_id_A':a,'left_item_id_B':l[a],'right_item_id_B':r[a]} for a in changed[:25]],
    }
    print(json.dumps(report,indent=2))
    if out: Path(out).write_text(json.dumps(report,indent=2),encoding='utf-8')

def _sample_csv(src,dst,n):
    with open(src,newline='',encoding='utf-8-sig') as f,open(dst,'w',newline='',encoding='utf-8') as g:
        r=csv.DictReader(f); fields=list(r.fieldnames or [])
        if '_row_key_A' not in fields:fields.append('_row_key_A')
        w=csv.DictWriter(g,fieldnames=fields,extrasaction='ignore');w.writeheader()
        for i,row in enumerate(r):
            if i>=n:break
            row['_row_key_A']=str(i);w.writerow(row)

def speed_mode(a,b,sample,out=None):
    if not WORKER.exists(): raise SystemExit(f'Missing {WORKER}')
    with tempfile.TemporaryDirectory(prefix='bb_bench_') as td:
        td=Path(td); sa=td/'sample_a.csv'; idx=td/'b.pkl'; db=td/'b.sqlite'; det=td/'det.csv'; q=td/'queue.csv'
        _sample_csv(a,sa,sample)
        t=time.perf_counter(); subprocess.run([sys.executable,str(WORKER),'--build-index','--b',str(Path(b).resolve()),'--index',str(idx),'--db',str(db)],check=True); index_s=time.perf_counter()-t
        t=time.perf_counter(); subprocess.run([sys.executable,str(WORKER),'--det','--a',str(sa),'--index',str(idx),'--db',str(db),'--out',str(det),'--queue',str(q)],check=True); det_s=time.perf_counter()-t
        c=Counter()
        with open(det,newline='',encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):c[row.get('final_verdict','')]+=1
        n=sum(c.values())
        report={'sample_rows':n,'index_build_seconds':round(index_s,3),'deterministic_seconds':round(det_s,3),
                'rows_per_second':round(n/det_s,2) if det_s else None,'verdict_counts':dict(c),
                'note':'Speed mode benchmarks deterministic retrieval/scoring only; GPT latency is intentionally excluded.'}
        print(json.dumps(report,indent=2))
        if out: Path(out).write_text(json.dumps(report,indent=2),encoding='utf-8')

def main():
    ap=argparse.ArgumentParser(description='BetterBasket benchmark and comparison utility')
    sp=ap.add_subparsers(dest='mode',required=True)
    p=sp.add_parser('profile',help='compare two input dataset schemas/statistics');p.add_argument('--a',required=True);p.add_argument('--b',required=True);p.add_argument('--out')
    p=sp.add_parser('compare',help='compare two submission_matches CSVs');p.add_argument('--left',required=True);p.add_argument('--right',required=True);p.add_argument('--out')
    p=sp.add_parser('speed',help='benchmark deterministic matcher on an A sample');p.add_argument('--a',required=True);p.add_argument('--b',required=True);p.add_argument('--sample',type=int,default=2500);p.add_argument('--out')
    x=ap.parse_args()
    if x.mode=='profile':profile_mode(x.a,x.b,x.out)
    elif x.mode=='compare':compare_mode(x.left,x.right,x.out)
    else:speed_mode(x.a,x.b,x.sample,x.out)
if __name__=='__main__':main()
