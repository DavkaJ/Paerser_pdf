# -*- coding: utf-8 -*-
"""
Скринер порчи текста — RECALL-детектор для резолвера latin recovery и арбитра.
НЕ автозамена. Даёт: (1) сводку по корпусу, (2) флаги на спан-уровне (screen_flags.jsonl)
под резолвер/арбитра, (3) режим регрессии (--baseline) — хиты обязаны падать после фиксов.

Два ведра:
  PRECISE — форма аномальна, почти всегда порча (питает резолвер напрямую).
  RECALL  — похоже на порчу, но шумно (OOV-кириллица) → на арбитра/человека, не в автозамену.

Границы (честно): чистые гомографы, орфографически похожие на рус. слова (Сапсег/Сгоир),
ловятся ТОЛЬКО recall-ведром через словарь и всё равно требуют арбитра. Ноль-пропусков не гарант.
"""
import re, json, glob, os, collections, sys

SRC = "outout_latin"; BASELINE=None; DO_OOV=False
args=sys.argv[1:]
_skip=False
for i,a in enumerate(args):
    if _skip: _skip=False; continue                 # значение --baseline не считать позиционным SRC
    if a=="--baseline" and i+1<len(args): BASELINE=args[i+1]; _skip=True
    elif a=="--oov": DO_OOV=True
    elif not a.startswith("--"): SRC=a

files=sorted(glob.glob(os.path.join(SRC,"КР*.json")))
CYR="А-Яа-яЁёІі"; LAT="A-Za-z"
lat_c=re.compile(f"[{LAT}]"); cyr_c=re.compile(f"[{CYR}]")
tok_re=re.compile(rf"[{LAT}{CYR}0-9][{LAT}{CYR}0-9\-/.]*")
digit_between=re.compile(rf"[{CYR}][0-9]+[{CYR}]")     # Вагсе1опа, М01А
adj_mix=re.compile(rf"[{CYR}][{LAT}]|[{LAT}][{CYR}]")  # ЭoЭ, Cтруктура, MCН
HOM="аеорсухАВЕКМНОРСТУХ"
hom_between=re.compile(rf"[{LAT}]{{2,}}\s+[{HOM}]\s+[{LAT}]{{2,}}")
HARD=set("шщжфцъыьэй")   # кир. буквы, невозможные как одиночный токен в рус. мед.тексте
CRIT_HINT=re.compile(r"^[A-ZА-Я]?\d{2}|мг|мл|доз|ATC|МКБ|T\d|N\d|M\d",re.I)  # грубая крит-подсказка

# RU vocab (грязный по I24/I25 — только recall-скрин, НЕ гейт)
RU=set()
try:
    v=json.load(open("_corpus/corpus_ru_vocab.json",encoding="utf-8"))
    RU=set(w.lower() for w in (v.get("words") or []))
except Exception: pass

def token_sigs(t):
    s=set()
    for p in re.split(r"[-/]",t):
        if adj_mix.search(p): s.add("adjacent_mixed"); break
    if digit_between.search(t): s.add("digit_in_cyrword")
    if t[0] in "ЫЬЪ": s.add("impossible_start")
    return s

def oov_suspect(t):
    # all-cyr, len>=4, есть гомоглиф-буква, не в словаре, не число/код
    core=t.strip(".,;:()[]«»\"'")
    if len(core)<4 or lat_c.search(core) or any(ch.isdigit() for ch in core): return False
    if not cyr_c.search(core): return False
    if not any(ch in HOM for ch in core): return False
    return core.lower() not in RU

per=collections.Counter(); dset=collections.defaultdict(set)
ex=collections.defaultdict(list); dhits_precise=collections.Counter()
flags=[]

def texts(d):
    out=[]
    def w(secs):
        for x in secs:
            if x.get("text"): out.append(("sec",x.get("number",""),x["text"]))
            w(x.get("children",[]))
    w(d.get("sections",[]))
    for t in d.get("tables",[]):
        if t.get("raw_text"): out.append(("tab",str(t.get("number","")),t["raw_text"]))
    ap=(d.get("excluded") or {}).get("appendices")
    if isinstance(ap,str) and ap: out.append(("app","",ap))
    return out

def add(doc,sec,sig,src,ctx,bucket):
    per[sig]+=1; dset[sig].add(doc)
    if bucket=="precise": dhits_precise[doc]+=1
    if len(ex[sig])<40: ex[sig].append((doc,sec,src if src else ctx))
    flags.append({"doc":doc,"section":sec,"signature":sig,"bucket":bucket,
                  "source_text":src,"context":ctx,
                  "is_critical":bool(CRIT_HINT.search(src or "")),"suggested_fix":None})

for fp in files:
    doc=os.path.basename(fp)[:-5]
    try: d=json.load(open(fp,encoding="utf-8"))
    except: continue
    for kind,sec,txt in texts(d):
        # токенные (precise)
        for m in tok_re.finditer(txt):
            t=m.group()
            for sg in token_sigs(t):
                add(doc,sec,sg,t,txt[max(0,m.start()-15):m.end()+15],"precise")
            if DO_OOV and oov_suspect(t):
                add(doc,sec,"oov_cyrillic",t,txt[max(0,m.start()-15):m.end()+15],"recall")
        # рассыпанное слово (precise, с гейтом латинской близости / hard-буквы)
        toks=[(mm.group(),mm.start(),mm.end()) for mm in re.finditer(r"\S+",txt)]
        i=0
        while i<len(toks):
            if len(re.sub(r"[^\w]","",toks[i][0],flags=re.U))==1 and cyr_c.search(toks[i][0]+"0") is not None or (len(toks[i][0])==1):
                j=i
                run=[]
                while j<len(toks) and len(toks[j][0])<=1 and (cyr_c.search(toks[j][0]) or toks[j][0].isdigit()):
                    run.append(toks[j]); j+=1
                if len(run)>=2:
                    letters="".join(r[0] for r in run)
                    lat_adj = any(lat_c.search(toks[k][0]) and len(toks[k][0])>1
                                  for k in range(max(0,i-2),min(len(toks),j+2)))
                    hard = any(ch in HARD for ch in letters)
                    if lat_adj or hard or len(run)>=4:
                        st=run[0][1]; en=run[-1][2]
                        add(doc,sec,"shattered_latin",txt[st:en],txt[max(0,st-15):en+10],"precise")
                    i=j; continue
            i+=1
        # гомоглиф между латиницей (precise)
        for m in hom_between.finditer(txt):
            add(doc,sec,"homoglyph_in_latin",txt[m.start():m.end()],txt[max(0,m.start()-5):m.end()+5],"precise")

# ---- вывод ----
PRECISE=["adjacent_mixed","digit_in_cyrword","shattered_latin","impossible_start","homoglyph_in_latin"]
RECALL=["oov_cyrillic"]
print(f"ИСТОЧНИК: {SRC}  ФАЙЛОВ: {len(files)}")
print(f"\n== PRECISE (питает резолвер) ==")
for sg in PRECISE:
    if per[sg]: print(f"  {sg:20} {per[sg]:7} хитов | {len(dset[sg]):4} док.")
print(f"\n== RECALL (на арбитра, шумно) ==")
for sg in RECALL:
    print(f"  {sg:20} {per[sg]:7} хитов | {len(dset[sg]):4} док.")
print(f"\nДокументов с precise-хитом: {len(dhits_precise)} / {len(files)}")
print("ТОП-20 по precise-хитам:", ", ".join(f"{b}({c})" for b,c in dhits_precise.most_common(20)))
print("\nПРИМЕРЫ:")
for sg in PRECISE+RECALL:
    if not per[sg]: continue
    print(f"  [{sg}] " + " | ".join(repr(e[2])[:18] for e in ex[sg][:8]))

rep={"source":SRC,"files":len(files),
     "precise":{sg:{"hits":per[sg],"docs":len(dset[sg])} for sg in PRECISE},
     "recall":{sg:{"hits":per[sg],"docs":len(dset[sg])} for sg in RECALL},
     "docs_with_precise":len(dhits_precise),
     "top_docs_precise":dhits_precise.most_common(80)}
json.dump(rep,open("_corpus/screen_report.json","w",encoding="utf-8"),ensure_ascii=False,indent=1)
with open("_corpus/screen_flags.jsonl","w",encoding="utf-8") as f:
    for fl in flags: f.write(json.dumps(fl,ensure_ascii=False)+"\n")
print(f"\n[отчёт: _corpus/screen_report.json | флаги: _corpus/screen_flags.jsonl ({len(flags)} записей)]")

# регрессия
if BASELINE and os.path.exists(BASELINE):
    old=json.load(open(BASELINE,encoding="utf-8"))
    print("\n== РЕГРЕССИЯ vs baseline ==")
    for sg in PRECISE:
        ov=old.get("precise",{}).get(sg,{}).get("hits",0)
        print(f"  {sg:20} {ov:6} -> {per[sg]:6}  ({per[sg]-ov:+d})")
