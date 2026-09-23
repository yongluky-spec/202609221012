# -*- coding: utf-8 -*-
"""
@module: acceleration_scanner
@type: engine
@version: v1
@created: 2026-09-21
@depends: pykrx
@status: dev

6번째 게이트: 대형주 모멘텀 가속도(Acceleration) 실시간 스캐너
------------------------------------------------------------
코스피 시가총액 상위 30개 종목을 폴링해서, "델타(속도)의 변화량인
가속도가 종목 자체의 평균 대비 급증/급감하는 종목"을 실시간으로 포착한다.

기존 02_engine/burst_early_warning.py(5번째 게이트)와의 관계:
    - burst_early_warning.py: 대장주/지수 "단일 종목"을 시간대 버킷별로
      심층 감시 (CR 응축 + 거래량 급증 2조건, FSM 상태전이)
    - 이 파일(acceleration_scanner_v1.py): "다종목 횡단 스캔" -- 유니버스
      30개를 동시에 보면서 그중 튀는 종목을 찾아냄
    서로 대체 관계가 아니라 병렬로 붙는 별개의 게이트다.

설계 합의 사항 (대화에서 확정):
    1. 유니버스: 코스피 시가총액 상위 30개
    2. 데이터 조회: 종목별 개별 호출이 아니라 get_market_cap_by_ticker()
       일괄 조회 1번으로 30개 전부의 종가/등락률/시총을 동시에 얻는다
       (개별 루프 시 Rate Limit/지연 위험이 커서 회피).
    3. 폴링 주기: 기본 10초. 단, pykrx는 비공식 스크래핑 기반이라 너무 잦은
       호출은 IP 차단/계정 잠금 위험이 있으므로, 연속 실패 시
       10 -> 30 -> 60초로 자동 후퇴(backoff)하고 성공하면 10초로 복귀한다.
    4. 메모리 구조: 종목별로 짧은 창(SHORT_WINDOW=5, 신호 계산용)과
       긴 창(LONG_WINDOW=25, 평균/표준편차 기준선용)을 이원화한다.
       (burst_early_warning.py의 "짧은 창/긴 창 이원 히스토리" 패턴 재사용)
    5. 유의미 신호 판정: 가속도가 그 종목 자체의 최근 가속도 평균 대비
       표준편차 Z_SCORE_THRESHOLD(기본 2.0)배 이상 벗어날 때만 인정.
       고정 % 임계값은 쓰지 않는다 (장세마다 의미가 달라 오탐 유발).
    6. 알림: 실시간 리스트(전 종목) + 임계 돌파 종목은 별도 경고 로그.
    7. 델타 맵 / 가속도 맵 두 트리맵에 쓸 데이터를 여기서 함께 가공해서
       내보낸다 (동일 팔레트를 쓰되 가속도 맵은 값 스케일이 훨씬 작으므로
       축 범위를 델타 맵과 분리 -- 02_engine/kospi_engine_v3_realtime.py의
       렌더링 쪽에서 반영).

주의 (정직하게 밝히는 한계):
    - pykrx는 실시간 틱 스트림이 아니라 폴링 스냅샷이라, "가속도"는 폴링
      간격(10~60초)을 하나의 단위 시간으로 삼은 근사치다. 진짜 틱 단위
      가속도가 필요하면 증권사 API(한투/키움) 연동 후 교체해야 한다.
    - 이 파일은 st.* 호출이 전혀 없는 core 로직이다 (kospi_engine_v3_realtime.py
      의 core/wrapper 분리 원칙을 그대로 승계). Streamlit 세션 상태에는
      이 클래스의 인스턴스 자체만 보관하면 된다.
"""
from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque, Dict, List, Optional

# =========================================================
# 0. 설정값 (여기 숫자만 바꿔서 튜닝)
# =========================================================
SCANNER_MARKET = "KOSPI"           # 합의: 코스닥 제외
SCANNER_UNIVERSE_TOP_N = 30        # 시총 상위 30개
SCANNER_UNIVERSE_REFRESH_SEC = 300  # 유니버스 구성 자체(어떤 30종목인지)는 5분마다만 재확인
                                     # (가격/델타는 매 폴링마다 갱신 -- 구성원만 5분 캐시)

SCANNER_INTERVAL_BASE_SEC = 10.0    # 정상 시 폴링 주기
SCANNER_BACKOFF_STEPS = (10.0, 30.0, 60.0)  # 연속 실패 시 이 순서로 후퇴
SCANNER_BACKOFF_RECOVER_ON_SUCCESS = True   # 성공하면 즉시 기본 주기(10초)로 복귀

SHORT_WINDOW = 5     # 신호(최근 델타/가속도) 계산용 짧은 창
LONG_WINDOW = 25      # 평균/표준편차 기준선용 긴 창
Z_SCORE_THRESHOLD = 2.0   # 가속도 급변 판정 기준 (표준편차 배수)
MIN_SAMPLES_FOR_ZSCORE = 6   # 이 개수 이상 쌓이기 전엔 Z-score 판정 보류 (초기 노이즈 방지)

ALERT_LOG_MAXLEN = 50   # 경고 로그 최대 보관 개수

# 델타 맵 색상 축 (기존 합의: 고정 -3% ~ +3%)
DELTA_MAP_COLOR_RANGE = (-3.0, 3.0)
# 가속도 맵 색상 축 (델타보다 한 자릿수 작음 -- 같은 팔레트, 별도 스케일)
ACCEL_MAP_COLOR_RANGE = (-0.5, 0.5)


# =========================================================
# 1. 종목별 이원 버퍼
# =========================================================
@dataclass
class TickerBuffer:
    """종목 1개당 델타/가속도 이력을 관리."""
    delta_short: Deque[float] = field(default_factory=lambda: deque(maxlen=SHORT_WINDOW))
    accel_long: Deque[float] = field(default_factory=lambda: deque(maxlen=LONG_WINDOW))

    def push_delta(self, delta_pct: float) -> Optional[float]:
        """새 델타를 넣고, 직전 델타와의 차이(가속도)를 계산해 반환.
        직전 델타가 없으면(첫 데이터) None."""
        prev = self.delta_short[-1] if self.delta_short else None
        self.delta_short.append(delta_pct)
        if prev is None:
            return None
        accel = round(delta_pct - prev, 5)
        self.accel_long.append(accel)
        return accel

    def zscore_of(self, accel: float) -> Optional[float]:
        """이번 가속도가 '이번 값을 제외한' 과거 분포 대비 몇 표준편차인지.
        표본이 MIN_SAMPLES_FOR_ZSCORE 미만이면 판정 보류(None)."""
        history = list(self.accel_long)[:-1]  # 방금 넣은 값 자신은 기준선 계산에서 제외
        if len(history) < MIN_SAMPLES_FOR_ZSCORE:
            return None
        mean = statistics.fmean(history)
        std = statistics.pstdev(history)
        if std <= 1e-9:
            return 0.0
        return round((accel - mean) / std, 3)


# =========================================================
# 2. 폴링 백오프 상태 머신
# =========================================================
@dataclass
class BackoffState:
    """
    정상 시 SCANNER_INTERVAL_BASE_SEC(10초), 연속 실패 시
    SCANNER_BACKOFF_STEPS를 따라 후퇴, 성공하면 즉시 기본 주기로 복귀.
    """
    consecutive_failures: int = 0
    last_poll_ts: float = 0.0
    last_status: str = "INIT"   # "OK" | "FAIL" | "INIT"
    last_error: str = ""

    def current_interval(self) -> float:
        if self.consecutive_failures <= 0:
            return SCANNER_INTERVAL_BASE_SEC
        idx = min(self.consecutive_failures - 1, len(SCANNER_BACKOFF_STEPS) - 1)
        return SCANNER_BACKOFF_STEPS[idx]

    def should_poll(self, now_ts: float) -> bool:
        return (now_ts - self.last_poll_ts) >= self.current_interval()

    def record_success(self, now_ts: float) -> None:
        self.last_poll_ts = now_ts
        self.last_status = "OK"
        self.last_error = ""
        if SCANNER_BACKOFF_RECOVER_ON_SUCCESS:
            self.consecutive_failures = 0

    def record_failure(self, now_ts: float, error: Exception) -> None:
        self.last_poll_ts = now_ts
        self.last_status = "FAIL"
        self.last_error = f"{type(error).__name__}: {error}"
        self.consecutive_failures += 1


# =========================================================
# 3. 스캐너 본체
# =========================================================
class AccelerationScanner:
    """
    st.* 호출이 전혀 없는 core 클래스. Streamlit 쪽(kospi_engine_v3_realtime.py)은
    이 인스턴스를 session_state에 하나만 보관하고, 매 rerun마다
    maybe_poll(now)를 호출하면 된다 (내부적으로 backoff 주기를 스스로 판단).
    """

    def __init__(self) -> None:
        self.backoff = BackoffState()
        self.buffers: Dict[str, TickerBuffer] = {}
        self.names: Dict[str, str] = {}
        self._universe: List[str] = []
        self._universe_last_check: float = 0.0
        self.alert_log: Deque[dict] = deque(maxlen=ALERT_LOG_MAXLEN)
        self.last_snapshot: Optional[dict] = None  # 가장 최근 성공한 스캔 결과 (UI가 재사용)

    # ---- 외부 진입점 ----
    def maybe_poll(self, now: Optional[datetime] = None) -> Optional[dict]:
        """
        backoff 주기가 지났을 때만 실제로 폴링한다. 아직 주기가 안 지났으면
        None을 반환하고, 호출부(UI)는 self.last_snapshot을 계속 재사용하면 된다.
        """
        now_dt = now or datetime.now()
        now_ts = time.time()
        if not self.backoff.should_poll(now_ts):
            return None

        try:
            snapshot = self._poll_once(now_dt)
            self.backoff.record_success(now_ts)
            self.last_snapshot = snapshot
            return snapshot
        except Exception as e:  # noqa: BLE001
            self.backoff.record_failure(now_ts, e)
            return None

    # ---- 내부 구현 ----
    def _poll_once(self, now: datetime) -> dict:
        from pykrx import stock  # 지역 임포트: krx_provider.py와 동일 패턴 (미설치 환경 방어)

        self._refresh_universe_if_needed(stock, now)
        if not self._universe:
            raise RuntimeError("스캐너 유니버스가 비어있음 (휴장일 또는 순위 조회 실패)")

        today = now.strftime("%Y%m%d")
        # get_market_ohlcv_by_ticker 는 "등락률" 컬럼을 직접 제공한다 (전일 대비 %).
        # get_market_cap_by_ticker 는 등락률이 없는 대신 시가총액을 제공하므로 둘을 합친다.
        # (개별 종목 반복 호출 없이 일괄 조회 2번으로 유니버스 전체를 처리 -- Rate Limit 방지)
        ohlcv_df = stock.get_market_ohlcv_by_ticker(today, market=SCANNER_MARKET)
        cap_df = stock.get_market_cap_by_ticker(today, market=SCANNER_MARKET)
        if ohlcv_df is None or ohlcv_df.empty or cap_df is None or cap_df.empty:
            raise RuntimeError("일괄 시세/시총 조회 응답이 비어있음")

        rows: List[dict] = []
        for ticker in self._universe:
            if ticker not in ohlcv_df.index or ticker not in cap_df.index:
                continue
            price = float(ohlcv_df.loc[ticker, "종가"])
            # delta_pct: 전일 대비 등락률(%) -- 대장주 카드/델타 맵과 동일한 정의로 통일
            delta_pct = round(float(ohlcv_df.loc[ticker, "등락률"]), 4)
            market_cap = float(cap_df.loc[ticker, "시가총액"])

            buf = self.buffers.setdefault(ticker, TickerBuffer())
            # accel: 이번 폴링과 직전 폴링 사이 "등락률 자체가 얼마나 움직였는지"
            # -> 하루치 등락률이 폴링 주기(10~60초)마다 어느 방향/속도로 커지고 있는지를 포착
            accel = buf.push_delta(delta_pct)
            z = buf.zscore_of(accel) if accel is not None else None
            is_alert = z is not None and abs(z) >= Z_SCORE_THRESHOLD
            direction = None
            if is_alert:
                direction = "UP_SURGE" if accel > 0 else "DOWN_PLUNGE"
                self._log_alert(now, ticker, accel, z, direction)

            rows.append({
                "ticker": ticker,
                "name": self.names.get(ticker, ticker),
                "price": price,
                "market_cap": market_cap,
                "delta_pct": delta_pct,
                "acceleration": accel if accel is not None else 0.0,
                "accel_zscore": z,
                "is_alert": is_alert,
                "alert_direction": direction,
            })

        rows.sort(key=lambda r: r["market_cap"], reverse=True)
        return {
            "timestamp": now.isoformat(timespec="seconds"),
            "rows": rows,
            "delta_map": self._build_treemap_data(rows, value_key="delta_pct",
                                                    color_range=DELTA_MAP_COLOR_RANGE),
            "accel_map": self._build_treemap_data(rows, value_key="acceleration",
                                                    color_range=ACCEL_MAP_COLOR_RANGE,
                                                    highlight_alerts=True),
            "alert_log": list(self.alert_log),
            "backoff_status": self.backoff.last_status,
            "backoff_interval": self.backoff.current_interval(),
            "backoff_failures": self.backoff.consecutive_failures,
        }

    def _refresh_universe_if_needed(self, stock, now: datetime) -> None:
        now_ts = time.time()
        if self._universe and (now_ts - self._universe_last_check) < SCANNER_UNIVERSE_REFRESH_SEC:
            return
        today = now.strftime("%Y%m%d")
        cap_df = stock.get_market_cap_by_ticker(today, market=SCANNER_MARKET)
        if cap_df is None or cap_df.empty:
            if self._universe:
                return  # 직전 유니버스라도 유지 (완전 실패 아님)
            raise RuntimeError("스캐너 유니버스 산정 실패 (초기 조회 실패)")
        ranked = cap_df.sort_values(by="시가총액", ascending=False)
        self._universe = list(ranked.index[:SCANNER_UNIVERSE_TOP_N])
        self._universe_last_check = now_ts

        for ticker in self._universe:
            if ticker not in self.names:
                try:
                    self.names[ticker] = stock.get_market_ticker_name(ticker)
                except Exception:  # noqa: BLE001
                    self.names[ticker] = ticker

    def _log_alert(self, now: datetime, ticker: str, accel: float, z: float, direction: str) -> None:
        self.alert_log.appendleft({
            "timestamp": now.isoformat(timespec="seconds"),
            "ticker": ticker,
            "name": self.names.get(ticker, ticker),
            "acceleration": accel,
            "zscore": z,
            "direction": direction,
        })

    @staticmethod
    def _build_treemap_data(rows: List[dict], value_key: str, color_range: tuple,
                             highlight_alerts: bool = False) -> dict:
        """Plotly treemap(px.treemap 또는 go.Treemap)에 바로 넣을 수 있는 형태로 가공.
        UI 쪽(kospi_engine_v3_realtime.py)에서 이 dict를 그대로 소비한다."""
        return {
            "labels": [r["name"] for r in rows],
            "tickers": [r["ticker"] for r in rows],
            "sizes": [max(r["market_cap"], 1) for r in rows],
            "values": [r[value_key] for r in rows],
            "color_range": color_range,
            "line_widths": [3 if (highlight_alerts and r["is_alert"]) else 0.5 for r in rows],
            "line_colors": [
                ("#791F1F" if r.get("alert_direction") == "DOWN_PLUNGE" else "#1F4E79")
                if (highlight_alerts and r["is_alert"]) else "#CCCCCC"
                for r in rows
            ],
        }
