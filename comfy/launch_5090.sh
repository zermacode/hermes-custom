#!/usr/bin/env bash
set -e

# Determine directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/main.py" ]; then
    COMFY_DIR="$SCRIPT_DIR"
    ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
elif [ -f "$SCRIPT_DIR/5090/main.py" ]; then
    COMFY_DIR="$SCRIPT_DIR/5090"
    ROOT_DIR="$SCRIPT_DIR"
else
    COMFY_DIR="/home/agent/AI/comfy/5090"
    ROOT_DIR="/home/agent/AI/comfy"
fi

RUN_DIR="$ROOT_DIR/.run"
mkdir -p "$RUN_DIR"
PID_FILE="$RUN_DIR/5090.pid"

SHARED_MODELS_DIR="$ROOT_DIR/models"
DEFAULT_OUTPUT_DIR="/home/agent/hermes-data/output"
mkdir -p "$DEFAULT_OUTPUT_DIR" "/home/agent/hermes-data-private/output"
DEFAULT_PORT="8188"
GPU_DEVICE="0"
VENV_DIR="$ROOT_DIR/.venv"

# Isolate GPU to RTX 5090 (Device 0)
export CUDA_VISIBLE_DEVICES="$GPU_DEVICE"

# Activate shared virtual environment
if [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
else
    echo "[ERROR] Shared virtual environment not found at $VENV_DIR" >&2
    exit 1
fi

PYTHON_BIN="$(command -v python || command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
    echo "[ERROR] No python binary found in PATH." >&2
    exit 1
fi

# Helper function to check if an argument or flag is already provided
has_arg() {
    local pattern="$1"
    shift
    for arg in "$@"; do
        if [[ "$arg" == "$pattern" ]] || [[ "$arg" =~ ^"$pattern"= ]]; then
            return 0
        fi
    done
    return 1
}

EXTRA_ARGS=()
if ! has_arg "--port" "$@"; then
    EXTRA_ARGS+=(--port "$DEFAULT_PORT")
fi

if ! has_arg "--models-directory" "$@"; then
    EXTRA_ARGS+=(--models-directory "$SHARED_MODELS_DIR")
fi

if ! has_arg "--output-directory" "$@"; then
    EXTRA_ARGS+=(--output-directory "$DEFAULT_OUTPUT_DIR")
fi

if ! has_arg "--listen" "$@"; then
    EXTRA_ARGS+=(--listen "0.0.0.0")
fi

# Detect effective port for display
PORT="$DEFAULT_PORT"
for i in "$@"; do
    if [[ "$i" =~ ^--port=(.+) ]]; then
        PORT="${BASH_REMATCH[1]}"
    fi
done
PREV=""
for i in "$@"; do
    if [[ "$PREV" == "--port" ]]; then
        PORT="$i"
    fi
    PREV="$i"
done

echo "=========================================================="
echo " Starting ComfyUI [RTX 5090]"
echo " GPU Device:           RTX 5090 (CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES)"
echo " Virtual Environment:  $VENV_DIR"
echo " Python Binary:        $PYTHON_BIN"
echo " ComfyUI Directory:    $COMFY_DIR"
echo " Shared Models Dir:    $SHARED_MODELS_DIR"
echo " Default Output Dir:   $DEFAULT_OUTPUT_DIR"
echo " Private Output Dir:   /home/agent/hermes-data-private/output"
echo " Web UI Port:          $PORT"
echo " Local Access:         http://localhost:$PORT"
echo " Tailscale Access:     http://nvidia:$PORT"
echo "=========================================================="

cd "$COMFY_DIR"
echo $$ > "$PID_FILE"
exec python main.py "${EXTRA_ARGS[@]}" "$@"
