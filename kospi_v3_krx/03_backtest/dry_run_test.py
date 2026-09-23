# ==========================================================================
# @module: dry_run_test
# @type: backtest (validation harness)
# @version: v2
# @depends: 02_engine/kospi_engine_v3_realtime.py, 04_data/mock_market_data.py
# @status: dev
#
# 코스피 v3 통합판 전용으로 수정한 Dry-Run 하네스.
#
# 원본(업로드본) 대비 바뀐 점:
#   1. mock_feed()가 {"volume","price_delta","symbol"}이 아니라 실제
#      tick 스키마(generate_mock_tick())를 생성하도록 교체.
#   2. process_fn이 임의의 스코어 계산이 아니라, 실제 엔진의
#      process_tick_core()를 그대로 호출 (dry-run이 실제 판정 로직을
#      검증하도록). guard_call은 process_tick_core 내부의 개별 함수
#      (decide_target_level, run_burst_gate_core, 시나리오 엔진)에
#      이미 걸려 있으므로 여기서 추가로 감쌀 필요 없음.
#   3. gate/prev_level/prev_bid_vol 상태를 사이클 간에 이어받도록
#      클로저로 유지 (Streamlit 세션 상태 대신 이 스크립트가 직접 관리).
#
# 실행: python 03_backtest/dry_run_test.py --cycles 240 --interval 15
# (240회 * 15초 = 1시간 세션 시뮬레이션)
# ==========================================================================
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

import numpy as np
import pandas as pd

# --- 로컬 모듈 경로 등록 (02_engine, 04_data) ---
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(_THIS_DIR, "..", "02_engine"))
sys.path.append(os.path.join(_THIS_DIR, "..", "04_data"))

from mock_market_data import generate_mock_tick  # noqa: E402
from burst_early_warning import BurstEarlyWarningGate  # noqa: E402
from kospi_engine_v3_realtime import process_tick_core  # noqa: E402
from guard_utils import GUARD_STATS  # noqa: E402


@dataclass
class DryRunReport:
    total_cycles: int = 0
    exceptions: list[str] = field(default_factory=list)
    rss_samples_mb: list[float] = field(default_factory=list)
    hard_flat_count: int = 0
    order_block_count: int = 0
    burst_warning_count: int = 0

    def add_exception(self, cycle: int, err: Exception) -> None:
        self.exceptions.append(f"[cycle {cycle}] {type(err).__name__}: {err}")

    def is_pass(self, memory_growth_threshold_mb: float = 50.0) -> bool:
        if self.exceptions:
            return False
        if len(self.rss_samples_mb) >= 2:
            growth = self.rss_samples_mb[-1] - self.rss_samples_mb[0]
            if growth > memory_growth_threshold_mb:
                return False
        return True

    def print_summary(self) -> None:
        print("\n" + "=" * 60)
        print("DRY-RUN 검증 결과 (코스피 v3 통합판)")
        print("=" * 60)
        print(f"총 사이클: {self.total_cycles}")
        print(f"예외 발생 횟수: {len(self.exceptions)}")
        for e in self.exceptions[:10]:
            print(f"  - {e}")
        print(f"Hard Flat 발동 횟수: {self.hard_flat_count}")
        print(f"신규 주문 차단(order_block) 횟수: {self.order_block_count}")
        print(f"Burst Warning 발동 횟수: {self.burst_warning_count}")
        if self.rss_samples_mb:
            print(f"메모리(RSS) 시작: {self.rss_samples_mb[0]:.1f} MB, "
                  f"종료: {self.rss_samples_mb[-1]:.1f} MB, "
                  f"증가량: {self.rss_samples_mb[-1] - self.rss_samples_mb[0]:.1f} MB")
        else:
            print("메모리 측정 불가 (psutil 미설치)")
        fallback = GUARD_STATS.summary()["fallback_count"]
        print(f"guard_utils 폴백 발생: {fallback if fallback else '없음'}")
        verdict = "✅ PASS - 실전 투입 가능" if self.is_pass() else "❌ FAIL - 실전 투입 보류, 원인 확인 필요"
        print(f"\n최종 판정: {verdict}")
        print("=" * 60)


def load_mirror_feed(path: str):
    """
    미러링된 원시 tick 로그(jsonl, generate_mock_tick()과 동일 스키마)를
    순차 재생. 실전 mirror 로그를 만들려면 fetch_market_data()가 반환하는
    tick 자체를 별도 jsonl로 남기는 로거를 붙여야 한다 (prediction_log*.jsonl은
    가공된 record라 스키마가 다르므로 그대로 재사용 불가).
    """
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    if not lines:
        raise ValueError(f"미러 로그 파일이 비어있습니다: {path}")
    idx = 0
    while True:
        if idx >= len(lines):
            random.shuffle(lines)
            idx = 0
        yield json.loads(lines[idx])
        idx += 1


def kospi_mock_feed():
    """실제 tick 스키마(11개 필드)를 그대로 생성하는 합성 피드."""
    while True:
        yield generate_mock_tick()


def make_process_fn():
    """
    사이클 간 상태(gate/prev_level/prev_bid_vol/mock_returns)를 클로저로
    유지하면서, 실제 엔진의 process_tick_core()를 그대로 호출하는
    process_fn을 만든다. Streamlit 세션 상태가 하던 일을 이 클로저가 대신한다.
    """
    state = {
        "gate": BurstEarlyWarningGate(),
        "prev_level": 0.0,
        "prev_bid_vol": 10000.0,
        "mock_returns": pd.Series(np.random.normal(0, 0.01, 100)),
    }

    def process_fn(tick: dict):
        record = process_tick_core(
            tick=tick,
            data_source_note="dry-run 합성 피드",
            prev_level=state["prev_level"],
            prev_bid_vol=state["prev_bid_vol"],
            mock_returns=state["mock_returns"],
            burst_gate=state["gate"],
            now=datetime.now(),
        )
        state["prev_level"] = record["current_position"]
        state["prev_bid_vol"] = record.pop("_new_bid_vol")
        record.pop("_burst_result", None)
        return record

    return process_fn


def run_dry_run(process_fn, cycles: int, interval: float, feed_source, report_hook=None) -> DryRunReport:
    report = DryRunReport()
    proc = psutil.Process(os.getpid()) if _HAS_PSUTIL else None

    for cycle in range(1, cycles + 1):
        report.total_cycles = cycle
        payload = next(feed_source)
        try:
            record = process_fn(payload)
            if report_hook is not None and record is not None:
                report_hook(report, record)
        except Exception as e:
            report.add_exception(cycle, e)
            traceback.print_exc()

        if proc is not None:
            rss_mb = proc.memory_info().rss / (1024 * 1024)
            report.rss_samples_mb.append(rss_mb)

        if cycle % 20 == 0 or cycle == cycles:
            print(f"[dry-run] cycle {cycle}/{cycles} 완료 "
                  f"(예외 {len(report.exceptions)}건 누적)")

        time.sleep(interval)

    return report


def _update_report_from_record(report: DryRunReport, record: dict) -> None:
    if record.get("hard_flat_reason"):
        report.hard_flat_count += 1
    if record.get("order_block"):
        report.order_block_count += 1
    if record.get("confidence_verdict") is None:
        return


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="코스피 v3 통합판 Dry-Run 샌드박스 검증")
    parser.add_argument("--cycles", type=int, default=20, help="반복 횟수 (기본 20)")
    parser.add_argument("--interval", type=float, default=1.0,
                         help="주기(초). 실전은 15 권장, 테스트는 짧게")
    parser.add_argument("--mirror-log", type=str, default=None,
                         help="미러링된 원시 tick jsonl 경로 (없으면 합성 모의데이터 사용)")
    args = parser.parse_args()

    feed = load_mirror_feed(args.mirror_log) if args.mirror_log else kospi_mock_feed()
    process_fn = make_process_fn()

    report = run_dry_run(
        process_fn, cycles=args.cycles, interval=args.interval,
        feed_source=feed, report_hook=_update_report_from_record,
    )
    report.print_summary()
