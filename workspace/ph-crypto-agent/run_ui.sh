#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Installing UI dependencies"
python3 -m pip install -r requirements-ui.txt

echo "==> Starting ph-crypto-agent UI"
echo "    Open http://localhost:8501 in your browser"
streamlit run streamlit_app.py
