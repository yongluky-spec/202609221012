# 장중 감시 퀀트 콕핏 v3 (코스피 통합판)

실시간(틱 단위) 포지션 감시 및 자동 헤지 판단 프로그램. "시장분석"(일봉 대시보드,
`kospi_model.py`/`kospi_engine.py` 계열) 프로그램과는 **별개**의 프로젝트입니다.

## 폴더 구조 (naming_master_v1.md 규칙 적용)

```
02_engine/
    kospi_engine_v3_realtime.py   ← 실행 엔트리포인트
    burst_early_warning.py        ← 5번째 게이트 (FinalBurstGate 패치 적용됨, 아래 참고)
    guard_utils.py                ← 런타임 방어(폴백/하드플랫) 유틸리티
03_backtest/
    dry_run_test.py               ← 배포 전 검증용 dry-run 하네스
04_data/
    mock_market_data.py           ← 모의 데이터 생성기 (모든 공급자 실패 시 폴백, 화면에 빨간 경고)
    providers/                    ← 시세 공급자 (krx 완료 / kis, kiwoom 스켈레톤)
99_archive/
    kospi_model_v1_492_mock.py    ← 통합 전 원본(492줄) 보관
deploy.sh                         ← 배포/롤백/헬스체크 스크립트 (.env 자동 로드)
.env.example                      ← 환경변수 견본 (.env 로 복사해서 사용)
requirements.txt
```

## 설치 및 실행

```bash
pip install -r requirements.txt
chmod +x deploy.sh
```

## 데이터 공급자 설정 (KRX 먼저, 한투/키움은 추후)

이 프로그램은 증권사 계좌가 아니라 **KRX 데이터 포털 로그인**이 필요합니다.
KRX 포털이 로그인 필수로 바뀌어서, 계정 정보 없이는 "거래소 인증 실패" → 모의데이터로 떨어집니다.

1. data.krx.co.kr 에서 회원가입/로그인 계정 준비
2. `.env.example` 을 `.env` 로 복사하고 `KRX_ID`, `KRX_PW` 입력 (`.env` 는 git 제외됨)
3. `./deploy.sh deploy stable` (또는 같은 터미널에서 streamlit 실행)
4. 화면 상단에 빨간 "모의데이터" 경고가 없으면 실전 연동 성공

- 인증 실패 후 5분간은 재시도하지 않습니다 (계정 잠금 방지).
- KRX 는 **일봉** 데이터라 장중 틱이 아닙니다. 장중 델타/가속도는 폴링 값을 누적해 근사합니다.
- basis 는 KRX 전용 모드에서 중립값(0.0)입니다. 예전 ES=F 프록시는 단위가 달라 의미가 없어서 기본 꺼짐
  (`USE_YF_BASIS_PROXY=1` 로 복원 가능, yfinance 필요).
- `foreign_net_futures`, `corp_straddle_iv_diff` 는 여전히 0.0 고정입니다.

### 한국투자증권 / 키움증권 추가 방법
`04_data/providers/kis_provider.py`, `kiwoom_provider.py` 의 체크리스트를 채워 `fetch_tick()` 만 구현하면 됩니다.
엔진 수정은 필요 없습니다. 우선순위는 `DATA_PROVIDER=kis,krx` 처럼 지정합니다 (앞 순위 실패 시 다음으로).
키움 OpenAPI+ 는 윈도우/32비트/로그인 창이 필요해서 streamlit 서버와 분리된 구조가 필요합니다.

### 배포 전 검증 (권장)
```bash
python3 03_backtest/dry_run_test.py --cycles 240 --interval 15
```
`✅ PASS` 확인 후 배포 진행.

### 실행
```bash
git branch stable          # 최초 1회, 안전하게 돌아갈 기준 브랜치 고정
./deploy.sh deploy stable
```
브라우저에서 `http://localhost:8501` 접속.

### 롤백 / 상태 확인
```bash
./deploy.sh rollback
./deploy.sh status
```

## 구성 (기존 4대 방어 쉴드 + 2개 셔터 + v3 신규 2종)

1. 등호비교 엡실론(EPSILON) — 경계값 근처 핑퐁 매매 차단
2. EWMA 변동성 폴백 — GARCH 실패 시 자동 대체
3. Basis 노이즈 블렌딩 — 극단 괴리(Z-score) 완화
4. 레벨 히스테리시스 — 어제 포지션 관성 유지
5. 델타 마름(Delta Fading) 감지 — 상승 동력 고갈 포착
6. 대장주 디커플링 Hard Flat — 이상 이탈 시 즉시 0% 강제
7. 장중 시나리오 확률/신뢰도 검증 엔진 — 3대 시나리오 소프트맥스 확률화 + 신뢰도 검증
8. **Burst Early Warning (5번째 게이트, FinalBurstGate)** — 아래 참고

## Burst Early Warning — FinalBurstGate (2026-09-20 패치 적용)

사후 검증 결과, 아래는 노이즈 증폭 + 사후 지연(1~4분) + 표본 부족 오버핏이
확인되어 **전면 폐기**했습니다.
- Jerk(저크), HMM, 딥러닝 기반 예측
- 시그모이드 합성 스코어, 버킷별 가중치(w1/w2/w3)

실전 채택된 유일한 규칙:
```
is_compressed = cr_z < -1.0        (버킷별 IQR 정규화, 고정 0.7 아님)
is_vol_burst  = volume_ratio >= 1.8
if is_compressed and is_vol_burst:
    → BURST_WARNING
```

이 패치 적용 과정에서 `compute_cr()`의 거래량 항이 분자에 잘못 들어가 있던
버그도 함께 수정했습니다 (분모여야 거래량 급증 시 CR이 더 낮아져 "응축"과
"거래량 폭발"이 같은 틱에서 동시에 성립 가능). 자세한 내용은
`02_engine/burst_early_warning.py` 상단 주석과 파일 내 `[PATCH v2]` /
`[BUGFIX]` 표기를 참고하세요.

## 한계 (정직하게 밝힘)

- Burst 게이트는 원래 1분봉 스트림을 전제로 설계했으나, 이 화면은 15초 폴링이라
  60초 경과를 직접 체크해서 "1분봉처럼" 근사합니다 (`maybe_push_tick` 참고).
- `foreign_net_futures`/`corp_straddle_iv_diff`는 실전 연동에서도 0.0 고정
  (무료 API로 실시간 취득 어려움).
- ATR14는 GARCH/EWMA 변동성에서 역산한 근사치이며 진짜 ATR14 계산과 다릅니다.

## 안전 원칙

이 프로그램은 매매 신호를 계산해서 보여주는 감시/판단 보조 도구이며, 실제
매매 주문을 대신 실행하지 않습니다. 실계좌 자동매매로 연결하려면 증권사
API의 주문 함수를 별도로 연결해야 하고, 그 전에 반드시 모의투자 계좌로
충분히 검증하시길 권합니다.
