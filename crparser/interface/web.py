#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Простой локальный веб-UI (Flask) — опциональная точка запуска.

Тот же движок и профили, что и в CLI. Пользователь указывает путь к PDF или папке,
выбирает профиль, путь к реестру и папку вывода; парсер пишет JSON и показывает
сводку. Работает только локально (127.0.0.1) — это инструмент, а не публичный сервис.

    python -m crparser.interface.web         # http://127.0.0.1:5000
"""

from __future__ import annotations

import html
import os
from typing import List

from flask import Flask, request

from crparser.engine.parser import DocumentParser
from crparser.engine.jsonio import JsonWriter
from crparser.profiles import available_profiles, create_profile

app = Flask(__name__)
_writer = JsonWriter()


def _iter_pdfs(path: str) -> List[str]:
    if os.path.isfile(path):
        return [path] if path.lower().endswith(".pdf") else []
    if os.path.isdir(path):
        return [os.path.join(path, n) for n in sorted(os.listdir(path))
                if n.lower().endswith(".pdf")]
    return []


_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>crparser</title>
<style>
 body{{font-family:system-ui,Segoe UI,Arial;margin:2rem auto;max-width:900px;color:#1d1d1f}}
 h1{{font-size:1.4rem}} label{{display:block;margin:.6rem 0 .2rem;font-weight:600}}
 input,select{{width:100%;padding:.5rem;border:1px solid #ccc;border-radius:8px;font-size:1rem}}
 button{{margin-top:1rem;padding:.6rem 1.2rem;border:0;border-radius:8px;background:#0071e3;color:#fff;font-size:1rem;cursor:pointer}}
 table{{border-collapse:collapse;width:100%;margin-top:1.2rem;font-size:.9rem}}
 th,td{{border:1px solid #e3e3e3;padding:.4rem .6rem;text-align:left}}
 th{{background:#f5f5f7}} .bad{{color:#b00020}} .ok{{color:#0a7d28}}
 .hint{{color:#666;font-size:.85rem}}
</style></head><body>
<h1>crparser — парсер клинических рекомендаций → JSON</h1>
<form method="post">
 <label>PDF-файл или папка</label>
 <input name="input" value="{input}" placeholder="data\\текст_после_чистки\\КР100_2.pdf или папка">
 <label>Профиль</label>
 <select name="profile">{profiles}</select>
 <label>Excel-реестр (необязательно)</label>
 <input name="registry" value="{registry}" placeholder="Список_утвержденных...xlsx">
 <label>Папка вывода</label>
 <input name="out" value="{out}" placeholder="out">
 <button type="submit">Разобрать</button>
 <p class="hint">Локальный инструмент: указываются пути на этой машине.</p>
</form>
{result}
</body></html>"""


def _render(input_="", registry="", out="out", result="") -> str:
    opts = "".join(f'<option value="{p}">{p}</option>' for p in available_profiles())
    return _PAGE.format(input=html.escape(input_), registry=html.escape(registry),
                        out=html.escape(out), profiles=opts, result=result)


@app.route("/", methods=["GET", "POST"])
def index() -> str:
    if request.method == "GET":
        return _render()

    inp = (request.form.get("input") or "").strip().strip('"')
    profile_key = request.form.get("profile") or "cr"
    registry = (request.form.get("registry") or "").strip().strip('"') or None
    out_dir = (request.form.get("out") or "out").strip() or "out"

    pdfs = _iter_pdfs(inp)
    if not pdfs:
        return _render(inp, registry or "", out_dir,
                       '<p class="bad">PDF не найдены по указанному пути.</p>')

    try:
        profile = create_profile(profile_key, registry)
    except ValueError as exc:
        return _render(inp, registry or "", out_dir, f'<p class="bad">{html.escape(str(exc))}</p>')

    parser = DocumentParser(profile)
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    ok = 0
    for pdf in pdfs:
        stem = os.path.splitext(os.path.basename(pdf))[0]
        out_path = os.path.join(out_dir, stem + ".json")
        try:
            res = parser.parse(pdf)
            _writer.write(res, out_path)
            ok += 1
            md, st = res.metadata, res.stats
            rows.append(
                "<tr><td>{f}</td><td>{id}</td><td>{title}</td><td>{sec}</td>"
                "<td>{tab}</td><td>{cov}%</td><td>{w}</td></tr>".format(
                    f=html.escape(os.path.basename(pdf)), id=html.escape(str(md.get("id"))),
                    title=html.escape((md.get("title") or "")[:60]),
                    sec=st["sections_found"], tab=st["tables_found"],
                    cov=st["coverage_percent"], w=len(res.warnings)))
        except Exception as exc:  # noqa: BLE001
            rows.append('<tr><td>{f}</td><td colspan="6" class="bad">{e}</td></tr>'.format(
                f=html.escape(os.path.basename(pdf)), e=html.escape(str(exc))))

    table = ("<p class='ok'>Готово: {ok}/{n}. JSON в папке <code>{out}</code>.</p>"
             "<table><tr><th>Файл</th><th>ID</th><th>Название</th><th>Разделов</th>"
             "<th>Таблиц</th><th>Покрытие</th><th>Warn</th></tr>{rows}</table>").format(
        ok=ok, n=len(pdfs), out=html.escape(out_dir), rows="".join(rows))
    return _render(inp, registry or "", out_dir, table)


def main() -> None:
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
