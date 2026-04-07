#!/usr/bin/env bash
set -euo pipefail

# Portable daily sync script for cron.
# Override any of these via environment variables in crontab.
#
# Example:
#   MEMPALACE_REPO=/path/to/mempalace
#   MEMPALACE_OBSIDIAN_PATH=/path/to/vault
#   MEMPALACE_NOTION_SYNC_LIMIT=100
#   MEMPALACE_PYTHON=/usr/bin/python3

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

MEMPALACE_REPO="${MEMPALACE_REPO:-$DEFAULT_REPO}"
MEMPALACE_PYTHON="${MEMPALACE_PYTHON:-python3}"
MEMPALACE_LOG_DIR="${MEMPALACE_LOG_DIR:-$HOME/.mempalace/logs}"
MEMPALACE_NOTION_SYNC_LIMIT="${MEMPALACE_NOTION_SYNC_LIMIT:-100}"
MEMPALACE_NOTION_SYNC_QUERY="${MEMPALACE_NOTION_SYNC_QUERY:-}"
MEMPALACE_OBSIDIAN_PATH="${MEMPALACE_OBSIDIAN_PATH:-}"
MEMPALACE_SKIP_NOTION="${MEMPALACE_SKIP_NOTION:-0}"
MEMPALACE_SKIP_OBSIDIAN="${MEMPALACE_SKIP_OBSIDIAN:-0}"

mkdir -p "$MEMPALACE_LOG_DIR"

STAMP="$(date +%Y-%m-%d_%H-%M-%S)"
LOG_FILE="$MEMPALACE_LOG_DIR/daily_sync_$STAMP.log"

cd "$MEMPALACE_REPO"

{
  echo "=== MemPalace daily sync started at $(date -Is) ==="
  echo "Repo: $MEMPALACE_REPO"
  echo "Python: $MEMPALACE_PYTHON"

  if [ "$MEMPALACE_SKIP_NOTION" != "1" ]; then
    echo
    echo "--- Notion sync ---"
    "$MEMPALACE_PYTHON" -m mempalace notion sync \
      --limit "$MEMPALACE_NOTION_SYNC_LIMIT" \
      --query "$MEMPALACE_NOTION_SYNC_QUERY"
  else
    echo
    echo "--- Notion sync skipped ---"
  fi

  if [ "$MEMPALACE_SKIP_OBSIDIAN" != "1" ]; then
    echo
    echo "--- Obsidian sync ---"
    if [ -z "$MEMPALACE_OBSIDIAN_PATH" ]; then
      echo "MEMPALACE_OBSIDIAN_PATH is not set; skipping Obsidian mine."
    elif [ ! -d "$MEMPALACE_OBSIDIAN_PATH" ]; then
      echo "Obsidian path does not exist: $MEMPALACE_OBSIDIAN_PATH"
      exit 1
    else
      if [ ! -f "$MEMPALACE_OBSIDIAN_PATH/mempalace.yaml" ]; then
        echo "mempalace.yaml missing, running init"
        "$MEMPALACE_PYTHON" -m mempalace init "$MEMPALACE_OBSIDIAN_PATH" --yes
      fi

      "$MEMPALACE_PYTHON" -m mempalace mine "$MEMPALACE_OBSIDIAN_PATH"
    fi
  else
    echo
    echo "--- Obsidian sync skipped ---"
  fi

  echo
  echo "--- Status ---"
  "$MEMPALACE_PYTHON" -m mempalace notion status || true
  "$MEMPALACE_PYTHON" -m mempalace status || true

  echo
  echo "=== MemPalace daily sync finished at $(date -Is) ==="
} >> "$LOG_FILE" 2>&1
