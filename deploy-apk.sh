#!/usr/bin/env bash
# Build the APK on this machine, copy it to the Pi, and install it on the Echo
# through the Pi's adb connection. The Pi (2 GB RAM) cannot run Gradle itself.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
pi_host=${PI_HOST:-mainframe}
pi_dir=${PI_DIR:-Clippy}
apk=dist/pi-voice-assistant-debug.apk
case ${1:-} in
  '') ./build-apk.sh ;;
  --no-build) [[ -f "$apk" ]] || { echo "No $apk; run without --no-build." >&2; exit 1; } ;;
  *) echo 'Usage: ./deploy-apk.sh [--no-build]   (env: PI_HOST, PI_DIR)' >&2; exit 2 ;;
esac
ssh "$pi_host" "mkdir -p '$pi_dir/dist'"
scp "$apk" "$pi_host:$pi_dir/dist/"
ssh "$pi_host" "adb wait-for-device \
  && adb install -r -t '$pi_dir/$apk' \
  && adb shell am start -n com.instinct.voice/.MainActivity"
