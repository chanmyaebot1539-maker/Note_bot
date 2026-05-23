#!/usr/bin/env bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "Build dir: $SCRIPT_DIR"
pip install -r "$SCRIPT_DIR/requirements.txt"
