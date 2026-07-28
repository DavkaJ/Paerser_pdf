# -*- coding: utf-8 -*-
"""Скринер порчи v2 — precision-tuned. Отсекаем легит Т2/ЭоЭ/IgE/«и в»."""
import re, json, glob, os, collections, sys
SRC = sys.argv[1] if len(sys.argv)>1 else "outout_latin"
files = sorted(glob.glob(os.path.join(SRC,"КР*.json")))
CYR="А-Яа-яЁёІі"; LAT="A-Za-z"
tok_re=re.compile(rf"[{LAT}{CYR}0-9][{LAT}{CYR}0-9\-/.]*")
# 1. цифра ЗАЖАТА между кир-буквами: Вагсе1опа(е1о), 8рае1И(е1И)  — НЕ хвостовая Т2/мм2
digit_between=re.compile(rf"[{CYR}][0-9]+[{CYR}]")
# 2. кир и лат буквы ВПЛОТНУЮ (без дефиса): ЭoЭ, HBз  — НЕ IgE-зависимый
adj_mix=re.compile(rf"[{CYR}][{LAT}]|[{LAT}][{CYR}]")
# 3. начинается с невозможной в русском Ы/Ь/Ъ
# 4. рассыпанное слово: run одно-символьных токенов, где есть НЕ-легит одиночная буква
LEGIT1=set("ивксаоуяИВКСАОУЯ")  # реальные однобуквенные рус. слова/инициалы
run_re=re.compile(rf"(?:(?<!\S)[{CYR}0-9](?!\S)\s+){{2,}}")
# 5. одиночная кир-гомоглиф между латинскими словами
HOM="аеорсухАВЕКМНОРСТУХ"
hom_between=re.compile(rf"[{LAT}]{{2,}}\s+[{HOM}]\s+[{LAT}]{{2,}}")

def sigs_token(t):
    s=set()
    if digit_between.search(t): s.add("digit_in_cyrword")
    # adjacency, но исключаем токены с дефисом-границей латиница|кириллица
    parts=re.split(r"[-/]",t)
    if any(adj_mix.search(p) for p in parts): s.add("adjacent_mixed")
    if t[0] in "ЫЬЪ": s.add("impossible_start")
    return s

per=collections.Counter(); dset=collections.defaultdict(set)
ex=collections.defaultdict(list); dhits=collections.Counter()
def texts(d):
    out=[]
    def w(secs):
        for x in secs:
            if x.get("text"): out.append((x.get("number",""),x["text"]))
            w(x.get("children",[]))
    w(d.get("sections",[]))
    for t in d.get("tables",[]):
        if t.get("raw_text"): out.append(("tab"+str(t.get("number","")),t["raw_text"]))
    ap=(d.get("excluded") or {}).get("appendices")
    if isinstance(ap,str) and ap: out.append(("app",ap))
    return out

for fp in files:
    base=os.path.basename(fp)[:-5]
    try: d=json.load(open(fp,encoding="utf-8"))
    except: continue
    for num,txt in texts(d):
        for m in tok_re.finditer(txt):
            for sg in sigs_token(m.group()):
                per[sg]+=1; dset[sg].add(base); dhits[base]+=1
                if len(ex[sg])<30: ex[sg].append((base,num,m.group()))
        for m in run_re.finditer(txt):
            toks=m.group().split()
            bad=[x for x in toks if re.fullmatch(rf"[{CYR}]",x) and x not in LEGIT1]
            if bad:
                per["shattered_run"]+=1; dset["shattered_run"].add(base); dhits[base]+=1
                if len(ex["shattered_run"])<30:
                    ex["shattered_run"].append((base,num,txt[max(0,m.start()-12):m.end()+2].strip()))
        for m in hom_between.finditer(txt):
            per["homoglyph_in_latin"]+=1; dset["homoglyph_in_latin"].add(base); dhits[base]+=1
            if len(ex["homoglyph_in_latin"])<30:
                ex["homoglyph_in_latin"].append((base,num,txt[max(0,m.start()-3):m.end()+3].strip()))

print(f"ИСТОЧНИК: {SRC}  ФАЙЛОВ: {len(files)}  ДОКОВ С ХИТОМ: {len(dhits)}")
print("\nСИГНАТУРА (хитов | документов):")
for sg,c in per.most_common(): print(f"  {sg:20} {c:7} | {len(dset[sg]):4}")
print("\nТОП-25 документов:")
for b,c in dhits.most_common(25): print(f"  {b:12} {c}")
print("\nПРИМЕРЫ (после отсева легита):")
for sg in per:
    print(f"  [{sg}]  " + " | ".join(repr(e[2])[:16] for e in ex[sg][:10]))
json.dump({"source":SRC,"by_sig":{k:{"hits":v,"docs":len(dset[k])} for k,v in per.items()},
           "top_docs":dhits.most_common(80),"examples":{k:ex[k] for k in ex}},
          open("_corpus/corruption_scan2.json","w",encoding="utf-8"),ensure_ascii=False,indent=1)
print("\n[отчёт: _corpus/corruption_scan2.json]")
