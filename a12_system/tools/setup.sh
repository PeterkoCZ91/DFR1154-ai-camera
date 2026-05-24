#!/usr/bin/env bash
# A12 System — first-time setup helper
# Run once before starting Docker: bash tools/setup.sh

set -e

DATA_DIR="${A12_DATA_DIR:-/opt/a12-data}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
A12_DIR="$(dirname "$SCRIPT_DIR")"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  [OK]${NC} $1"; }
warn() { echo -e "${YELLOW}  [!] ${NC} $1"; }
err()  { echo -e "${RED}  [ERR]${NC} $1"; }

echo ""
echo "A12 System — Setup"
echo "=================================="

# 1. Data directory
echo ""
echo "1. Data directory: $DATA_DIR"
if [ ! -d "$DATA_DIR" ]; then
    mkdir -p "$DATA_DIR" && ok "Created $DATA_DIR" || { err "Cannot create $DATA_DIR — try with sudo or set A12_DATA_DIR"; exit 1; }
else
    ok "Exists"
fi

# 2. config.env
echo ""
echo "2. config.env"
if [ ! -f "$DATA_DIR/config.env" ]; then
    cp "$A12_DIR/config.env.example" "$DATA_DIR/config.env"
    warn "Created config.env from template — edit it before starting A12:"
    warn "  nano $DATA_DIR/config.env"
    warn "  Required: ESP32_IP, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
else
    ok "Exists (not overwritten)"
fi

# 3. YOLO model
echo ""
echo "3. YOLO model (yolo11n.onnx)"
MODEL_DST="$DATA_DIR/yolo11n.onnx"
MODEL_URL="https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.onnx"
if [ ! -f "$MODEL_DST" ]; then
    echo "   Downloading yolo11n.onnx (~6 MB)..."
    if command -v curl &>/dev/null; then
        curl -L --progress-bar "$MODEL_URL" -o "$MODEL_DST" && ok "Downloaded"
    elif command -v wget &>/dev/null; then
        wget -q --show-progress "$MODEL_URL" -O "$MODEL_DST" && ok "Downloaded"
    else
        err "curl/wget not found — download manually:"
        err "  $MODEL_URL"
        err "  → $MODEL_DST"
    fi
else
    ok "Exists ($(du -sh "$MODEL_DST" | cut -f1))"
fi

# 4. Screenshots directory
echo ""
echo "4. Screenshots directory"
mkdir -p "$DATA_DIR/screenshots/person" "$DATA_DIR/screenshots/animal"
ok "$DATA_DIR/screenshots/"

# 5. Docker check
echo ""
echo "5. Docker"
if command -v docker &>/dev/null; then
    ok "Docker $(docker --version | grep -oP '\d+\.\d+\.\d+' | head -1) found"
    if docker compose version &>/dev/null 2>&1; then
        ok "Docker Compose plugin found"
    else
        warn "Docker Compose plugin not found — install: sudo apt install docker-compose-plugin"
    fi
else
    warn "Docker not found — install from https://docs.docker.com/engine/install/"
fi

# 6. Summary
echo ""
echo "=================================="
echo "Setup complete. Next steps:"
echo ""
echo "  1. Edit config.env:"
echo "     nano $DATA_DIR/config.env"
echo ""
echo "  2. Run diagnostics:"
echo "     ${A12_DIR}/tools/a12 doctor"
echo ""
echo "  3. Build and start A12:"
echo "     ${A12_DIR}/tools/a12 build"
echo "     ${A12_DIR}/tools/a12 up"
echo ""
echo "  4. Watch logs:"
echo "     ${A12_DIR}/tools/a12 logs 80"
echo ""
if grep -q "ESP32_IP=192.168.1.100" "$DATA_DIR/config.env" 2>/dev/null; then
    warn "config.env still has default IP (192.168.1.100) — update ESP32_IP before starting!"
fi
if grep -q "^#TELEGRAM_BOT_TOKEN" "$DATA_DIR/config.env" 2>/dev/null; then
    warn "Telegram is disabled — uncomment TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to enable alerts"
fi
