# Pi Voice Assistant

A Raspberry Pi runs wake-word detection, speech recognition, WhatsApp messaging,
and speech synthesis. An Echo Show 5 running LineageOS supplies the microphone,
speaker, and Jetpack Compose dashboard. No cloud speech service is used.

```
Echo mic → authenticated WebSocket → openWakeWord → utterance capture → Whisper
    → WhatsApp text to the "Instinct" chat (wa-bridge, linked to your account)
    ← Instinct's WhatsApp reply ← Piper, one sentence at a time ← Echo speaker
```

The server emits `idle`, `listening`, `transcribing`, `waiting`, and `speaking`
events with the last transcript and reply. Only one microphone client can own
the server at a time. Say the trained wake phrase, pause briefly for the
Listening card, then speak. One second of quiet ends the utterance by default.
There is a 5-second no-speech timeout and a 20-second utterance limit.

## Files

- `server/voice_assistant/`: Python server, audio framing, speech engines, WhatsApp client, SQLite outbox.
- `bridge/`: Go WhatsApp bridge (whatsmeow) the server talks to over localhost HTTP.
- `android/`: Kotlin app, minimum API 26, target API 33, compiled against API 35.
- `config.example.yaml`: configuration template; `config.yaml` is private and ignored by Git.
- `models/`: custom wake classifier plus downloaded Whisper / Piper models.
- `setup.sh`: Debian packages, isolated Python 3.11 runtime, dependencies, model downloads.
- `install`, `uninstall`: install/remove separate live copies and a systemd user unit.
- `build-apk.sh`: checked Gradle distribution download, APK build, Android lint (refuses to run on the Pi).
- `scripts/echo-autostart.sh`, `systemd/echo-autostart.*`: Pi timer that restarts the Echo app over adb.
- `deploy-apk.sh`: build on the dev machine, copy to the Pi, install on the Echo via the Pi's adb.
- `build-bridge.sh`, `deploy-bridge.sh`, `systemd/wa-bridge.service`: cross-compile the bridge on the
  dev machine and install it on the Pi as a user service.

## Pi setup (ARM64 Debian / Raspberry Pi OS)

Use a 64-bit OS (`uname -m` must report `aarch64`), preferably a Pi 4/5 with at
least 2 GB RAM, and several GB free during setup. Copy this entire project to
the Pi. Run scripts as your normal login user; privileged package/linger
commands use `pkexec`. A headless Pi needs a working polkit authentication agent
in that login session; do not run the whole setup as root.

If authentication fails with **“No session for cookie”**, the polkit authentication
exchange failed; this message alone does not establish that the password was wrong.
Use an external terminal agent:

```sh
./setup.sh --external-agent
```

The script prints `pkttyagent --process NUMBER`. Run that exact command in a
second terminal/SSH session on the same machine, logged in as the same user.
Leave the agent running, then press Enter in the setup terminal. Enter the
password in the **agent terminal** when prompted. After setup exits, stop the
agent with Ctrl+C. Use `./install --external-agent` the same way for the later
`loginctl enable-linger` step; it prints a new process number.
See [pkttyagent's manual](https://manpages.debian.org/trixie/polkitd/pkttyagent.1.en.html).

If packages have already been installed separately, `./setup.sh --runtime-only`
skips the privileged package steps. It still creates the runtime and downloads
models. This does not fix a broken polkit daemon; if the external agent also
fails, inspect `journalctl -b -u polkit --no-pager` on the target machine.

```sh
cd pi-voice-assistant
./setup.sh
cp /path/to/your/trained-wake-word.onnx models/custom.onnx
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Edit `config.yaml`, setting `server.token` to that random value, and
`whatsapp.contact` to the chat to text (default `Instinct`). Relative model, state,
and TLS paths resolve against the config file's directory. The Android app receives
only the shared WebSocket token. Then install the WhatsApp bridge and pair it; see
[WhatsApp](#whatsapp).

The custom `.onnx` must be an **openWakeWord-compatible classifier**, exported
against the matching feature embeddings. Setup downloads the shared feature
models but cannot supply your trained custom phrase. See the
[openWakeWord project](https://github.com/dscripka/openWakeWord) for training/export.

Setup downloads [Whisper tiny.en](https://huggingface.co/Systran/faster-whisper-tiny.en) (run with
faster-whisper, int8 on the CPU)
and [Piper en_US-lessac-medium](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/lessac/medium).
It installs openWakeWord's ONNX dependencies explicitly to avoid the upstream
unconditional TFLite wheel requirement. Python 3.11 is kept inside the project,
so Debian's system Python is untouched. Review model licenses and the Piper
GPL license before redistributing a bundled runtime.

```sh
# Load and validate all models without contacting WhatsApp:
PYTHONPATH=server .venv/bin/python -m voice_assistant.app --config config.yaml --check

# Optional foreground run before installing:
PYTHONPATH=server .venv/bin/python -m voice_assistant.app --config config.yaml

# Install separate live copies, enable boot, and start:
./install
```

Install writes `~/.local/share/pi-voice-assistant/` and
`~/.config/systemd/user/pi-voice-assistant.service`. It creates a fresh runtime
at the installed path, keeps existing installed configuration on upgrades,
and enables user lingering so the service starts without an interactive login.
It does not move the source tree. **After installation, edit the installed
`~/.local/share/pi-voice-assistant/config.yaml`**, then restart:

```sh
systemctl --user restart pi-voice-assistant
systemctl --user status pi-voice-assistant
journalctl --user -u pi-voice-assistant -f
```

A reply wait survives bridge and WhatsApp reconnects within the original deadline.
A temporary startup network failure is handled by the reconnect loops. No Pi
microphone or audio device is needed. If CPU inference cannot keep pace with
16 kHz input, the server closes the connection instead of accumulating stale audio.

## WhatsApp

The Pi texts `whatsapp.contact` from **your own WhatsApp account**, as a linked
device (like WhatsApp Web), and speaks that chat's reply. `bridge/` is a small Go
service built on [whatsmeow](https://github.com/tulir/whatsmeow). It listens only
on `127.0.0.1:8766` and keeps its session keys in `state/whatsapp.db`; treat that
file like a password. Automating a personal account is outside WhatsApp's terms
of service. At one message per spoken request the risk is low, but WhatsApp could
still restrict the account.

Build on the development machine (Go comes from mise if it isn't installed) and
install on the Pi:

```sh
./deploy-bridge.sh
```

Pair once, with your phone number (country code, digits only). The command prints an
8-character code. On your phone, open **WhatsApp → Settings → Linked devices → Link
a device → Link with phone number instead** and enter it:

```sh
ssh tps-l2 'curl -sS -X POST 127.0.0.1:8766/pair -d "{\"phone\": \"15551234567\"}"'
ssh tps-l2 'curl -sS 127.0.0.1:8766/status'            # {"paired":true,"connected":true,...}
ssh tps-l2 'curl -sS "127.0.0.1:8766/contacts?q=instinct"'
ssh tps-l2 'curl -sS "127.0.0.1:8766/chat?contact=Instinct"'  # recent messages, for debugging
```

If the pairing code is rejected ("Couldn't link device"), scan a QR code instead:
tunnel the bridge to this machine and open its page, then use **Link a device** and scan.

```sh
ssh -N -L 18766:127.0.0.1:8766 tps-l2 &
xdg-open http://127.0.0.1:18766/qr
```

`whatsapp.contact` matches a contact, push or business name (exactly, then as a
substring) or takes a phone number with country code.

The chat may also hold conversation typed on your phone, so the bridge tracks it in
both directions and only reads out answers to the voice request: a reply that quotes
the request always counts, and one quoting another message never does. Unquoted
replies count only until you send anything else in that chat, and not at all if your
previous message there was still unanswered when the request went out (Instinct's next
message probably answers that). When in doubt it stays silent and reports no reply.
Answers split across several messages are joined if each follows within
`reply_settle_seconds`. Every request ends with `whatsapp.instruction`, which asks for plain,
unabbreviated sentences with times and room and course numbers written as spoken, since
the reply is read aloud. The reply is cleaned for speech (sign-off,
formatting marks, emoji and links removed) and spoken one sentence at a time, so
playback starts after the first sentence is synthesized. Unlinking the device on the
phone logs the bridge out; pair again to restore it.

## Build the APK

On a Linux development machine install **JDK 17**, `curl`, `unzip`, and the
[Android SDK command-line tools](https://developer.android.com/tools).
Set `JAVA_HOME` and `ANDROID_HOME` to their locations. This project does not
need a globally installed Gradle; the script downloads and checks Gradle 8.9.

```sh
sdkmanager --licenses
sdkmanager 'platforms;android-35' 'build-tools;34.0.0' 'build-tools;35.0.0'
./build-apk.sh
# Output: dist/pi-voice-assistant-debug.apk
```

If using project-local tools, the script recognizes `.tools/jdk` and
`.tools/android-sdk`. Gradle caches and the debug signing key are kept under
`.tools/`. Keep that signing key if you want subsequent `adb install -r` builds
to update the same installation. The output is a debug-signed, test-only sideload APK,
not a Play Store release. `lintDebug` runs as part of the build.

**Do not build on the Pi.** Gradle and lint exhaust a 2 GB Pi and leave it
unresponsive, so `build-apk.sh` refuses to run on ARM or on hosts with less than 4 GB
RAM (`FORCE_LOCAL_BUILD=1` overrides). When the Echo is attached to the Pi by USB,
build on the development machine and install through the Pi's adb:

```sh
./deploy-apk.sh              # build, scp to $PI_HOST:$PI_DIR/dist, adb install, launch
./deploy-apk.sh --no-build   # redeploy the existing dist/ APK
# Defaults: PI_HOST=tps-l2 PI_DIR=HAL
```

## Dashboard

The app is an always-on, full-screen display: 24-hour clock and date; weather from
[Open-Meteo](https://open-meteo.com/) (no API key) for a location looked up from the
Echo's public IP (ipapi.co, falling back to geojs.io; cached for a day), with
today's high/low and the next six hours; today's and tomorrow's events from calendars
synced on the Echo (cancelled and declined events hidden); and a status bar with the
voice state and Pi health (hottest temperature sensor, CPU and RAM use), which the server sends
every 30 seconds. While the assistant is listening, waiting or speaking, a conversation
card replaces the calendar and stays for 20 seconds after the reply. The background
follows sunrise and sunset. The app never dims the screen. A long press
anywhere opens the connection settings with Start/Stop. Text uses the bundled Inter typeface (SIL Open Font License;
`android/app/src/main/assets/licenses/Inter-OFL.txt`); weather icons are drawn in code. Events come from the private
iCal links in `calendar.ical_urls` on the Pi: the server expands recurring events
(icalendar, recurring-ical-events), refreshes every `refresh_minutes`, and sends today's
and tomorrow's events to the Echo; a calendar that fails to load is skipped. With no links
configured the Echo reads its own synced calendars instead, which needs Google Play
services and the `READ_CALENDAR` permission (tap the calendar panel, or
`adb shell pm grant com.instinct.voice android.permission.READ_CALENDAR`). Weather is
fetched by the Echo itself.

## Install on Echo Show 5 / LineageOS (Android 11–13)

LineageOS must already be installed and its microphone/speaker drivers working.
Enable Developer options and USB debugging on the Echo, connect USB, and accept
the device's debugging authorization prompt.

```sh
adb devices
adb install -r -t dist/pi-voice-assistant-debug.apk
adb shell am start -n com.instinct.voice/.MainActivity
```

In **Connection settings**, enter `ws://PI_LAN_IP:8765` and the exact shared
token from `config.yaml`. Use a DHCP reservation for the Pi; `.local` hostname
resolution is not consistent on every Android ROM. Tap **Start**, grant microphone
permission and (Android 13) notifications, and confirm Connected / Idle.
Keep the Pi and Echo on the same reachable LAN. Turn up media volume.
The UI can be closed while the foreground service continues; its notification
provides a Stop action. Stop disables future boot autostart until Start is pressed again.

### Boot microphone access

Android 11–13 restricts microphone capture from background-started services.
For unattended boot on this dedicated device, provision this app as the
**device owner**. The included minimal device-admin receiver requests no device
policies, but device ownership itself is a management privilege. The app does
not wipe, lock, or otherwise configure the device.

On a device eligible for adb device-owner provisioning (typically with no
accounts or existing owner), run:

```sh
adb shell dpm set-device-owner com.instinct.voice/.OwnerReceiver
adb shell dumpsys device_policy
```

**Without device ownership (e.g. a Google account is signed in):** when the Echo stays
USB-connected to the Pi, `./install` also enables `echo-autostart.timer`. Every 30
seconds it checks over adb whether the Echo's voice service is running and, if the
user last pressed Start (not Stop), launches the dashboard with the
`com.instinct.voice.START` action. A visible activity may start the microphone service,
and the dashboard stays in front as the always-on display. This covers Echo reboots and app
updates. It cannot unlock a secure lock screen. Check the last run with
`systemctl --user status echo-autostart.service` (exit status 0 = OK).

If provisioning is rejected, read the command's reason and the ROM's requirements.
Do not factory-reset a device just to bypass the failure. Without device-owner
provisioning, boot posts a notification asking you to open the dashboard and
tap Start; it cannot provide unattended microphone capture on Android 11–13.
This follows the documented [device-owner exemption for microphone foreground
services](https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start).

After provisioning, open the dashboard, grant permissions, press Start once,
and set the app's battery mode to **Unrestricted** in LineageOS settings. Reboot:

```sh
adb reboot
adb wait-for-device
adb shell dumpsys activity services com.instinct.voice
adb logcat -s AndroidRuntime ActivityManager
```

The boot receiver starts the foreground service after `BOOT_COMPLETED` (and
first unlock if the device is credential-encrypted). It does not forcibly open
the dashboard over other apps. Verify the mic on your ROM after reboot by saying
the wake phrase. An Android force-stop suppresses boot broadcasts until the app
is opened again. Android 14+ boot policies are outside this Android 11–13 target;
do not assume the same provisioning suffices there.

This debug build is explicitly marked test-only so adb can remove its device
ownership without resetting the device. Device-owner removal before uninstall is:

```sh
adb shell dpm remove-active-admin com.instinct.voice/.OwnerReceiver
adb uninstall com.instinct.voice
```

## Protocol and operational details

- WebSocket URL: `ws://host:8765` (or `wss://` with configured TLS).
  Header: `Authorization: Bearer <server.token>`. Invalid tokens close with 1008;
  a second microphone closes with 1013.
- Client → server binary messages: raw **16,000 Hz, mono, signed 16-bit
  little-endian PCM**, no WAV header. Android sends 1,280-sample / 2,560-byte
  chunks every 80 ms. Complete-sample fragments are reassembled by the server.
  Maximum incoming message size is 32 KiB.
- Server → client JSON: `{"type":"status","status":"listening",
  "last_transcript":"...","last_reply":"...","message_id":null,"error":null}`.
- WAV transfer, one segment per sentence: `audio_start` JSON with `id`, `seq`,
  `last`, `format:"wav"`, `bytes`, and `duration_seconds`; binary chunks of at most
  32 KiB; then `audio_end` with the same `id`, `seq` and `last`. Each WAV carries
  Piper's actual sample rate.
- Android writes each segment to a temporary cache file and queues it; segments
  play back to back while later ones are still being synthesized and downloaded.
- After the segment marked `last`, Android sends `{"type":"playback_done","id":"<message-id>"}`
  (or `playback_error`). The server remains speaking until a matching acknowledgement
  or timeout. Mic packets continue, containing zeros while transcribing/waiting/
  speaking; the server also discards them. Wake detection cannot trigger on TTS.
- Connection failures retry with 1–30 second backoff. Queues and utterance size
  are bounded. Mic streams that stop for 15 seconds are disconnected.
- `state/requests.sqlite3` records a request ID, transcript, reply, creation time,
  and delivery/playback state. A reconnect resumes the latest pending reply watch
  or replays an unacknowledged reply; a request past `reply_timeout_seconds` is
  marked expired instead. A process crash while sending is ambiguous; it keeps
  watching for the reply but **never automatically resends**. Requests are not
  deduplicated across distinct new spoken commands. State is retained indefinitely;
  stop the service before deleting the database to clear history.
- Raw microphone audio is held in bounded memory and not saved, unless
  `audio.debug_save_utterances` is on: then the last five recordings are kept in
  `state/utterances/` for tuning speech recognition. Transcripts and replies are
  retained in SQLite and in the WhatsApp chat. The shared token is stored in the
  Android app's private preferences, with Android backup disabled.

Plain `ws://` sends the token and audio unencrypted; use it only on a trusted
private LAN. For other networks configure `server.tls_cert` and `server.tls_key`
and a `wss://` URL whose hostname matches a certificate trusted by Android.
Certificate verification is never disabled. Do not expose the plain port to the
internet. The bridge's HTTP API has no authentication, so keep it on localhost.

## Tests and troubleshooting

Lightweight server tests don't require speech models or WhatsApp:

```sh
python3 -m venv .test-venv
.test-venv/bin/pip install PyYAML==6.0.2 websockets==15.0.1
PYTHONPATH=server .test-venv/bin/python -m unittest discover -s server/tests -v
bash -n setup.sh install uninstall build-apk.sh deploy-apk.sh build-bridge.sh deploy-bridge.sh scripts/echo-autostart.sh
(cd bridge && go vet ./...)
```

Tests cover fragmented PCM, silence/max-duration endpointing, wake-relative speech
thresholds and gain, reply cleanup and sentence chunking, the WhatsApp client against
a fake bridge (multi-message replies, timeouts, bridge down), saved IDs, recovery
without resending, expiry, streamed WAV segments, and acknowledgement gating. They
do not contact WhatsApp. Actual wake accuracy, Pi inference latency, WhatsApp round trips,
Echo audio routing, and boot behavior require the target hardware and credentials.

If Listening never appears, check the custom classifier and lower
`audio.wake_threshold` cautiously. If recordings end too early or never end,
adjust `audio.speech_rms` and `silence_seconds` for ambient noise; the server logs
the wake-phrase and utterance levels. If Waiting times out, check
`curl 127.0.0.1:8766/status` on the Pi and `journalctl --user -u wa-bridge`.
If connected but the microphone is silent after reboot, check device-owner status,
microphone permission, the Android microphone privacy toggle, and ROM drivers.

`./uninstall` stops the Pi services and removes their installed code/runtime and units.
It preserves installed config, models, history (including the WhatsApp session in
`state/whatsapp.db`), and user lingering. Remove these
manually if desired after reviewing their contents. The source project is untouched.
