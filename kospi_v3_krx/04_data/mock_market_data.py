# -*- coding: utf-8 -*-
"""
@module: mock_market_data
@type: data
@version: v1
@depends: (없음, numpy만 사용)
@status: stable

가짜 틱 데이터 생성 및 검증용 모듈.
02_engine/kospi_engine_v3_realtime.py의 fetch_market_data()가 실전 API
연동에 실패했을 때 폴백으로 사용한다. 실전 연동 코드와 완전히 분리되어
있으므로, 나중에 실전 데이터로 전환한 뒤에도 테스트 시 이 모듈을
그대로 재사용할 수 있다 (원안 문서 3장 "core 로직 공유" 원칙 적용).
"""

import time
from datetime import datetime

import numpy as np


def generate_mock_tick():
    """
    실전 API 연결 전, 알고리즘 검증용 모의 틱 데이터.

    기존 kospi_model.py(492줄본)의 _mock_market_data()에 시나리오 엔진용
    필드 2개(foreign_net_futures, corp_straddle_iv_diff)를 추가 병합했다
    (update3.patch에서 도입된 필드).

    [v3.1 추가] leaders / leader_event -- 대장주 개별 종목 UI가 모의데이터
    폴백 상태에서도 빈 화면이 아니라 그럴듯한 값을 보여주도록 가짜 2종목을 생성.
    """
    rng = np.random.default_rng(int(time.time()) % 10000)
    mock_leaders = [
        {"ticker": "005930", "name": "삼성전자(모의)", "price": round(75000 + rng.normal(0, 500), 0),
         "prev_close": 75000.0, "delta_pct": round(rng.normal(0.0, 0.006), 5),
         "market_cap": 4.5e14, "weight": 0.62, "volume": 12000000.0},
        {"ticker": "000660", "name": "SK하이닉스(모의)", "price": round(188000 + rng.normal(0, 1200), 0),
         "prev_close": 188000.0, "delta_pct": round(rng.normal(0.0, 0.008), 5),
         "market_cap": 2.7e14, "weight": 0.38, "volume": 3500000.0},
    ]
    return {
        "spot": 2650.0 + rng.normal(0, 3),
        "futures_basis": round(rng.normal(0.8, 0.6), 3),
        "basis_zscore": round(rng.normal(0, 1.3), 2),
        "leader_delta": round(rng.normal(0.0, 0.006), 5),
        "market_delta_1h": round(rng.normal(0.0, 0.01), 5),
        "acceleration_1h": round(rng.normal(0.0, 0.01), 5),
        "recent_2d_avg_basis": round(rng.normal(0.5, 0.2), 3),
        "bid_volume": round(10000 + rng.normal(0, 400), 1),
        "foreign_net_futures": round(rng.normal(0.0, 0.5), 3),      # +상방/-하방 성향
        "corp_straddle_iv_diff": round(rng.normal(0.0, 0.01), 4),   # 콜/풋 IV 스프레드 변화
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "leaders": mock_leaders,
        "leader_event": None,
    }
