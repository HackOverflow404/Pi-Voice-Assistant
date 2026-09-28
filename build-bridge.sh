#!/usr/bin/env bash
# Cross-compile the WhatsApp bridge for the Pi (linux/arm64, no cgo) into dist/wa-bridge.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
# Compiling the pure-Go SQLite driver needs more memory than a 2 GB Pi has to spare.
mem_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
if [[ ( $(uname -m) == aarch64 || $mem_kb -lt 4000000 ) && ${FORCE_LOCAL_BUILD:-0} != 1 ]]; then
  echo 'Refusing to build on this host (ARM or <4 GB RAM). Build on a development machine' >&2
  echo 'and run ./deploy-bridge.sh there. FORCE_LOCAL_BUILD=1 overrides.' >&2
  exit 1
fi
mkdir -p .tools dist
export GOPATH="$PWD/.tools/go" GOCACHE="$PWD/.tools/go-cache" GOTOOLCHAIN=local
export CGO_ENABLED=0 GOOS=linux GOARCH=arm64
go=(go)
go version >/dev/null 2>&1 || go=(mise exec go@1.27.1 -- go)
(cd bridge && "${go[@]}" build -trimpath -ldflags='-s -w' -o ../dist/wa-bridge .)
echo "Bridge: $PWD/dist/wa-bridge"
