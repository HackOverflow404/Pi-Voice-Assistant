# Pi Voice Assistant

A Raspberry Pi runs wake-word detection, speech recognition, email, and speech
synthesis. An Echo Show 5 running LineageOS supplies the microphone, speaker,
and Jetpack Compose dashboard. No cloud speech service is used.

```
Echo mic → authenticated WebSocket → openWakeWord → utterance capture → Vosk
    → Gmail SMTP → medhanshgarg@mail.instinct.com
    ← matching email reply via IMAP IDLE ← Piper WAV ← Echo speaker
```

The server emits `idle`, `listening`, `transcribing`, `waiting`, and `speaking`
events with the last transcript and reply. Only one microphone client can own
the server at a time. Say the trained wake phrase, pause briefly for the
Listening card, then speak. One second of quiet ends the utterance by default.
There is a 5-second no-speech timeout and a 20-second utterance limit.

## Files

- `server/voice_assistant/`: Python server, audio framing, speech engines, mail, SQLite outbox.
- `android/`: Kotlin app, minimum API 26, target API 33, compiled against API 35.
- `config.example.yaml`: configuration template; `config.yaml` is private and ignored by Git.
- `models/`: custom wake classifier plus downloaded Vosk / Piper models.
- `setup.sh`: Debian packages, isolated Python 3.11 runtime, dependencies, model downloads.
- `install`, `uninstall`: install/remove separate live copies and a systemd user unit.
- `build-apk.sh`: checked Gradle distribution download, APK build, Android lint.

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

Edit `config.yaml`, setting `server.token` to that random value, and the Gmail
username and app password. The default destination is already
`medhanshgarg@mail.instinct.com`. Relative model, state, and TLS paths resolve
against the config file's directory. Keep the app password only on the Pi;
the Android app receives only the shared WebSocket token.

The custom `.onnx` must be an **openWakeWord-compatible classifier**, exported
against the matching feature embeddings. Setup downloads the shared feature
models but cannot supply your trained custom phrase. See the
[openWakeWord project](https://github.com/dscripka/openWakeWord) for training/export.

Setup downloads [Vosk small English 0.15](https://alphacephei.com/vosk/models)
and [Piper en_US-amy-low](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/amy/low).
It installs openWakeWord's ONNX dependencies explicitly to avoid the upstream
unconditional TFLite wheel requirement. Python 3.11 is kept inside the project,
so Debian's system Python is untouched. Review model licenses and the Piper
GPL license before redistributing a bundled runtime.

```sh
# Load and validate all models without connecting to Gmail:
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

The service reconnects to IMAP within the original reply deadline. A temporary
startup network failure is handled by the client/mail reconnect loops. No Pi
microphone or audio device is needed. If CPU inference cannot keep pace with
16 kHz input, the server closes the connection instead of accumulating stale audio.

## Gmail app password

1. Use a Gmail account that can send to the destination and receive its replies.
2. Enable [2-Step Verification](https://support.google.com/accounts/answer/185839).
3. Open [Google App passwords](https://myaccount.google.com/apppasswords), create
   a password named “Pi Voice Assistant,” and put its 16-character value into
   `mail.app_password`. Spaces in the displayed password are stripped by the server.
4. Set `mail.username` to the full Gmail address. Keep the default SMTP SSL
   endpoint `smtp.gmail.com:465` and IMAP SSL endpoint `imap.gmail.com:993`.
5. Ensure IMAP is permitted for the account. Workspace administrators can
   restrict IMAP/app passwords. If App passwords is unavailable, consult
   [Google's eligibility guidance](https://support.google.com/accounts/answer/185833);
   a normal account password will not work here.

Reply from the recipient using the email client's **Reply** action. The reply
must contain the original exact `Message-ID` in `In-Reply-To` and a `text/plain`
MIME body. Subject matching and `References` alone do not count. HTML-only replies
are ignored. Multipart plain-text alternatives are supported; attachments are
excluded. The plain text, including any quoted history/signature, is spoken up
to `max_reply_chars`. Set the reply sender to avoid unnecessary quoted history.

The default watched mailbox is `INBOX`; replies routed to Spam or archived by
filters will not be seen there. Set `mail.mailbox` to the correct IMAP folder if
needed (Gmail special folder names may be localized). The recipient must actually
produce a reply; sending email does not itself create an assistant response.

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
- WAV transfer: `audio_start` JSON with `id`, `format:"wav"`, `bytes`, and
  `duration_seconds`; binary chunks of at most 32 KiB; then `audio_end` with
  the same `id`. The WAV carries Piper's actual sample rate.
- Android writes the streamed WAV to a bounded temporary cache file and plays
  it once the transfer completes. This is chunked transfer, not progressive
  speech generation. No base64 conversion or sample-rate assumptions are used.
- After playback Android sends `{"type":"playback_done","id":"<message-id>"}`
  (or `playback_error`). The server remains speaking until a matching acknowledgement
  or timeout. Mic packets continue, containing zeros while transcribing/waiting/
  speaking; the server also discards them. Wake detection cannot trigger on TTS.
- Connection failures retry with 1–30 second backoff. Queues and utterance size
  are bounded. Mic streams that stop for 15 seconds are disconnected.
- `state/requests.sqlite3` records Message-ID, transcript, reply, creation time,
  and delivery/playback state. A reconnect resumes the latest pending reply watch
  or replays an unacknowledged reply. A process crash during SMTP is ambiguous;
  it watches the stored ID but **never automatically resends**. Requests are not
  deduplicated across distinct new spoken commands. State is retained indefinitely;
  stop the service before deleting the database to clear history.
- Raw microphone audio is held in bounded memory and not saved. Transcripts and
  replies are retained in SQLite and Gmail. The shared token is stored in the
  Android app's private preferences, with Android backup disabled.

Plain `ws://` sends the token and audio unencrypted; use it only on a trusted
private LAN. For other networks configure `server.tls_cert` and `server.tls_key`
and a `wss://` URL whose hostname matches a certificate trusted by Android.
Certificate verification is never disabled. Do not expose the plain port to the
internet. Gmail connections always use verified TLS.

## Tests and troubleshooting

Lightweight server tests don't require speech models or Gmail credentials:

```sh
python3 -m venv .test-venv
.test-venv/bin/pip install PyYAML==6.0.2 websockets==15.0.1 IMAPClient==3.0.1
PYTHONPATH=server .test-venv/bin/python -m unittest discover -s server/tests -v
bash -n setup.sh install uninstall build-apk.sh
```

Tests cover fragmented PCM, silence/max-duration endpointing, exact email
matching, MIME alternatives, IMAP IDLE, saved IDs, recovery without resending,
WAV transfer, and acknowledgement gating with fake inference/mail. They do not
send email. Actual wake accuracy, Pi inference latency, Gmail round trips,
Echo audio routing, and boot behavior require the target hardware and credentials.

If Listening never appears, check the custom classifier and lower
`audio.wake_threshold` cautiously. If recordings end too early or never end,
adjust `audio.speech_rms` and `silence_seconds` for ambient noise. If Waiting
times out, inspect the reply's `In-Reply-To` header and watched mailbox. Gmail
authentication failures often mean a revoked app password or account policy.
If connected but the microphone is silent after reboot, check device-owner status,
microphone permission, the Android microphone privacy toggle, and ROM drivers.

`./uninstall` stops the Pi service and removes its installed code/runtime and unit.
It preserves installed config, models, history, and user lingering. Remove these
manually if desired after reviewing their contents. The source project is untouched.
