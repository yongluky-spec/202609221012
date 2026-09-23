# -*- coding: utf-8 -*-
"""
@module: kospi_engine
@type: realtime
@version: v3
@created: 2026-09-19
@depends: 04_data/mock_market_data.py, 02_engine/burst_early_warning.py
@status: dev

장중 감시 퀀트 콕핏 v3 (KOSPI / NASDAQ 오토파일럿 대시보드)
------------------------------------------------------
실행 방법:
    1) pip install -r requirements.txt
    2) python -m streamlit run 02_engine/kospi_engine_v3_realtime.py

구성 (기존 4대 방어 쉴드 + 2개 셔터 + v3 신규 2종):
    1. 등호비교 엡실론(EPSILON)      : 경계값 근처 핑퐁 매매 차단
    2. EWMA 변동성 폴백              : GARCH 실패 시 자동 대체
    3. Basis 노이즈 블렌딩           : 극단 괴리(Z-score) 완화
    4. 레벨 히스테리시스             : 어제 포지션 관성 유지
    5. 델타 마름(Delta Fading) 감지  : 상승 동력 고갈 포착
    6. 대장주 디커플링 Hard Flat     : 이상 이탈 시 즉시 0% 강제
    [v3 신규]
    7. 장중 시나리오 확률/신뢰도 검증 엔진 : 3대 시나리오 소프트맥스 확률화 + 신뢰도 검증
    8. Burst Early Warning (5번째 게이트)  : 급변동 전조(응축->폭발) 조기경보

v3에서 바뀐 것 (계보):
    99_archive/kospi_model_v1_492_mock.py (492줄, base)
        + render_meter_html은 이미 base에 포함되어 있었음(중복 추가 안 함)
        + 시나리오/신뢰도 엔진 (update3.patch 내용 반영)
        + Burst Early Warning 게이트 (02_engine/burst_early_warning.py) 통합
        + fetch_market_data() 공급자 방식 (04_data/providers: KRX 완료, 한투/키움 추후)
        = 이 파일 (kospi_engine_v3_realtime.py)

주의 (정직하게 밝히는 한계):
    - foreign_net_futures(외국인 선물 순매수 성향)는 pykrx 투자자별 거래 API로
      근사 가능하나, corp_straddle_iv_diff(콜/풋 IV 스프레드)는 무료 API로
      실시간 취득이 어려워 실전 연동 모드에서도 0.0으로 둔다 (TODO 표시).
    - Burst Early Warning 게이트는 원래 1분봉 스트림을 전제로 설계했으나,
      이 화면의 갱신 주기(기본 15초)를 그대로 1분봉처럼 밀어넣으면 신호가
      왜곡된다. 그래서 60초가 지날 때마다 한 번만 게이트에 값을 넣도록
      세션 상태에서 시간을 직접 체크한다 (아래 TODO 참고, 실제 1분봉 피드로
      교체 전까지의 임시 근사치).
"""

import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import List

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------
# 로컬 모듈 경로 등록 (04_data/mock_market_data.py, 같은 폴더의
# burst_early_warning.py). 패키지화(__init__.py) 대신 sys.path로
# 처리 -- 뼈대 단계에서는 이게 가장 단순하고, 나중에 정식 패키지
# 구조로 옮길 때 이 블록만 걷어내면 된다.
# ---------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_THIS_DIR, "..", "04_data")
if _DATA_DIR not in sys.path:
    sys.path.append(_DATA_DIR)
if _THIS_DIR not in sys.path:
    sys.path.append(_THIS_DIR)

from mock_market_data import generate_mock_tick  # noqa: E402
from burst_early_warning import (  # noqa: E402
    BurstEarlyWarningGate,
    BurstState,
)
from guard_utils import safe_call, GUARD_STATS  # noqa: E402
from providers import get_providers  # noqa: E402
from acceleration_scanner_v1 import AccelerationScanner  # noqa: E402

# =========================================================
# 0. 전역 설정값 (여기 숫자만 바꿔서 튜닝하시면 됩니다)
# =========================================================
EPSILON = 0.05                 # 레벨 후보 변경을 허용하는 최소 격차
Z_SCORE_THRESHOLD = 2.0        # Basis 괴리 극단 판정 기준
EWMA_LAMBDA = 0.94             # EWMA 변동성 계수 (RiskMetrics 표준값)
LEADER_DRY_THRESHOLD = -0.003  # 대장주 델타가 이 값보다 낮으면 "마름"
ACCEL_DROP_THRESHOLD = -0.008  # 1시간 가속도 급감속 기준
DECOUPLING_LEADER_DELTA = -0.003
LOG_FILE = "prediction_log_v3.jsonl"   # v3 전용 로그로 분리 (기존 저장소1의 버전 접미사 패턴 승계)
REFRESH_SECONDS = 15           # 자동 갱신 주기(초)

LEVELS = [-1.0, -0.5, 0.0, 0.5, 1.0]  # 허용 포지션 레벨(공매도~풀롱)

# --- 시나리오 확률 / 신뢰도 검증 엔진 가중치 (update3.patch 반영) ---
SCENARIO_ENTROPY_WEIGHT = 0.4   # 확률 분포 쏠림(엔트로피) 가중치
SCENARIO_DATA_WEIGHT = 0.3      # 틱 노이즈(변동성) 청정도 가중치
SCENARIO_FLOW_WEIGHT = 0.3      # 수급 방향 일치도 가중치
CONFIDENCE_HIGH_THRESHOLD = 0.78
CONFIDENCE_CAUTION_THRESHOLD = 0.60  # 이 밑으로 떨어지면 신규 주문 차단 플래그

st.set_page_config(page_title="장중 감시 퀀트 콕핏 v3", layout="wide")


# =========================================================
# 1. 데이터 수집 (공급자 방식) - 모든 공급자 실패 시에만 mock 폴백
#    공급자 선택: 환경변수 DATA_PROVIDER (기본 "krx", 예: "kis,krx")
#    공급자 구현: 04_data/providers/  (KRX 완료, 한투/키움은 스켈레톤)
# =========================================================
LAST_FETCH = {"is_mock": False, "errors": [], "provider": None}


def fetch_market_data():
    """
    DATA_PROVIDER 에 지정된 공급자를 순서대로 시도한다. 전부 실패하면 mock 으로
    폴백하되, 실패 사유를 data_source_note 와 LAST_FETCH 에 그대로 남겨서
    화면에 "왜 mock 인지"가 보이게 한다.
    """
    errors = []
    try:
        providers = get_providers()
    except Exception as e:  # noqa: BLE001
        providers = []
        errors.append(f"공급자 설정 오류: {e}")

    for prov in providers:
        try:
            tick = prov.fetch_tick()
            LAST_FETCH.update(is_mock=False, errors=errors, provider=prov.name)
            note = prov.source_note()
            if errors:
                note += f" [앞 순위 실패: {' / '.join(errors)}]"
            return tick, note
        except Exception as e:  # noqa: BLE001
            errors.append(f"{prov.name}: {e}")

    LAST_FETCH.update(is_mock=True, errors=errors, provider=None)
    return generate_mock_tick(), f"모의데이터 사용 중 (사유: {' / '.join(errors)})"


# =========================================================
# 2. 변동성: GARCH 시도 -> 실패 시 EWMA 폴백
# =========================================================
def compute_volatility_with_fallback(returns: pd.Series):
    try:
        from arch import arch_model  # 미설치/실패 시 즉시 except로
        res = arch_model(returns * 100, vol="GARCH", p=1, q=1).fit(disp="off")
        vol = res.conditional_volatility.iloc[-1] / 100
        return vol, "GARCH(1,1)"
    except Exception:
        sq_ret = returns ** 2
        ewma_var = sq_ret.ewm(alpha=(1 - EWMA_LAMBDA)).mean().iloc[-1]
        vol = float(np.sqrt(max(ewma_var, 1e-12)))
        return vol, f"EWMA(λ={EWMA_LAMBDA}) [Fallback]"


# =========================================================
# 3. Basis 노이즈 블렌딩
# =========================================================
def apply_basis_noise_filter(raw_score, current_basis, recent_2d_avg_basis, zscore):
    if abs(zscore) > Z_SCORE_THRESHOLD:
        adj = np.clip((recent_2d_avg_basis - current_basis) * 0.5, -0.05, 0.05)
        return raw_score + adj, True
    return raw_score, False


# =========================================================
# 4. 델타 마름 감지
# =========================================================
def detect_delta_fading(leader_delta, accel_1h, market_delta_1h):
    is_up_context = market_delta_1h > 0.001
    leader_dry = leader_delta < LEADER_DRY_THRESHOLD
    accel_drop = accel_1h < ACCEL_DROP_THRESHOLD
    return bool(is_up_context and (leader_dry or accel_drop))


# =========================================================
# 5. 대장주 디커플링 감지
# =========================================================
def detect_decoupling(leader_delta, market_delta_1h):
    return bool(leader_delta < DECOUPLING_LEADER_DELTA and market_delta_1h > 0)


# =========================================================
# 5-1. 선물 매수잔량 기반 프락시 델타/감마 (Orderbook Greeks)
# =========================================================
def compute_orderbook_greeks(prev_bid_volume, curr_bid_volume, price_delta):
    bid_diff = curr_bid_volume - prev_bid_volume
    pseudo_delta = np.clip(bid_diff / 500.0, -1.0, 1.0) if prev_bid_volume > 0 else 0.0
    pseudo_gamma = float(np.abs(price_delta) * abs(pseudo_delta))
    return {
        "bid_volume_diff": int(bid_diff),
        "orderbook_delta": float(pseudo_delta),
        "orderbook_gamma": pseudo_gamma,
    }


# =========================================================
# 5-2. 대장주/가속도 수치 해설 (사람이 읽는 한 줄 설명)
# =========================================================
def describe_leader_delta(leader_delta):
    pct = f"{leader_delta*100:+.2f}%"
    if leader_delta <= LEADER_DRY_THRESHOLD:
        return f"{pct} — 대장주 하락 (마름 감지 임계값 이하)"
    if leader_delta < 0:
        return f"{pct} — 대장주 미세 하락 중"
    if leader_delta > 0:
        return f"{pct} — 대장주 상승 중"
    return "0.00% — 대장주 보합"


def describe_market_delta(market_delta_1h):
    pct = f"{market_delta_1h*100:+.2f}%"
    if market_delta_1h < -0.001:
        return f"{pct} — 최근 1시간 시장이 하락 추세"
    if market_delta_1h > 0.001:
        return f"{pct} — 최근 1시간 시장이 상승 추세"
    return f"{pct} — 최근 1시간 방향성 뚜렷하지 않음(횡보)"


def describe_acceleration(market_delta_1h, acceleration_1h):
    if market_delta_1h < 0:
        if acceleration_1h < ACCEL_DROP_THRESHOLD:
            return f"{acceleration_1h:+.5f} — 하락 속도가 점점 빨라지는 중(가속)"
        elif acceleration_1h < 0:
            return f"{acceleration_1h:+.5f} — 완만하게 하락 가속 붙는 중"
        else:
            return f"{acceleration_1h:+.5f} — 하락세이나 속도는 둔화되는 중(감속)"
    elif market_delta_1h > 0:
        if acceleration_1h > 0:
            return f"{acceleration_1h:+.5f} — 상승 속도가 점점 빨라지는 중(가속)"
        else:
            return f"{acceleration_1h:+.5f} — 상승세이나 속도는 둔화되는 중(감속)"
    return f"{acceleration_1h:+.5f} — 뚜렷한 가속/감속 신호 없음"


def build_market_summary(record):
    market_dir = "밀려내려가는" if record["market_delta_1h"] < 0 else (
        "밀어올리는" if record["market_delta_1h"] > 0 else "횡보하는"
    )
    accel_phrase = (
        "속도도 가속화되고" if (
            (record["market_delta_1h"] < 0 and record["acceleration_1h"] < 0)
            or (record["market_delta_1h"] > 0 and record["acceleration_1h"] > 0)
        ) else "속도는 둔화되고"
    )
    leader_phrase = "약보합" if abs(record["leader_delta"]) < 0.005 else (
        "약세" if record["leader_delta"] < 0 else "강세"
    )

    if record["hard_flat_reason"]:
        shutter_phrase = (
            f"Hard Flat 셔터가 발동해 포지션이 0%로 강제 전환된 상태"
            f"(사유: {record['hard_flat_reason']})"
        )
    else:
        shutter_phrase = "아직 Hard Flat 셔터 발동점까지는 도달하지 않아 🟢정상 밴드로 유지 중"

    return (
        f"현재 시장이 1시간 동안 약 {record['market_delta_1h']*100:+.2f}% "
        f"{market_dir} 중이고, {accel_phrase} 있으며, 대장주도 {leader_phrase}"
        f"({record['leader_delta']*100:+.2f}%)이라 전체적으로 "
        f"{'하방 압력/주의' if record['market_delta_1h'] < 0 else '상방 탄력'} 구간이나, "
        f"{shutter_phrase}입니다."
    )


def render_meter_html(label, value_pct, danger_threshold_pct=None,
                       axis_min=-2.0, axis_max=2.0):
    """단일 지표를 가로 미터바(HTML)로 렌더링. base(492줄)에 이미 있던 함수 그대로 승계."""
    span = axis_max - axis_min
    pos = min(max((value_pct - axis_min) / span, 0), 1) * 100
    neutral_pos = min(max((0 - axis_min) / span, 0), 1) * 100

    if danger_threshold_pct is not None and value_pct <= danger_threshold_pct:
        color = "#E24B4A"
    elif value_pct < 0:
        color = "#EF9F27"
    else:
        color = "#639922"

    danger_marker = ""
    danger_caption = ""
    if danger_threshold_pct is not None:
        danger_pos = min(max((danger_threshold_pct - axis_min) / span, 0), 1) * 100
        danger_marker = (
            f'<div style="position:absolute;left:{danger_pos:.1f}%;top:0;'
            f'height:100%;width:2px;background:#791F1F;"></div>'
        )
        danger_caption = f"위험선 {danger_threshold_pct:+.2f}%"

    return f"""
    <div style="margin-bottom:14px;">
      <div style="display:flex;justify-content:space-between;font-size:13px;
                  color:#666;margin-bottom:4px;">
        <span>{label}</span>
        <span style="font-weight:600;color:{color};">{value_pct:+.2f}%</span>
      </div>
      <div style="position:relative;height:10px;background:#eee;
                  border-radius:6px;overflow:hidden;">
        <div style="position:absolute;left:0;top:0;height:100%;
                    width:{pos:.1f}%;background:{color};"></div>
        <div style="position:absolute;left:{neutral_pos:.1f}%;top:0;
                    height:100%;width:1px;background:#999;"></div>
        {danger_marker}
      </div>
      <div style="font-size:11px;color:#999;margin-top:2px;">{danger_caption}</div>
    </div>
    """


# =========================================================
# 5-3. 장중 시나리오 자동 생성 + 확률/신뢰도 검증 엔진
#      (update3.patch 내용 그대로 승계)
# =========================================================
@dataclass
class MarketTickContext:
    spot: float
    basis: float
    foreign_net_futures: float
    corp_straddle_iv_diff: float
    vol_estimate: float
    leader_delta: float
    accel_1h: float


@dataclass
class DynamicScenario:
    scen_code: str
    title: str
    expected_return: float
    expected_iv_shift: float
    raw_score: float
    calibrated_prob: float
    reliability_index: float


@dataclass
class ReliabilityReport:
    global_confidence: float
    entropy_sharpness: float
    data_cleanliness: float
    flow_consistency: float
    status_verdict: str  # HIGH_CONFIDENCE_EXECUTE / CAUTION_REDUCE_SIZE / REFRESH_LOCK_UNCERTAIN


class IntradayScenarioProbabilityEngine:
    def __init__(self,
                 entropy_weight: float = SCENARIO_ENTROPY_WEIGHT,
                 data_weight: float = SCENARIO_DATA_WEIGHT,
                 flow_weight: float = SCENARIO_FLOW_WEIGHT):
        self.w_e = entropy_weight
        self.w_d = data_weight
        self.w_f = flow_weight

    @safe_call(
        default=(
            [],
            ReliabilityReport(
                global_confidence=0.0,
                entropy_sharpness=0.0,
                data_cleanliness=0.0,
                flow_consistency=0.0,
                status_verdict="REFRESH_LOCK_UNCERTAIN",
            ),
        ),
        name="scenario_engine",
    )
    def generate_and_evaluate(self, ctx: MarketTickContext):
        """
        실패 시 safe_call의 default가 곧바로 적용된다: 시나리오 리스트는 빈 배열,
        신뢰도 판정은 REFRESH_LOCK_UNCERTAIN(신규 주문 차단)으로 떨어진다.
        스칼라 0.5가 아니라 이 상태를 default로 준 이유는, run_one_tick()의
        order_block 로직이 status_verdict 문자열을 그대로 읽기 때문이다.
        """
        raw_scenarios = [
            {
                "code": "S1_UP_BREAK",
                "title": "상방 델타 확장 및 외국인 커버링 가속",
                "ret": 0.012,
                "iv": -0.015,
                "logit": 1.2 * ctx.foreign_net_futures + 2.0 * max(0.0, ctx.leader_delta * 100),
            },
            {
                "code": "S2_BOX_DECAY",
                "title": "박스권 횡보 (세타 감쇠 및 변동성 축소)",
                "ret": 0.001,
                "iv": -0.030,
                "logit": 1.8 - abs(ctx.accel_1h * 100) * 10.0,
            },
            {
                "code": "S3_DOWN_SQUEEZE",
                "title": "하방 숏 감마 발작 및 추격 선물 매도",
                "ret": -0.018,
                "iv": 0.055,
                "logit": (
                    -1.5 * ctx.foreign_net_futures
                    + 3.0 * max(0.0, -ctx.accel_1h * 100)
                    + 1.5 * max(0.0, ctx.corp_straddle_iv_diff * 100)
                ),
            },
        ]

        logits = np.array([s["logit"] for s in raw_scenarios], dtype=float)
        exp_logits = np.exp(logits - np.max(logits))
        probs = exp_logits / np.sum(exp_logits)

        scenarios: List[DynamicScenario] = []
        for i, s in enumerate(raw_scenarios):
            indiv_rel = float(np.clip(
                1.0 - (ctx.vol_estimate * 5.0) / (abs(s["logit"]) + 1.0), 0.2, 0.98
            ))
            scenarios.append(DynamicScenario(
                scen_code=s["code"],
                title=s["title"],
                expected_return=s["ret"],
                expected_iv_shift=s["iv"],
                raw_score=float(s["logit"]),
                calibrated_prob=float(probs[i]),
                reliability_index=indiv_rel,
            ))

        eps = 1e-9
        entropy = -np.sum(probs * np.log(probs + eps))
        max_entropy = np.log(len(scenarios))
        sharpness = float(1.0 - (entropy / max_entropy))

        cleanliness = float(np.clip(1.0 - (ctx.vol_estimate / 0.05), 0.0, 1.0))

        top_idx = int(np.argmax(probs))
        top_scen = scenarios[top_idx]
        flow_sign_match = (ctx.foreign_net_futures * top_scen.expected_return) >= 0
        consistency = 0.9 if flow_sign_match else 0.4

        global_conf = float(
            self.w_e * sharpness + self.w_d * cleanliness + self.w_f * consistency
        )

        if global_conf >= CONFIDENCE_HIGH_THRESHOLD and cleanliness >= 0.6:
            verdict = "HIGH_CONFIDENCE_EXECUTE"
        elif global_conf >= CONFIDENCE_CAUTION_THRESHOLD:
            verdict = "CAUTION_REDUCE_SIZE"
        else:
            verdict = "REFRESH_LOCK_UNCERTAIN"

        report = ReliabilityReport(
            global_confidence=global_conf,
            entropy_sharpness=sharpness,
            data_cleanliness=cleanliness,
            flow_consistency=consistency,
            status_verdict=verdict,
        )
        return scenarios, report


SCENARIO_ENGINE = IntradayScenarioProbabilityEngine()


# =========================================================
# 6. 레벨 결정: 엡실론 + 히스테리시스
# =========================================================
def snap_to_nearest_level(score):
    return min(LEVELS, key=lambda lv: abs(lv - score))


@safe_call(default=0.0, name="decide_target_level")
def decide_target_level(filtered_score, prev_level):
    """실패 시 default=0.0 (전량 청산/플랫) -- 스코어류 기본값(0.5)이 아니라
    포지션 레벨은 실패 시 안전한 쪽(0%)으로 떨어뜨리는 게 원칙에 맞는다."""
    candidate = snap_to_nearest_level(filtered_score)
    if abs(candidate - prev_level) <= EPSILON:
        return prev_level
    return candidate


# =========================================================
# 7. 로그 입출력 (JSONL)
# =========================================================
def load_last_position():
    if not os.path.exists(LOG_FILE):
        return 0.0
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if lines:
            return json.loads(lines[-1].strip()).get("current_position", 0.0)
    except Exception:
        pass
    return 0.0


def append_log(record: dict):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_log_df(n=200):
    if not os.path.exists(LOG_FILE):
        return pd.DataFrame()
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        return pd.DataFrame([json.loads(l) for l in lines])
    except Exception:
        return pd.DataFrame()


# =========================================================
# 7-1. Burst Early Warning 게이트 인스턴스 (세션 상태에 유지)
# =========================================================
@safe_call(default=None, name="burst_gate")
def run_burst_gate_core(gate: BurstEarlyWarningGate, tick: dict, vol: float, now: datetime):
    """
    st.* 호출이 전혀 없는 core 함수 -- dry_run_test.py가 Streamlit 없이도
    그대로 호출할 수 있다. 스로틀링(60초 1회) 상태는 gate 객체 자신이
    들고 있으므로(burst_early_warning.py의 maybe_push_tick 참고), 여기서는
    상태를 별도로 관리할 필요가 없다.
    """
    return gate.maybe_push_tick(
        spot=float(tick["spot"]),
        volume=float(tick.get("bid_volume", 0.0)),
        vol_estimate=float(vol) if vol else None,
        now=now,
    )


# =========================================================
# 8. 한 틱(tick) 처리 -> 최종 판정
# =========================================================
def process_tick_core(
    tick: dict,
    data_source_note: str,
    prev_level: float,
    prev_bid_vol: float,
    mock_returns: pd.Series,
    burst_gate: BurstEarlyWarningGate,
    now: datetime,
) -> dict:
    """
    st.* 호출이 전혀 없는 순수 core 함수.

    Streamlit UI(run_one_tick)뿐 아니라 03_backtest/dry_run_test.py도
    이 함수를 그대로 호출해서 실제 판정 로직을 검증할 수 있다
    (지난 설계 논의의 "core/wrapper 분리" 원칙을 여기서 실제로 적용한 것).

    필요한 상태(prev_level, prev_bid_vol, burst_gate)는 모두 인자로 받고,
    새로 갱신된 상태(new_bid_vol)를 함께 반환한다 -- 세션 상태 관리는
    호출자(wrapper)의 책임으로 완전히 분리.
    """
    vol, vol_source = compute_volatility_with_fallback(mock_returns)

    raw_score = float(np.tanh(tick["market_delta_1h"] * 50))

    filtered_score, is_blended = apply_basis_noise_filter(
        raw_score,
        tick["futures_basis"],
        tick["recent_2d_avg_basis"],
        tick["basis_zscore"],
    )

    fading = detect_delta_fading(
        tick["leader_delta"], tick["acceleration_1h"], tick["market_delta_1h"]
    )
    decoupled = detect_decoupling(tick["leader_delta"], tick["market_delta_1h"])

    greeks = compute_orderbook_greeks(prev_bid_vol, tick["bid_volume"], tick["market_delta_1h"])
    new_bid_vol = tick["bid_volume"]

    target_level = decide_target_level(filtered_score, prev_level)

    hard_flat_reason = None
    if fading:
        target_level = 0.0
        hard_flat_reason = "델타 마름 감지"
    if decoupled:
        target_level = 0.0
        hard_flat_reason = "대장주 디커플링"
    if greeks["orderbook_delta"] <= -0.8:
        target_level = 0.0
        hard_flat_reason = hard_flat_reason or "선물 매수잔량 급이탈"

    # --- 장중 시나리오 자동 생성 + 확률/신뢰도 검증 ---
    scenario_ctx = MarketTickContext(
        spot=tick["spot"],
        basis=tick["futures_basis"],
        foreign_net_futures=tick["foreign_net_futures"],
        corp_straddle_iv_diff=tick["corp_straddle_iv_diff"],
        vol_estimate=float(vol),
        leader_delta=tick["leader_delta"],
        accel_1h=tick["acceleration_1h"],
    )
    scenarios, reliability = SCENARIO_ENGINE.generate_and_evaluate(scenario_ctx)

    # 신뢰도 낮음(REFRESH_LOCK)은 "신규 주문 차단" 플래그로만 분리 처리.
    # 기존 4대 쉴드의 Hard Flat(포지션 강제청산)과는 별개의 조치임에 유의.
    order_block = reliability.status_verdict == "REFRESH_LOCK_UNCERTAIN"

    # --- 5번째 게이트: Burst Early Warning ---
    # 게이트 자신이 60초 스로틀링 상태를 들고 있으므로, None이 오면 "아직
    # 1분 안 지나서 스킵"과 "guard_call 폴백" 둘 다를 의미하고, 이 틱에서는
    # 그냥 burst 신호를 갱신하지 않는다(직전 값 유지는 wrapper 쪽 책임).
    burst_result = run_burst_gate_core(burst_gate, tick, vol, now)

    record = {
        "timestamp": tick["timestamp"],
        "data_source": data_source_note,
        "raw_score": round(raw_score, 4),
        "filtered_score": round(filtered_score, 4),
        "basis_blended": is_blended,
        "vol_source": vol_source,
        "vol_value": round(float(vol), 6),
        "leader_delta": tick["leader_delta"],
        "market_delta_1h": tick["market_delta_1h"],
        "acceleration_1h": tick["acceleration_1h"],
        "fading_flag": fading,
        "decoupled_flag": decoupled,
        "hard_flat_reason": hard_flat_reason,
        "bid_volume_diff": greeks["bid_volume_diff"],
        "orderbook_delta": greeks["orderbook_delta"],
        "orderbook_gamma": greeks["orderbook_gamma"],
        "scenarios": [asdict(s) for s in scenarios],
        "global_confidence": round(reliability.global_confidence, 4),
        "entropy_sharpness": round(reliability.entropy_sharpness, 4),
        "data_cleanliness": round(reliability.data_cleanliness, 4),
        "flow_consistency": round(reliability.flow_consistency, 4),
        "confidence_verdict": reliability.status_verdict,
        "order_block": order_block,
        "prev_position": prev_level,
        "current_position": target_level,
        "leaders": tick.get("leaders", []),           # [v3.1 신규] 대장주 개별 종목 상세
        "leader_event": tick.get("leader_event"),      # [v3.1 신규] 대장주 교체 알림 (없으면 None)
        "_new_bid_vol": new_bid_vol,       # wrapper가 세션 상태 갱신할 때 사용, 로그에는 그대로 남겨도 무해
        "_burst_result": burst_result,     # GateResult 또는 None -- wrapper가 UI 렌더링에 사용
    }
    return record


def run_one_tick(mock_returns: pd.Series):
    """
    Streamlit 전용 얇은 wrapper. 세션 상태를 process_tick_core에 넣고
    빼는 것 외에는 아무 로직도 갖지 않는다.
    """
    tick, data_source_note = fetch_market_data()
    prev_level = load_last_position()
    prev_bid_vol = st.session_state.setdefault("prev_bid_vol", tick["bid_volume"])
    burst_gate = st.session_state.setdefault("burst_gate", BurstEarlyWarningGate())

    record = process_tick_core(
        tick=tick,
        data_source_note=data_source_note,
        prev_level=prev_level,
        prev_bid_vol=prev_bid_vol,
        mock_returns=mock_returns,
        burst_gate=burst_gate,
        now=datetime.now(),
    )

    st.session_state.prev_bid_vol = record.pop("_new_bid_vol")
    burst_result = record.pop("_burst_result")
    if burst_result is not None:
        st.session_state.last_burst_result = burst_result
    burst_result = st.session_state.get("last_burst_result")
    record["burst_state"] = burst_result.state.value if (burst_result and burst_result.state) else "N/A"
    record["burst_score"] = burst_result.score if burst_result else None
    record["burst_status"] = burst_result.status if burst_result else "N/A"

    append_log(record)
    return record


# =========================================================
# 8-1. 가속도 스캐너 (6번째 게이트) - 폴링 + 트리맵 렌더링
# =========================================================
def get_or_poll_scanner():
    """
    session_state에 AccelerationScanner 인스턴스를 하나만 유지하고, 매 rerun마다
    maybe_poll()을 호출한다. 스캐너 자신이 backoff 주기를 판단하므로, 아직
    폴링할 때가 아니면 None이 오고 그때는 마지막 성공 스냅샷을 그대로 재사용한다.
    """
    scanner: AccelerationScanner = st.session_state.setdefault("accel_scanner", AccelerationScanner())
    snapshot = scanner.maybe_poll(datetime.now())
    return snapshot or scanner.last_snapshot, scanner


def render_treemap(map_data: dict, title: str) -> go.Figure:
    """acceleration_scanner_v1._build_treemap_data()가 만든 dict -> Plotly Figure.
    델타 맵/가속도 맵 둘 다 같은 함수로 그리되, color_range만 서로 다르게 넘어온다
    (합의: 같은 팔레트, 다른 축 -- 두 지도가 "같은 언어"로 보이게)."""
    if not map_data or not map_data.get("labels"):
        fig = go.Figure()
        fig.update_layout(title=f"{title} (데이터 없음)", height=380)
        return fig

    lo, hi = map_data["color_range"]
    fig = go.Figure(go.Treemap(
        labels=map_data["labels"],
        parents=[""] * len(map_data["labels"]),
        values=map_data["sizes"],
        customdata=list(zip(map_data["tickers"], map_data["values"])),
        text=[f"{v:+.2f}%" if abs(v) < 100 else f"{v:+.2f}" for v in map_data["values"]],
        textinfo="label+text",
        marker=dict(
            colors=map_data["values"],
            colorscale="RdBu_r",   # 빨강(하락/음수) ~ 파랑(상승/양수), 델타·가속도 맵 공통
            cmin=lo, cmax=hi, cmid=0,
            line=dict(width=map_data["line_widths"], color=map_data["line_colors"]),
        ),
        hovertemplate="<b>%{label}</b><br>%{text}<extra></extra>",
    ))
    fig.update_layout(title=title, height=380, margin=dict(t=40, l=5, r=5, b=5))
    return fig


# =========================================================
# 9. Streamlit UI
# =========================================================
def main():
    st.title("🛡️ 장중 감시 퀀트 콕핏 v3 (4대 쉴드 + 시나리오 엔진 + Burst 게이트)")

    with st.sidebar:
        st.header("⚙️ 설정")
        auto_refresh = st.checkbox("자동 갱신 사용", value=True)
        interval = st.slider("갱신 주기(초)", 5, 60, REFRESH_SECONDS)
        st.caption("※ 모든 데이터 공급자가 실패하면 모의데이터로 폴백합니다 (화면 상단에 빨간 경고 표시).")
        with st.expander("🔌 데이터 공급자 상태"):
            try:
                _provs = get_providers()
            except Exception as e:  # noqa: BLE001
                _provs = []
                st.caption(f"공급자 설정 오류: {e}")
            for prov in _provs:
                env = prov.env_status()
                env_txt = ", ".join(f"{k}: {'설정됨' if ok else '없음'}" for k, ok in env.items()) or "-"
                st.write(f"**{prov.name}** ({prov.label})")
                st.caption(f"환경변수 {env_txt}")
        if st.button("로그 초기화"):
            if os.path.exists(LOG_FILE):
                os.remove(LOG_FILE)
            st.success(f"{LOG_FILE} 초기화 완료")

        fallback_counts = GUARD_STATS.summary()["fallback_count"]
        if fallback_counts:
            st.caption(f"⚠️ 가드 폴백 발생: {fallback_counts}")
        else:
            st.caption("✅ 가드 폴백 발생 없음")

    if "mock_returns" not in st.session_state:
        st.session_state.mock_returns = pd.Series(np.random.normal(0, 0.01, 100))

    record = run_one_tick(st.session_state.mock_returns)

    if LAST_FETCH["is_mock"]:
        st.error(
            "🧪 **지금 화면은 모의데이터(가짜 값)입니다. 실제 매매 판단에 쓰지 마세요.**\n\n"
            + "\n\n".join(f"- {m}" for m in LAST_FETCH["errors"])
        )

    # ---- 상단 신호등 패널 ----
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("현재 포지션 레벨", f"{record['current_position']*100:.0f}%")
    c2.metric("필터링된 방향점수", f"{record['filtered_score']:.3f}")
    c3.metric("변동성 소스", record["vol_source"])
    c4.metric("Basis 블렌딩 작동", "예" if record["basis_blended"] else "아니오")

    if record["hard_flat_reason"]:
        st.error(f"🚨 HARD FLAT 발동 — 사유: {record['hard_flat_reason']} → 포지션 0% 강제")
    elif record["fading_flag"] or record["decoupled_flag"]:
        st.warning("⚠️ 주의 신호 감지 중 (아직 셔터 미발동)")
    else:
        st.success("🟢 정상 밴드 — 동조 양호")

    st.divider()

    # ---- 선물 잔량 민감도 콕핏 ----
    st.markdown("### ⚡ 선물 잔량 민감도 콕핏 (Orderbook Greeks)")
    g1, g2, g3, g4 = st.columns(4)
    g1.metric("매수잔량 Δ증감", f"{record.get('bid_volume_diff', 0):,}")
    g2.metric("잔량 프락시 델타", f"{record.get('orderbook_delta', 0.0):+.3f}")
    g3.metric("잔량 가속도(감마)", f"{record.get('orderbook_gamma', 0.0):.4f}")
    ob_delta = record.get("orderbook_delta", 0.0)
    g4.metric("잔량 상태등", "STABLE" if abs(ob_delta) < 0.5 else "SURGE/DRY")

    st.divider()

    # ---- 장중 자동 시나리오 및 신뢰도 검증 콕핏 ----
    st.markdown("### 🎲 장중 자동 시나리오 및 신뢰도 검증 콕핏")
    verdict = record.get("confidence_verdict", "N/A")
    verdict_label = {
        "HIGH_CONFIDENCE_EXECUTE": "🟢 고신뢰(정상 집행)",
        "CAUTION_REDUCE_SIZE": "🟡 주의(사이즈 축소 권고)",
        "REFRESH_LOCK_UNCERTAIN": "🔴 불확실(신규 주문 차단)",
    }.get(verdict, verdict)

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("글로벌 종합 신뢰도(C)", f"{record.get('global_confidence', 0):.2f}", delta=verdict_label)
    s2.metric("분포 집중도(Entropy)", f"{record.get('entropy_sharpness', 0):.2f}")
    s3.metric("데이터 청정도", f"{record.get('data_cleanliness', 0):.2f}")
    s4.metric("수급 정합성", f"{record.get('flow_consistency', 0):.2f}")

    if record.get("order_block"):
        st.error("🚨 신뢰도 0.60 미만 — 신규 주문 차단(order_block) 활성화 (기존 포지션은 유지, 신규 진입만 금지)")

    scen_df = pd.DataFrame([
        {
            "ID": s["scen_code"],
            "시나리오": s["title"],
            "예상변동": f"{s['expected_return']*100:+.2f}%",
            "IV 변동": f"{s['expected_iv_shift']*100:+.2f}%",
            "확률(P)": f"{s['calibrated_prob']*100:.1f}%",
            "개별신뢰도": f"{s['reliability_index']:.2f}",
        }
        for s in record.get("scenarios", [])
    ])
    st.dataframe(scen_df, use_container_width=True, hide_index=True)
    st.caption(
        "확률(P)은 3개 시나리오 로짓을 소프트맥스로 정규화한 값이며 항상 합=100%입니다. "
        "개별신뢰도는 변동성이 클수록, 근거(로짓)가 약할수록 낮아집니다."
    )

    st.divider()

    # ---- 5번째 게이트: Burst Early Warning 콕핏 ----
    st.markdown("### 💥 Burst Early Warning (5번째 게이트, 급변동 전조 경보)")
    st.caption("⚠️ 60초 주기로 근사 계산됩니다 (실제 1분봉 피드 연결 전 임시 방식, 코드 상단 TODO 참고).")
    burst_state = record.get("burst_state", "N/A")
    burst_state_label = {
        "QUIET": "⚪ QUIET (평상)",
        "COILING": "🟡 COILING (응축 중)",
        "BURST_WARNING": "🔴 BURST_WARNING (폭발 임박)",
    }.get(burst_state, burst_state)
    b1, b2, b3 = st.columns(3)
    b1.metric("FSM 상태", burst_state_label)
    burst_score = record.get("burst_score")
    b2.metric("BurstScore", f"{burst_score:.1f}" if burst_score is not None else "N/A")
    b3.metric("데이터 품질", record.get("burst_status", "N/A"))
    if burst_state == "BURST_WARNING":
        st.error("🚨 응축(Coiling) 이후 저크/거래량 임계 동시 충족 — 급변동 임박 가능성")

    st.divider()

    # ---- 상세 지표 ----
    d1, d2 = st.columns(2)
    with d1:
        st.subheader("대장주 / 가속도 상태")

        st.markdown(
            render_meter_html(
                "대장주 델타 (leader_delta)",
                record["leader_delta"] * 100,
                danger_threshold_pct=LEADER_DRY_THRESHOLD * 100,
                axis_min=-2.0, axis_max=2.0,
            )
            + render_meter_html(
                "1시간 델타 (market_delta_1h)",
                record["market_delta_1h"] * 100,
                danger_threshold_pct=None,
                axis_min=-3.0, axis_max=3.0,
            )
            + render_meter_html(
                "1시간 가속도 (acceleration_1h)",
                record["acceleration_1h"] * 100,
                danger_threshold_pct=ACCEL_DROP_THRESHOLD * 100,
                axis_min=-3.0, axis_max=3.0,
            ),
            unsafe_allow_html=True,
        )

        st.table(pd.DataFrame([
            {"항목": "대장주 델타 (leader_delta)", "현재 값": f"{record['leader_delta']}",
             "의미": describe_leader_delta(record["leader_delta"])},
            {"항목": "1시간 델타 (market_delta_1h)", "현재 값": f"{record['market_delta_1h']}",
             "의미": describe_market_delta(record["market_delta_1h"])},
            {"항목": "1시간 가속도 (acceleration_1h)", "현재 값": f"{record['acceleration_1h']}",
             "의미": describe_acceleration(record["market_delta_1h"], record["acceleration_1h"])},
            {"항목": "데이터 소스", "현재 값": record["data_source"], "의미": (
                "아직 실전 데이터 연동 실패 -> 시뮬레이션 값으로 쉴드 로직만 테스트 중"
                if "모의데이터" in record["data_source"] else "실전 데이터 연동 중"
            )},
        ]))
        st.markdown(f"💡 **한 줄 요약**: {build_market_summary(record)}")

    with d2:
        st.subheader("이번 틱 원본 로그(JSON)")
        st.json(record)

    st.divider()

    # ---- 대장주 개별 종목 카드 (신규) ----
    st.markdown("### 🏆 대장주 개별 종목 (코스피 시총 1~2위, 동적 산정)")
    leader_event = record.get("leader_event")
    if leader_event:
        st.info(f"🔄 대장주 교체: {leader_event}")

    leaders = record.get("leaders", [])
    if not leaders:
        st.caption("아직 대장주 데이터를 산정하지 못했습니다 (휴장일이거나 첫 조회 중일 수 있습니다).")
    else:
        lcols = st.columns(len(leaders))
        for col, leader in zip(lcols, leaders):
            with col:
                delta_pct = leader["delta_pct"] * 100
                st.metric(
                    label=f"{leader['name']} ({leader['ticker']})",
                    value=f"{leader['price']:,.0f}원",
                    delta=f"{delta_pct:+.2f}%",
                )
                st.caption(f"시총비중 {leader['weight']*100:.1f}% · 전일종가 {leader['prev_close']:,.0f}원")

    st.divider()

    # ---- 가속도 스캐너 + 델타/가속도 트리맵 (6번째 게이트, 신규) ----
    st.markdown("### 🗺️ 대형주 트리맵 — 델타 vs 가속도 (코스피 시총 상위 30개)")
    scanner_snapshot, _scanner = get_or_poll_scanner()

    if not scanner_snapshot:
        st.caption("스캐너 데이터 수집 중입니다 (첫 폴링 완료까지 잠시 기다려주세요).")
    else:
        backoff_label = {
            "OK": "🟢 정상", "FAIL": "🟠 실패(백오프 중)", "INIT": "⚪ 초기화",
        }.get(scanner_snapshot["backoff_status"], scanner_snapshot["backoff_status"])
        st.caption(
            f"폴링 상태: {backoff_label} · 현재 주기 {scanner_snapshot['backoff_interval']:.0f}초 "
            f"(연속 실패 {scanner_snapshot['backoff_failures']}회) · 기준시각 {scanner_snapshot['timestamp']}"
        )

        t1, t2 = st.columns(2)
        with t1:
            st.plotly_chart(
                render_treemap(scanner_snapshot["delta_map"], "델타 맵 (등락률, -3%~+3%)"),
                use_container_width=True,
            )
        with t2:
            st.plotly_chart(
                render_treemap(scanner_snapshot["accel_map"], "가속도 맵 (Δ의 변화량, -0.5%~+0.5%, 급변 종목 테두리 강조)"),
                use_container_width=True,
            )

        alerts = [r for r in scanner_snapshot["rows"] if r["is_alert"]]
        if alerts:
            st.warning(f"⚡ 가속도 급변 종목 {len(alerts)}개 포착 (|Z-score| ≥ 2.0)")
            alert_df = pd.DataFrame([
                {
                    "종목명": a["name"],
                    "현재가": f"{a['price']:,.0f}",
                    "등락률": f"{a['delta_pct']:+.2f}%",
                    "가속도": f"{a['acceleration']:+.3f}%",
                    "Z-score": f"{a['accel_zscore']:+.2f}",
                    "방향": "🔺 매수세 급증" if a["alert_direction"] == "UP_SURGE" else "🔻 투매 포착",
                }
                for a in alerts
            ])
            st.dataframe(alert_df, use_container_width=True, hide_index=True)
        else:
            st.caption("현재 급변 포착 종목 없음 (정상 범위).")

        with st.expander("📜 가속도 급변 경고 로그 (최근순)"):
            log = scanner_snapshot.get("alert_log", [])
            if log:
                log_df = pd.DataFrame([
                    {
                        "시각": e["timestamp"], "종목명": e["name"],
                        "가속도": f"{e['acceleration']:+.3f}%", "Z-score": f"{e['zscore']:+.2f}",
                        "방향": "🔺 급증" if e["direction"] == "UP_SURGE" else "🔻 급락",
                    }
                    for e in log
                ])
                st.dataframe(log_df, use_container_width=True, hide_index=True)
            else:
                st.caption("아직 경고 로그가 없습니다.")

    st.divider()

    # ---- 히스토리 차트 ----
    st.subheader("📈 최근 포지션 / 점수 히스토리")
    df = load_log_df(200)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        chart_df = df.set_index("timestamp")[["current_position", "filtered_score"]]
        st.line_chart(chart_df)
        st.caption("빨간 신호(Hard Flat)가 잦다면 EPSILON / 임계값을 조정해보세요.")
    else:
        st.info("아직 누적된 로그가 없습니다. 잠시 후 다시 확인하세요.")

    st.caption(
        f"마지막 갱신: {record['timestamp']}  |  "
        f"EPSILON={EPSILON}, Z_THRESH={Z_SCORE_THRESHOLD}, "
        f"LEADER_DRY={LEADER_DRY_THRESHOLD}, ACCEL_DROP={ACCEL_DROP_THRESHOLD}"
    )

    if auto_refresh:
        time.sleep(interval)
        st.rerun()


if __name__ == "__main__":
    main()
