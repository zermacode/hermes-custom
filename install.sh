#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
COMFY_DEST="${COMFY_DEST:-$HOME/AI/comfy}"
AGENTS_SKILLS_DIR="${AGENTS_SKILLS_DIR:-$HOME/.agents/skills}"

echo "==> Installing ComfyUI runner, output router & API workflows to: $COMFY_DEST"
mkdir -p "$COMFY_DEST/workflows/api" "$COMFY_DEST/custom_nodes"
mkdir -p "$HOME/hermes-data/output" "$HOME/hermes-data-private/output"
cp "$REPO_DIR/comfy/comfy_api.py" "$COMFY_DEST/comfy_api.py"
chmod +x "$COMFY_DEST/comfy_api.py"
cp "$REPO_DIR/comfy/custom_nodes/hermes_output_router.py" "$COMFY_DEST/custom_nodes/hermes_output_router.py"
cp "$REPO_DIR/comfy/launch_3090.sh" "$COMFY_DEST/launch_3090.sh"
cp "$REPO_DIR/comfy/launch_5090.sh" "$COMFY_DEST/launch_5090.sh"
chmod +x "$COMFY_DEST/launch_3090.sh" "$COMFY_DEST/launch_5090.sh"
cp "$REPO_DIR/comfy/workflows/api/"*.json "$COMFY_DEST/workflows/api/"

echo "==> Installing skills into: $HERMES_HOME/skills/creative and $AGENTS_SKILLS_DIR"
mkdir -p "$HERMES_HOME/skills/creative"
mkdir -p "$HERMES_HOME/profiles/limited/skills/creative"
mkdir -p "$AGENTS_SKILLS_DIR"

cp -r "$REPO_DIR/skills/creative/comfyui-media" "$HERMES_HOME/skills/creative/"
cp -r "$REPO_DIR/skills/creative/h3-prompt-writing" "$HERMES_HOME/skills/creative/"
cp -r "$REPO_DIR/skills/creative/comfyui-media" "$HERMES_HOME/profiles/limited/skills/creative/"
cp -r "$REPO_DIR/skills/creative/h3-prompt-writing" "$HERMES_HOME/profiles/limited/skills/creative/"
cp -r "$REPO_DIR/skills/creative/h3-prompt-writing" "$AGENTS_SKILLS_DIR/"

echo "==> Installing Hermes config.yaml & SOUL.md into: $HERMES_HOME"
mkdir -p "$HERMES_HOME/profiles/limited"
cp "$REPO_DIR/hermes/config.yaml" "$HERMES_HOME/config.yaml"
cp "$REPO_DIR/hermes/SOUL.md" "$HERMES_HOME/SOUL.md"
cp "$REPO_DIR/hermes/profiles/limited/config.yaml" "$HERMES_HOME/profiles/limited/config.yaml"
cp "$REPO_DIR/hermes/profiles/limited/SOUL.md" "$HERMES_HOME/profiles/limited/SOUL.md"

if [ ! -f "$HERMES_HOME/.env" ]; then
    cp "$REPO_DIR/hermes/.env.example" "$HERMES_HOME/.env"
    echo "==> Created $HERMES_HOME/.env from .env.example (edit as needed)"
else
    echo "==> Keeping existing $HERMES_HOME/.env"
fi

if [ -d "$HERMES_HOME/hermes-agent/plugins" ]; then
    echo "==> Syncing ComfyUI plugins into $HERMES_HOME/hermes-agent/plugins"
    mkdir -p "$HERMES_HOME/hermes-agent/plugins/image_gen"
    mkdir -p "$HERMES_HOME/hermes-agent/plugins/video_gen"
    cp -r "$REPO_DIR/plugins/image_gen/comfyui" "$HERMES_HOME/hermes-agent/plugins/image_gen/"
    cp -r "$REPO_DIR/plugins/video_gen/comfyui" "$HERMES_HOME/hermes-agent/plugins/video_gen/"
fi

echo "✓ Done! Restart the Hermes gateway with:"
echo "  hermes gateway stop && hermes gateway ensure"
