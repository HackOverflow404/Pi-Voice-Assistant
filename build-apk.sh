#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
# Gradle + lint exhaust a 2 GB Pi and make it unresponsive; build elsewhere and use deploy-apk.sh.
mem_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
if [[ ( $(uname -m) == aarch64 || $mem_kb -lt 4000000 ) && ${FORCE_LOCAL_BUILD:-0} != 1 ]]; then
  echo 'Refusing to build on this host (ARM or <4 GB RAM). Build on a development machine' >&2
  echo 'and run ./deploy-apk.sh there. FORCE_LOCAL_BUILD=1 overrides.' >&2
  exit 1
fi
mkdir -p .tools dist
export GRADLE_USER_HOME="$PWD/.tools/gradle-home"
export ANDROID_USER_HOME="$PWD/.tools/android-user"
if [[ -d .tools/jdk ]]; then export JAVA_HOME="$PWD/.tools/jdk"; export PATH="$JAVA_HOME/bin:$PATH"; fi
sdk="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$PWD/.tools/android-sdk}}"
[[ -d "$sdk/platforms/android-35" ]] || {
  echo 'Install Android SDK platform 35 and build-tools 35.0.0; set ANDROID_HOME (see README).' >&2; exit 1;
}
export ANDROID_HOME="$sdk"
if [[ ! -x .tools/gradle-8.9/bin/gradle ]]; then
  curl -fL --retry 3 https://services.gradle.org/distributions/gradle-8.9-bin.zip -o .tools/gradle-8.9-bin.zip
  curl -fL --retry 3 https://services.gradle.org/distributions/gradle-8.9-bin.zip.sha256 -o .tools/gradle.sha256
  printf '%s  %s\n' "$(cat .tools/gradle.sha256)" '.tools/gradle-8.9-bin.zip' | sha256sum -c -
  unzip -q .tools/gradle-8.9-bin.zip -d .tools
fi
.tools/gradle-8.9/bin/gradle --no-daemon -p android assembleDebug lintDebug "$@"
cp android/app/build/outputs/apk/debug/app-debug.apk dist/pi-voice-assistant-debug.apk
echo "APK: $PWD/dist/pi-voice-assistant-debug.apk"
