#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Running InverterScout tests..."
exec python -m pytest "$@"
