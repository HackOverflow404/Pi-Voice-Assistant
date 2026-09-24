#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
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
