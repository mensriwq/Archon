#!/usr/bin/env bash
# Archon UI — start the dashboard for a project
set -euo pipefail

# ── Colors ──────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()  { echo -e "${GREEN}[UI]${NC} $*"; }
warn()  { echo -e "${YELLOW}[UI]${NC} $*"; }
err()   { echo -e "${RED}[UI]${NC} $*" >&2; }
bold()  { echo -e "${BOLD}$*${NC}"; }

# ── Defaults ────────────────────────────────────────────────
UI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_PORT=8080
PORT="$DEFAULT_PORT"
PROJECT_PATH=""
DEV_MODE=false
BUILD_ONLY=false
OPEN_BROWSER=false

# ── Usage ───────────────────────────────────────────────────
usage() {
    cat << 'EOF'
Usage: ui/start.sh --project /path/to/lean-project [OPTIONS]

Start the Archon dashboard for a formalization project.

Required:
  --project PATH       Path to the Lean project (must contain .archon/)

Options:
  --port PORT          Server port (default: 8080)
  --dev                Run in dev mode (vite dev server + tsx watch)
  --build              Build client only (no server start)
  --open               Open browser after starting
  -h, --help           Show this help

Examples:
  ./ui/start.sh --project workspace/my-project
  ./ui/start.sh --project /home/user/lean-project --port 9090 --open
  ./ui/start.sh --project workspace/my-project --dev
EOF
    exit 0
}

usage_err() { usage 2>/dev/null; exit 1; }

# ── Parse args ──────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --project)  PROJECT_PATH="$2"; shift 2 ;;
        --port)     PORT="$2"; shift 2 ;;
        --dev)      DEV_MODE=true; shift ;;
        --build)    BUILD_ONLY=true; shift ;;
        --open)     OPEN_BROWSER=true; shift ;;
        -h|--help)  usage ;;
        *)          err "Unknown option: $1"; usage_err ;;
    esac
done

if [[ -z "$PROJECT_PATH" ]]; then
    err "Missing required --project flag"
    echo ""
    usage_err
fi

# Resolve to absolute path
PROJECT_PATH="$(cd "$PROJECT_PATH" 2>/dev/null && pwd)" || {
    err "Project path does not exist: $PROJECT_PATH"
    exit 1
}

# ── Validate project ───────────────────────────────────────
if [[ ! -d "$PROJECT_PATH/.archon" ]]; then
    err "No .archon/ directory found in $PROJECT_PATH"
    err "Run init.sh first, or check the project path."
    exit 1
fi

info "Project: ${PROJECT_PATH}"
info "Port:    ${PORT}"

# ── Check dependencies ─────────────────────────────────────
check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        err "Required command not found: $1"
        err "Install it and try again."
        return 1
    fi
}

info "Checking dependencies..."
check_cmd node || exit 1
check_cmd npm  || exit 1

NODE_VERSION=$(node -v | sed 's/v//' | cut -d. -f1)
if (( NODE_VERSION < 18 )); then
    err "Node.js 18+ required (found: $(node -v))"
    exit 1
fi
info "  node $(node -v) ✓"

# ── Install npm dependencies if needed ─────────────────────
install_if_needed() {
    local dir="$1"
    local name="$2"
    if [[ ! -d "$dir/node_modules" ]]; then
        info "Installing ${name} dependencies..."
        (cd "$dir" && npm install --no-fund --no-audit --loglevel=error) || {
            err "Failed to install ${name} dependencies"
            exit 1
        }
        info "  ${name} dependencies installed ✓"
    else
        # Quick check: if package.json is newer than node_modules, re-install
        if [[ "$dir/package.json" -nt "$dir/node_modules/.package-lock.json" ]] 2>/dev/null; then
            info "Updating ${name} dependencies..."
            (cd "$dir" && npm install --no-fund --no-audit --loglevel=error) || true
        fi
    fi
}

install_if_needed "$UI_DIR/server" "server"
install_if_needed "$UI_DIR/client" "client"

# ── Build client if needed ─────────────────────────────────
CLIENT_DIR="$UI_DIR/client"
DIST_DIR="$CLIENT_DIR/dist"

needs_build() {
    [[ ! -d "$DIST_DIR" ]] && return 0
    # Rebuild if any source file is newer than dist
    local newest_src newest_dist
    newest_src=$(find "$CLIENT_DIR/src" -type f -newer "$DIST_DIR/index.html" 2>/dev/null | head -1)
    [[ -n "$newest_src" ]] && return 0
    return 1
}

if [[ "$DEV_MODE" != true ]]; then
    if needs_build; then
        info "Building client..."
        (cd "$CLIENT_DIR" && node node_modules/vite/bin/vite.js build --logLevel warn) || {
            err "Client build failed"
            exit 1
        }
        info "  Client built ✓"
    else
        info "  Client up to date ✓"
    fi
fi

if [[ "$BUILD_ONLY" == true ]]; then
    info "Build complete."
    exit 0
fi

# ── Instance + port utilities ──────────────────────────────
INSTANCE_DIR="$UI_DIR/.archon-ui"
mkdir -p "$INSTANCE_DIR"

project_key() {
    local input="$1"
    if command -v shasum &>/dev/null; then
        printf '%s' "$input" | shasum -a 256 | awk '{print substr($1,1,16)}'
        return
    fi
    if command -v sha256sum &>/dev/null; then
        printf '%s' "$input" | sha256sum | awk '{print substr($1,1,16)}'
        return
    fi
    printf '%s' "$input" | cksum | awk '{print $1}'
}

PROJECT_KEY="$(project_key "$PROJECT_PATH")"
PID_FILE="$INSTANCE_DIR/${PROJECT_KEY}.pid"

# Check if a port is in LISTEN state.
# Returns 0 (true) if in use, 1 (false) if free.
# Fallback chain: lsof → ss → /proc/net/tcp
port_in_use() {
    local p="$1"
    if command -v lsof &>/dev/null; then
        lsof -iTCP:"$p" -sTCP:LISTEN &>/dev/null && return 0
        return 1
    fi
    if command -v ss &>/dev/null; then
        ss -tlnH 2>/dev/null | grep -qE "[:.]${p}\b" && return 0
        return 1
    fi
    if [[ -r /proc/net/tcp ]]; then
        local hex
        hex=$(printf '%04X' "$p")
        awk '{print $2, $4}' /proc/net/tcp 2>/dev/null | grep -qi ":${hex} 0A" && return 0
        return 1
    fi
    return 1
}

stop_pid() {
    local pid="$1"
    local label="$2"
    if ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi

    info "Stopping ${label} (PID $pid)..."
    kill "$pid" 2>/dev/null || true
    for _i in 1 2 3 4 5; do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 1
    done

    warn "PID $pid did not exit after SIGTERM, forcing stop..."
    kill -9 "$pid" 2>/dev/null || true
    for _i in 1 2 3; do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 1
    done
    return 0
}

find_next_free_port() {
    local base="$1"
    local p
    for p in $(seq $((base + 1)) $((base + 10))); do
        if ! port_in_use "$p"; then
            echo "$p"
            return 0
        fi
    done
    return 1
}

# Clean up current project's previous instance before checking ports.
if [[ -f "$PID_FILE" ]]; then
    old_pid=$(cat "$PID_FILE")
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
        stop_pid "$old_pid" "previous UI server for this project"
    else
        warn "Removing stale UI instance record for this project"
    fi
    rm -f "$PID_FILE"
fi

# Only after project-local cleanup do we resolve external port conflicts.
if port_in_use "$PORT"; then
    warn "Port $PORT is already in use by another process or project"
    next_port="$(find_next_free_port "$PORT" || true)"
    if [[ -z "$next_port" ]]; then
        err "Could not find a free port in range ${PORT}–$((PORT + 10))"
        err "Free the current port or pass an explicit --port"
        exit 1
    fi
    PORT="$next_port"
    echo ""
    warn "╔══════════════════════════════════════════════════╗"
    warn "║  Port changed! Using ${PORT} instead              ║"
    warn "╚══════════════════════════════════════════════════╝"
    echo ""
fi

# ── Start server ───────────────────────────────────────────
SERVER_DIR="$UI_DIR/server"

echo ""
if [[ "$DEV_MODE" == true ]]; then
    bold "Starting in dev mode..."
    info ""
    info "  Dashboard:  http://localhost:${PORT}"
    info "  Vite dev:   http://localhost:5173 (auto-opens)"
    info ""
    info "  Press Ctrl+C to stop"
    info ""

    # Start server in background
    (cd "$SERVER_DIR" && exec node --import tsx src/index.ts --project "$PROJECT_PATH" --port "$PORT") &
    SERVER_PID=$!
    echo "$SERVER_PID" > "$PID_FILE"

    # Start vite dev server in foreground
    trap "kill $SERVER_PID 2>/dev/null; rm -f '$PID_FILE'" EXIT
    (cd "$CLIENT_DIR" && node node_modules/vite/bin/vite.js --port 5173)
else
    (cd "$SERVER_DIR" && exec node --import tsx src/index.ts --project "$PROJECT_PATH" --port "$PORT") &
    SERVER_PID=$!
    echo "$SERVER_PID" > "$PID_FILE"

    # Wait a moment to see if it crashes
    sleep 1
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        err "Server failed to start"
        rm -f "$PID_FILE"
        exit 1
    fi

    echo ""
    bold "╔══════════════════════════════════════════╗"
    bold "║          Archon Dashboard                ║"
    bold "╚══════════════════════════════════════════╝"
    info ""
    info "  Dashboard:  ${BLUE}http://localhost:${PORT}${NC}"
    info "  Overview:   ${BLUE}http://localhost:${PORT}/${NC}"
    info "  Logs:       ${BLUE}http://localhost:${PORT}/logs${NC}"
    info "  Journal:    ${BLUE}http://localhost:${PORT}/journal${NC}"
    info ""
    info "  Project:    ${PROJECT_PATH}"
    info "  PID:        ${SERVER_PID}"
    info "  PID file:   ${PID_FILE}"
    info ""
    info "  Stop:  kill ${SERVER_PID}  (or: kill \$(cat ${PID_FILE}))"
    info ""

    # Open browser if requested
    if [[ "$OPEN_BROWSER" == true ]]; then
        local_url="http://localhost:${PORT}"
        if command -v open &>/dev/null; then
            open "$local_url"
        elif command -v xdg-open &>/dev/null; then
            xdg-open "$local_url"
        fi
    fi

    # Wait for server process (so Ctrl+C works)
    trap "kill $SERVER_PID 2>/dev/null; rm -f '$PID_FILE'; echo ''; info 'Dashboard stopped.'" EXIT INT TERM
    wait "$SERVER_PID" 2>/dev/null || true
fi
