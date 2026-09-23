# -*- coding: utf-8 -*-
"""
5번째 게이트: Burst Early Warning (급변동 전조 조기경보)
------------------------------------------------------
[PATCH v2 -- FinalBurstGate, 2026-09-20]
    사후 검증 결과, Jerk(저크)/HMM/딥러닝/엔트로피/시그모이드 합성 스코어는
    전부 폐기했다 (노이즈 증폭 + 1~4분 사후 지연 + 표본 부족 오버핏).
    생존한 유일한 실전 규칙은 아래 2조건뿐이다:
        1) 버킷별 정규화 CR 응축 (cr_z < -1.0, 고정 0.7 아님)
        2) 1분봉 거래량 ≥ 1.8 × 20일 평균
    두 조건이 동시에 성립하면 BURST_WARNING. 이하 설계 요약은 이 패치
    이전 버전 기준이며, 1)의 "고정 0.7"이 아니라는 점과 4)의 Jerk 항목은
    이 패치로 무효화됐다는 점에 유의할 것.

설계 확정 사항 요약 (대화에서 합의된 내용, 패치 이전 버전 기준):
    1. 시간대 버킷(B1~B5)별로 CR/저크(Jerk)/엔트로피를 "그 시간대 자체의
       과거 분포"에 대한 IQR 정규화로 평가한다 (고정 임계값 사용 금지).
    2. 0단계 "데이터 품질 게이트"를 신호 계산보다 먼저 통과시킨다:
       - 신선도(Staleness): 90초 이상 지연 시 계산 중단 (DATA_STALE)
       - 결측(Gap): 보간 금지, skip 처리. 최근 5분 내 결측 2개 이상 시 LOW_CONFIDENCE
       - 소스 이원화: 두 소스 간 괴리가 크면 양쪽 다 불신 (기존 디커플링 킬스위치 재사용 패턴)
    3. 링버퍼는 단일 구조가 아니라 이중 구조:
       - DailyMinuteGrid: 결측을 "몇 분이 비었는지" 정밀 추적 (당일 세션 고정 크기)
       - FeatureWindow: 실제 연산용 슬라이딩 윈도우. 불연속(gap) 이후 재개 시
         저크(3차 미분급) 계산을 일정 구간 보류하여 인위적 튀는 값 방지
    4. 가중치/임계값 튜닝은 버킷별 Shrinkage로 안정화:
       - Shrinkage 타겟은 Global Prior가 아니라 "해당 버킷 전용 LongTerm(100일)
         베이스라인" (버킷 간 이질성 오염 방지가 핵심 이유)
       - N_target은 버킷별 이벤트 발생 빈도에 맞춰 차등 적용
         (고빈도 버킷 B1/B5: 30, 저빈도 버킷 B3: 10~15)
       - Train 20일 / Val 5일 워크포워드, 변동성 급증 시 Train 10일로 동적 단축,
         버킷 표본 부족 시 Val 10일로 확대
       - 최근 4주 평균 F_0.5가 직전 대비 -15% 이상 하락하면 Kill-switch로
         직전 안정 파라미터 롤백

주의: 이 파일은 "뼈대(skeleton)"이며, 실전 배포 전 아래가 필요합니다.
    - 실제 1분봉/틱 스트림 어댑터 연결 (현재는 인터페이스만 정의)
    - 버킷별 20일/100일 히스토리 영속 저장소 (DB 또는 파일) 연결
    - Optuna 기반 파라미터 탐색 스크립트 (별도 오프라인 배치 잡으로 분리 권장)
    - 동시성 제어: 실제 배포 시 threading.Lock 또는 asyncio.Lock 적용 필요
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple


# =====================================================================
# 1. 시간대 버킷 정의
# =====================================================================

class Bucket(Enum):
    B1_OPEN = "B1_open"          # 09:00-09:30 시초가 직후, 원래 시끄러움 -> 둔감하게
    B2_MORNING = "B2_morning"    # 09:30-11:00 오전 안정화
    B3_LUNCH = "B3_lunch"        # 11:00-13:00 점심 소강, 원래 조용함 -> 민감하게
    B4_AFTERNOON = "B4_afternoon"  # 13:00-14:50 오후장
    B5_CLOSING = "B5_closing"    # 14:50-15:30 동시호가 임박, 원래 출렁임 -> 둔감하게


_BUCKET_RANGES: List[Tuple[dtime, dtime, Bucket]] = [
    (dtime(9, 0), dtime(9, 30), Bucket.B1_OPEN),
    (dtime(9, 30), dtime(11, 0), Bucket.B2_MORNING),
    (dtime(11, 0), dtime(13, 0), Bucket.B3_LUNCH),
    (dtime(13, 0), dtime(14, 50), Bucket.B4_AFTERNOON),
    (dtime(14, 50), dtime(15, 30), Bucket.B5_CLOSING),
]

SESSION_START = dtime(9, 0)
SESSION_END = dtime(15, 30)
SESSION_MINUTES = 390  # 09:00~15:30


def get_time_bucket(dt: datetime) -> Optional[Bucket]:
    """현재 시각이 어느 버킷에 속하는지 반환. 장외 시간이면 None."""
    t = dt.time()
    for start, end, bucket in _BUCKET_RANGES:
        if start <= t < end:
            return bucket
    return None


def minute_offset_of_session(dt: datetime) -> Optional[int]:
    """세션 시작(09:00) 대비 몇 분째인지. DailyMinuteGrid 인덱스로 사용."""
    t = dt.time()
    if t < SESSION_START or t >= SESSION_END:
        return None
    delta = datetime.combine(dt.date(), t) - datetime.combine(dt.date(), SESSION_START)
    return int(delta.total_seconds() // 60)


# =====================================================================
# 2. [PATCH v2 -- FinalBurstGate] 버킷별 가중치(w1/w2/w3) 및 시그모이드
# 합성 스코어는 전면 폐기했다. Jerk/엔트로피 항이 사라지면서 가중치 블렌딩
# 자체가 무의미해졌고, 실전 채택 규칙은 아래의 불린 2조건(CR응축+거래량)뿐이다.
# =====================================================================

# 버킷별 이벤트 발생 빈도 차등 (Shrinkage N_target)
# 고빈도 버킷(시초가/마감)은 30, 저빈도(점심)는 10~15로 낮춰
# 국소 적응이 영원히 막히는 문제를 방지한다.
BUCKET_N_TARGET: Dict[Bucket, int] = {
    Bucket.B1_OPEN: 30,
    Bucket.B2_MORNING: 20,
    Bucket.B3_LUNCH: 12,
    Bucket.B4_AFTERNOON: 20,
    Bucket.B5_CLOSING: 30,
}


# =====================================================================
# 3. 원시 데이터 타입
# =====================================================================

@dataclass
class MinuteBar:
    timestamp: datetime
    close: float
    volume: float
    atr14: Optional[float] = None
    source: str = "primary"  # "primary" | "secondary" (이원화 소스 구분용)


# =====================================================================
# 4. DailyMinuteGrid — 결측을 정밀 추적하는 당일 고정 크기 그리드
# =====================================================================

class DailyMinuteGrid:
    """
    당일 세션(390분) 전용 고정 크기 그리드.
    링버퍼처럼 밀리지 않고, 인덱스(분 오프셋)가 실제 시각에 고정 대응하므로
    "몇 시 몇 분이 결측인지"를 정확히 알 수 있다.
    """

    def __init__(self, session_minutes: int = SESSION_MINUTES):
        self.session_minutes = session_minutes
        self._slots: List[Optional[MinuteBar]] = [None] * session_minutes

    def insert(self, bar: MinuteBar) -> bool:
        offset = minute_offset_of_session(bar.timestamp)
        if offset is None or not (0 <= offset < self.session_minutes):
            return False
        self._slots[offset] = bar
        return True

    def is_missing(self, minute_offset: int) -> bool:
        if not (0 <= minute_offset < self.session_minutes):
            return True
        return self._slots[minute_offset] is None

    def recent_gap_count(self, up_to_offset: int, k: int = 5) -> int:
        """최근 k분 중 결측 개수. LOW_CONFIDENCE 판정에 사용."""
        start = max(0, up_to_offset - k + 1)
        return sum(1 for i in range(start, up_to_offset + 1) if self.is_missing(i))

    def get(self, minute_offset: int) -> Optional[MinuteBar]:
        if not (0 <= minute_offset < self.session_minutes):
            return None
        return self._slots[minute_offset]

    def persist_end_of_day(self) -> List[MinuteBar]:
        """장 마감 후 유효 바만 추출하여 히스토리 저장소로 flush할 때 사용."""
        return [b for b in self._slots if b is not None]


# =====================================================================
# 5. FeatureWindow — 연산용 슬라이딩 윈도우 (불연속 인지)
# =====================================================================

class FeatureWindow:
    """
    실제 Jt/CR/엔트로피 계산에 쓰이는 슬라이딩 윈도우.
    결측은 애초에 편입시키지 않되(보간 금지 원칙), 편입 사이의 시간 간격이
    비정상적으로 크면(gap) 불연속으로 표시하여 저크 계산을 보류한다.
    이유: 결측 이후 재개 직후의 델타는 실제 간격이 1분이 아니라 여러 분에
    걸친 값이라, 그대로 3차 미분(Jerk)에 넣으면 인위적으로 큰 값이 나온다.
    """

    def __init__(self, maxlen: int = 60, max_gap_seconds: int = 90):
        self._bars: Deque[MinuteBar] = deque(maxlen=maxlen)
        self._discontinuity_at: Deque[bool] = deque(maxlen=maxlen)
        self.max_gap_seconds = max_gap_seconds

    def push(self, bar: MinuteBar) -> None:
        is_discontinuous = False
        if self._bars:
            gap = (bar.timestamp - self._bars[-1].timestamp).total_seconds()
            if gap > self.max_gap_seconds:
                is_discontinuous = True
        self._bars.append(bar)
        self._discontinuity_at.append(is_discontinuous)

    def _continuous_tail_length(self) -> int:
        """가장 최근 데이터부터 거슬러 올라가며 연속(불연속 아님)인 구간 길이."""
        count = 0
        for disc in reversed(self._discontinuity_at):
            if disc and count > 0:
                break
            count += 1
            if disc:
                # 이 지점 자체가 불연속의 시작이므로 여기서 끊는다
                break
        return count

    def compute_delta(self, k: int = 1) -> Optional[float]:
        if len(self._bars) < k + 1:
            return None
        if self._continuous_tail_length() < k + 1:
            return None
        p_now = self._bars[-1].close
        p_prev = self._bars[-1 - k].close
        if p_prev == 0:
            return None
        return (p_now - p_prev) / p_prev

    # [PATCH v2 -- FinalBurstGate] compute_jerk() / compute_entropy() 제거.
    # 두 지표 모두 사후 검증에서 노이즈 증폭 + 사후 지연 + 오버핏으로
    # 실전 채택에서 폐기되어, 더 이상 계산하지 않는다.

    def compute_cr(self, atr_ma20: Optional[float], volume_avg: Optional[float]) -> Optional[float]:
        """
        CR_t = [ATR14_t / MA(ATR14,20)] × [Volume_avg / Volume_actual]

        [BUGFIX] 거래량 항은 반드시 분모(Volume_actual)에 있어야 한다.
        거래량이 급증할 때 CR이 더 낮아져야(=더 응축된 것처럼 보여야) "CR 응축"과
        "거래량 폭발"이 같은 틱에서 동시에 성립할 수 있다. 거래량을 분자에 두면
        거래량이 뛰는 순간 CR도 같이 커져서 응축 판정이 풀려버려, 두 조건이
        구조적으로 동시에 성립할 수 없게 된다 (테스트로 실제 재현/확인함).
        """
        if not self._bars or not atr_ma20 or not volume_avg or atr_ma20 == 0 or volume_avg == 0:
            return None
        last = self._bars[-1]
        if last.atr14 is None or last.volume == 0:
            return None
        return (last.atr14 / atr_ma20) * (volume_avg / last.volume)


# =====================================================================
# 6. 버킷별 히스토리 저장소 + IQR 정규화 + Bucket-wise Shrinkage
# =====================================================================

@dataclass
class BucketHistory:
    """버킷별 ShortTerm(20일)/LongTerm(100일) 값 히스토리. 실전에서는 DB 대체 권장."""
    short_term: Deque[float] = field(default_factory=lambda: deque(maxlen=20 * 390))
    long_term: Deque[float] = field(default_factory=lambda: deque(maxlen=100 * 390))

    def add(self, value: float) -> None:
        self.short_term.append(value)
        self.long_term.append(value)

    def iqr_zscore(self, value: float, use_long_term: bool = False) -> Optional[float]:
        data = list(self.long_term if use_long_term else self.short_term)
        if len(data) < 10:
            return None
        sorted_data = sorted(data)
        q1 = sorted_data[int(len(sorted_data) * 0.25)]
        q3 = sorted_data[int(len(sorted_data) * 0.75)]
        iqr = q3 - q1
        if iqr == 0:
            return 0.0
        median = statistics.median(sorted_data)
        return (value - median) / iqr


class BucketwiseShrinkage:
    """
    확정된 설계: Global Prior가 아니라 "해당 버킷 전용 LongTerm(100일)"을
    Shrinkage 타겟으로 사용한다. 서로 이질적인 버킷(B1 vs B3)이 서로의
    파라미터에 영향을 주지 않도록 하기 위함.

        최종(bucket,t) = λ_b · ShortTerm(bucket,20일) + (1-λ_b) · LongTerm(bucket,100일)
        λ_b = min(1, N_events(bucket,20일) / N_target(bucket))
    """

    def __init__(self, n_target_by_bucket: Dict[Bucket, int] = BUCKET_N_TARGET):
        self.n_target_by_bucket = n_target_by_bucket

    def blend(
        self,
        bucket: Bucket,
        short_term_value: float,
        long_term_value: float,
        n_events_short_term: int,
    ) -> float:
        n_target = self.n_target_by_bucket.get(bucket, 20)
        lam = min(1.0, n_events_short_term / n_target) if n_target > 0 else 1.0
        return lam * short_term_value + (1 - lam) * long_term_value


# =====================================================================
# 7. 데이터 품질 게이트 (0단계)
# =====================================================================

class DataQualityStatus(Enum):
    FRESH = "FRESH"
    DATA_STALE = "DATA_STALE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"


def check_data_quality(
    latest_bar: Optional[MinuteBar],
    now: datetime,
    grid: DailyMinuteGrid,
    current_offset: int,
    staleness_threshold_sec: int = 90,
    gap_lookback: int = 5,
    gap_warn_count: int = 2,
    secondary_bar: Optional[MinuteBar] = None,
    source_divergence_threshold_pct: float = 0.3,
) -> DataQualityStatus:
    # 1) 신선도
    if latest_bar is None:
        return DataQualityStatus.DATA_STALE
    staleness = (now - latest_bar.timestamp).total_seconds()
    if staleness > staleness_threshold_sec:
        return DataQualityStatus.DATA_STALE

    # 2) 소스 이원화 -- 두 소스 간 괴리가 크면 양쪽 다 불신
    #    (기존 "대장주 디커플링 킬스위치"와 동일한 패턴 재사용)
    if secondary_bar is not None and latest_bar.close and secondary_bar.close:
        divergence_pct = abs(latest_bar.close - secondary_bar.close) / latest_bar.close * 100
        if divergence_pct > source_divergence_threshold_pct:
            return DataQualityStatus.DATA_STALE

    # 3) 결측(gap) -- 보간하지 않고, 최근 N분 내 결측이 임계 이상이면 LOW_CONFIDENCE
    gap_count = grid.recent_gap_count(current_offset, k=gap_lookback)
    if gap_count >= gap_warn_count:
        return DataQualityStatus.LOW_CONFIDENCE

    return DataQualityStatus.FRESH


# =====================================================================
# 8. FSM (상태 전이) + 쿨다운
# =====================================================================

class BurstState(Enum):
    QUIET = "QUIET"
    COILING = "COILING"
    BURST_WARNING = "BURST_WARNING"


@dataclass
class FSMConfig:
    coiling_confirm_minutes: int = 10   # CR 응축이 이만큼 연속돼야 COILING 진입
    burst_confirm_ticks: int = 2        # 폭발 조건이 이만큼 연속돼야 BURST_WARNING 발동 (핑퐁 방지)
    cooldown_minutes: int = 15          # 경보 발동 후 재발동 억제 기간
    cr_z_threshold: float = -1.0        # 버킷 정규화 CR이 이보다 낮으면 응축
    volume_ratio_threshold: float = 1.8
    # [PATCH v2 -- FinalBurstGate] jerk_z_threshold 제거.
    # 사후 검증 결과 Jerk는 노이즈 증폭 + 1~4분 사후 지연 + 표본 부족 오버핏으로
    # 실전 채택에서 폐기되었다. 생존 규칙은 CR 응축 + 거래량 급증 2조건뿐이다.


class BurstFSM:
    def __init__(self, config: FSMConfig = FSMConfig()):
        self.config = config
        self.state = BurstState.QUIET
        self._coiling_streak = 0
        self._burst_condition_streak = 0
        self._cooldown_until: Optional[datetime] = None

    def update(
        self,
        now: datetime,
        cr_z: Optional[float],
        volume_ratio: Optional[float],
    ) -> BurstState:
        if self._cooldown_until and now < self._cooldown_until:
            self.state = BurstState.QUIET
            return self.state

        is_compressed = cr_z is not None and cr_z < self.config.cr_z_threshold
        # [PATCH v2 -- FinalBurstGate] Jerk 조건 제거. CR 응축 + 거래량 급증
        # 2조건만으로 폭발 판정한다.
        is_burst_condition = (
            is_compressed
            and volume_ratio is not None
            and volume_ratio > self.config.volume_ratio_threshold
        )

        if self.state == BurstState.QUIET:
            self._coiling_streak = self._coiling_streak + 1 if is_compressed else 0
            if self._coiling_streak >= self.config.coiling_confirm_minutes:
                self.state = BurstState.COILING

        elif self.state == BurstState.COILING:
            if not is_compressed:
                self._coiling_streak = 0
                self.state = BurstState.QUIET
            self._burst_condition_streak = (
                self._burst_condition_streak + 1 if is_burst_condition else 0
            )
            if self._burst_condition_streak >= self.config.burst_confirm_ticks:
                self.state = BurstState.BURST_WARNING
                self._cooldown_until = now + timedelta(minutes=self.config.cooldown_minutes)
                self._coiling_streak = 0
                self._burst_condition_streak = 0

        elif self.state == BurstState.BURST_WARNING:
            # 쿨다운 로직은 update() 최상단에서 처리되므로 여기 도달 시 바로 리셋
            self.state = BurstState.QUIET

        return self.state


# =====================================================================
# 9. 합성 스코어 (BurstScore)
# =====================================================================

# [PATCH v2 -- FinalBurstGate] compute_sigmoid_score() 삭제.
# 합성 스코어(0~100) 방식은 Jerk/엔트로피 의존이라 폐기 대상과 함께 제거.
# GateResult.score는 이제 항상 None이며, 판정은 BurstState(QUIET/COILING/
# BURST_WARNING) 불린 상태만으로 이뤄진다.


# =====================================================================
# 10. 전체 파이프라인 (5번째 게이트 진입점)
# =====================================================================

@dataclass
class GateResult:
    status: str                      # "DATA_STALE" | "LOW_CONFIDENCE" | "OK"
    bucket: Optional[Bucket] = None
    state: Optional[BurstState] = None
    score: Optional[float] = None
    detail: Optional[Dict] = None


class BurstEarlyWarningGate:
    """
    5번째 게이트. 기존 4대 게이트(선물 충돌 / 대장주 디커플링 킬스위치 /
    1시간 델타·가속도 / 세션 요약)와 나란히 통합되는 것을 전제로 설계.
    """

    def __init__(self):
        self.grid = DailyMinuteGrid()
        self._grid_date: Optional[object] = None  # 날짜 경계 리셋 추적용 (datetime.date)
        self.window = FeatureWindow()
        self.fsm = BurstFSM()
        self.shrinkage = BucketwiseShrinkage()
        # 버킷별 CR/Jerk 히스토리 -- 실전에서는 영속 저장소로 교체
        # (주의: 이 히스토리는 날짜가 바뀌어도 리셋하지 않는다 -- 버킷별 20일/100일
        #  분포를 누적하는 것이 목적이므로, 날짜 경계 리셋 대상은 self.grid 뿐이다.)
        self.cr_history: Dict[Bucket, BucketHistory] = {b: BucketHistory() for b in Bucket}

        # --- 1분 스로틀링 상태 (원래 st.session_state에 두었던 것을 여기로 이동) ---
        # 이 상태를 게이트 객체 자체에 두면, Streamlit 세션이든 dry-run 테스트
        # 하네스든 st.* 호출 없이 동일하게 재사용할 수 있다 (core/wrapper 분리 원칙).
        self._last_push_time: Optional[datetime] = None
        self._volume_history: List[float] = []
        self._atr_history: List[float] = []

    def maybe_push_tick(
        self,
        spot: float,
        volume: float,
        vol_estimate: Optional[float],
        now: datetime,
        min_interval_sec: float = 60.0,
    ) -> Optional[GateResult]:
        """
        st.* 의존 없는 순수 진입점. 15초/그 이하 주기로 계속 호출해도,
        min_interval_sec(기본 60초)가 지날 때마다 딱 한 번만 실제 게이트
        연산(on_new_bar)을 수행하여 "1분봉처럼" 근사한다.

        TODO(실전 1분봉 피드 연결 시): 이 스로틀링 자체를 걷어내고, 스트림
        콜백에서 직접 on_new_bar()를 호출하도록 교체할 것.
        """
        if self._last_push_time is not None and (now - self._last_push_time).total_seconds() < min_interval_sec:
            return None

        self._last_push_time = now

        atr14 = float(vol_estimate) * float(spot) * 10 if vol_estimate else None  # 대략적 근사치

        self._volume_history.append(volume)
        self._volume_history = self._volume_history[-20:]
        volume_avg = float(sum(self._volume_history) / len(self._volume_history)) if self._volume_history else None

        atr_ma20 = None
        if atr14 is not None:
            self._atr_history.append(atr14)
            self._atr_history = self._atr_history[-20:]
            atr_ma20 = float(sum(self._atr_history) / len(self._atr_history))

        bar = MinuteBar(timestamp=now, close=float(spot), volume=float(volume), atr14=atr14, source="primary")
        return self.on_new_bar(bar=bar, now=now, atr_ma20=atr_ma20, volume_avg=volume_avg)

    def _reset_grid_if_new_day(self, now: datetime) -> None:
        """
        무중단 배포 전제: 프로세스가 여러 날에 걸쳐 재시작 없이 계속 떠 있을 수 있다.
        DailyMinuteGrid는 하루(390슬롯) 고정 크기라 날짜가 바뀌면 반드시 새로 만들어야
        한다. 그러지 않으면 전날 마지막 슬롯이 "최근 데이터"로 오인될 수 있다.
        """
        today = now.date()
        if self._grid_date != today:
            self.grid = DailyMinuteGrid()
            self._grid_date = today

    def on_new_bar(
        self,
        bar: MinuteBar,
        now: datetime,
        atr_ma20: Optional[float] = None,
        volume_avg: Optional[float] = None,
        secondary_bar: Optional[MinuteBar] = None,
    ) -> GateResult:
        self._reset_grid_if_new_day(now)
        self.grid.insert(bar)
        offset = minute_offset_of_session(bar.timestamp)
        if offset is None:
            return GateResult(status="OK", detail={"note": "장외 시간, 계산 스킵"})

        # 0단계: 데이터 품질 게이트
        quality = check_data_quality(
            latest_bar=bar, now=now, grid=self.grid, current_offset=offset,
            secondary_bar=secondary_bar,
        )
        if quality == DataQualityStatus.DATA_STALE:
            return GateResult(status="DATA_STALE")

        self.window.push(bar)

        # 1단계: 시간대 버킷 식별
        bucket = get_time_bucket(now)
        if bucket is None:
            return GateResult(status="OK", detail={"note": "버킷 매핑 불가"})

        # 2단계: 원시 피처 계산
        # [PATCH v2 -- FinalBurstGate] Jerk/엔트로피/시그모이드 합성 스코어는
        # 전면 폐기했다. 사후 검증 결과 노이즈 증폭 + 1~4분 사후 지연 +
        # 표본 부족으로 인한 오버핏이 확인되어, 실전 채택 규칙에서 제외한다.
        # 생존한 유일한 규칙: 버킷별 정규화 CR(응축) + 1분봉 거래량 급증.
        cr_raw = self.window.compute_cr(atr_ma20, volume_avg)

        if cr_raw is not None:
            self.cr_history[bucket].add(cr_raw)

        cr_z_short = self.cr_history[bucket].iqr_zscore(cr_raw, use_long_term=False) if cr_raw is not None else None

        # Bucket-wise Shrinkage 적용 (Global Prior 아님 -- 버킷 전용 LongTerm과 블렌드)
        cr_z_long = self.cr_history[bucket].iqr_zscore(cr_raw, use_long_term=True) if cr_raw is not None else None

        n_events_short = len(self.cr_history[bucket].short_term)
        cr_z = None
        if cr_z_short is not None and cr_z_long is not None:
            cr_z = self.shrinkage.blend(bucket, cr_z_short, cr_z_long, n_events_short)
        elif cr_z_short is not None:
            cr_z = cr_z_short

        volume_ratio = (bar.volume / volume_avg) if volume_avg else None

        # 3단계: FSM 상태 전이 (CR 응축 + 거래량 급증 2조건만)
        state = self.fsm.update(now=now, cr_z=cr_z, volume_ratio=volume_ratio)

        status = "LOW_CONFIDENCE" if quality == DataQualityStatus.LOW_CONFIDENCE else "OK"
        return GateResult(
            status=status,
            bucket=bucket,
            state=state,
            score=None,  # [PATCH v2] 합성 스코어 폐기 -- 2조건 불린 판정만 사용
            detail={
                "cr_z": cr_z,
                "volume_ratio": volume_ratio,
            },
        )


# =====================================================================
# 참고: 오프라인 튜닝(워크포워드) 설계는 별도 배치 스크립트로 분리 권장.
# 이 파일에는 포함하지 않았음 (온라인 추론 경로와 책임 분리).
#   - Train 20일 / Val 5일, 변동성 급증 시 Train 10일로 동적 단축
#   - 버킷 표본 부족 시 Val 10일로 확대
#   - 목적함수: F_0.5-score (정밀도 2배 가중)
#   - 탐색: Optuna(Global, 100회) -> Coordinate Descent(버킷별, ±40% 범위)
#   - Kill-switch: 최근 4주 평균 F_0.5가 직전 대비 -15% 이상 하락 시
#     파라미터 롤백하고 직전 안정값 유지
# =====================================================================
