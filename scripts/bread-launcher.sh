#!/usr/bin/env bash
# bread-launcher.sh — Launch and manage the Bread trading bot on macOS.
#
# Combines caffeinate (prevent sleep on AC power) with pmset repeat
# (scheduled wake/sleep) for reliable operation during market hours
# with minimal energy consumption.
#
# Usage:
#   bread-launcher.sh start [-- BREAD_ARGS...]   Start the bot (default: --mode paper --dashboard)
#   bread-launcher.sh stop                        Gracefully stop the bot
#   bread-launcher.sh status                      Show bot status and recent logs
#   bread-launcher.sh schedule                    Set macOS wake/sleep for market hours (requires sudo)
#   bread-launcher.sh unschedule                  Remove scheduled wake/sleep (requires sudo)
#   bread-launcher.sh logs                        Tail today's log file
#
# Notes:
#   - caffeinate -s prevents system sleep while on AC power with lid OPEN.
#     Closing the lid will still sleep the Mac. Keep the lid open and let
#     the display sleep (System Settings > Displays > Turn display off after).
#   - pmset repeat times are in system local time. This script assumes the
#     system timezone matches the bot's trading timezone (America/New_York).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/logs"
PID_FILE="$LOG_DIR/bread.pid"
CAFFEINATE_PID_FILE="$LOG_DIR/caffeinate.pid"

MAX_CONSECUTIVE_CRASHES=5
RESTART_DELAY=10

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

_today_log() {
    echo "$LOG_DIR/bread-$(date +%Y-%m-%d).log"
}

_is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

cmd_start() {
    if _is_running; then
        echo "Bread bot is already running (PID $(cat "$PID_FILE"))."
        echo "Use '$0 stop' first, or '$0 status' to check."
        exit 1
    fi

    mkdir -p "$LOG_DIR"

    # Parse optional bread args after "--"
    local bread_args=(run --mode paper --dashboard)
    if [ $# -gt 0 ]; then
        bread_args=(run "$@")
    fi

    echo "Starting bread ${bread_args[*]}..."
    echo "Logs: $(_today_log)"

    # Launch the restart-loop wrapper in the background
    _run_loop "${bread_args[@]}" &
    local wrapper_pid=$!
    echo "$wrapper_pid" > "$PID_FILE"

    # Tie caffeinate to the wrapper — auto-exits when bot stops
    caffeinate -s -w "$wrapper_pid" &
    echo "$!" > "$CAFFEINATE_PID_FILE"

    echo "Bread bot started (PID $wrapper_pid)."
    echo "caffeinate active (prevents system sleep on AC power)."
}

_run_loop() {
    local bread_args=("$@")
    local crash_count=0

    # Forward SIGTERM to the child bread process, then exit cleanly
    local child_pid=""
    local shutting_down=false

    trap '_shutdown_child' TERM INT

    while true; do
        local log_file
        log_file="$(_today_log)"

        _log "Starting: bread ${bread_args[*]}" >> "$log_file"

        bread "${bread_args[@]}" >> "$log_file" 2>&1 &
        child_pid=$!
        local exit_code=0
        wait "$child_pid" || exit_code=$?

        # Clean exit via our signal handler
        if [ "$shutting_down" = true ]; then
            _log "Bread stopped gracefully." >> "$log_file"
            rm -f "$PID_FILE" "$CAFFEINATE_PID_FILE"
            exit 0
        fi

        # Clean exit from the process itself
        if [ "$exit_code" -eq 0 ]; then
            _log "Bread exited cleanly (exit 0)." >> "$log_file"
            rm -f "$PID_FILE" "$CAFFEINATE_PID_FILE"
            exit 0
        fi

        # Crash — check restart budget
        crash_count=$((crash_count + 1))
        if [ "$crash_count" -ge "$MAX_CONSECUTIVE_CRASHES" ]; then
            _log "FATAL: $crash_count consecutive crashes — giving up." >> "$log_file"
            _log "Check logs and fix the issue, then restart with: $0 start" >> "$log_file"
            rm -f "$PID_FILE" "$CAFFEINATE_PID_FILE"
            exit 1
        fi

        _log "Bread crashed (exit $exit_code, crash #$crash_count/$MAX_CONSECUTIVE_CRASHES). Restarting in ${RESTART_DELAY}s..." >> "$log_file"
        sleep "$RESTART_DELAY"
    done
}

_shutdown_child() {
    shutting_down=true
    if [ -n "${child_pid:-}" ]; then
        kill -TERM "$child_pid" 2>/dev/null || true
        wait "$child_pid" 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

cmd_stop() {
    if ! _is_running; then
        echo "Bread bot is not running."
        rm -f "$PID_FILE" "$CAFFEINATE_PID_FILE"
        exit 0
    fi

    local pid
    pid="$(cat "$PID_FILE")"
    echo "Stopping bread bot (PID $pid)..."

    kill -TERM "$pid" 2>/dev/null || true

    # Wait up to 30s for graceful shutdown
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 30 ]; do
        sleep 1
        waited=$((waited + 1))
    done

    if kill -0 "$pid" 2>/dev/null; then
        echo "Still running after 30s — sending SIGKILL."
        kill -9 "$pid" 2>/dev/null || true
    fi

    # Clean up caffeinate (should have exited via -w, but verify)
    if [ -f "$CAFFEINATE_PID_FILE" ]; then
        local caf_pid
        caf_pid="$(cat "$CAFFEINATE_PID_FILE")"
        kill "$caf_pid" 2>/dev/null || true
    fi

    rm -f "$PID_FILE" "$CAFFEINATE_PID_FILE"
    echo "Bread bot stopped."
}

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

cmd_status() {
    echo "=== Bread Bot Status ==="
    echo

    if _is_running; then
        local pid
        pid="$(cat "$PID_FILE")"
        local uptime
        uptime="$(ps -o etime= -p "$pid" 2>/dev/null | xargs)" || uptime="unknown"
        echo "  Status:    RUNNING"
        echo "  PID:       $pid"
        echo "  Uptime:    $uptime"
    else
        echo "  Status:    STOPPED"
        rm -f "$PID_FILE"  # clean stale PID
    fi

    # caffeinate
    if [ -f "$CAFFEINATE_PID_FILE" ] && kill -0 "$(cat "$CAFFEINATE_PID_FILE")" 2>/dev/null; then
        echo "  Caffeinate: active (sleep prevention on AC)"
    else
        echo "  Caffeinate: inactive"
    fi

    # pmset schedule
    echo
    echo "=== Wake/Sleep Schedule ==="
    pmset -g sched 2>/dev/null || echo "  (unable to read pmset schedule)"

    # Recent logs
    echo
    echo "=== Recent Logs ==="
    local log_file
    log_file="$(_today_log)"
    if [ -f "$log_file" ]; then
        tail -5 "$log_file"
    else
        # Fall back to most recent log
        local latest
        latest="$(ls -t "$LOG_DIR"/bread-*.log 2>/dev/null | head -1)"
        if [ -n "$latest" ]; then
            echo "(from $latest)"
            tail -5 "$latest"
        else
            echo "  No logs found."
        fi
    fi
}

# ---------------------------------------------------------------------------
# schedule / unschedule
# ---------------------------------------------------------------------------

cmd_schedule() {
    echo "Setting macOS wake/sleep schedule for market hours..."
    echo "  Wake:  Mon-Fri 09:25 (5 min before market open)"
    echo "  Sleep: Mon-Fri 16:10 (after daily summary at 16:05)"
    echo
    echo "NOTE: Times are in system local time. This assumes your Mac"
    echo "      timezone matches the trading timezone (America/New_York)."
    echo

    sudo pmset repeat wakeorpoweron MTWRF 09:25:00 sleep MTWRF 16:10:00

    echo "Schedule set. Current schedule:"
    pmset -g sched
}

cmd_unschedule() {
    echo "Removing macOS wake/sleep schedule..."
    sudo pmset repeat cancel
    echo "Schedule removed."
    pmset -g sched
}

# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------

cmd_logs() {
    local log_file
    log_file="$(_today_log)"
    if [ -f "$log_file" ]; then
        echo "Tailing $log_file (Ctrl+C to stop)..."
        tail -f "$log_file"
    else
        # Fall back to most recent log
        local latest
        latest="$(ls -t "$LOG_DIR"/bread-*.log 2>/dev/null | head -1)"
        if [ -n "$latest" ]; then
            echo "No log for today. Tailing most recent: $latest"
            tail -f "$latest"
        else
            echo "No log files found in $LOG_DIR"
            exit 1
        fi
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

case "${1:-help}" in
    start)
        shift
        # Collect args after "--" if present
        if [ "${1:-}" = "--" ]; then
            shift
        fi
        cmd_start "$@"
        ;;
    stop)
        cmd_stop
        ;;
    status)
        cmd_status
        ;;
    schedule)
        cmd_schedule
        ;;
    unschedule)
        cmd_unschedule
        ;;
    logs)
        cmd_logs
        ;;
    help|--help|-h)
        echo "Usage: $0 {start|stop|status|schedule|unschedule|logs}"
        echo
        echo "Commands:"
        echo "  start [-- ARGS]   Start bot (default: --mode paper --dashboard)"
        echo "  stop              Gracefully stop the bot"
        echo "  status            Show bot status and recent logs"
        echo "  schedule          Set macOS wake/sleep for market hours (sudo)"
        echo "  unschedule        Remove wake/sleep schedule (sudo)"
        echo "  logs              Tail today's log file"
        echo
        echo "Examples:"
        echo "  $0 start                           # paper mode with dashboard"
        echo "  $0 start -- --mode paper --no-dashboard"
        echo "  $0 schedule                        # wake 9:25, sleep 16:10 weekdays"
        ;;
    *)
        echo "Unknown command: $1"
        echo "Usage: $0 {start|stop|status|schedule|unschedule|logs}"
        exit 1
        ;;
esac
