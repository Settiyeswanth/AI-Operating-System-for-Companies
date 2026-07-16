#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# run-local.sh — Start all AI OS application services (Mac / Linux)
# No Docker required — all infrastructure runs as cloud services
# ═══════════════════════════════════════════════════════════════════════════════
#
# PREREQUISITES (all free, no Docker):
#   1. uv:   curl -LsSf https://astral.sh/uv/install.sh | sh
#   2. .env: cp .env.ibmcloud.example .env → fill in credentials
#   3. Verify: uv run python scripts/verify_ibmcloud.py
#
# USAGE:
#   ./run-local.sh                    Start all services
#   ./run-local.sh --stop             Stop all services
#   ./run-local.sh --status           Show running services
#   ./run-local.sh --logs             Tail all logs
#   ./run-local.sh --logs enrichment  Tail one service log
#   ./run-local.sh --help             This message

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/.aios-local-logs"
PID_DIR="$SCRIPT_DIR/.aios-local-pids"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; RESET='\033[0m'; BOLD='\033[1m'

ok()   { echo -e "  ${GREEN}✅${RESET} $1"; }
warn() { echo -e "  ${YELLOW}⚠️ ${RESET} $1"; }
fail() { echo -e "  ${RED}❌${RESET} $1"; }
info() { echo -e "  $1"; }
hdr()  { echo -e "\n${CYAN}${BOLD}═══ $1 ═══${RESET}"; }

ARG="${1:-}"

show_help() {
    echo ""
    echo "run-local.sh — AI OS No-Docker Launcher"
    echo ""
    echo "  ./run-local.sh                    Start all 7 services"
    echo "  ./run-local.sh --stop             Stop all services"
    echo "  ./run-local.sh --status           Show service status"
    echo "  ./run-local.sh --logs             Tail all service logs"
    echo "  ./run-local.sh --logs <service>   Tail one service's log"
    echo "  ./run-local.sh --help             Show this message"
    echo ""
}

# ── Helpers ───────────────────────────────────────────────────────────────────

get_pid_file() { echo "$PID_DIR/$1.pid"; }
get_log_file() { echo "$LOG_DIR/$1.log"; }

is_running() {
    local f; f=$(get_pid_file "$1")
    [[ -f "$f" ]] || return 1
    local pid; pid=$(cat "$f" 2>/dev/null) || return 1
    kill -0 "$pid" 2>/dev/null || { rm -f "$f"; return 1; }
    return 0
}

# ── Stop ──────────────────────────────────────────────────────────────────────

stop_all() {
    hdr "Stopping AI OS Services"
    local stopped=0
    for name in memory agents connectors ingestion enrichment interface gateway; do
        local f; f=$(get_pid_file "$name")
        if [[ -f "$f" ]]; then
            local pid; pid=$(cat "$f" 2>/dev/null)
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                kill "$pid" && ok "Stopped $name (PID $pid)" && ((stopped++)) || true
            else
                info "$name — already stopped"
            fi
            rm -f "$f"
        else
            info "$name — not running"
        fi
    done
    info ""
    info "Stopped $stopped service(s). Cloud services (Neo4j/Qdrant/Redis) are unaffected."
}

# ── Status ────────────────────────────────────────────────────────────────────

show_status() {
    hdr "AI OS Service Status"
    for name in memory agents connectors ingestion enrichment interface gateway; do
        if is_running "$name"; then
            local pid; pid=$(cat "$(get_pid_file "$name")")
            ok "$name  (PID $pid)"
        else
            warn "$name  — stopped"
        fi
    done
    info ""
    info "Interface:  http://localhost:8001"
    info "Gateway:    http://localhost:8000"
    info "Memory API: http://localhost:8002"
}

# ── Logs ──────────────────────────────────────────────────────────────────────

show_logs() {
    local svc="${2:-}"
    if [[ -n "$svc" ]]; then
        local f; f=$(get_log_file "$svc")
        [[ -f "$f" ]] || { fail "No log file for '$svc'"; exit 1; }
        hdr "Tailing: $svc (Ctrl+C to stop)"
        tail -f -n 50 "$f"
    else
        local files=("$LOG_DIR"/*.log)
        [[ -f "${files[0]}" ]] || { warn "No log files yet — start services first"; exit 0; }
        hdr "Tailing all logs (Ctrl+C to stop)"
        tail -f -n 20 "${files[@]}"
    fi
}

# ── Verify cloud services ─────────────────────────────────────────────────────

verify_env() {
    hdr "Verifying .env Configuration"
    local env_file="$SCRIPT_DIR/.env"
    if [[ ! -f "$env_file" ]]; then
        fail ".env file not found"
        info "Run: cp .env.ibmcloud.example .env"
        info "Then fill in your cloud credentials"
        return 1
    fi
    ok ".env file found"

    local ok_count=0 fail_count=0

    check_var() {
        local key="$1" hint="$2"
        local val; val=$(grep "^${key}=" "$env_file" | cut -d= -f2- | tr -d ' ')
        if [[ -n "$val" && "$val" != *"xxxxxxxx"* && "$val" != *"yourpassword"* && "$val" != *"FILL"* ]]; then
            ok "$key is set"
            ((ok_count++))
        else
            fail "$key is not set — $hint"
            ((fail_count++))
        fi
    }

    check_var "IBM_API_KEY"          "cloud.ibm.com → Profile → API keys"
    check_var "WATSONX_PROJECT_ID"   "dataplatform.cloud.ibm.com → project → Manage → General"
    check_var "NEO4J_URI"            "cloud.neo4j.com → instance → Connect"
    check_var "NEO4J_PASSWORD"       "cloud.neo4j.com → instance → credentials dialog"
    check_var "QDRANT_HOST"          "cloud.qdrant.io → cluster → Dashboard → Connection details"
    check_var "QDRANT_API_KEY"       "cloud.qdrant.io → cluster → API Keys"
    check_var "REDIS_URL"            "cloud.ibm.com → Databases for Redis → Credentials → rediss block"

    if [[ $fail_count -gt 0 ]]; then
        info ""
        fail "$fail_count required variable(s) missing. Fill in .env and re-run."
        info "Full guide: IBM-CLOUD-SETUP.md"
        return 1
    fi
    return 0
}

# ── Start ─────────────────────────────────────────────────────────────────────

start_service() {
    local name="$1" subdir="$2" cmd="$3"
    local work_dir="$SCRIPT_DIR/$subdir"
    local log_file; log_file=$(get_log_file "$name")
    local pid_file; pid_file=$(get_pid_file "$name")

    if is_running "$name"; then
        warn "$name already running — skipping"
        return
    fi

    (cd "$work_dir" && eval "uv run $cmd" >> "$log_file" 2>&1) &
    local pid=$!
    echo "$pid" > "$pid_file"

    if [[ "$cmd" == *"uvicorn"* ]]; then
        local port; port=$(echo "$cmd" | grep -oP '(?<=--port )\d+')
        ok "Started $name  (PID $pid)  → http://localhost:$port"
    else
        ok "Started $name  (PID $pid)  → background worker"
    fi
    sleep 0.4
}

main_start() {
    hdr "AI OS for Companies — No-Docker Start"

    # Check uv
    command -v uv &>/dev/null || {
        fail "uv not installed"
        info "Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
    }
    ok "uv found: $(uv --version)"

    # Verify .env
    verify_env || exit 1

    # Create dirs
    mkdir -p "$LOG_DIR" "$PID_DIR"

    # Install dependencies
    info ""
    info "Installing workspace dependencies..."
    cd "$SCRIPT_DIR"
    uv sync --all-packages --quiet
    ok "Dependencies installed"

    # Start services
    info ""
    info "Starting application services..."
    info ""

    start_service "memory"     "services/memory"     "uvicorn memory.main:app --host 0.0.0.0 --port 8002"
    start_service "agents"     "services/agents"     "python -m agents.main"
    start_service "connectors" "services/connectors" "python -m connectors.main"
    start_service "ingestion"  "services/ingestion"  "python -m ingestion.pipeline"
    start_service "enrichment" "services/enrichment" "python -m enrichment.pipeline"
    start_service "interface"  "services/interface"  "uvicorn interface.main:app --host 0.0.0.0 --port 8001"
    start_service "gateway"    "services/gateway"    "uvicorn gateway.main:app --host 0.0.0.0 --port 8000"

    # Wait for HTTP services
    info ""
    info "Waiting for HTTP services..."
    for pair in "8002:memory" "8001:interface" "8000:gateway"; do
        local port="${pair%%:*}" name="${pair##*:}"
        local ready=false
        for _ in $(seq 1 20); do
            sleep 1
            (echo >/dev/tcp/127.0.0.1/"$port") 2>/dev/null && { ready=true; break; } || true
        done
        if $ready; then ok "$name ready → http://localhost:$port"
        else warn "$name not ready yet — check .aios-local-logs/$name.log"
        fi
    done

    # Summary
    hdr "AI OS is Running!"
    info ""
    info "Chat interface:  http://localhost:8001"
    info "API gateway:     http://localhost:8000"
    info "Memory API:      http://localhost:8002"
    info ""
    info "Ingest data (first time):"
    info "  uv run python scripts/backfill.py --days 30"
    info ""
    info "Send a query:"
    echo "  curl -s -X POST http://localhost:8001/v1/chat \\"
    echo '    -H "Authorization: Bearer test" -H "Content-Type: application/json" \'
    echo '    -d '"'"'{"query":"What was worked on this week?","stream":false}'"'"' | python -m json.tool'
    info ""
    info "Commands: ./run-local.sh --stop | --status | --logs"
    info "Log files: $LOG_DIR"
    info ""
    info "Press Ctrl+C to stop all services"

    trap "stop_all; exit 0" INT TERM
    wait
}

# ── Entry point ───────────────────────────────────────────────────────────────

case "$ARG" in
    --help|-h)   show_help; exit 0 ;;
    --stop)      stop_all;  exit 0 ;;
    --status)    show_status; exit 0 ;;
    --logs)      show_logs "$@"; exit 0 ;;
    "")          main_start ;;
    *)           fail "Unknown argument: $ARG"; show_help; exit 1 ;;
esac
