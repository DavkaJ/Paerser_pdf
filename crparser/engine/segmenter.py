#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Нарезка документа на разделы и регионы-исключения (конечный автомат).

Это движок-уровень: он НЕ знает тип документа. Все доменные решения (что считать
заголовком, продолжением, что относится к исключениям) принимает профиль через
методы DocumentProfile. Здесь — только механика:

  1. Убрать строки, попавшие в таблицы (вычитание) и одиночные номера страниц.
  2. Найти реальное начало разделов и отсечь ToC/front-matter ДО нарезки.
  3. Прогнать конечный автомат (режимы front/section/refs/appendices) с учётом
     висящих номеров заголовков и склейки многострочных заголовков.
  4. Собрать плоские разделы в дерево по уровням.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, TYPE_CHECKING

from crparser.engine.models import (
    BBox,
    ExcludedSpec,
    Heading,
    HeadingKind,
    Line,
    Page,
    Section,
)

if TYPE_CHECKING:  # импорт только для типов — без рантайм-зависимости от профилей
    from crparser.profiles.base import DocumentProfile

_RE_PAGE_NUMBER = re.compile(r"^\d{1,3}$")
# лидеры оглавления: точки или подчёркивания («.....» / «_____»)
_RE_LEADER = re.compile(r"\.{3,}|_{3,}")


def _top_int(number: Optional[str]) -> int:
    """Старший компонент номера как int («3.1» -> 3); 0 при неудаче."""
    if not number:
        return 0
    head = number.split(".")[0]
    return int(head) if head.isdigit() else 0


class Segmenter:
    """
    Превращает страницы со строками в дерево разделов + бакеты исключений.

    Экземпляр одноразовый: создаётся под один документ и профиль.
    """

    def __init__(self, profile: "DocumentProfile", body_size: float) -> None:
        self._profile = profile
        self._body = body_size
        self._spec: ExcludedSpec = profile.excluded_regions()
        self._warnings: List[str] = []
        self._blank_gap: float = 1e9  # порог «пустой строки», считается на segment()

    # ---- публичный вход --------------------------------------------------

    def segment(
        self,
        pages: List[Page],
        subtraction_map: Dict[int, List[BBox]],
        warnings_list: List[str],
    ) -> Dict[str, object]:
        """
        Вернуть {'sections': List[Section], 'excluded': dict}.
        `subtraction_map`: page_index(0-based) -> bbox таблиц для вычитания.
        """
        self._warnings = warnings_list

        lines = self._collect_lines(pages, subtraction_map)
        self._blank_gap = self._compute_blank_gap(lines)
        start = self._find_content_start(lines)

        front_lines = lines[:start]
        body_lines = lines[start:]

        # инлайн-разбиение строк основного текста (профиль-хук)
        split_body: List[Line] = []
        for line in body_lines:
            split_body.extend(self._profile.split_inline_headings(line))

        excluded = self._empty_excluded()
        self._split_front_matter(front_lines, excluded)

        sections = self._run_state_machine(split_body, excluded)
        tree = self._build_hierarchy(sections)

        return {"sections": tree, "excluded": excluded}

    # ---- подготовка строк ------------------------------------------------

    def _collect_lines(
        self, pages: List[Page], subtraction_map: Dict[int, List[BBox]]
    ) -> List[Line]:
        """Все строки документа без табличных (вычитание) и без номеров страниц."""
        out: List[Line] = []
        for page in pages:
            boxes = subtraction_map.get(page.number - 1, [])
            for line in page.lines:
                text = line.text.strip()
                if not text or _RE_PAGE_NUMBER.fullmatch(text):
                    continue
                if self._covered_by_table(line.bbox, boxes):
                    # заголовок раздела верхнего уровня не может быть «внутри»
                    # таблицы — ложная детекция таблицы не должна его съедать
                    h = self._classify(line)
                    if not (h and h.level == 1):
                        continue
                out.append(line)
        return out

    @staticmethod
    def _covered_by_table(line_bbox: BBox, boxes: List[BBox]) -> bool:
        if not boxes:
            return False
        x0, y0, x1, y1 = line_bbox
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        for tx0, ty0, tx1, ty1 in boxes:
            if tx0 - 2 <= cx <= tx1 + 2 and ty0 <= cy <= ty1:
                return True
        return False

    # ---- отсечение ToC: ищем реальное начало разделов --------------------

    def _find_content_start(self, lines: List[Line]) -> int:
        """
        Найти реальное начало основного текста и отсечь оглавление ДО нарезки.

        Ключевой признак: настоящий нумерованный заголовок сопровождается прозой
        (несколькими строками тела), а пункт оглавления — нет (за ним идут только
        другие пункты ToC, точки-лидеры и номера страниц). Считаем строки прозы в
        окне после каждого нумерованного заголовка; первый заголовок, за которым
        набирается достаточно прозы, и есть начало контента.

        Проза считается ПО СТРОКАМ (без порога длины), потому что PyMuPDF на части
        КР дробит абзац и даже заголовок на отдельные слова. Заголовки распознаём
        только через профиль, поэтому метод остаётся документ-агностичным.
        """
        n = len(lines)
        cls = [self._classify(ln) for ln in lines]
        prose = [self._is_prose(lines[k].text, cls[k]) for k in range(n)]

        # 1) Главный признак: оглавление — это плотный кластер строк с точками-
        #    лидерами; в теле КР их нет. Находим конец кластера и берём первый
        #    нумерованный заголовок после него (он же — начало реального текста).
        #    Доверяем методу, только если рядом с «Оглавлением» (или в начале
        #    документа) есть НАСТОЯЩИЙ кластер точек (>=4) — иначе единичные точки
        #    в приложениях увели бы старт в конец документа.
        dot_idx = [k for k in range(n) if _RE_LEADER.search(lines[k].text)]
        toc_anchor = next((k for k in range(n)
                           if self._spec.toc.match(lines[k].text.strip())), None)
        if toc_anchor is not None:
            near = [k for k in dot_idx if 0 <= k - toc_anchor <= 60]
        else:
            near = [k for k in dot_idx if k <= 400]
        if len(near) >= 4:
            start = near[0]
            toc_end = start
            for k in dot_idx:
                if k < start:
                    continue
                if k - toc_end <= 40:   # тот же кластер ToC (учёт пословной вёрстки)
                    toc_end = k
                else:
                    break
            idx = self._first_heading_with_prose(lines, cls, prose, toc_end + 1, n)
            if idx is not None:
                return idx

        # 2) Нет лидеров. Если документ ОФОРМЛЯЕТ разделы визуально (часть заголовков
        #    жирные/крупные), пункты оглавления — нет: сначала ищем первый ВИЗУАЛЬНЫЙ
        #    заголовок с прозой (так пропускаем пословно-свёрстанное оглавление, как
        #    в КР16_4). Иначе (заголовки кеглем тела, как в КР359) — без этого условия.
        visual_tops = sum(1 for k in range(n)
                          if cls[k] and cls[k].level == 1
                          and cls[k].kind != HeadingKind.NAMED and self._visual(lines[k]))
        if visual_tops >= 2:
            idx = self._first_heading_with_prose(lines, cls, prose, 0, n, require_visual=True)
            if idx is not None:
                return idx
        idx = self._first_heading_with_prose(lines, cls, prose, 0, n)
        if idx is not None:
            return idx

        # 3) fallbacks: первый раздел верхнего уровня / первый нумерованный / с начала
        for i in range(n):
            h = cls[i]
            if h and h.level == 1 and h.kind != HeadingKind.NAMED:
                return i
        for i in range(n):
            if cls[i] and cls[i].number:
                return i
        return 0

    def _first_heading_with_prose(self, lines: List[Line], cls: List[Optional[Heading]],
                                  prose: List[bool], lo: int, hi: int,
                                  require_visual: bool = False) -> Optional[int]:
        """
        Первый нумерованный заголовок в [lo, hi), за которым идёт проза тела.

        Окно прерывается на: маркере региона (Список литературы/Приложение/
        Оглавление — это хвост оглавления в КР без точек-лидеров), именованном
        заголовке (переход к front-matter) или следующем разделе верхнего уровня.
        У реального раздела в окне идёт проза без этих маркеров.
        """
        for i in range(lo, hi):
            h = cls[i]
            if not h or h.kind == HeadingKind.NAMED or not h.number:
                continue
            if require_visual and not self._visual(lines[i]):
                continue
            run = 0  # ПОДРЯД идущих строк прозы (тело — это прогон прозы; в ToC же
            #          обрывки-переносы разбиты пунктами 1.1/1.2 и прогон не растёт)
            for j in range(i + 1, min(i + 24, hi)):
                if self._spec.detect(lines[j].text.strip()):
                    break
                hj = cls[j]
                if hj is not None and hj.kind == HeadingKind.NAMED:
                    break
                if hj and hj.level == 1 and hj.kind != HeadingKind.NAMED:
                    break
                if prose[j]:
                    run += 1
                    if run >= 3:
                        return i
                elif hj is not None:   # подзаголовок (>=2) — прогон прозы прерывается
                    run = 0
        return None

    @staticmethod
    def _is_prose(text: str, heading: Optional[Heading]) -> bool:
        """Строка похожа на прозу: не заголовок, не точки-лидеры, не номер страницы."""
        if heading is not None:
            return False
        text = text.strip()
        if not text or _RE_PAGE_NUMBER.fullmatch(text):
            return False
        if _RE_LEADER.search(text):  # точки/подчёркивания-лидеры -> это оглавление
            return False
        return bool(re.search(r"[А-Яа-яA-Za-z]", text))

    # ---- разбор front-matter на front_matter / toc -----------------------

    def _split_front_matter(self, front_lines: List[Line], excluded: Dict) -> None:
        """Поделить предтекст на front_matter и toc по маркеру оглавления."""
        front_parts: List[str] = []
        toc_parts: List[str] = []
        in_toc = False
        for line in front_lines:
            text = line.text.strip()
            if not text:
                continue
            if self._spec.toc.match(text):
                in_toc = True
                continue
            (toc_parts if in_toc else front_parts).append(text)

        if front_parts:
            excluded["front_matter"].append(
                {"title": "front_matter", "text": " ".join(front_parts).strip()})
        if toc_parts:
            excluded["toc"].append(
                {"title": "Оглавление", "text": " ".join(toc_parts).strip()})

    # ---- основной конечный автомат ---------------------------------------

    def _run_state_machine(self, lines: List[Line], excluded: Dict) -> List[Section]:
        sections: List[Section] = []
        current: Optional[Section] = None
        excl_mode: Optional[str] = None   # None | 'references' | 'appendices'
        last_number: Optional[str] = None
        seen_references = False           # встречали ли «Список литературы»
        max_top = 0                       # наибольший НОМЕР раздела верхнего уровня

        # «висящий» номер (kind=NUMBER_ONLY) и накопитель открытого заголовка
        pending_number: Optional[Heading] = None
        open_heading: Optional[Dict] = None  # {'heading': Heading, 'title_parts': [...], 'extra': int}

        def flush_open() -> None:
            nonlocal open_heading, current, last_number, max_top
            if open_heading is None:
                return
            heading: Heading = open_heading["heading"]
            title = self._join_title(open_heading["title_parts"])
            section = Section(
                number=heading.number,
                title=title or heading.title,
                level=heading.level,
                text="",
            )
            sections.append(section)
            current = section
            last_number = heading.number or last_number
            open_heading = None

        def claim_top(h: Heading) -> None:
            """Зафиксировать номер открытого раздела верхнего уровня сразу (не ждать
            flush): иначе два одинаковых заголовка подряд («6.» и «6.») оба пройдут."""
            nonlocal max_top
            if h.level == 1 and h.number:
                max_top = max(max_top, _top_int(h.number))

        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            text = line.text.strip()
            if not text:
                i += 1
                continue

            region = self._spec.detect(text)
            # приложения структурно идут ПОСЛЕ списка литературы: не пускаем в
            # режим приложений, пока не встретили список литературы — это убивает
            # ложные срабатывания на перекрёстных ссылках «Приложение ...» в теле
            if region == "appendices" and not seen_references:
                region = None
            if region == "references":
                seen_references = True

            # --- уже внутри references/appendices: всё льётся в исключения ---
            if excl_mode:
                if region in ("references", "appendices"):
                    excl_mode = region
                    excluded[excl_mode].append({"title": text, "text": ""})
                elif excluded[excl_mode]:
                    excluded[excl_mode][-1]["text"] += " " + text
                else:
                    excluded[excl_mode].append({"title": excl_mode, "text": text})
                i += 1
                continue

            # --- 1. дозаклейка заголовка к висящему номеру («4.» + след. строка) ---
            # Собираем заголовок даже если PyMuPDF разбил его на отдельные слова
            # (частый кейс раздела 4 «Медицинская реабилитация»).
            if pending_number is not None:
                title, consumed = self._assemble_pending_title(lines, i, pending_number)
                probe = line.clone(title) if title else line
                if title and self._profile.can_attach_title(probe, pending_number, self._body):
                    heading = Heading(
                        number=pending_number.number,
                        title=title,
                        level=pending_number.level,
                        kind=HeadingKind.NUMBERED,
                        visual=self._visual(line),
                        page=line.page,
                        bbox=line.bbox,
                    )
                    open_heading = {"heading": heading, "title_parts": [title],
                                    "extra": 0}
                    claim_top(heading)
                    pending_number = None
                    i += consumed
                    continue
                pending_number = None  # номер «повис» зря — забываем

            # --- 2. продолжение уже открытого многострочного заголовка ---
            if open_heading is not None and self._is_continuation(line, open_heading):
                open_heading["title_parts"].append(text)
                open_heading["extra"] += 1
                i += 1
                continue

            # --- 3. старт региона-исключения ---
            if region in ("references", "appendices"):
                flush_open()
                current = None
                excl_mode = region
                excluded[excl_mode].append({"title": text, "text": ""})
                i += 1
                continue

            heading = self._classify(line)

            # Монотонность раздела ВЕРХНЕГО уровня по отдельному счётчику max_top
            # (только разделы уровня 1), чтобы классификационные подпункты
            # «3.1 ХОБЛ»/«5.1 …» внутри раздела 1 его не ломали. Принимаем раздел,
            # если это СЛЕДУЮЩИЙ по порядку номер (== max_top+1; шрифт не важен —
            # в части КР заголовки идут кеглем тела) ЛИБО больший номер, оформленный
            # как визуальный заголовок. Это отсекает фантомы-перекрёстные-ссылки
            # («…в разделе 6. Организация…» с пропуском 4–5), дубли и хвост ToC.
            # Для подуровней (>=2) — обычная монотонность относительно last_number.
            def order_ok(h: Heading) -> bool:
                if h.level == 1:
                    top = _top_int(h.number)
                    if top == max_top + 1:
                        return True
                    return top > max_top and self._visual(line)
                return self._profile.heading_order_valid(h.number, last_number)

            # --- 4. полноценный заголовок в одной строке ---
            if heading and heading.kind == HeadingKind.NUMBERED and heading.number:
                if order_ok(heading):
                    flush_open()
                    open_heading = {"heading": heading, "title_parts": [heading.title],
                                    "extra": 0}
                    claim_top(heading)
                    i += 1
                    continue

            # --- 5. заголовок одним номером («4.») ---
            if heading and heading.kind == HeadingKind.NUMBER_ONLY and heading.number:
                if order_ok(heading):
                    if heading.level == 1 or self._visual(line):
                        flush_open()
                        pending_number = heading
                        i += 1
                        continue

            # --- 6. именованный раздел (Критерии оценки качества и т.п.) ---
            if heading and heading.kind == HeadingKind.NAMED:
                flush_open()
                section = Section(number=None, title=heading.title,
                                  level=heading.level, text="")
                sections.append(section)
                current = section
                i += 1
                continue

            # --- 7. обычный текст: закрываем открытый заголовок и копим тело ---
            if open_heading is not None:
                flush_open()
            if current is not None:
                current.text += " " + text
            else:
                excluded["other"].append({"title": "unassigned", "text": text})
            i += 1

        flush_open()

        # финальная нормализация текста
        for section in sections:
            section.text = self._normalize(section.text)
        for bucket in excluded.values():
            for item in bucket:
                item["text"] = self._normalize(item.get("text", ""))
        if not sections:
            self._warnings.append(
                "не найдено ни одного раздела — проверьте структуру PDF / эвристики")
        return sections

    # ---- сборка дерева по уровням ----------------------------------------

    @staticmethod
    def _build_hierarchy(flat: List[Section]) -> List[Section]:
        roots: List[Section] = []
        stack: List[Section] = []
        for section in flat:
            section.children = []
            while stack and stack[-1].level >= section.level:
                stack.pop()
            if stack:
                stack[-1].children.append(section)
            else:
                roots.append(section)
            stack.append(section)
        return roots

    # ---- тонкие обёртки над профилем -------------------------------------

    def _classify(self, line: Line) -> Optional[Heading]:
        return self._profile.classify_heading(line, self._body)

    def _assemble_pending_title(self, lines: List[Line], start: int,
                                pending: Heading) -> "tuple[Optional[str], int]":
        """
        Собрать заголовок для висящего номера, начиная со строки start.

        Обычный случай — одна строка с полным заголовком. Если же первая строка
        короткий визуальный фрагмент (PyMuPDF разбил заголовок на слова), доклеиваем
        последующие визуальные строки до начала тела. Возвращает (title, сколько
        строк поглощено).
        """
        n = len(lines)
        first = lines[start]
        ftext = first.text.strip()
        if not ftext or self._spec.detect(ftext):
            return None, 0
        h = self._classify(first)
        if h and h.kind in (HeadingKind.NUMBERED, HeadingKind.NUMBER_ONLY):
            return None, 0

        parts = [ftext]
        consumed = 1
        # пословная вёрстка заголовка: первая строка — короткий жирный/крупный фрагмент
        if self._visual(first) and len(ftext) <= 25:
            j = start + 1
            while j < n and consumed < 15:
                ln = lines[j]
                t = ln.text.strip()
                if not t:
                    j += 1
                    continue
                if ln.gap_before >= self._blank_gap:
                    break  # пустая строка — конец заголовка
                if self._spec.detect(t):
                    break
                hj = self._classify(ln)
                if hj and hj.kind in (HeadingKind.NUMBERED, HeadingKind.NUMBER_ONLY):
                    break
                if not self._visual(ln):
                    break  # дошли до тела (не визуальная строка)
                parts.append(t)
                consumed += 1
                j += 1
        return self._join_title(parts), consumed

    def _is_continuation(self, line: Line, open_heading: Dict) -> bool:
        # Главный признак конца заголовка — лексический (профиль видит, что строка
        # уже не продолжение: началась с заглавной/нового предложения), плюс новый
        # нумерованный/именованный заголовок. Прежний жёсткий лимит «5 строк» (из-за
        # которого длинные/пословно-свёрстанные заголовки обрывались) заменён на:
        #   * символьный предохранитель от разгона (тело не утянется целиком);
        #   * КОНСЕРВАТИВНЫЙ разрыв «пустой строки» (срабатывает лишь на явно
        #     большом интервале, чтобы не рубить заголовки с увеличенным лидингом).
        if line.gap_before >= self._blank_gap:
            return False
        current_title = self._join_title(open_heading["title_parts"])
        if len(current_title) > 320:
            return False
        # новый заголовок/номер — не продолжение
        h = self._classify(line)
        if h and h.kind in (HeadingKind.NUMBERED, HeadingKind.NUMBER_ONLY,
                            HeadingKind.NAMED):
            return False
        return self._profile.is_title_continuation(
            current_title, line, open_heading["heading"], self._body)

    @staticmethod
    def _compute_blank_gap(lines: List[Line]) -> float:
        """
        Порог «пустой строки» = 1.85× типичного межстрочного интервала документа.
        Пустая строка добавляет ~целую высоту строки, т.е. интервал ≈2×; увеличенный
        лидинг внутри заголовка (бывает ~1.7×) ниже порога и не рубит заголовок.
        Медиана положительных разрывов базовых линий (без пословной вёрстки с нулевым
        разрывом и скачков страниц). Нет данных — «бесконечность» (сигнал не нужен).
        """
        gaps = sorted(ln.gap_before for ln in lines if 2.0 < ln.gap_before < 60.0)
        if len(gaps) < 5:
            return 1e9
        median = gaps[len(gaps) // 2]
        return median * 1.85

    def _visual(self, line: Line) -> bool:
        return line.size >= self._body * 1.35 or line.bold

    # ---- утилиты текста --------------------------------------------------

    @staticmethod
    def _join_title(parts: List[str]) -> str:
        title = " ".join(p.strip() for p in parts if p and p.strip())
        title = re.sub(r"-\s+", "-", title)          # «ВИЧ- инфекция» -> «ВИЧ-инфекция»
        title = re.sub(r"\s+([,.;:])", r"\1", title)  # пробел перед пунктуацией
        return re.sub(r"\s+", " ", title).strip()

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "")).strip()

    @staticmethod
    def _empty_excluded() -> Dict[str, List[Dict[str, str]]]:
        return {"front_matter": [], "toc": [], "references": [],
                "appendices": [], "other": []}
