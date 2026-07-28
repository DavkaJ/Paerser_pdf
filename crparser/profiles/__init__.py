#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Профили документов + фабрика выбора профиля по ключу.

Сейчас доступен один профиль — КР (`cr`). Чтобы добавить новый тип документа,
достаточно создать наследника DocumentProfile и зарегистрировать его в _PROFILES.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Type

from crparser.profiles.base import DocumentProfile
from crparser.profiles.clinical import ClinicalRecommendationProfile
from crparser.profiles.minimal import MinimalProfile
from crparser.profiles.registry import ClinicalRegistry

# Реестр доступных профилей: ключ CLI -> класс профиля.
_PROFILES: Dict[str, Type[DocumentProfile]] = {
    ClinicalRecommendationProfile.key: ClinicalRecommendationProfile,
    MinimalProfile.key: MinimalProfile,
}


def available_profiles() -> List[str]:
    """Список ключей доступных профилей (для CLI choices)."""
    return sorted(_PROFILES.keys())


def create_profile(key: str, registry_path: Optional[str] = None) -> DocumentProfile:
    """
    Создать профиль по ключу. Профиль КР дополнительно получает реестр (если
    путь задан); прочие профили создаются без аргументов.
    """
    key = (key or "").lower()
    if key not in _PROFILES:
        raise ValueError(
            f"неизвестный профиль '{key}'. Доступны: {', '.join(available_profiles())}")
    cls = _PROFILES[key]
    if cls is ClinicalRecommendationProfile:
        registry = ClinicalRegistry.load(registry_path) if registry_path else None
        return cls(registry=registry)
    return cls()


__all__ = [
    "DocumentProfile",
    "ClinicalRecommendationProfile",
    "MinimalProfile",
    "ClinicalRegistry",
    "available_profiles",
    "create_profile",
]
