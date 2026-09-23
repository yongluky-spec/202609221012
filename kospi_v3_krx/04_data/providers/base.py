# -*- coding: utf-8 -*-
"""
@module: providers.base
@type: data
@version: v1

시세 공급자(Provider) 공통 규격.

새 증권사(한국투자증권, 키움증권 등)를 붙일 때는 MarketDataProvider를 상속해서
fetch_tick() 하나만 구현하면 된다. 엔진(02_engine)은 provider 내부를 모른다.

fetch_tick()이 반환해야 하는 dict 스키마 (mock_market_data.generate_mock_tick()과 동일):
    spot, futures_basis, basis_zscore, leader_delta, market_delta_1h,
    acceleration_1h, recent_2d_avg_basis, bid_volume,
    foreign_net_futures, corp_straddle_iv_diff, timestamp

[v3.1 추가, 선택 필드 -- 없어도 엔진이 죽지 않도록 record 쪽에서 .get()으로 방어함]
    leaders: list[dict] -- 대장주 TOP_N 개별 종목 정보.
        각 원소 = {ticker, name, price, prev_close, delta_pct, market_cap, weight}
        leader_delta(기존 필드)는 이 리스트의 시총 가중평균과 같아야 한다.
    leader_event: str | None -- 이번 틱에서 대장주 구성이 바뀌었으면
        "OO → OO" 형태의 문자열, 안 바뀌었으면 None.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

TICK_KEYS = (
    "spot", "futures_basis", "basis_zscore", "leader_delta", "market_delta_1h",
    "acceleration_1h", "recent_2d_avg_basis", "bid_volume",
    "foreign_net_futures", "corp_straddle_iv_diff", "timestamp",
)

# 선택 필드 (있으면 사용, 없어도 무방 -- 하위 호환 위해 TICK_KEYS엔 안 넣음)
OPTIONAL_TICK_KEYS = ("leaders", "leader_event")


class ProviderError(Exception):
    """일반적인 데이터 취득 실패 (휴장일, 빈 응답, 네트워크 등)."""


class ProviderAuthError(ProviderError):
    """인증 실패 (로그인/키/토큰). 반복 재시도하면 계정 잠금 위험이 있다."""


class ProviderNotImplemented(ProviderError):
    """아직 구현되지 않은 공급자."""


class MarketDataProvider(ABC):
    name: str = "base"      # 환경변수 DATA_PROVIDER 에 쓰는 이름
    label: str = "Base"     # 화면 표시용 이름

    @abstractmethod
    def fetch_tick(self) -> dict:
        """TICK_KEYS 스키마의 dict 를 반환. 실패 시 ProviderError 계열 예외."""

    def source_note(self) -> str:
        return f"실전 데이터 연동 중 ({self.label})"

    def env_status(self) -> dict:
        """필요한 환경변수의 '설정 여부'만 반환 (값은 절대 반환하지 않는다)."""
        return {}
