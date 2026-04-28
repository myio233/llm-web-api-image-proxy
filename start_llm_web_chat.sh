#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/llm_web_frontend"
python3 app.py
