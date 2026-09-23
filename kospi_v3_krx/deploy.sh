#!/usr/bin/env bash
# ==========================================================================
# deploy.sh - 장중감시 프로그램(v1 signal 버스트) 원클릭 배포/롤백 스크립트
#
# 사용법:
#   ./deploy.sh deploy <git-ref>   예) ./deploy.sh deploy feature/burst-gate-v3
#   ./deploy.sh rollback           예) ./deploy.sh rollback   (stable 태그로 즉시 복귀)
#   ./deploy.sh status             현재 실행 중인 버전/PID 확인
#
# 안전장치:
#   1) 장중(09:00~15:30 KST, 평일)에는 --force 없이는 배포 자체를 막음
#   2) 배포 전 현재 실행중인 프로세스를 PID 파일 기준으로 정상 종료(SIGTERM) 후
#      3초 대기, 안죽으면 SIGKILL
#   3) 신규 버전 기동 후 지정 포트에 healthcheck(HTTP 200) 확인, 실패시 자동 롤백
#   4) 배포 시점의 git ref를 로그 파일에 남겨 감사 추적 가능
# ==========================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ENTRY="02_engine/kospi_engine_v3_realtime.py"   # 코스피 v3 통합판 엔트리포인트
PORT=8501
PID_FILE="${APP_DIR}/.run/app.pid"
BACKUP_BRANCH="stable"             # 항상 안전하게 돌아갈 브랜치/태그
LOG_FILE="${APP_DIR}/logs/deploy.log"
HEALTHCHECK_URL="http://127.0.0.1:${PORT}/_stcore/health"
HEALTHCHECK_RETRIES=10
HEALTHCHECK_INTERVAL=1

mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# --------------------------------------------------------------------------
# 1) 장중 시간 체크 (KST 기준, 평일 09:00~15:30). --force 옵션으로 강제 우회 가능.
# --------------------------------------------------------------------------
check_market_hours() {
  local force_flag="${1:-}"
  local now_hm now_dow tz_now

  tz_now=$(TZ="Asia/Seoul" date +"%H:%M")
  now_dow=$(TZ="Asia/Seoul" date +"%u")   # 1=월 ... 7=일

  if [[ "$force_flag" == "--force" ]]; then
    log "⚠️  --force 지정됨: 장중 여부와 관계없이 배포를 강행합니다."
    return 0
  fi

  if (( now_dow >= 1 && now_dow <= 5 )); then
    if [[ "$tz_now" > "09:00" && "$tz_now" < "15:30" ]]; then
      log "🚫 현재 장중입니다 (KST ${tz_now}). 장 마감 후 또는 --force 옵션으로만 배포 가능합니다."
      exit 1
    fi
  fi
  log "✅ 장중 시간 아님(KST ${tz_now}). 배포를 진행합니다."
}

# --------------------------------------------------------------------------
# 2) 기존 프로세스 안전 종료
# --------------------------------------------------------------------------
stop_current() {
  if [[ -f "$PID_FILE" ]]; then
    local old_pid
    old_pid=$(cat "$PID_FILE")
    if kill -0 "$old_pid" 2>/dev/null; then
      log "기존 프로세스(PID=${old_pid}) 종료 요청(SIGTERM)..."
      kill -TERM "$old_pid" || true
      for _ in 1 2 3; do
        sleep 1
        kill -0 "$old_pid" 2>/dev/null || break
      done
      if kill -0 "$old_pid" 2>/dev/null; then
        log "정상 종료 실패, SIGKILL 강제 종료."
        kill -KILL "$old_pid" || true
      fi
    fi
    rm -f "$PID_FILE"
  else
    log "실행 중인 PID 파일 없음 (최초 실행이거나 이미 종료됨)."
  fi
}

# --------------------------------------------------------------------------
# 3) 신규 버전 기동
# --------------------------------------------------------------------------
start_new() {
  local ref="$1"
  cd "$APP_DIR"

  log "git ref '${ref}' 로 체크아웃..."
  git fetch --all --quiet
  git checkout "$ref" --quiet

  # .env 가 있으면 환경변수로 읽어서 앱에 전달 (KRX_ID/KRX_PW 등, 값은 로그에 찍지 않음)
  if [[ -f "${APP_DIR}/.env" ]]; then
    set -a; source "${APP_DIR}/.env"; set +a
    log ".env 로드됨 (키 이름만: $(grep -oE '^[A-Z_]+=' "${APP_DIR}/.env" | tr -d '=' | tr '\n' ' '))"
  else
    log "⚠️ .env 없음 -> KRX 로그인 정보가 없으면 모의데이터로 동작합니다."
  fi

  log "Streamlit 앱 기동 (port=${PORT})..."
  nohup streamlit run "$APP_ENTRY" \
      --server.port "$PORT" \
      --server.headless true \
      > "${APP_DIR}/logs/app_stdout.log" 2>&1 &

  local new_pid=$!
  echo "$new_pid" > "$PID_FILE"
  log "신규 프로세스 기동됨 (PID=${new_pid}, ref=${ref})"
}

# --------------------------------------------------------------------------
# 4) 헬스체크 - 실패 시 자동 롤백
# --------------------------------------------------------------------------
healthcheck() {
  local i
  for (( i=1; i<=HEALTHCHECK_RETRIES; i++ )); do
    if curl -sf "$HEALTHCHECK_URL" > /dev/null 2>&1; then
      log "✅ 헬스체크 성공 (시도 ${i}/${HEALTHCHECK_RETRIES})"
      return 0
    fi
    sleep "$HEALTHCHECK_INTERVAL"
  done
  log "❌ 헬스체크 실패. 자동 롤백을 수행합니다."
  return 1
}

# --------------------------------------------------------------------------
# 메인 커맨드 분기
# --------------------------------------------------------------------------
cmd="${1:-}"

case "$cmd" in
  deploy)
    ref="${2:?git ref(브랜치/태그)를 지정하세요. 예: ./deploy.sh deploy feature/burst-gate-v3}"
    force_flag="${3:-}"
    check_market_hours "$force_flag"
    stop_current
    start_new "$ref"
    if ! healthcheck; then
      log "롤백 시작: ${BACKUP_BRANCH} 으로 복귀합니다."
      stop_current
      start_new "$BACKUP_BRANCH"
      healthcheck || log "🔥 CRITICAL: 백업 버전조차 헬스체크 실패. 수동 확인 필요."
      exit 1
    fi
    log "🎉 배포 완료 (ref=${ref}, port=${PORT})"
    ;;

  rollback)
    force_flag="${2:-}"
    check_market_hours "$force_flag"
    stop_current
    start_new "$BACKUP_BRANCH"
    healthcheck && log "🎉 롤백 완료 (${BACKUP_BRANCH})" || log "🔥 롤백 후에도 헬스체크 실패. 수동 확인 필요."
    ;;

  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      cd "$APP_DIR"
      echo "실행 중: PID=$(cat "$PID_FILE"), branch=$(git rev-parse --abbrev-ref HEAD), port=${PORT}"
    else
      echo "실행 중인 프로세스 없음."
    fi
    ;;

  *)
    echo "사용법: $0 {deploy <git-ref> [--force] | rollback [--force] | status}"
    exit 1
    ;;
esac
