# -*- coding: utf-8 -*-
"""
Целостность пакета в git (страж регрессии .gitignore).

История: правило `.gitignore` `_*.py` (для отладочных `_scripts.py`) заодно ловило
ВСЕ `__init__.py` — пакет уходил в git БЕЗ них, и `git clone` давал не-импортируемый
`crparser`. Фикс — негатив `!__init__.py`. Этот тест ловит рецидив: если пакетный
`__init__.py` снова окажется вне git, чистый клон сломается — а тест упадёт здесь.

Проверяем ИМЕННО git-трекинг (`git ls-files`), потому что это ровно то, что попадает
в клон; плюс — что фабрика профилей и регистрация MinimalProfile действительно работают.
"""
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git_tracked():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8")
    if out.returncode != 0:
        pytest.skip("git недоступен / не репозиторий")
    return set(out.stdout.split())


def test_all_package_init_files_tracked():
    """Каждый пакетный __init__.py под контролем версий — иначе клон = сломанный пакет."""
    tracked = _git_tracked()
    expected = [
        "crparser/__init__.py",
        "crparser/engine/__init__.py",
        "crparser/interface/__init__.py",
        "crparser/profiles/__init__.py",
    ]
    missing = [p for p in expected if p not in tracked]
    assert not missing, (
        "пакетные __init__.py вне git (клон не импортируется): %s" % missing)


def test_profile_factory_imports_and_registers_minimal():
    """`from crparser.profiles import create_profile` работает и знает про minimal —
    именно это ломалось, когда __init__.py не попадал в клон."""
    from crparser.profiles import create_profile, available_profiles
    assert "cr" in available_profiles()
    assert "minimal" in available_profiles()
    prof = create_profile("minimal")
    assert prof.document_type == "generic_document"
