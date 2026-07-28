# -*- coding: utf-8 -*-
import json, glob, re, collections, os, sys, time
LATIN_DIR='outout_latin'; t0=time.time()
def sec_texts(sec):
    if sec.get('text'): yield sec['text']
    for ch in (sec.get('children') or []): yield from sec_texts(ch)
SKIP={'span_uid','span_uids','bbox','crop_png','crop_word','crop_line','crop_page','section_id','sha256','output_sha256'}
def all_strings(obj):
    if isinstance(obj,str): yield obj
    elif isinstance(obj,dict):
        for k,v in obj.items():
            if k not in SKIP: yield from all_strings(v)
    elif isinstance(obj,list):
        for v in obj: yield from all_strings(v)
def body_regions(doc):
    # yields (bucket, text)
    for s in (doc.get('sections') or []):
        for t in sec_texts(s): yield ('body', t)
    for tb in (doc.get('tables') or []):
        for v in all_strings(tb): yield ('body', v)
    ex=doc.get('excluded') or {}
    for v in all_strings(ex.get('references')): yield ('refs', v)
    for r in ('front_matter','appendices','other'):
        for v in all_strings(ex.get(r)): yield ('front', v)
CYR=lambda c:('А'<=c<='я') or c in 'ЁёІіЇїҐґ'
SOFT=set('ьъЬЪ'); CAPSOFT=set('ЬЪ')
TOK=re.compile(r'[А-Яа-яЁёІіЇїҐґ0-9]+')
def suspect(tok):
    for c in tok:
        if c in SOFT: return True
    if tok!=tok.lower() and tok!=tok.upper() and tok!=tok.capitalize(): return True
    for i in range(1,len(tok)-1):
        if tok[i] in '013': return True
    return False
HARD={'soft_start','soft_double','cap_soft','digit_homoglyph'}
def garble(tok):
    if not suspect(tok): return None
    if len([c for c in tok if c.isalpha()])<3: return None
    if any(('A'<=c<='z') and c.isalpha() and not CYR(c) for c in tok): return None
    if not all(CYR(c) for c in tok if c.isalpha()): return None
    sig=set()
    if tok[0] in SOFT: sig.add('soft_start')
    for i in range(len(tok)-1):
        if tok[i] in SOFT and tok[i+1] in SOFT: sig.add('soft_double'); break
    if not tok.isupper():
        if any(c in CAPSOFT for c in tok): sig.add('cap_soft')
        for i in range(1,len(tok)):
            if tok[i].isalpha() and tok[i-1].isalpha() and tok[i].isupper() and tok[i-1].islower(): sig.add('mixcase'); break
    for i in range(1,len(tok)-1):
        if tok[i] in '013' and tok[i-1].isalpha() and tok[i-1].islower() and CYR(tok[i-1]) and tok[i+1].isalpha() and tok[i+1].islower() and CYR(tok[i+1]): sig.add('digit_homoglyph'); break
    return sig or None
try:
    wl=json.load(open('_corpus/verify_queue/_mixcase_whitelist.json',encoding='utf-8')); WL=set(wl) if isinstance(wl,list) else set(wl)
except: WL=set()
# per-region, per-hardness: occ counter + per-token(body-hard) detail
occ=collections.Counter()          # (bucket,hardness)->occ
body_hard_occ=collections.Counter()  # token->occ  (body & hard)
body_hard_docs=collections.defaultdict(set)
body_hard_sig=collections.defaultdict(set)
files=sorted(glob.glob(LATIN_DIR+'/КР*.json'))
for fi,fp in enumerate(files):
    doc=os.path.basename(fp)[:-5]
    try:d=json.load(open(fp,encoding='utf-8'))
    except: continue
    for bucket,txt in body_regions(d):
        for m in TOK.finditer(txt):
            tok=m.group()
            if len(tok)<3 or tok in WL: continue
            s=garble(tok)
            if not s: continue
            hard = bool(HARD & s)
            occ[(bucket,'hard' if hard else 'mix')]+=1
            if bucket=='body' and hard:
                body_hard_occ[tok]+=1; body_hard_docs[tok].add(doc); body_hard_sig[tok]|=s
    if fi%150==0: print('R',fi,'%.1f'%(time.time()-t0),file=sys.stderr,flush=True)
print('=== garble occ by region x hardness ===')
for b in ('body','refs','front'):
    for h in ('hard','mix'):
        print(f'  {b:5} {h:4} occ={occ[(b,h)]}')
print(f'\n=== BODY & HARD (real poison in clinical text): distinct={len(body_hard_occ)} occ={sum(body_hard_occ.values())} docs={len(set().union(*body_hard_docs.values())) if body_hard_occ else 0}')
print('  top 45 body-hard tokens:')
for t,n in body_hard_occ.most_common(45):
    print(f'    {t!r:20} occ={n:<4} docs={len(body_hard_docs[t]):<3} {sorted(body_hard_sig[t])}')
dg=collections.Counter()
for t,n in body_hard_occ.items():
    for dd in body_hard_docs[t]: dg[dd]+=n
print('\n  top docs by body-hard occ:')
for dd,n in dg.most_common(20): print(f'    {dd:12} ~{n}')
json.dump({'occ':{f'{b}|{h}':occ[(b,h)] for b in('body','refs','front') for h in('hard','mix')},
           'body_hard_occ':dict(body_hard_occ.most_common()),
           'body_hard_docs':{k:sorted(v) for k,v in body_hard_docs.items()},
           'body_hard_top_docs':dict(dg.most_common())},
          open('_corpus/_rm_region.json','w',encoding='utf-8'),ensure_ascii=False)
print('done %.1f'%(time.time()-t0),file=sys.stderr)
