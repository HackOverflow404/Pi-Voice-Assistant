#!/usr/bin/env bash
# Restart the Echo's voice service over USB adb when it is not running (e.g. after the
# Echo reboots). Needed because without device ownership Android 11+ refuses to start a
# microphone service from boot; launching the activity with the START action can.
set -uo pipefail
pkg=com.instinct.voice
# adb shell reads stdin by default; never let it.
adb() { command adb "$@" </dev/null; }
[[ $(adb get-state 2>/dev/null) == device ]] || exit 0
[[ $(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r') == 1 ]] || exit 0
adb shell dumpsys activity services "$pkg" | grep -q "ServiceRecord.*$pkg/.VoiceService" && exit 0
# Only resume a service the user started; Stop in the app clears this flag.
adb shell run-as "$pkg" cat shared_prefs/connection.xml 2>/dev/null |
  grep -q 'name="enabled" value="true"' || exit 0
echo "VoiceService not running; launching $pkg with START"
adb shell input keyevent KEYCODE_WAKEUP
adb shell am start -a "$pkg.START" -n "$pkg/.MainActivity"
