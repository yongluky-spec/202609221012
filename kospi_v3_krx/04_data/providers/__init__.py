# -*- coding: utf-8 -*-
"""
@module: providers
@type: data
@version: v1

환경변수 DATA_PROVIDER 로 공급자를 고른다 (기본 "krx").
쉼표로 우선순위를 줄 수 있다:  DATA_PROVIDER=kis,krx  (앞에서부터 시도, 실패하면 다음)
"""
from __future__ import annotations

import os
from typing import Dict, List

from .base import (  # noqa: F401
    MarketDataProvider,
    ProviderAuthError,
    ProviderError,
    ProviderNotImplemented,
    TICK_KEYS,
)
from .kis_provider import KisProvider
from .kiwoom_provider import KiwoomProvider
from .krx_provider import KrxProvider

_REGISTRY = {
    "krx": KrxProvider,
    "kis": KisProvider,
    "kiwoom": KiwoomProvider,
}
_INSTANCES: Dict[str, MarketDataProvider] = {}   # 상태(쿨다운/이력) 유지를 위한 싱글턴


def configured_names() -> List[str]:
    raw = os.environ.get("DATA_PROVIDER", "krx")
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    return names or ["krx"]


def get_provider(name: str) -> MarketDataProvider:
    if name not in _REGISTRY:
        raise ProviderError(f"알 수 없는 DATA_PROVIDER '{name}' (가능: {', '.join(_REGISTRY)})")
    if name not in _INSTANCES:
        _INSTANCES[name] = _REGISTRY[name]()
    return _INSTANCES[name]


def get_providers() -> List[MarketDataProvider]:
    return [get_provider(n) for n in configured_names()]
