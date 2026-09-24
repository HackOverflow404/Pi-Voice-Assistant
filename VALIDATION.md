# Validation performed

- Android debug APK compiled with JDK 17, Gradle 8.9, AGP 8.7.3, and SDK 35.
- Android lint: **0 errors, 7 warnings** (deliberately older target/dependencies
  for the Android 11–13 appliance and service-lifetime wake lock).
- APK metadata: package `com.instinct.voice`, minSdk 26, targetSdk 33.
- 13 Python tests passed on managed Python 3.11, including IMAP cancellation,
  timeouts, saved-ID recovery, MIME extraction, utterance boundaries, and WAV ACKs.
- Live localhost WebSocket check passed for bearer authentication, status delivery,
  exclusive microphone ownership, malformed PCM rejection, and graceful shutdown.
- Real openWakeWord 0.6 ONNX inference loaded successfully and scored silence at
  zero, using the stock `hey_jarvis` classifier solely for the smoke test.
- Real Piper 1.3 `en_US-amy-low` generated a 3.94-second, 16 kHz WAV. Real Vosk small
  English 0.15 transcribed it as “the voice assistant is ready please tell me the
  weather today,” matching the synthesized sentence.
- Shell scripts pass `bash -n`; Python source compiles.
- systemd unit syntax verified using a temporary copy with the executable path
  mapped to the local runtime. No service was installed on the development host.

The APK is `dist/pi-voice-assistant-debug.apk` (approximately 9.3 MiB), installed
with `adb install -r -t`. SHA-256:

```
5d050ef9b6dd6222f50c0596b9869218b5fddce048a0127bb75e3c1b850451af
```

The development machine is x86_64 Linux. These checks do **not** establish Pi
ARM64 inference speed, custom wake-word accuracy, Echo microphone/speaker routing,
device-owner boot behavior on the specific LineageOS ROM, or Gmail delivery.
Those require the target devices, the custom classifier, and account credentials.
No email was sent during development. Follow the README's on-device checks.
