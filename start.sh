#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python >/dev/null 2>&1; then
  echo "Python was not found. Activate the oemof-system Conda environment first." >&2
  echo "Run: conda activate oemof-system" >&2
  exit 1
fi

exec python "$SCRIPT_DIR/oemof-hri.py"
