# -*- coding: utf-8 -*-
"""
장중 감시 퀀트 콕핏 (KOSPI / NASDAQ 오토파일럿 대시보드)
------------------------------------------------------
실행 방법:
    1) pip install -r requirements.txt
    2) python -m streamlit run kospi_model.py

구성 (4대 방어 쉴드 + 2개 추가 셔터):
    1. 등호비교 엡실론(EPSILON)      : 경계값 근처 핑퐁 매매 차단
    2. EWMA 변동성 폴백              : GARCH 실패 시 자동 대체
    3. Basis 노이즈 블렌딩           : 극단 괴리(Z-score) 완화
    4. 레벨 히스테리시스             : 어제 포지션 관성 유지
    5. 델타 마름(Delta Fading) 감지  : 상승 동력 고갈 포착
    6. 대장주 디커플링 Hard Flat     : 이상 이탈 시 즉시 0% 강제
"""

import json
import time
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import streamlit as st

# =========================================================
# 0. 전역 설정값 (여기 숫자만 바꿔서 튜닝하시면 됩니다)
# =========================================================
EPSILON = 0.05                 # 레벨 후보 변경을 허용하는 최소 격차
Z_SCORE_THRESHOLD = 2.0        # Basis 괴리 극단 판정 기준
EWMA_LAMBDA = 0.94             # EWMA 변동성 계수 (RiskMetrics 표준값)
LEADER_DRY_THRESHOLD = -0.003  # 대장주 델타가 이 값보다 낮으면 "마름"
ACCEL_DROP_THRESHOLD = -0.008  # 1시간 가속도 급감속 기준
DECOUPLING_LEADER_DELTA = -0.003
LOG_FILE = "prediction_log.jsonl"
REFRESH_SECONDS = 15           # 자동 갱신 주기(초)

LEVELS = [-1.0, -0.5, 0.0, 0.5, 1.0]  # 허용 포지션 레벨(공매도~풀롱)

st.set_page_config(page_title="장중 감시 퀀트 콕핏", layout="wide")


# =========================================================
# 1. 데이터 수집 (실전 API 연동 지점) - 실패 시 순차 폴백
# =========================================================
def fetch_market_data():
    """
    실전 연동 시 이 함수 안을 pykrx / yfinance / 네이버 금융 순으로
    시도하도록 채우세요. 지금은 네트워크가 없는 환경 기준으로
    구조만 잡아두고, 실패하면 즉시 모의 데이터로 안전하게 폴백합니다.
    """
    try:
        # ---- 실전 연동 예시 (필요 시 주석 해제 후 사용) ----
        # from pykrx import stock
        # import yfinance as yf
        #
        # today = datetime.now().strftime("%Y%m%d")
        # kospi = stock.get_index_ohlcv(today, today, "1001")
        # samsung = stock.get_market_ohlcv(today, today, "005930")
        # sk_hynix = stock.get_market_ohlcv(today, today, "000660")
        # es = yf.Ticker("ES=F").history(period="1d", interval="1m")
        # nq = yf.Ticker("NQ=F").history(period="1d", interval="1m")
        #
        # ... 여기서 spot / futures_basis / leader_delta 등을 계산 ...
        raise NotImplementedError("실전 API 연동 전에는 모의데이터 사용")
    except Exception as e:
        return _mock_market_data(), f"모의데이터 사용 중 (사유: {e})"


def _mock_market_data():
    """실전 API 연결 전, 알고리즘 검증용 모의 틱 데이터."""
    rng = np.random.default_rng(int(time.time()) % 10000)
    return {
        "spot": 2650.0 + rng.normal(0, 3),
        "futures_basis": round(rng.normal(0.8, 0.6), 3),
        "basis_zscore": round(rng.normal(0, 1.3), 2),
        "leader_delta": round(rng.normal(0.0, 0.006), 5),
        "market_delta_1h": round(rng.normal(0.0, 0.01), 5),
        "acceleration_1h": round(rng.normal(0.0, 0.01), 5),
        "recent_2d_avg_basis": round(rng.normal(0.5, 0.2), 3),
        "bid_volume": round(10000 + rng.normal(0, 400), 1),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


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
    """
    선물 매수잔량 변화 기반 프락시 델타(방향 쏠림) 및 감마(변화 가속도) 산출.
    실전 연동 시 curr_bid_volume 자리에 증권사 API의 실시간 매수총잔량을 넣으면 됩니다.
    """
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
    """단일 지표를 가로 미터바(HTML)로 렌더링. Streamlit st.markdown(..., unsafe_allow_html=True)로 출력."""
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
# 6. 레벨 결정: 엡실론 + 히스테리시스
# =========================================================
def snap_to_nearest_level(score):
    """방향 점수(-1~1)를 허용 레벨 중 가장 가까운 값으로 매핑."""
    return min(LEVELS, key=lambda lv: abs(lv - score))


def decide_target_level(filtered_score, prev_level):
    candidate = snap_to_nearest_level(filtered_score)
    if abs(candidate - prev_level) <= EPSILON:
        return prev_level  # 핑퐁 방지: 기존 레벨 유지
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
# 8. 한 틱(tick) 처리 -> 최종 판정
# =========================================================
def run_one_tick(mock_returns: pd.Series):
    tick, data_source_note = fetch_market_data()
    prev_level = load_last_position()

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

    if "prev_bid_vol" not in st.session_state:
        st.session_state.prev_bid_vol = tick["bid_volume"]
    greeks = compute_orderbook_greeks(
        st.session_state.prev_bid_vol, tick["bid_volume"], tick["market_delta_1h"]
    )
    st.session_state.prev_bid_vol = tick["bid_volume"]

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
        "prev_position": prev_level,
        "current_position": target_level,
    }
    append_log(record)
    return record


# =========================================================
# 9. Streamlit UI
# =========================================================
def main():
    st.title("🛡️ 장중 감시 퀀트 콕핏 (4대 쉴드 + 델타 마름 + 디커플링 셔터)")

    with st.sidebar:
        st.header("⚙️ 설정")
        auto_refresh = st.checkbox("자동 갱신 사용", value=True)
        interval = st.slider("갱신 주기(초)", 5, 60, REFRESH_SECONDS)
        st.caption("※ 실전 API(pykrx/yfinance) 연동 전에는 모의데이터로 동작합니다.")
        if st.button("로그 초기화"):
            if os.path.exists(LOG_FILE):
                os.remove(LOG_FILE)
            st.success("prediction_log.jsonl 초기화 완료")

    if "mock_returns" not in st.session_state:
        st.session_state.mock_returns = pd.Series(np.random.normal(0, 0.01, 100))

    record = run_one_tick(st.session_state.mock_returns)

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

    # ---- 선물 잔량 민감도 콕핏 (Orderbook Greeks) ----
    st.markdown("### ⚡ 선물 잔량 민감도 콕핏 (Orderbook Greeks)")
    g1, g2, g3, g4 = st.columns(4)
    g1.metric("매수잔량 Δ증감", f"{record.get('bid_volume_diff', 0):,}")
    g2.metric("잔량 프락시 델타", f"{record.get('orderbook_delta', 0.0):+.3f}")
    g3.metric("잔량 가속도(감마)", f"{record.get('orderbook_gamma', 0.0):.4f}")
    ob_delta = record.get("orderbook_delta", 0.0)
    g4.metric("잔량 상태등", "STABLE" if abs(ob_delta) < 0.5 else "SURGE/DRY")

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
                "아직 HTS/실전 API 연결 전이라 시뮬레이션 값으로 쉴드 로직만 테스트 중"
                if "모의데이터" in record["data_source"] else "실전 데이터 연동 중"
            )},
        ]))
        st.markdown(f"💡 **한 줄 요약**: {build_market_summary(record)}")

    with d2:
        st.subheader("이번 틱 원본 로그(JSON)")
        st.json(record)

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
