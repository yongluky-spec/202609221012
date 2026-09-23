# -*- coding: utf-8 -*-
"""
@module: providers.kiwoom_provider
@type: data
@version: v0 (스켈레톤, 미구현)

키움증권 공급자 -- 추후 구현 자리.

구현 전에 방식을 먼저 정해야 한다 (둘은 환경 제약이 완전히 다르다):
    A) OpenAPI+ (구형, OCX): 윈도우 전용 + 32비트 파이썬 + 로그인 창(GUI) 필요.
       streamlit 서버/리눅스/클라우드에서는 사용 불가. 별도 로컬 프로세스에서 시세를 받아
       파일/소켓으로 넘겨주는 구조가 필요.
    B) 키움 REST API (신규): 사용 가능 여부와 인증 방식은 키움 공식 문서에서 확인 후 결정.

공통 체크리스트:
    [ ] 키움 홈페이지에서 API 사용 신청
    [ ] 환경변수: KIWOOM_APP_KEY, KIWOOM_APP_SECRET, KIWOOM_ACCOUNT_NO (방식 B 기준, 문서 확인 후 확정)
    [ ] 실전/모의 구분, 인증 실패 시 쿨다운 (KrxProvider 참고)
"""
from __future__ import annotations

import os

from .base import MarketDataProvider, ProviderNotImplemented

_ENV_KEYS = ("KIWOOM_APP_KEY", "KIWOOM_APP_SECRET", "KIWOOM_ACCOUNT_NO")


class KiwoomProvider(MarketDataProvider):
    name = "kiwoom"
    label = "키움증권 API"

    def env_status(self) -> dict:
        return {k: bool(os.environ.get(k)) for k in _ENV_KEYS}

    def fetch_tick(self) -> dict:
        raise ProviderNotImplemented("키움증권 공급자는 아직 구현되지 않았습니다 (kiwoom_provider.py 체크리스트 참고).")
