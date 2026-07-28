# -*- coding: utf-8 -*-
"""Read-only karta ostatka, split into passes to fit per-call time budget.
Usage: rmap.py A   -> codepoint inventory -> _corpus/_rm_cp.json
       rmap.py B   -> garble+hyphen        -> _corpus/_rm_garble.json
       rmap.py C   -> combine+coverage+report"""
import json, glob, re, unicodedata, collections, os, sys, time
LATIN_DIR='outout_latin'; QUEUE_DIR='_corpus/verify_queue'; OUT='_corpus'
mode=sys.argv[1] if len(sys.argv)>1 else 'C'
t0=time.time()

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
def body_texts(doc):
    for s in (doc.get('sections') or []):
        for t in sec_texts(s): yield t
    for tb in (doc.get('tables') or []):
        for v in all_strings(tb): yield v
    ex=doc.get('excluded') or {}
    for r in ('front_matter','references','appendices','other'):
        for v in all_strings(ex.get(r)): yield v
def cp_cat(cp):
    if 0xE000<=cp<=0xF8FF or 0xF0000<=cp<=0xFFFFD or 0x100000<=cp<=0x10FFFD: return 'PUA'
    if 0x0370<=cp<=0x03FF or 0x1F00<=cp<=0x1FFF: return 'Greek'
    if 0x0400<=cp<=0x04FF or cp==0x0510: return 'Cyrillic'
    if cp<0x80: return 'ASCII'
    if 0x0041<=cp<=0x024F: return 'Latin'
    c=chr(cp); k=unicodedata.category(c)
    if k[0]=='C': return 'Control/Format'
    if k[0] in('P','S','Z'): return 'Punct/Symbol'
    if k[0]=='N': return 'Number'
    return 'Other'
files=sorted(glob.glob(LATIN_DIR+'/КР*.json'))

if mode=='A':
    cp_counts=collections.Counter(); cp_docs=collections.defaultdict(set); cp_ex={}
    for fi,fp in enumerate(files):
        doc=os.path.basename(fp)[:-5]
        try:d=json.load(open(fp,encoding='utf-8'))
        except Exception as e: print('rerr',fp,e,file=sys.stderr); continue
        for s in all_strings(d):
            for c in set(s):
                o=ord(c)
                if 0x20<=o<0x80 or o in(0x9,0xA,0xD): continue
                cat=cp_cat(o)
                if cat in('Cyrillic','Latin','Number'): continue
                cp_counts[o]+=s.count(c); cp_docs[o].add(doc)
                if o not in cp_ex:
                    i=s.find(c); cp_ex[o]=s[max(0,i-15):i+16].replace('\n',' ')
        if fi%150==0: print('A',fi,'%.1f'%(time.time()-t0),file=sys.stderr,flush=True)
    json.dump({'cp_counts':{str(k):v for k,v in cp_counts.items()},
               'cp_docs':{str(k):sorted(v) for k,v in cp_docs.items()},
               'cp_ex':{str(k):v for k,v in cp_ex.items()}},
              open(OUT+'/_rm_cp.json','w',encoding='utf-8'),ensure_ascii=False)
    print('A done %.1f'%(time.time()-t0),file=sys.stderr)

elif mode=='B':
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
    def garble(tok):
        if not suspect(tok): return None
        if len([c for c in tok if c.isalpha()])<3: return None
        if any(('A'<=c<='z') and c.isalpha() and not CYR(c) for c in tok): return None
        if not all(CYR(c) for c in tok if c.isalpha()): return None
        sig=[]
        if tok[0] in SOFT: sig.append('soft_start')
        for i in range(len(tok)-1):
            if tok[i] in SOFT and tok[i+1] in SOFT: sig.append('soft_double'); break
        if not tok.isupper():
            if any(c in CAPSOFT for c in tok): sig.append('cap_soft')
            for i in range(1,len(tok)):
                if tok[i].isalpha() and tok[i-1].isalpha() and tok[i].isupper() and tok[i-1].islower(): sig.append('mixcase'); break
        for i in range(1,len(tok)-1):
            if tok[i] in '013' and tok[i-1].isalpha() and tok[i-1].islower() and CYR(tok[i-1]) and tok[i+1].isalpha() and tok[i+1].islower() and CYR(tok[i+1]): sig.append('digit_homoglyph'); break
        return sig or None
    try:
        wl=json.load(open(QUEUE_DIR+'/_mixcase_whitelist.json',encoding='utf-8')); WL=set(wl) if isinstance(wl,list) else set(wl)
    except: WL=set()
    g_occ=collections.Counter(); g_docs=collections.defaultdict(set); g_sig=collections.defaultdict(set)
    hy=collections.Counter(); hy_docs=collections.defaultdict(set)
    HY=re.compile(r'([А-Яа-яЁёA-Za-z0-9]+)-([А-Яа-яЁёA-Za-z0-9]+)')
    for fi,fp in enumerate(files):
        doc=os.path.basename(fp)[:-5]
        try:d=json.load(open(fp,encoding='utf-8'))
        except Exception as e: print('rerr',fp,e,file=sys.stderr); continue
        for txt in body_texts(d):
            for m in TOK.finditer(txt):
                tok=m.group()
                if len(tok)<3: continue
                s=garble(tok)
                if s and tok not in WL:
                    g_occ[tok]+=1; g_docs[tok].add(doc); g_sig[tok].update(s)
            for hm in HY.finditer(txt):
                a,b=hm.group(1),hm.group(2)
                if (garble(a) or garble(b)) and a not in WL and b not in WL:
                    c=hm.group(); hy[c]+=1; hy_docs[c].add(doc)
        if fi%150==0: print('B',fi,'%.1f'%(time.time()-t0),file=sys.stderr,flush=True)
    json.dump({'g_occ':dict(g_occ),'g_docs':{k:sorted(v) for k,v in g_docs.items()},
               'g_sig':{k:sorted(v) for k,v in g_sig.items()},
               'hy':dict(hy),'hy_docs':{k:sorted(v) for k,v in hy_docs.items()}},
              open(OUT+'/_rm_garble.json','w',encoding='utf-8'),ensure_ascii=False)
    print('B done %.1f'%(time.time()-t0),file=sys.stderr)

else:  # C: combine + coverage + report
    TOK=re.compile(r'[А-Яа-яЁёІіЇїҐґ0-9]+')
    def load_queue():
        srcs=set()
        for path in glob.glob(QUEUE_DIR+'/*.json')+glob.glob(QUEUE_DIR+'/*.jsonl'):
            try:
                if path.endswith('.jsonl'):
                    for line in open(path,encoding='utf-8'):
                        line=line.strip()
                        if not line: continue
                        try:o=json.loads(line)
                        except:continue
                        if isinstance(o,dict):
                            for k in('source_text','source','text','crop_word'):
                                v=o.get(k)
                                if isinstance(v,str) and v.strip(): srcs.add(v.strip())
                else:
                    dd=json.load(open(path,encoding='utf-8'))
                    items=dd if isinstance(dd,list) else (dd.get('tasks') or [dd])
                    for o in items:
                        if isinstance(o,dict):
                            for k in('source_text','source','text','crop_word'):
                                v=o.get(k)
                                if isinstance(v,str) and v.strip(): srcs.add(v.strip())
            except Exception as e: print('qwarn',path,e,file=sys.stderr)
        return srcs
    QUEUE=load_queue(); QTOK=set(QUEUE)
    for q in list(QUEUE):
        for m in TOK.finditer(q): QTOK.add(m.group())
    A=json.load(open(OUT+'/_rm_cp.json',encoding='utf-8'))
    B=json.load(open(OUT+'/_rm_garble.json',encoding='utf-8'))
    cp_counts={int(k):v for k,v in A['cp_counts'].items()}
    cp_docs={int(k):set(v) for k,v in A['cp_docs'].items()}
    cp_ex={int(k):v for k,v in A['cp_ex'].items()}
    g_occ=collections.Counter(B['g_occ']); g_docs={k:set(v) for k,v in B['g_docs'].items()}; g_sig=B['g_sig']
    hy=collections.Counter(B['hy']); hy_docs={k:set(v) for k,v in B['hy_docs'].items()}
    cat_tot=collections.Counter()
    for cp,n in cp_counts.items(): cat_tot[cp_cat(cp)]+=n
    uncov=[(t,n) for t,n in g_occ.items() if t not in QTOK]
    cov=[(t,n) for t,n in g_occ.items() if t in QTOK]
    out=[]; P=lambda *a: out.append(' '.join(str(x) for x in a))
    P('# КАРТА ОСТАТКА — outout_latin, файлов:',len(files),' (2026-07-23)'); P('')
    P('queue: distinct source_text=%d, queue-tokens=%d'%(len(QUEUE),len(QTOK))); P('')
    P('## A. Инвентарь non-ASCII/служебных кодпоинтов'); 
    for cat,n in cat_tot.most_common():
        cps=[cp for cp in cp_counts if cp_cat(cp)==cat]
        nd=len(set().union(*[cp_docs[cp] for cp in cps])) if cps else 0
        P(f'  {cat:16} occ={n:<9} distinct={len(cps):<4} docs={nd}')
    P(''); P('## A1. PUA — шрифтовые глифы')
    for cp in sorted([c for c in cp_counts if cp_cat(c)=='PUA'],key=lambda c:-cp_counts[c]):
        P(f'  U+{cp:04X} occ={cp_counts[cp]:<7} docs={len(cp_docs[cp]):<4} ex={cp_ex[cp]!r}')
    P(''); P('## A2. Control/Format')
    for cp in sorted([c for c in cp_counts if cp_cat(c)=='Control/Format'],key=lambda c:-cp_counts[c]):
        P(f'  U+{cp:04X} {unicodedata.name(chr(cp),"?"):26} occ={cp_counts[cp]:<7} docs={len(cp_docs[cp])}')
    P(''); P('## A3. Punct/Symbol — top 45')
    for cp in sorted([c for c in cp_counts if cp_cat(c)=='Punct/Symbol'],key=lambda c:-cp_counts[c])[:45]:
        P(f'  U+{cp:04X} {unicodedata.name(chr(cp),"?"):24} {chr(cp)!r:5} occ={cp_counts[cp]:<7} docs={len(cp_docs[cp])}')
    P(''); P('## A4. Greek + Other (сводно)')
    for cat in ('Greek','Other'):
        cps=[c for c in cp_counts if cp_cat(c)==cat]
        P(f'  {cat}: distinct={len(cps)} occ={sum(cp_counts[c] for c in cps)}')
        for cp in sorted(cps,key=lambda c:-cp_counts[c])[:12]:
            P(f'     U+{cp:04X} {chr(cp)!r} occ={cp_counts[cp]} docs={len(cp_docs[cp])}')
    P(''); P('## B. Mixcase/homoglyph garble (pure-Cyrillic masquerade)')
    tt=len(g_occ); to=sum(g_occ.values())
    P(f'  distinct токенов={tt} вхождений={to}')
    P(f'  covered (в очереди): distinct={len(cov)} occ={sum(n for _,n in cov)}')
    P(f'  UNCOVERED (хвост):   distinct={len(uncov)} occ={sum(n for _,n in uncov)}')
    if tt: P(f'  доля хвоста: {100*len(uncov)/tt:.1f}% токенов / {100*sum(n for _,n in uncov)/max(1,to):.1f}% вхождений')
    P(''); P('  Топ-50 НЕ покрытых (token occ docs signals):')
    for t,n in sorted(uncov,key=lambda x:-x[1])[:50]:
        P(f'     {t!r:18} occ={n:<4} docs={len(g_docs[t]):<3} {g_sig.get(t,[])}')
    P(''); P('  Документы с наибольшим uncovered (top 20):')
    dg=collections.Counter()
    for t,n in uncov:
        for dd in g_docs[t]: dg[dd]+=n
    for dd,n in dg.most_common(20): P(f'     {dd:12} ~{n}')
    P(''); P('## C. Битые дефис-составные')
    P(f'  distinct={len(hy)} occ={sum(hy.values())}')
    for comp,n in hy.most_common(30): P(f'     {comp!r:28} occ={n:<4} docs={len(hy_docs[comp])}')
    rep='\n'.join(out)
    open(OUT+'/RESIDUAL_MAP_2026-07-23.md','w',encoding='utf-8').write(rep)
    print(rep)
    print('C done %.1f'%(time.time()-t0),file=sys.stderr)
