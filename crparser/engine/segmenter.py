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
from collections import defaultdict
from typing import Dict, List, Optional, TYPE_CHECKING

from crparser.engine.models import (
    BBox,
    ExcludedItem,
    ExcludedSpec,
    Heading,
    HeadingKind,
    Line,
    Page,
    Section,
)
from crparser.engine.toc import TocIndex, norm

if TYPE_CHECKING:  # импорт только для типов — без рантайм-зависимости от профилей
    from crparser.profiles.base import DocumentProfile

_RE_PAGE_NUMBER = re.compile(r"^\d{1,3}$")
# лидеры оглавления: точки/подчёркивания/юникод-многоточие («.....» / «_____» / «…»)
_RE_LEADER = re.compile(r"\.{3,}|_{3,}|…+|‥+|․{2,}")
# хвост строки оглавления: «...... 26» ИЛИ «  26» (номер страницы в конце). Часть КР
# верстает TOC БЕЗ точек-лидеров, одними номерами страниц (КР715_2). Используется ТОЛЬКО
# как запасной сигнал кластера TOC при явном маркере «Оглавление» (промпт 12 ШАГ 2).
_RE_TOC_TAIL = re.compile(r"(\.{2,}\s*\d{1,4}|\s\d{1,4})\s*$")
# Тумблер запасного tail-пути (ШАГ 2). Дефолт True; изоляционный A/B-замер выключает его,
# чтобы получить baseline «Jul-20 без ШАГ 2» и отделить эффект правки от дрейфа Jul-14->Jul-20.
_TAIL_TOC_ENABLED = True
# Предел длины заголовка подраздела теперь задаёт NumberingPolicy профиля
# (промпт 11): self._numbering.max_subtitle_len. Кандидат длиннее — это абзац прозы
# с номером, а не раздел (отвергается, если не подтверждён оглавлением).


def _top_int(number: Optional[str]) -> int:
    """Старший компонент номера как int («3.1» -> 3); 0 при неудаче."""
    if not number:
        return 0
    head = number.split(".")[0]
    return int(head) if head.isdigit() else 0


def _walk_tree(sections: List[Section]):
    """Обход дерева разделов в ДОКУМЕНТНОМ порядке (узел, затем его дети)."""
    for s in sections:
        yield s
        yield from _walk_tree(s.children)


class Segmenter:
    """
    Превращает страницы со строками в дерево разделов + бакеты исключений.

    Экземпляр одноразовый: создаётся под один документ и профиль.
    """

    def __init__(self, profile: "DocumentProfile", body_size: float) -> None:
        self._profile = profile
        self._body = body_size
        # доменные политики (промпт 11): движок спрашивает профиль, а не хардкодит
        self._regions = profile.regions
        self._numbering = profile.numbering
        self._spec: ExcludedSpec = self._regions.excluded_spec()
        self._warnings: List[str] = []
        self._blank_gap: float = 1e9  # порог «пустой строки», считается на segment()
        self._toc: Optional[TocIndex] = None  # оглавление как арбитр заголовков (баг 4)
        # включать ли главы с римским номером: только для документов с пунктирными
        # подразделами (в файлах с одиночно-арабскими подразделами римские главы
        # столкнулись бы номерами — там их целиком отключаем, поведение как раньше)
        self._roman_ok: bool = True

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

        # Оглавление как арбитр приёма заголовков (баг 4): строим индекс из сырых
        # строк документа (тот же код, что и в валидаторе). None — если оглавление
        # не распарсилось (тогда отсева фантомов по TOC нет, работаем как раньше).
        self._toc = TocIndex.from_lines(
            [ln.text for page in pages for ln in page.lines], self._spec.toc)

        lines = self._collect_lines(pages, subtraction_map)
        # римские главы включаем только если ПОЛИТИКА профиля их использует (промпт 11)
        # И структура документа безопасна (нет одиночно-арабских подпунктов). Профиль
        # без римских глав (uses_roman_chapters=False) их вовсе не эмитит — гейт инертен.
        self._roman_ok = (self._numbering.uses_roman_chapters
                          and self._roman_chapters_safe(lines))
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
        sections = self._drop_phantom_duplicates(sections)
        # provenance: схлопнуть повторы span_uid ВНУТРИ узла/элемента (одна строка,
        # разбитая split_inline_headings, даёт клоны с ОДНИМ span_uid). На владельца
        # это не влияет, но список должен быть чистым множеством (промпт 10).
        for s in sections:
            s.span_uids = self._dedupe(s.span_uids)
        for bucket in excluded.values():
            for item in bucket:
                item.span_uids = self._dedupe(item.span_uids)
        tree = self._build_hierarchy(sections)
        self._check_heading_order(tree)

        return {"sections": tree, "excluded": excluded}

    def _check_heading_order(self, tree: List[Section]) -> None:
        """Промпт 11b: подключить NumberingPolicy.heading_order_valid (монотонность
        номеров 1<1.1<1.2<2). Проходим дерево в ДОКУМЕНТНОМ порядке (DFS) и сверяем
        каждый нумерованный узел с предыдущим.

        Нарушение НЕ отбрасывает раздел молча (guard 11b): реальный отсев фантомов
        уже делают order_ok (level-1 по max_top), toc_reject и _drop_phantom_duplicates
        (конкурирующий кандидат, подтверждённый оглавлением) — дублировать отбрасывание
        значило бы прятать проблемы (PASS не должен расти). Здесь проверка делает
        строго-ОБРАТНЫЙ шаг нумерации ВИДИМЫМ через warning. Равные номера (дубли)
        пропускаем — их уже отмечает отдельный warning «коллизия номера». Формулировка
        БЕЗ подстроки «ocr» (иначе ложно сработал бы гейт OCR_REQUIRED валидатора)."""
        last: Optional[str] = None
        flagged: set = set()
        for s in _walk_tree(tree):
            num = s.number
            if not num:
                continue
            if last and num != last and not self._numbering.heading_order_valid(num, last):
                if num not in flagged:
                    flagged.add(num)
                    self._warnings.append(
                        "порядок номеров: %s идёт после %s — обратный шаг нумерации "
                        "(heading_order_valid, промпт 11b)" % (num, last))
            last = num

    @staticmethod
    def _dedupe(uids: List[str]) -> List[str]:
        """Порядок-сохраняющее удаление повторов span_uid."""
        seen: set = set()
        out: List[str] = []
        for u in uids:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _drop_phantom_duplicates(self, sections: List[Section]) -> List[Section]:
        """
        Снять мнимый дубль номера: когда ОДИН узел с номером N.N подтверждён
        оглавлением, а другой с тем же номером — нет (и его заголовка нигде в
        оглавлении нет), второй — это одноимённый пункт-классификация из прозы
        («2.1 Нечастая ГБН» при реальном «2.1 Жалобы»). Убираем его, а текст
        (заголовок + тело) подклеиваем к предыдущему уцелевшему разделу, чтобы не
        терять покрытие. Реальные коллизии источника целы: там оба заголовка есть
        в оглавлении, оба confirmed — ни один не снимается. Без оглавления — no-op.
        """
        if self._toc is None:
            return sections
        groups: Dict[str, List[Section]] = defaultdict(list)
        for s in sections:
            if s.number:
                groups[s.number].append(s)
        drop: set = set()
        for num, group in groups.items():
            if len(group) < 2:
                continue
            confirmed = [s for s in group if self._toc.confirmed(num, s.title)]
            if not confirmed:
                continue
            for s in group:
                if s not in confirmed and not self._toc.title_anywhere(s.title):
                    drop.add(id(s))
        if not drop:
            return sections
        out: List[Section] = []
        for s in sections:
            if id(s) in drop:
                if out:
                    tail = (" " + s.title + " " + s.text).rstrip()
                    out[-1].text = (out[-1].text + tail).strip()
                    # provenance: спаны снятого узла переходят к тому, к чьему тексту
                    # подклеены (иначе они стали бы «без владельца»)
                    out[-1].span_uids.extend(s.span_uids)
            else:
                out.append(s)
        return out

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
        elif toc_anchor is not None and _TAIL_TOC_ENABLED:
            # ШАГ 2 (промпт 12, START_IN_TOC): оглавление БЕЗ точек-лидеров, свёрстанное
            # одними номерами страниц (КР715_2: «3.1.2 Физическая активность 58»). Лидерный
            # путь не сработал (near<4), но есть явный маркер «Оглавление» и рядом — плотный
            # кластер строк с ХВОСТОМ-номером-страницы. Тогда TOC кончается на этом кластере,
            # а тело начинается ПОСЛЕ него. Гейтим маркером + плотностью (>=4), чтобы одиночные
            # «...в 2020 15» в теле не увели старт (класс 3 не должен пострадать — I33).
            tail_idx = [k for k in range(n)
                        if _RE_TOC_TAIL.search(lines[k].text.strip())
                        and re.search(r"[А-Яа-яA-Za-z]", lines[k].text)]
            tnear = [k for k in tail_idx if 0 <= k - toc_anchor <= 60]
            if len(tnear) >= 4:
                start = tnear[0]
                toc_end = start
                for k in tail_idx:
                    if k < start:
                        continue
                    if k - toc_end <= 40:
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
        front_uids: List[str] = []
        toc_uids: List[str] = []
        in_toc = False
        for line in front_lines:
            text = line.text.strip()
            if not text:
                continue
            if self._spec.toc.match(text):
                in_toc = True
                toc_uids.append(line.span_uid)   # сам маркер «Оглавление» — в toc
                continue
            if in_toc:
                toc_parts.append(text)
                toc_uids.append(line.span_uid)
            else:
                front_parts.append(text)
                front_uids.append(line.span_uid)

        if front_parts:
            excluded["front_matter"].append(ExcludedItem(
                title="front_matter", text=" ".join(front_parts).strip(),
                span_uids=front_uids, kind="front_matter"))
        if toc_parts:
            excluded["toc"].append(ExcludedItem(
                title="Оглавление", text=" ".join(toc_parts).strip(),
                span_uids=toc_uids, kind="toc"))

    # ---- основной конечный автомат ---------------------------------------

    def _run_state_machine(self, lines: List[Line], excluded: Dict) -> List[Section]:
        sections: List[Section] = []
        current: Optional[Section] = None
        excl_mode: Optional[str] = None   # None | 'references' | 'appendices'
        last_number: Optional[str] = None
        seen_references = False           # встречали ли «Список литературы»
        max_top = 0                       # наибольший НОМЕР раздела верхнего уровня
        seen_numbers: Dict[str, str] = {}  # номер -> первый заголовок (детект дублей)
        collided: set = set()             # номера, по которым уже выдан warning
        confirmed_seen: set = set()       # номера, принятые как ПОДТВЕРЖДЁННЫЕ TOC

        # «висящий» номер (kind=NUMBER_ONLY) и накопитель открытого заголовка
        pending_number: Optional[Heading] = None
        pending_idx: int = -1                # строка, где встретился висящий номер
        # open_heading: {'heading', 'title_parts', 'extra', 'span_uids', 'page', 'bbox'}
        open_heading: Optional[Dict] = None

        def flush_open() -> None:
            nonlocal open_heading, current, last_number, max_top
            if open_heading is None:
                return
            heading: Heading = open_heading["heading"]
            title = self._join_title(open_heading["title_parts"]) or heading.title
            title = self._close_dangling_paren(title)
            num = heading.number
            if num:
                norm_new = self._normalize(title).lower()
                if num in seen_numbers:
                    # НАСТОЯЩИЙ дубль: тот же номер И тот же/пустой заголовок —
                    # гасим (фантомный повтор). Сравнение БЕЗ пунктуации: «ППИ.» и
                    # «ППИ» — один раздел (различие только в OCR-точке), а не коллизия.
                    if not norm_new or norm(title) == norm(seen_numbers[num]):
                        open_heading = None
                        return
                    # КОЛЛИЗИЯ: один номер с РАЗНЫМИ заголовками — это дефект
                    # источника (напр. опечатка нумерации). Сохраняем ОБА узла,
                    # но фиксируем предупреждение (молча не сливаем).
                    if num not in collided:
                        collided.add(num)
                        self._warnings.append(
                            "коллизия номера %s в источнике: «%s» и «%s» — "
                            "сохранены оба раздела" % (num, seen_numbers[num], title))
                else:
                    seen_numbers[num] = title
            section = Section(number=num, title=title, level=heading.level, text="",
                              span_uids=list(open_heading["span_uids"]),
                              page=open_heading["page"], bbox=open_heading["bbox"])
            sections.append(section)
            current = section
            last_number = num or last_number
            # запоминаем номера, чьи разделы ПОДТВЕРЖДЕНЫ оглавлением — по ним потом
            # отсеиваем мнимые дубли (баг 4)
            if num and self._toc is not None and self._toc.confirmed(num, title):
                confirmed_seen.add(num)
            open_heading = None

        def claim_top(h: Heading) -> None:
            """Зафиксировать номер открытого раздела верхнего уровня сразу (не ждать
            flush): иначе два одинаковых заголовка подряд («6.» и «6.») оба пройдут."""
            nonlocal max_top
            if h.level == 1 and h.number:
                max_top = max(max_top, _top_int(h.number))

        def toc_reject(number: Optional[str], title: str, idx: int = -1) -> bool:
            """
            Кандидат-подзаголовок — это ложный пункт нумерованного списка из прозы,
            а не раздел документа? (баги 4 и 6б). Работает только для подуровней.

            Не отклоняем, если номер+заголовок ПОДТВЕРЖДЁН оглавлением (в т.ч.
            легитимная коллизия источника — у номера несколько заголовков в TOC).
            Иначе отклоняем по любому из признаков:
              * длина заголовка > предела — это абзац прозы с номером, а не раздел
                (фантомные «исходы»/«критерии» в КР16_4: 3.1/3.2/3.4/7.1);
              * (есть оглавление) верхний компонент номера не равен текущему разделу
                ИЛИ номер уже занят подтверждённым разделом (мнимый дубль) — баг 4;
              * (НЕТ оглавления) предыдущая непустая строка кончилась двоеточием
                (начался нумерованный список) ЛИБО номер уже встречался (локальный
                сброс нумерации 1.1/1.2… в прозе) — баг 6б.
            """
            if not number or "." not in number:
                return False
            confirmed = self._toc is not None and self._toc.confirmed(number, title)
            if confirmed:
                return False
            # (0) метка тела с номером («Рекомендуется…», «Комментарии:») — не раздел
            if self._profile.title_is_body_label(title):
                return True
            # (1) длина — работает и с оглавлением, и без него
            if len(title) > self._numbering.max_subtitle_len:
                return True
            # (2) арбитраж по оглавлению
            if self._toc is not None:
                if _top_int(number) != max_top:
                    return True
                return number in confirmed_seen
            # (3) оглавление недоступно — структурные эвристики отсева перечислений
            if idx > 0:
                prev = self._prev_nonblank(lines, idx)
                if prev is not None and prev.rstrip().endswith(":"):
                    return True
            return number in seen_numbers

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
                    excluded[excl_mode].append(ExcludedItem(
                        title=text, text="", span_uids=[line.span_uid],
                        kind=excl_mode))
                elif excluded[excl_mode]:
                    excluded[excl_mode][-1]["text"] += " " + text
                    excluded[excl_mode][-1].span_uids.append(line.span_uid)
                else:
                    excluded[excl_mode].append(ExcludedItem(
                        title=excl_mode, text=text, span_uids=[line.span_uid],
                        kind=excl_mode))
                i += 1
                continue

            # --- 1. дозаклейка заголовка к висящему номеру («4.» + след. строка) ---
            # Собираем заголовок даже если PyMuPDF разбил его на отдельные слова
            # (частый кейс раздела 4 «Медицинская реабилитация»).
            if pending_number is not None:
                title, consumed = self._assemble_pending_title(lines, i, pending_number)
                probe = line.clone(title) if title else line
                if title and self._profile.can_attach_title(probe, pending_number, self._body) \
                        and not toc_reject(pending_number.number, title, pending_idx) \
                        and self._top_accept(pending_number.number, title, pending_number.level):
                    heading = Heading(
                        number=pending_number.number,
                        title=title,
                        level=pending_number.level,
                        kind=HeadingKind.NUMBERED,
                        visual=self._visual(line),
                        canonical=self._profile.main_title_canonical(title, pending_number.number),
                        page=line.page,
                        bbox=line.bbox,
                    )
                    # provenance: якорь раздела — строка с висящим номером («4.»);
                    # спаны узла = номер + все строки, поглощённые в заголовок.
                    title_uids = [lines[k].span_uid for k in range(i, i + consumed)]
                    open_heading = {"heading": heading, "title_parts": [title],
                                    "extra": 0,
                                    "span_uids": [lines[pending_idx].span_uid] + title_uids,
                                    "page": lines[pending_idx].page,
                                    "bbox": lines[pending_idx].bbox}
                    claim_top(heading)
                    pending_number = None
                    i += consumed
                    continue
                pending_number = None  # номер «повис» зря — забываем

            # --- 2. продолжение уже открытого многострочного заголовка ---
            if open_heading is not None and self._is_continuation(line, open_heading):
                open_heading["title_parts"].append(text)
                open_heading["extra"] += 1
                open_heading["span_uids"].append(line.span_uid)
                i += 1
                continue

            # --- 3. старт региона-исключения ---
            if region in ("references", "appendices"):
                flush_open()
                current = None
                excl_mode = region
                excluded[excl_mode].append(ExcludedItem(
                    title=text, text="", span_uids=[line.span_uid], kind=region))
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
            # Подуровни (>=2) принимаются без монотонности (см. ниже, баг 3).
            def order_ok(h: Heading) -> bool:
                if h.level == 1:
                    top = _top_int(h.number)
                    if top == max_top + 1:
                        return True
                    return top > max_top and self._visual(line)
                # Подуровни (N.N, N.N.N…): принимаем ЛЮБОЙ корректно оформленный
                # заголовок. НЕ отбрасываем по «номер меньше текущего» — номера
                # законно откатываются на новой ветке (2.4.2.2.2 -> 2.4.3;
                # 2.4.1.1 -> 2.4.2). Иерархию строит _build_hierarchy, разбирая сам
                # пунктирный номер; реальные коллизии гасит/помечает flush_open.
                # Это чинит каскадную потерю разделов (баг 3).
                return True

            # --- 4. полноценный заголовок в одной строке ---
            if heading and heading.kind == HeadingKind.NUMBERED and heading.number:
                # toc_reject: ложный пункт нумерованного списка из прозы (баг 4) —
                # оставляем как тело текущего раздела; _top_accept: неканонический
                # раздел верхнего уровня принимаем только при подтверждении TOC
                if order_ok(heading) and not toc_reject(heading.number, heading.title, i) \
                        and self._top_accept(heading.number, heading.title, heading.level,
                                             heading.canonical):
                    flush_open()
                    open_heading = {"heading": heading, "title_parts": [heading.title],
                                    "extra": 0, "span_uids": [line.span_uid],
                                    "page": line.page, "bbox": line.bbox}
                    claim_top(heading)
                    i += 1
                    continue

            # --- 5. заголовок одним номером («4.», «2.5.2») ---
            # Строка целиком — номер раздела (NUMBER_ONLY уже это гарантирует), а
            # заголовок придёт следующей строкой. Не требуем жирный/крупный шрифт:
            # в части КР номера подразделов набраны кеглем тела (иначе терялись,
            # напр. «2.5.2 Подтверждение диагноза…»). Реальный фильтр — can_attach_title
            # на следующей итерации: если за номером не идёт правдоподобный заголовок,
            # «висящий» номер забывается без потери текста.
            if heading and heading.kind == HeadingKind.NUMBER_ONLY and heading.number:
                if order_ok(heading):
                    flush_open()
                    pending_number = heading
                    pending_idx = i
                    i += 1
                    continue

            # --- 6. именованный раздел (Критерии оценки качества и т.п.) ---
            if heading and heading.kind == HeadingKind.NAMED:
                flush_open()
                section = Section(number=None, title=heading.title,
                                  level=heading.level, text="",
                                  span_uids=[line.span_uid], page=line.page,
                                  bbox=line.bbox)
                sections.append(section)
                current = section
                i += 1
                continue

            # --- 7. обычный текст: закрываем открытый заголовок и копим тело ---
            if open_heading is not None:
                flush_open()
            if current is not None:
                current.text += " " + text
                current.span_uids.append(line.span_uid)
            else:
                excluded["other"].append(ExcludedItem(
                    title="unassigned", text=text, span_uids=[line.span_uid],
                    kind="other"))
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
        """
        Собрать дерево, определяя родителя РАЗБОРОМ пунктирного номера (баг 6).

        Родитель узла N.N(.N…) — это номер без последнего компонента (7.1 -> 7;
        2.4.2.2 -> 2.4.2). Ищем такой узел по ПОЛНОМУ индексу всех номеров, поэтому
        родитель находится, даже если в потоке он идёт ПОЗЖЕ ребёнка (как §7 после
        своего 7.1 в КР16_4). Если точного родителя нет — поднимаемся к ближайшему
        существующему ПРЕДКУ (отбрасываем ещё компонент), а если и его нет — узел
        становится корнем. К ЧУЖОМУ соседнему разделу (привязка «по последнему
        открытому узлу») не цепляем НИКОГДА — это и был источник HIERARCHY_BROKEN.

        Именованные / без-номерные разделы вешаем по уровню через стек.
        При коллизии номера ребёнок берёт ближайшего по позиции одноимённого
        родителя (предпочитая предшествующего) — так сохраняются реальные
        коллизии источника (КР51_2 3.1.3 ×2 со своими подпунктами).
        """
        idx_of: Dict[int, int] = {id(s): k for k, s in enumerate(flat)}
        by_number: Dict[str, List[Section]] = defaultdict(list)
        for s in flat:
            s.children = []          # обнулить ДО линковки: ребёнок может прийти к
            if s.number:             # родителю РАНЬШЕ его собственной итерации (7.1
                by_number[s.number].append(s)   # до §7) — сброс внутри цикла стёр бы его

        def nearest(cands: List[Section], child_idx: int) -> Optional[Section]:
            before = [c for c in cands if idx_of[id(c)] < child_idx]
            if before:
                return before[-1]            # ближайший предшествующий одноимённый
            after = [c for c in cands if idx_of[id(c)] > child_idx]
            return after[0] if after else None

        roots: List[Section] = []
        stack: List[Section] = []
        for k, section in enumerate(flat):
            parent: Optional[Section] = None
            num = section.number
            if num and "." in num:
                parts = num.split(".")
                for cut in range(len(parts) - 1, 0, -1):   # родитель -> предок -> ...
                    cand = nearest(by_number.get(".".join(parts[:cut]), []), k)
                    if cand is not None and cand is not section:
                        parent = cand
                        break
                # родитель/предок не найден -> корень (НЕ чужой сосед)
            else:
                while stack and stack[-1].level >= section.level:
                    stack.pop()
                parent = stack[-1] if stack else None
            if parent is not None:
                parent.children.append(section)
            else:
                roots.append(section)
            while stack and stack[-1].level >= section.level:
                stack.pop()
            stack.append(section)
        return roots

    # ---- тонкие обёртки над профилем -------------------------------------

    def _classify(self, line: Line) -> Optional[Heading]:
        h = self._profile.classify_heading(line, self._body)
        # римские главы включены только для «безопасных» документов (пунктирные
        # подразделы). Иначе игнорируем — строка станет прозой, как раньше.
        if h is not None and getattr(h, "roman", False) and not self._roman_ok:
            return None
        return h

    def _roman_chapters_safe(self, lines: List[Line]) -> bool:
        """Можно ли включать главы с римским номером для ЭТОГО документа.

        Опасность — файлы, где подразделы нумерованы ОДИНОЧНОЙ арабской цифрой
        («1. Определение», «2. Этиология», перезапуск в каждой главе): там номер
        римской главы (Краткая→1) столкнулся бы с номером подраздела «1». Признак:
        сразу за римской главой идёт одиночно-арабский (без точки) под-заголовок.
        Если хоть у одной римской главы так — отключаем римские главы целиком
        (документ парсится как раньше, без регрессий). Если подразделы пунктирные
        (N.M) — включаем.
        """
        cls = [self._profile.classify_heading(ln, self._body) for ln in lines]
        romans = [i for i, h in enumerate(cls)
                  if h is not None and getattr(h, "roman", False)]
        if not romans:
            return True
        for i in romans:
            for j in range(i + 1, min(i + 20, len(cls))):
                h = cls[j]
                if h is None or h.kind == HeadingKind.NAMED:
                    continue
                if getattr(h, "roman", False):
                    break            # следующая римская глава — подраздела между нет
                if h.number:
                    if "." not in h.number:
                        return False  # одиночно-арабский подраздел -> опасно
                    break             # пунктирный подраздел -> для этой главы ок
        return True

    def _top_accept(self, number: Optional[str], title: str, level: int,
                    canonical: Optional[bool] = None) -> bool:
        """
        Допустим ли раздел ВЕРХНЕГО уровня? Подуровни — всегда (их фильтрует
        toc_reject). Level-1: канонический раздел шаблона (название+номер) — да;
        иначе (нестандартное имя или сдвиг номера) — только если ПОДТВЕРЖДЁН
        оглавлением. Для файлов без оглавления неканонический level-1 не
        принимается (поведение как раньше — без регрессий).
        """
        if level != 1:
            return True
        if canonical is None:
            canonical = self._profile.main_title_canonical(title, number)
        if canonical:
            return True
        return self._toc is not None and self._toc.confirmed(number, title)

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
                # склеиваем ТОЛЬКО пословный прогон одной визуальной строки (нулевой
                # разрыв базовых линий). На первом РЕАЛЬНОМ переводе строки выходим:
                # дальнейшие строки-продолжения и границу с телом (список кодов МКБ
                # после «Особенности кодирования…») доведёт основной _is_continuation.
                if ln.gap_before >= self._blank_gap * 0.5:
                    break
                if self._spec.detect(t):
                    break
                # баг 2: тело (маркер списка, «Рекомендуется…», код услуги) или
                # завершённое предложение в накопленном заголовке — обрываем склейку,
                # даже если строка визуально жирная (тело рекомендаций часто жирное)
                if self._profile.heading_breaks_before(ln, self._join_title(parts)):
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
    def _prev_nonblank(lines: List[Line], idx: int) -> Optional[str]:
        """Текст ближайшей непустой строки перед idx (для эвристик отсева списков)."""
        j = idx - 1
        while j >= 0:
            t = lines[j].text.strip()
            if t:
                return t
            j -= 1
        return None

    @staticmethod
    def _join_title(parts: List[str]) -> str:
        title = " ".join(p.strip() for p in parts if p and p.strip())
        title = re.sub(r"-\s+", "-", title)          # «ВИЧ- инфекция» -> «ВИЧ-инфекция»
        title = re.sub(r"\s+([,.;:])", r"\1", title)  # пробел перед пунктуацией
        return re.sub(r"\s+", " ", title).strip()

    @staticmethod
    def _close_dangling_paren(title: str) -> str:
        """Закрыть «висящую» открытую скобку заголовка, чей закрывающий глиф испорчен
        OCR/битым cmap («…(группы заболеваний или состояний!» -> «…состояний)»).
        Срабатывает ТОЛЬКО когда в заголовке есть НЕЗАКРЫТАЯ «(» и он кончается таким
        глифом — легитимные заголовки (скобки сбалансированы) не трогаются."""
        if title.count("(") > title.count(")") and title[-1:] in "!’'`":
            return title[:-1] + ")"
        return title

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "")).strip()

    @staticmethod
    def _empty_excluded() -> Dict[str, List[ExcludedItem]]:
        return {"front_matter": [], "toc": [], "references": [],
                "appendices": [], "other": []}
