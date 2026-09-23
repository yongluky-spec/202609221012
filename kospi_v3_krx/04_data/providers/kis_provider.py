# -*- coding: utf-8 -*-
"""
@module: providers.kis_provider
@type: data
@version: v0 (스켈레톤, 미구현)

한국투자증권 Open API 공급자 -- 추후 구현 자리.

구현 시 체크리스트:
    [ ] 한국투자증권 홈페이지에서 Open API 서비스 신청 -> App Key / App Secret 발급
    [ ] 환경변수: KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO(앞 8자리), KIS_ACCOUNT_PROD(뒤 2자리),
                  KIS_ENV = real | paper   (실전/모의는 도메인과 키가 서로 다름)
    [ ] 접근토큰은 발급 횟수 제한이 있으므로 파일/메모리에 캐시하고 만료 전까지 재사용
        (토큰 발급 실패 시 KrxProvider 처럼 쿨다운을 두고 ProviderAuthError 로 올릴 것)
    [ ] 실시간 코스피/코스피200 선물 현재가 -> futures_basis, bid_volume 채우기
    [ ] 엔드포인트/TR ID 는 한국투자증권 공식 개발자 문서 기준으로 확인해서 작성

fetch_tick() 반환 스키마는 providers/base.py 의 TICK_KEYS 참고.
"""
from __future__ import annotations

import os

from .base import MarketDataProvider, ProviderNotImplemented

_ENV_KEYS = ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO")


class KisProvider(MarketDataProvider):
    name = "kis"
    label = "한국투자증권 Open API"

    def env_status(self) -> dict:
        return {k: bool(os.environ.get(k)) for k in _ENV_KEYS}

    def fetch_tick(self) -> dict:
        raise ProviderNotImplemented("한국투자증권 공급자는 아직 구현되지 않았습니다 (kis_provider.py 체크리스트 참고).")
