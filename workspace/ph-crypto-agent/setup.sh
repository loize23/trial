#!/usr/bin/env bash
# setup.sh -- get the agent running on a fresh machine or a $5 VPS.
# Safe to re-run. Does NOT create accounts, does NOT deposit anything, does NOT trade live.
set -euo pipefail

echo "==> ph-crypto-agent setup"

PY=python3
command -v $PY >/dev/null 2>&1 || { echo "python3 not found. Install Python 3.9+ first."; exit 1; }
echo "    python: $($PY --version)"

cd "$(dirname "$0")"

if [ ! -f config/config.json ]; then
  cp config/config.example.json config/config.json
  echo "==> created config/config.json from the example (paper mode, 1000 USDT simulated)"
else
  echo "==> config/config.json already exists, leaving it alone"
fi

mkdir -p data
echo "==> data/ ready"

echo "==> running self-tests"
$PY run.py selftest

echo
echo "==> optional: install ccxt (only needed for LIVE trading)"
read -r -p "    install ccxt now? [y/N] " ans
case "$ans" in
  [yY]*) $PY -m pip install --user ccxt || pip install ccxt ;;
  *) echo "    skipped (paper trading needs nothing)" ;;
esac

echo
echo "==> done. Next:"
echo "    python3 run.py backtest        # test the strategy on real history"
echo "    python3 run.py cycle           # one paper cycle on live prices"
echo "    python3 run.py loop            # keep it running"
echo
echo "    Read GUIDE.md before you ever set mode=live."
