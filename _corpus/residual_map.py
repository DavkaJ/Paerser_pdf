# -*- coding: utf-8 -*-
"""Read-only 'карта остатка'. Optimized: set-based codepoint counting (C-level)."""
import json, glob, re, unicodedata, collections, os, sys, time
import signal
class _TO(Exception): pass
def _alarm(sig,fr): raise _TO()
signal.signal(signal.SIGALRM,_alarm)

LATIN_DIR='outout_latin'; QUEUE_DIR='_corpus/verify_queue'
t0=time.time()

def sec_texts(sec):
    if sec.get('text'): yield sec['text']
    for ch in (sec.get('children') or []): yield from sec_texts(ch)

SKIP_KEYS={'span_uid','span_uids','bbox','crop_png','crop_word','crop_line','crop_page','section_id','sha256','output_sha256'}
def all_strings(obj):
    if isinstance(obj,str): yield obj
    elif isinstance(obj,dict):
        for k,v in obj.items():
            if k in SKIP_KEYS: continue
            yield from all_strings(v)
    elif isinstance(obj,list):
        for v in obj: yield from all_strings(v)

def body_texts(doc):
    for s in (doc.get('sections') or []):
        for t in sec_texts(s): yield t
    for tb in (doc.get('tables') or []):
        for v in all_strings(tb): yield v
    ex=doc.get('excluded') or {}
    for region in ('front_matter','references','appendices','other'):
        for v in all_strings(ex.get(region)): yield v

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

CYR=lambda c:('А'<=c<='я') or c in 'ЁёІіЇїҐґ'
SOFT=set('ьъЬЪ'); CAPSOFT=set('ЬЪ')
TOK_RE=re.compile(r'[А-Яа-яЁёІіЇїҐґ0-9]+')
def suspect(tok):
    for c in tok:
        if c in SOFT: return True
    if tok!=tok.lower() and tok!=tok.upper() and tok!=tok.capitalize(): return True
    for i in range(1,len(tok)-1):
        if tok[i] in '013': return True
    return False
def looks_garble(tok):
    if not suspect(tok): return None
    letters=[c for c in tok if c.isalpha()]
    if len(letters)<3: return None
    if any(('A'<=c<='z') and c.isalpha() and not CYR(c) for c in tok): return None
    if not all(CYR(c) for c in tok if c.isalpha()): return None
    sig=[]
    if tok[0] in SOFT: sig.append('soft_start')
    for i in range(len(tok)-1):
        if tok[i] in SOFT and tok[i+1] in SOFT: sig.append('soft_double'); break
    if not tok.isupper():
        if any(c in CAPSOFT for c in tok): sig.append('cap_soft')
        for i in range(1,len(tok)):
            if tok[i].isalpha() and tok[i-1].isalpha() and tok[i].isupper() and tok[i-1].islower():
                sig.append('mixcase'); break
    for i in range(1,len(tok)-1):
        if tok[i] in '013' and tok[i-1].isalpha() and tok[i-1].islower() and CYR(tok[i-1]) and tok[i+1].isalpha() and tok[i+1].islower() and CYR(tok[i+1]):
            sig.append('digit_homoglyph'); break
    return sig or None

# queue coverage
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
                d=json.load(open(path,encoding='utf-8'))
                items=d if isinstance(d,list) else (d.get('tasks') or [d])
                for o in items:
                    if isinstance(o,dict):
                        for k in('source_text','source','text','crop_word'):
                            v=o.get(k)
                            if isinstance(v,str) and v.strip(): srcs.add(v.strip())
        except Exception as e: print('qwarn',path,e,file=sys.stderr)
    return srcs
QUEUE=load_queue()
QTOK=set(QUEUE)
for q in list(QUEUE):
    for m in TOK_RE.finditer(q): QTOK.add(m.group())
print('queue distinct',len(QUEUE),'qtok',len(QTOK),'t=%.1f'%(time.time()-t0),file=sys.stderr)
try:
    wl=json.load(open(QUEUE_DIR+'/_mixcase_whitelist.json',encoding='utf-8')); WL=set(wl) if isinstance(wl,list) else set(wl)
except: WL=set()

cp_counts=collections.Counter(); cp_docs=collections.defaultdict(set); cp_ex={}
garble_occ=collections.Counter(); garble_docs=collections.defaultdict(set); garble_sig=collections.defaultdict(set)
hyph=collections.Counter(); hyph_docs=collections.defaultdict(set)

files=sorted(glob.glob(LATIN_DIR+'/КР*.json'))
for _fi,fp in enumerate(files):
    if _fi%100==0: print('  file',_fi,'t=%.1f'%(time.time()-t0),file=sys.stderr,flush=True)
    doc=os.path.basename(fp)[:-5]
    try:d=json.load(open(fp,encoding='utf-8'))
    except Exception as e: print('rerr',fp,e,file=sys.stderr); continue
    signal.alarm(6)
    try:
        for s in all_strings(d):
            for c in set(s):
                o=ord(c)
                if 0x20<=o<0x80: continue
                if o in (0x9,0xA,0xD): continue
                cat=cp_cat(o)
                if cat in ('Cyrillic','Latin','Number'): continue
                cp_counts[o]+=s.count(c); cp_docs[o].add(doc)
                if o not in cp_ex:
                    i=s.find(c); cp_ex[o]=s[max(0,i-15):i+16].replace('\n',' ')
        for txt in body_texts(d):
            for m in TOK_RE.finditer(txt):
                tok=m.group()
                if len(tok)<3: continue
                sig=looks_garble(tok)
                if sig and tok not in WL:
                    garble_occ[tok]+=1; garble_docs[tok].add(doc); garble_sig[tok].update(sig)
            for hm in re.finditer(r'([А-Яа-яЁёA-Za-z0-9]+)-([А-Яа-яЁёA-Za-z0-9]+)',txt):
                a,b=hm.group(1),hm.group(2)
                if ((TOK_RE.fullmatch(a) and looks_garble(a)) or (TOK_RE.fullmatch(b) and looks_garble(b))) and a not in WL and b not in WL:
                    comp=hm.group(); hyph[comp]+=1; hyph_docs[comp].add(doc)
        signal.alarm(0)
    except _TO:
        signal.alarm(0); print('TIMEOUT_FILE',doc,file=sys.stderr,flush=True)
print('scan done t=%.1f'%(time.time()-t0),file=sys.stderr)

cat_totals=collections.Counter()
for cp,n in cp_counts.items(): cat_totals[cp_cat(cp)]+=n
uncov=[(t,n) for t,n in garble_occ.items() if t not in QTOK]
cov=[(t,n) for t,n in garble_occ.items() if t in QTOK]

out=[]; P=lambda *a: out.append(' '.join(str(x) for x in a))
P('# КАРТА ОСТАТКА — outout_latin, файлов:',len(files),' (2026-07-23)'); P('')
P('## A. Инвентарь non-ASCII/служебных кодпоинтов по категориям')
for cat,n in cat_totals.most_common():
    nd=len(set().union(*[cp_docs[cp] for cp in cp_counts if cp_cat(cp)==cat])) if any(cp_cat(cp)==cat for cp in cp_counts) else 0
    P(f'  {cat:16} occ={n:<9} docs={nd}')
P(''); P('## A1. PUA — шрифтовые глифы (приоритет)')
for cp in sorted([c for c in cp_counts if cp_cat(c)=='PUA'],key=lambda c:-cp_counts[c]):
    P(f'  U+{cp:04X} occ={cp_counts[cp]:<7} docs={len(cp_docs[cp]):<4} ex={cp_ex[cp]!r}')
P(''); P('## A2. Control/Format')
for cp in sorted([c for c in cp_counts if cp_cat(c)=='Control/Format'],key=lambda c:-cp_counts[c]):
    P(f'  U+{cp:04X} {unicodedata.name(chr(cp),"?"):26} occ={cp_counts[cp]:<7} docs={len(cp_docs[cp])}')
P(''); P('## A3. Punct/Symbol — top 40')
for cp in sorted([c for c in cp_counts if cp_cat(c)=='Punct/Symbol'],key=lambda c:-cp_counts[c])[:40]:
    P(f'  U+{cp:04X} {unicodedata.name(chr(cp),"?"):22} {chr(cp)!r:5} occ={cp_counts[cp]:<7} docs={len(cp_docs[cp])}')
P(''); P('## A4. Greek + Other (сводно)')
for cat in ('Greek','Other'):
    cps=[c for c in cp_counts if cp_cat(c)==cat]
    P(f'  {cat}: distinct={len(cps)} occ={sum(cp_counts[c] for c in cps)}')
    for cp in sorted(cps,key=lambda c:-cp_counts[c])[:12]:
        P(f'     U+{cp:04X} {chr(cp)!r} occ={cp_counts[cp]} docs={len(cp_docs[cp])}')
P(''); P('## B. Mixcase/homoglyph garble (pure-Cyrillic masquerade)')
tt=len(garble_occ); to=sum(garble_occ.values())
P(f'  distinct токенов={tt} вхождений={to}')
P(f'  covered (в очереди): distinct={len(cov)} occ={sum(n for _,n in cov)}')
P(f'  UNCOVERED (хвост): distinct={len(uncov)} occ={sum(n for _,n in uncov)}')
if tt: P(f'  доля хвоста: {100*len(uncov)/tt:.1f}% токенов / {100*sum(n for _,n in uncov)/max(1,to):.1f}% вхождений')
P(''); P('  Топ-50 НЕ покрытых (token occ docs signals):')
for t,n in sorted(uncov,key=lambda x:-x[1])[:50]:
    P(f'     {t!r:18} occ={n:<4} docs={len(garble_docs[t]):<3} {sorted(garble_sig[t])}')
P(''); P('  Документы с наибольшим uncovered (top 20):')
dg=collections.Counter()
for t,n in uncov:
    for dd in garble_docs[t]: dg[dd]+=n
for dd,n in dg.most_common(20): P(f'     {dd:12} ~{n}')
P(''); P('## C. Битые дефис-составные')
P(f'  distinct={len(hyph)} occ={sum(hyph.values())}')
for comp,n in hyph.most_common(30): P(f'     {comp!r:28} occ={n:<4} docs={len(hyph_docs[comp])}')
rep='\n'.join(out)
open('_corpus/RESIDUAL_MAP_2026-07-23.md','w',encoding='utf-8').write(rep)
print(rep); print('TOTAL t=%.1f'%(time.time()-t0),file=sys.stderr)
