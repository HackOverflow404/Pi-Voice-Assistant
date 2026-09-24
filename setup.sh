#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
case ${1:-} in
  ''|--runtime-only|--external-agent) ;;
  *) echo 'Usage: ./setup.sh [--external-agent | --runtime-only]' >&2; exit 2 ;;
esac
source scripts/polkit.sh
if [[ $(uname -m) != aarch64 && ${ALLOW_OTHER_ARCH:-0} != 1 ]]; then
  echo 'Expected ARM64 (aarch64). Use 64-bit Debian; ALLOW_OTHER_ARCH=1 allows development hosts.' >&2
  exit 1
fi
if [[ ${1:-} != --runtime-only ]]; then
  echo 'Pi setup will install Debian packages with pkexec, download models into this project,'
  echo 'and create local Python environments. It does not install or start the service.'
  if ! command -v apt-get >/dev/null; then
    echo 'Run this script on Debian / Raspberry Pi OS (64-bit).' >&2; exit 1
  fi
  if [[ ${1:-} == --external-agent ]]; then prepare_external_agent; fi
  run_privileged apt-get update
  run_privileged apt-get install -y python3 python3-venv ca-certificates curl unzip libgomp1
fi
# A local managed 3.11 runtime also supports Debian releases whose default is 3.13+.
python3 -m venv .bootstrap
export PIP_CACHE_DIR="$PWD/.bootstrap/pip-cache"
.bootstrap/bin/python -m pip install --disable-pip-version-check 'uv==0.8.22'
export UV_PYTHON_INSTALL_DIR="$PWD/.python"
export UV_CACHE_DIR="$PWD/.uv-cache"
.bootstrap/bin/uv python install 3.11
.bootstrap/bin/uv venv --allow-existing --managed-python --python 3.11 .venv
# ONNX-only openWakeWord avoids its unnecessary TFLite wheel requirement on ARM64.
sed '/^openwakeword==/d' server/requirements.txt > .bootstrap/requirements.txt
.bootstrap/bin/uv pip install --python .venv/bin/python -r .bootstrap/requirements.txt -r server/requirements-wake.txt
.bootstrap/bin/uv pip install --python .venv/bin/python --no-deps 'openwakeword==0.6.0'
.venv/bin/python scripts/download_models.py
if [[ ! -f config.yaml ]]; then
  cp config.example.yaml config.yaml
  chmod 600 config.yaml
fi
echo 'Runtime ready. Add models/custom.onnx, edit config.yaml, then run ./install.'
