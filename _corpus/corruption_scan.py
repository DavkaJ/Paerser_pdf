# -*- coding: utf-8 -*-
"""Скринер порчи текста высокого recall. Не автозамена — инвентарь для проверки.
Сигнатуры ловят классы, которые токен-детектор 13b пропускает."""
import re, json, glob, os, collections, sys

SRC = sys.argv[1] if len(sys.argv)>1 else "outout_latin"
files = sorted(glob.glob(os.path.join(SRC,"КР*.json")))

CYR="А-Яа-яЁёІі"; LAT="A-Za-z"
lat_re=re.compile(f"[{LAT}]"); cyr_re=re.compile(f"[{CYR}]"); dig_re=re.compile(r"[0-9]")
HOM=set("аеорсухАВЕКМНОРСТУХ")           # кир. гомоглифы латиницы
tok_re=re.compile(rf"[{LAT}{CYR}0-9][{LAT}{CYR}0-9\-/.]*")
# рассыпанное слово: >=2 подряд одно-символьных кир/цифро-токенов
shatter_re=re.compile(rf"(?:(?<!\S)[{CYR}0-9](?!\S)\s+){{2,}}")
# одиночная кир-буква между латинскими словами
hom_between=re.compile(rf"[{LAT}]{{2,}}\s+([{ ''.join(HOM) }])\s+[{LAT}]{{2,}}")

def sig_token(t):
    s=set()
    hl=bool(lat_re.search(t)); hc=bool(cyr_re.search(t)); hd=bool(dig_re.search(t))
    if hl and hc: s.add("mixed_script")                       # HBsAg-порча, Т1Ь
    if hd and (hl or hc) and re.search(rf"[{LAT}{CYR}][0-9]|[0-9][{LAT}{CYR}]", t) \
       and not re.fullmatch(r"[0-9A-Za-z.\-/]+", t):          # цифра внутри кир-слова: Вагсе1опа,8рае1И
        s.add("digit_in_cyrword")
    if re.search(rf"[{CYR}].*?[а-яё][А-ЯЁ]", t) or re.search(r"[а-яё][А-ЯЁ]", t):
        s.add("camel_inside")                                 # ЫазаШ, СНшс
    if t and t[0] in "ЫЬЪ": s.add("impossible_start")         # Ы/Ь/Ъ в начале
    return s

per_sig=collections.Counter(); docs_sig=collections.defaultdict(set)
examples=collections.defaultdict(list); doc_hits=collections.Counter()
silent_vs_flagged={"flagged":0,"silent":0}

def texts_of(d):
    out=[]
    def w(secs):
        for x in secs:
            if x.get("text"): out.append(("sec",x.get("number",""),x["text"]))
            w(x.get("children",[]))
    w(d.get("sections",[]))
    for t in d.get("tables",[]):
        if t.get("raw_text"): out.append(("tab",str(t.get("number","")),t["raw_text"]))
    # excluded.appendices — тоже обучаемая зона; references НЕ трогаем (контракт)
    exc=d.get("excluded",{})
    ap=exc.get("appendices")
    if isinstance(ap,str) and ap: out.append(("app","",ap))
    return out

for fp in files:
    base=os.path.basename(fp)[:-5]
    try: d=json.load(open(fp,encoding="utf-8"))
    except: continue
    # уже помеченные latin_recovery (needs_review) — чтобы отделить молчаливые промахи
    lr=d.get("latin_recovery") or {}
    flagged_src=set()
    if isinstance(lr,dict):
        for r in lr.get("corrections",[]):
            if r.get("decision")=="needs_review": flagged_src.add((r.get("source_text") or "").strip())
    hit=False
    for kind,num,txt in texts_of(d):
        # токенные сигнатуры
        for m in tok_re.finditer(txt):
            t=m.group()
            for sg in sig_token(t):
                per_sig[sg]+=1; docs_sig[sg].add(base); hit=True; doc_hits[base]+=1
                if len(examples[sg])<25: examples[sg].append((base,num,t))
                if t.strip() in flagged_src: silent_vs_flagged["flagged"]+=1
                else: silent_vs_flagged["silent"]+=1
        # текст-уровневые
        for m in shatter_re.finditer(txt):
            frag=m.group().strip()
            if len(frag)>=3:
                per_sig["shattered_run"]+=1; docs_sig["shattered_run"].add(base); hit=True; doc_hits[base]+=1
                if len(examples["shattered_run"])<25:
                    ctx=txt[max(0,m.start()-15):m.end()+3]
                    examples["shattered_run"].append((base,num,ctx.strip()))
        for m in hom_between.finditer(txt):
            per_sig["homoglyph_in_latin"]+=1; docs_sig["homoglyph_in_latin"].add(base); hit=True; doc_hits[base]+=1
            if len(examples["homoglyph_in_latin"])<25:
                examples["homoglyph_in_latin"].append((base,num,txt[max(0,m.start()-5):m.end()+5].strip()))

print(f"ИСТОЧНИК: {SRC}  ФАЙЛОВ: {len(files)}")
print(f"ДОКУМЕНТОВ С ХОТЯ БЫ ОДНИМ ХИТОМ: {len(doc_hits)} / {len(files)}\n")
print("ПО СИГНАТУРАМ (всего хитов | документов):")
for sg,c in per_sig.most_common():
    print(f"  {sg:20} {c:7} | {len(docs_sig[sg]):4} док.")
print(f"\nИз токенных хитов: уже в needs_review={silent_vs_flagged['flagged']}  МОЛЧА (не помечены)={silent_vs_flagged['silent']}")
print("\nТОП-20 документов по числу хитов:")
for b,c in doc_hits.most_common(20): print(f"  {b:12} {c}")
print("\nПРИМЕРЫ по классам:")
for sg in per_sig:
    print(f"  [{sg}]")
    for b,num,ex in examples[sg][:6]:
        print(f"     {b} §{num}: {ex!r}")

# полный отчёт
rep={"source":SRC,"files":len(files),"docs_with_hits":len(doc_hits),
     "by_signature":{k:{"hits":v,"docs":len(docs_sig[k])} for k,v in per_sig.items()},
     "silent_vs_flagged":silent_vs_flagged,
     "top_docs":doc_hits.most_common(60),
     "examples":{k:examples[k] for k in examples}}
json.dump(rep,open("_corpus/corruption_scan.json","w",encoding="utf-8"),ensure_ascii=False,indent=1)
print("\n[отчёт: _corpus/corruption_scan.json]")
