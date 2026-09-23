# ==========================================================================
# @module: guard_utils
# @type: engine (cross-cutting)
# @version: v1
# @depends: (없음, 표준 라이브러리만 사용)
# @status: stable
#
# guard_utils.py
# 장중감시 코어 로직(예: signal 버스트 스코어 계산, API 파싱)을 감싸는
# 런타임 방어 유틸리티. 신규 로직이 예외를 던지거나 API가 죽어도
# 콕핏 전체가 크래시하지 않고 "기본값(Default Score)"으로 하드 플랫되도록 함.
#
# 사용 예:
#   @safe_call(default=0.5, name="burst_score")
#   def compute_burst_score(payload: dict) -> float:
#       ...실전 로직...
# ==========================================================================
from __future__ import annotations

import functools
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("cockpit.guard")
logging.basicConfig(level=logging.INFO)


class GuardStats:
    """폴백 발생 횟수를 추적해서 대시보드에 '현재 몇 번 하드플랫 됐는지' 표시할 수 있게 함."""
    def __init__(self) -> None:
        self.fallback_count: dict[str, int] = {}
        self.last_error: dict[str, str] = {}

    def record(self, name: str, error: Exception) -> None:
        self.fallback_count[name] = self.fallback_count.get(name, 0) + 1
        self.last_error[name] = f"{type(error).__name__}: {error}"

    def summary(self) -> dict:
        return {
            "fallback_count": dict(self.fallback_count),
            "last_error": dict(self.last_error),
        }


GUARD_STATS = GuardStats()


def safe_call(
    default: Any = 0.5,
    name: Optional[str] = None,
    max_retries: int = 0,
    retry_delay_sec: float = 0.5,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
):
    """
    함수를 try-except로 감싸서, 예외 발생 시 크래시 대신 default 값을 반환.
    max_retries > 0 이면 실패 시 짧게 재시도 후 그래도 실패하면 default로 하드 플랫.

    - default=0.5 : 스코어류(신뢰도 등) 기본값
    - default=0   : 신호 강도/수량류 기본값
    - default=None: 파싱 결과 없음을 명시적으로 표현하고 싶을 때
    """
    def decorator(func: Callable) -> Callable:
        fn_name = name or func.__name__

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    attempt += 1
                    if attempt <= max_retries:
                        logger.warning(
                            "[%s] 예외 발생 (재시도 %d/%d): %s",
                            fn_name, attempt, max_retries, e,
                        )
                        time.sleep(retry_delay_sec)
                        continue
                    GUARD_STATS.record(fn_name, e)
                    logger.error(
                        "[%s] 하드 플랫 발동 → 기본값(%r) 반환. 원인: %s",
                        fn_name, default, e,
                    )
                    return default
        return wrapper
    return decorator


def safe_network_call(timeout_sec: float = 3.0, default: Any = None, name: Optional[str] = None):
    """
    네트워크/API 호출 전용 래퍼. requests 등에서 발생하는 Timeout/ConnectionError까지
    별도로 잡아서, 네트워크 단절 시에도 콕핏이 멈추지 않고 즉시 폴백하도록 함.
    """
    import requests  # 지역 임포트: guard_utils 자체는 requests 미설치 환경에서도 임포트 가능하게

    def decorator(func: Callable) -> Callable:
        fn_name = name or func.__name__

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, timeout=timeout_sec, **kwargs)
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.RequestException) as e:
                GUARD_STATS.record(fn_name, e)
                logger.error("[%s] 네트워크 단절/오류 → 폴백(%r). 원인: %s", fn_name, default, e)
                return default
            except Exception as e:
                GUARD_STATS.record(fn_name, e)
                logger.error("[%s] 알 수 없는 예외 → 폴백(%r). 원인: %s", fn_name, default, e)
                return default
        return wrapper
    return decorator


def validate_payload_or_default(payload: dict, required_keys: list[str], default: Any = 0):
    """
    잘못된 데이터 포맷(필수 키 누락, 타입 불일치)을 즉시 감지해서 기본값으로 떨어뜨림.
    파싱 로직 진입 전 1차 방어선으로 사용.
    """
    if not isinstance(payload, dict):
        logger.warning("payload가 dict가 아님 (type=%s) → 기본값(%r) 사용", type(payload), default)
        return default
    missing = [k for k in required_keys if k not in payload]
    if missing:
        logger.warning("필수 키 누락 %s → 기본값(%r) 사용", missing, default)
        return default
    return payload


# --------------------------------------------------------------------------
# 데모: 실제 코어 로직에 적용하는 예시
# --------------------------------------------------------------------------
if __name__ == "__main__":
    @safe_call(default=0.5, name="burst_score", max_retries=1)
    def compute_burst_score(payload: dict) -> float:
        payload = validate_payload_or_default(payload, ["volume", "price_delta"], default=None)
        if payload is None:
            raise ValueError("잘못된 payload 포맷")
        return payload["volume"] * payload["price_delta"]

    print("정상 케이스:", compute_burst_score({"volume": 10, "price_delta": 0.02}))
    print("잘못된 포맷 케이스 (폴백 발동):", compute_burst_score({"bad": "data"}))
    print("완전히 이상한 타입 (폴백 발동):", compute_burst_score("not-a-dict"))
    print("가드 통계:", GUARD_STATS.summary())
