# -*- coding: utf-8 -*-
"""
@module: providers.krx_provider
@type: data
@version: v1

한국거래소(KRX) 데이터 공급자 (pykrx 기반).

KRX 데이터 포털은 로그인이 필수로 바뀌었다. pykrx 는 환경변수 KRX_ID / KRX_PW 로
로그인하므로, 이 값이 없으면 "거래소 인증 실패"가 난다.

    # 윈도우 PowerShell
    $env:KRX_ID="아이디";  $env:KRX_PW="비밀번호"
    # 맥/리눅스
    export KRX_ID="아이디"; export KRX_PW="비밀번호"

또는 프로젝트 루트의 .env 파일에 적어두면 (python-dotenv 설치 시) 자동으로 읽는다.
.env 는 절대 git 에 올리지 말 것 (.gitignore 에 추가됨).

한계 (정직하게):
    - pykrx 의 get_index_ohlcv / get_market_ohlcv 는 '일봉' 이다. 장중 틱이 아니다.
      장중 델타/가속도는 폴링 값을 메모리에 누적해서 근사한다.
    - 인증 실패 후에는 AUTH_COOLDOWN_SEC 동안 재시도하지 않는다 (계정 잠금 방지).

[v3.1 변경 -- 대장주 동적 산정 + "leader_delta 항상 0" 버그 수정]
    이전 버전 버그: get_market_ohlcv(today, today, "005930") 처럼 당일 하루치만
    조회해서 종가 컬럼이 1행뿐이었고, pct_change()가 계산 불가능해 leader_delta가
    항상 0.0으로 떨어졌다. 또한 종목코드가 "005930"(삼성전자)로 하드코딩되어
    있어 시가총액 순위가 바뀌어도 절대 반영되지 않았다.

    수정:
      1) get_market_ohlcv 조회 범위를 "오늘~오늘"에서 "오늘-LEADER_LOOKBACK_DAYS일~오늘"로
         넓혀서 전일 종가를 확보 (pct_change 정상 동작).
      2) LEADER_TOP_N(기본 2)개 종목을 get_market_cap_by_ticker()로 매
         LEADER_RANK_CHECK_INTERVAL_SEC(기본 60초)마다 재산정 (하드코딩 제거).
      3) leader_delta(기존 필드, 하위 호환용)는 TOP_N 종목의 시총 가중평균으로 계산.
      4) leaders(신규 필드)에 종목별 상세(현재가/전일종가/등락률/시총비중)를 담는다.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import numpy as np

from .base import (
    MarketDataProvider,
    ProviderAuthError,
    ProviderError,
)

AUTH_COOLDOWN_SEC = 300          # 인증 실패 후 재시도 금지 시간
_AUTH_HINTS = ("login", "로그인", "logout", "jsondecode", "인증", "auth")

# --- 대장주 동적 산정 설정 (여기 숫자만 바꿔서 튜닝) ---
LEADER_TOP_N = 2                       # 대장주 산정 개수 (합의: 코스피 시총 1~2위)
LEADER_RANK_CHECK_INTERVAL_SEC = 60    # 순위 재확인 주기 (Slow Loop)
LEADER_LOOKBACK_DAYS = 7               # 전일 종가 확보용 조회 범위 (휴일 여유 포함)
LEADER_MARKET = "KOSPI"                # 합의: 코스닥 제외

_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=False)
    except Exception:
        pass


class KrxProvider(MarketDataProvider):
    name = "krx"
    label = "KRX/pykrx, 일봉 기준"

    def __init__(self) -> None:
        self._auth_blocked_until = 0.0
        self._last_auth_error = ""
        self._basis_history: list[float] = []
        self._spot_history: list[tuple[datetime, float]] = []
        # --- 대장주 동적 산정 상태 (Slow Loop 60초 캐시) ---
        self._leader_tickers: list[str] = []      # 현재 확정된 TOP_N 종목코드 (시총 내림차순)
        self._leader_last_check: float = 0.0       # 마지막 순위 재확인 시각 (time.time())
        self._pending_leader_event: "str | None" = None  # 대장주 교체 발생 시 1회성 알림 문자열

    # ---- 환경 ----
    def env_status(self) -> dict:
        _load_dotenv_if_available()
        return {k: bool(os.environ.get(k)) for k in ("KRX_ID", "KRX_PW")}

    def _check_credentials(self) -> None:
        missing = [k for k, ok in self.env_status().items() if not ok]
        if missing:
            raise ProviderAuthError(
                f"KRX 로그인 정보 없음: 환경변수 {', '.join(missing)} 를 설정하세요 "
                "(data.krx.co.kr 계정). 설정 방법은 04_data/providers/krx_provider.py 상단 참고."
            )

    def _check_cooldown(self) -> None:
        remain = self._auth_blocked_until - time.time()
        if remain > 0:
            raise ProviderAuthError(
                f"KRX 인증 실패 후 재시도 대기 중 ({int(remain)}초 남음, 계정 잠금 방지). "
                f"직전 오류: {self._last_auth_error}"
            )

    def _mark_auth_failure(self, err: Exception) -> None:
        self._auth_blocked_until = time.time() + AUTH_COOLDOWN_SEC
        self._last_auth_error = f"{type(err).__name__}: {err}"

    @staticmethod
    def _looks_like_auth_error(err: Exception) -> bool:
        text = f"{type(err).__name__} {err}".lower()
        return any(h in text for h in _AUTH_HINTS)

    # ---- 본체 ----
    def fetch_tick(self) -> dict:
        self._check_cooldown()
        self._check_credentials()

        # pykrx 는 import 시점에 KRX 로그인을 시도한다 -> import 실패도 인증 실패로 취급.
        try:
            from pykrx import stock
        except Exception as e:  # noqa: BLE001
            self._mark_auth_failure(e)
            raise ProviderAuthError(
                f"pykrx 로드/KRX 로그인 실패: {type(e).__name__}: {e} "
                "(KRX_ID/KRX_PW 확인, 점검 시간대인지 확인)"
            ) from e

        end = datetime.now()
        start = end - timedelta(days=7)
        end_s, start_s = end.strftime("%Y%m%d"), start.strftime("%Y%m%d")

        def _load_kospi() -> pd.DataFrame:
            """당일 단일 조회가 비어 있는 경우를 대비해 최근 7일 범위로 안전하게 재조회"""
            # pykrx는 호출 방식이 버전별로 달라서 두 형태를 모두 시도한다.
            attempts = [
                lambda: stock.get_index_ohlcv(start_s, end_s, "1001"),
                lambda: stock.get_index_ohlcv_by_date(start_s, end_s, "1001"),
            ]
            last_error = None
            for attempt in attempts:
                try:
                    df = attempt()
                    if df is not None and not getattr(df, "empty", True):
                        return df
                except Exception as e:  # noqa: BLE001
                    last_error = e
            if last_error is not None:
                raise last_error
            return pd.DataFrame()

        try:
            kospi = _load_kospi()
        except Exception as e:  # noqa: BLE001
            if self._looks_like_auth_error(e):
                self._mark_auth_failure(e)
                raise ProviderAuthError(f"KRX 조회 중 인증 오류: {type(e).__name__}: {e}") from e
            raise ProviderError(f"KRX 조회 실패: {type(e).__name__}: {e}") from e

        if kospi is None or kospi.empty:
            raise ProviderError("KRX 응답이 비어있음 (휴장일 또는 장 시작 전/API 지연)")

        now = datetime.now()
        spot = float(kospi["종가"].iloc[-1])

        # ---- basis ----
        # 기존 코드는 (KOSPI - 미국 ES=F 선물)로 basis 를 만들었으나, 두 값은 단위/수준이
        # 달라 의미 있는 basis 가 아니다. KRX 전용 모드에서는 중립값(0.0)을 쓰고,
        # 진짜 KOSPI200 선물 시세는 증권사 API(한투/키움) 추가 시 채운다.
        # 예전 동작이 필요하면 USE_YF_BASIS_PROXY=1 (yfinance 필요).
        futures_basis = 0.0
        if os.environ.get("USE_YF_BASIS_PROXY", "0") == "1":
            futures_basis = self._yf_basis_proxy(spot)

        self._basis_history.append(futures_basis)
        self._basis_history = self._basis_history[-200:]
        hist = np.array(self._basis_history)
        basis_mean = float(hist.mean()) if len(hist) > 1 else futures_basis
        basis_std = float(hist.std()) if len(hist) > 1 else 1.0
        basis_zscore = round((futures_basis - basis_mean) / basis_std, 2) if basis_std > 0 else 0.0

        # ---- 대장주 TOP_N 동적 산정 + 개별 델타 ----
        leaders, leader_event = self._get_leaders(stock)
        if leaders:
            leader_delta = round(
                sum(l["delta_pct"] * l["weight"] for l in leaders), 5
            )
        else:
            # 산정 실패(휴장/오류) 시 기존처럼 0.0으로 안전하게 떨어뜨림
            leader_delta = 0.0

        # ---- 1시간 델타 / 가속도 (폴링값 누적 근사) ----
        self._spot_history.append((now, spot))
        cutoff = now - timedelta(hours=1)
        self._spot_history = [(t, p) for (t, p) in self._spot_history if t >= cutoff]
        hs = self._spot_history
        market_delta_1h = accel = 0.0
        if len(hs) >= 2 and hs[0][1]:
            market_delta_1h = round((hs[-1][1] - hs[0][1]) / hs[0][1], 5)
        if len(hs) >= 3:
            mid = len(hs) // 2
            if hs[0][1] and hs[mid][1]:
                first = (hs[mid][1] - hs[0][1]) / hs[0][1]
                second = (hs[-1][1] - hs[mid][1]) / hs[mid][1]
                accel = round(second - first, 5)

        # bid_volume(선물 잔량 프락시): 기존엔 삼성전자 거래량을 하드코딩해서 썼으나,
        # 이제 대장주가 동적이므로 "현재 1위 종목"의 거래량을 그대로 재사용한다.
        # leaders 산정 자체가 실패했을 때는 0.0으로 안전하게 떨어뜨린다.
        volume = float(leaders[0]["volume"]) if leaders else 0.0

        return {
            "spot": spot,
            "futures_basis": futures_basis,
            "basis_zscore": basis_zscore,
            "leader_delta": leader_delta,
            "market_delta_1h": market_delta_1h,
            "acceleration_1h": accel,
            "recent_2d_avg_basis": round(basis_mean, 3),
            "bid_volume": volume,
            "foreign_net_futures": 0.0,      # TODO: 증권사 API 또는 pykrx 투자자별 거래
            "corp_straddle_iv_diff": 0.0,    # TODO: 옵션 데이터 벤더
            "timestamp": now.isoformat(timespec="seconds"),
            "leaders": leaders,              # [v3.1 신규] TOP_N 종목별 상세
            "leader_event": leader_event,    # [v3.1 신규] "OO → OO" 또는 None
        }

    # ---- 대장주 동적 산정 (Slow Loop 60초 + Fast Loop 매 틱 가격 조회) ----
    def _get_leaders(self, stock) -> tuple[list[dict], "str | None"]:
        """
        1) LEADER_RANK_CHECK_INTERVAL_SEC(60초)마다: 시총 상위 LEADER_TOP_N 종목코드 재산정
        2) 매 틱마다: 확정된 종목코드들의 최신 가격/등락률 조회 (여기가 버그였던 부분 --
           7일치를 조회해 전일 종가를 확보해야 pct_change가 계산됨)
        실패해도 예외를 던지지 않고 ([], None)으로 안전하게 떨어뜨린다 (leader_delta는
        호출부에서 0.0으로 폴백).
        """
        now_ts = time.time()
        if not self._leader_tickers or (now_ts - self._leader_last_check) >= LEADER_RANK_CHECK_INTERVAL_SEC:
            try:
                self._refresh_leader_ranking(stock)
                self._leader_last_check = now_ts
            except Exception:
                # 순위 재확인 실패해도 직전에 확정된 종목이 있으면 그걸로 계속 진행
                # (첫 실행부터 실패하면 self._leader_tickers가 비어 있으므로 아래에서 걸러짐)
                pass

        if not self._leader_tickers:
            return [], None

        try:
            leaders = self._fetch_leader_details(stock, self._leader_tickers)
        except Exception:
            return [], None

        leader_event = self._pending_leader_event
        self._pending_leader_event = None  # 한 번 표시하고 소비
        return leaders, leader_event

    def _refresh_leader_ranking(self, stock) -> None:
        """LEADER_MARKET 시총 상위 LEADER_TOP_N 종목코드를 재산정하고,
        직전 구성과 달라졌으면 self._pending_leader_event에 "OO → OO" 문자열을 남긴다."""
        today = datetime.now().strftime("%Y%m%d")
        cap_df = stock.get_market_cap_by_ticker(today, market=LEADER_MARKET)
        if cap_df is None or cap_df.empty:
            raise ProviderError("시가총액 순위 조회 실패 (휴장일 또는 응답 없음)")

        ranked = cap_df.sort_values(by="시가총액", ascending=False)
        new_tickers = list(ranked.index[:LEADER_TOP_N])

        prev_tickers = self._leader_tickers
        if prev_tickers and prev_tickers != new_tickers:
            def _name(t: str) -> str:
                try:
                    return stock.get_market_ticker_name(t)
                except Exception:  # noqa: BLE001
                    return t
            old_label = "/".join(_name(t) for t in prev_tickers)
            new_label = "/".join(_name(t) for t in new_tickers)
            self._pending_leader_event = f"{old_label} → {new_label}"

        self._leader_tickers = new_tickers

    def _fetch_leader_details(self, stock, tickers: list[str]) -> list[dict]:
        """확정된 종목코드들의 현재가/전일종가/등락률/시총비중을 조회.
        [버그 수정 핵심] 당일 하루치가 아니라 LEADER_LOOKBACK_DAYS일 범위로 조회해야
        종가 컬럼이 2행 이상 확보되어 pct_change()가 정상 계산된다."""
        end = datetime.now()
        start = end - timedelta(days=LEADER_LOOKBACK_DAYS)
        end_s, start_s = end.strftime("%Y%m%d"), start.strftime("%Y%m%d")

        today = end.strftime("%Y%m%d")
        cap_df = stock.get_market_cap_by_ticker(today, market=LEADER_MARKET)
        total_cap = float(cap_df.loc[tickers, "시가총액"].sum()) if cap_df is not None and not cap_df.empty else 0.0

        leaders: list[dict] = []
        for ticker in tickers:
            ohlcv = stock.get_market_ohlcv(start_s, end_s, ticker)
            if ohlcv is None or ohlcv.empty:
                continue
            close = ohlcv["종가"]
            price = float(close.iloc[-1])
            prev_close = float(close.iloc[-2]) if len(close) >= 2 else price
            delta_pct = round((price - prev_close) / prev_close, 5) if prev_close else 0.0
            volume = float(ohlcv["거래량"].iloc[-1]) if "거래량" in ohlcv else 0.0
            try:
                name = stock.get_market_ticker_name(ticker)
            except Exception:  # noqa: BLE001
                name = ticker
            market_cap = (
                float(cap_df.loc[ticker, "시가총액"])
                if cap_df is not None and ticker in cap_df.index else 0.0
            )
            weight = (market_cap / total_cap) if total_cap else (1.0 / len(tickers))

            leaders.append({
                "ticker": ticker,
                "name": name,
                "price": price,
                "prev_close": prev_close,
                "delta_pct": delta_pct,
                "market_cap": market_cap,
                "weight": round(weight, 4),
                "volume": volume,
            })
        return leaders

    @staticmethod
    def _yf_basis_proxy(spot: float) -> float:
        try:
            import yfinance as yf
            es = yf.Ticker("ES=F").history(period="1d", interval="1m")
            if es.empty:
                raise ProviderError("yfinance ES=F 응답이 비어있음")
            last = float(es["Close"].iloc[-1])
            return round((spot - last) / spot * 100, 3) if spot else 0.0
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"yfinance 실패: {type(e).__name__}: {e}") from e
