#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TOOLS_DIR="$REPO_ROOT/tools/piper"
MODELS_DIR="$REPO_ROOT/models/piper"
RUNTIME_ARCHIVE="$TOOLS_DIR/piper_macos_x64.tar.gz"
RUNTIME_URL="https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_macos_x64.tar.gz"
MODEL_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_female/medium/en_US-hfc_female-medium.onnx?download=true"
CONFIG_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_female/medium/en_US-hfc_female-medium.onnx.json?download=true"

mkdir -p "$TOOLS_DIR" "$MODELS_DIR"

echo "Downloading Piper runtime..."
curl -L -o "$RUNTIME_ARCHIVE" "$RUNTIME_URL"

echo "Extracting Piper runtime..."
tar -xzf "$RUNTIME_ARCHIVE" -C "$TOOLS_DIR"

echo "Downloading Piper HFC female voice model..."
curl -L -o "$MODELS_DIR/en_US-hfc_female-medium.onnx" "$MODEL_URL"
curl -L -o "$MODELS_DIR/en_US-hfc_female-medium.onnx.json" "$CONFIG_URL"

echo
echo "Piper setup complete."
echo "Set PIPER_MODEL to: $REPO_ROOT/models/piper/en_US-hfc_female-medium.onnx"
echo "Set PIPER_EXE to the extracted Piper binary under: $REPO_ROOT/tools/piper"
