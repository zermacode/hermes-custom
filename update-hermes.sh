#!/usr/bin/env bash
set -euo pipefail

HERMES_RUNTIME_REPO="${HERMES_RUNTIME_REPO:-$HOME/.hermes/hermes-agent}"
HERMES_DEV_REPO="${HERMES_DEV_REPO:-$HOME/hermes-agent}"
HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"

echo "==> Fetching latest upstream commits from NousResearch/hermes-agent..."
git -C "$HERMES_RUNTIME_REPO" fetch upstream main

LOCAL_BASE="$(git -C "$HERMES_RUNTIME_REPO" merge-base HEAD upstream/main)"
UPSTREAM_HEAD="$(git -C "$HERMES_RUNTIME_REPO" rev-parse upstream/main)"

if [ "$LOCAL_BASE" = "$UPSTREAM_HEAD" ]; then
    echo "✓ Already up-to-date with upstream/main ($UPSTREAM_HEAD)."
else
    echo "==> Rebasing custom ComfyUI/Signal commits onto upstream/main..."
    if ! git -C "$HERMES_RUNTIME_REPO" rebase upstream/main; then
        echo "✗ Rebase encountered a conflict. Aborting rebase so your working tree stays clean."
        git -C "$HERMES_RUNTIME_REPO" rebase --abort || true
        echo "  Tip: Ask your AI coding agent to resolve the upstream rebase for you!"
        exit 1
    fi

    echo "==> Pushing updated branch to origin (zermacode/hermes-agent)..."
    git -C "$HERMES_RUNTIME_REPO" push origin main --force-with-lease
fi

# Keep ~/hermes-agent in sync if present
if [ -d "$HERMES_DEV_REPO/.git" ] && [ "$HERMES_DEV_REPO" != "$HERMES_RUNTIME_REPO" ]; then
    echo "==> Syncing $HERMES_DEV_REPO with origin/main..."
    git -C "$HERMES_DEV_REPO" fetch origin main
    git -C "$HERMES_DEV_REPO" fetch upstream main || true
    git -C "$HERMES_DEV_REPO" reset --hard origin/main
fi

# Run Hermes config migration & restart gateway
if [ -x "$HERMES_BIN" ]; then
    echo "==> Restarting Hermes gateway..."
    "$HERMES_BIN" gateway stop || true
    "$HERMES_BIN" gateway ensure
fi

echo "✓ Hermes is updated, synced to GitHub, and running!"
