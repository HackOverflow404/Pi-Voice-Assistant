# echo-presence

Part of Clippy. It watches the Echo Show 5's camera from mainframe (the Pi) and tells the
dashboard how close someone is, and optionally which hand gesture they made. It runs as its own
user service (`echo-presence`), with its own venv and lower CPU priority, so a MediaPipe problem
can't take the wake word down with it. The server side is `server/voice_assistant/hass.py`
(relay) and `Session.gesture` in `app.py`; the Echo side is `Dashboard.kt` and `Media.kt`.

```
Echo camera → SECUR-T hub (localhost:7002, H.264) → ffmpeg → MediaPipe on the Pi
  → MQTT clippy/vision/{presence,gesture} → pi-voice-assistant (hass.py) → Echo dashboard
```

| Zone | What the Echo does |
|---|---|
| `near` | Full dashboard (clock, weather curve, agenda, voice and Pi tiles) |
| `far` | Tiles glide into a large clock, the weather and the next event |
| `away` (nobody for 90 s) | Far layout (the screen stays at full brightness) |
| `unknown` (service down, no video) | Full dashboard |

## How presence is decided (`logic.py`)

- Face detection runs about 4 times a second (18 ms per frame on a Pi 4). The largest face's
  width, as a fraction of the frame width, stands in for distance.
- `near` starts at `near_enter` (0.12) and ends below `near_exit` (0.095). The band between
  them stops leaning back and forth from flipping the layout.
- A zone has to hold before it is reported: 0.4 s to become near, 1.0 s to become far, and
  0.5 s when coming back from away.
- With no face (your back is turned, or you're across the room, where faces are too small to
  detect), a person detector runs every 1.5 s (250 ms each). It runs on the left and right square
  of the frame in turn: squashing the whole 16:9 frame into its 320x320 input dropped a person
  across the room from a 0.9 score to 0.11. A person seen keeps you present for 6 s.
- Near with no face for 3.5 s becomes far right away, unless the body seen is at least
  `person_near` (0.85) of the frame tall. That's you at the desk looking down at a laptop (1.0);
  across the room is about 0.57. Walking out of frame entirely also becomes far (then away after
  90 s). At the desk the face drops out for up to ~17 s at a time, but the body scored 0.99-1.0 on
  every check, so near holds. A body alone never switches far to near; that takes a face.
- No face or person for 90 s means away.

The Echo animates each change. Both layouts stay laid out, and the morph only changes GPU layer
transforms (no re-layout), because the Echo's CPU can't re-measure the dashboard 60 times a second.

## Calibrating distances

```sh
ssh mainframe
systemctl --user stop echo-presence
cd ~/.local/share/echo-presence && .venv/bin/python presence.py --debug   # logs "face 0.141 …" ~4x/s
# stand where "near" should begin, note the face width; then where it should end
systemctl --user start echo-presence
```

Calibrated 2026-10-10: desk 0.13–0.19, near/far boundary median 0.128 (p10 0.119), and no
face found at the far spot (about 3 m). That gives `near_enter` 0.115 and `near_exit` 0.095.

Set `near_enter` to the width where near should begin, and `near_exit` about 20% lower, in
`~/.local/share/echo-presence/config.toml` on the Pi. `./install` never overwrites that file.

## Hand gestures (off, waiting for a Pi 5)

The hand stage is implemented and unit-tested, but disabled (`[hands] enabled = false`). It
handles an open palm (stop or dismiss, otherwise play/pause) and swipes (next/previous). Measured
on the Pi 4, 2026-10-10:

| Model | Per frame |
|---|---|
| MediaPipe gesture recognizer (Tasks API) | 191 ms (≈5 fps) |
| Palm detector lite, LiteRT, 2 threads | 83 ms |
| Hand landmarks lite, LiteRT, 4 threads | 41 ms |

Reliable swipes need 12–15 fps of tracking. That would take all four cores and starve Clippy's
wake word. MediaPipe 1.x also can't run on a Pi 4 at all: its wheels need the ARMv8 AES
extension, so the process dies with SIGILL (`requirements.txt` pins 0.10.18). On a Pi 5, move to
the current MediaPipe, set `enabled = true`, and run `./install`.

The server's palm handling comes first: a ringing timer is dismissed, then Clippy talking (or
about to send) is stopped, and otherwise music toggles. A request already sent is never unsent.

## Face unlock (`faceauth.py`)

echo-unlock on the laptop (`~/Documents/Projects/claude/echo-unlock`) unlocks it when you sit
down at the desk. When presence turns `near` it publishes a one-time challenge
(`clippy/faceauth/challenge`). For the next 6 s, this service runs OpenCV's YuNet face detector
(at 320x180, ~37 ms) and SFace recognizer (~175 ms) on the newest frames, 3 times a second. It
answers on `clippy/faceauth/verdict`. A match needs 2 consecutive checks where the largest face
scores at least `threshold` against the enrolled samples while the zone is `near`. Only matches
are signed (Ed25519, `faceauth/ed25519.pem`, created by `install`). The signature covers the
laptop's hostname, the nonce and the time. Nothing runs between challenges.

```sh
cd ~/.local/share/echo-presence
.venv/bin/python faceauth.py enroll   # ~30 s at the desk, turning your head a little
.venv/bin/python faceauth.py test     # live scores, for choosing [faceauth] threshold
systemctl --user restart echo-presence
.venv/bin/python faceauth.py pubkey   # what the laptop pins (echo-unlock/install fetches it)
```

The models (`face_detection_yunet_2023mar.onnx`, `face_recognition_sface_2021dec.onnx`) come
from OpenCV Zoo and are checksummed by `install`. `faceauth/` (the key and the enrolled samples)
is only on the Pi and is never copied off it. Set `[faceauth] enabled = false` to stop answering.

## Files

- `presence.py`: stream reader (newest frame only), models, MQTT and Home Assistant discovery
  (`sensor.presence`, `binary_sensor.someone_at_the_echo`, `event.gesture` on the Clippy device)
- `logic.py`: zone hysteresis and gesture state machines, with no MediaPipe or I/O
- `faceauth.py`: face verification for echo-unlock (challenges, YuNet + SFace, signing, enroll CLI)
- `tests/`: `python3 -m unittest discover -s tests`
- `config.toml`: defaults, copied to the Pi on first install only
- `systemd/echo-presence.service`: user unit with Nice=10 and CPUWeight=30, so Clippy and Home
  Assistant win any CPU contention
- `install` / `uninstall`: run from the development machine (`presence/install`), not on the
  Pi. `PI_HOST` defaults to `mainframe`.
  `uninstall` also clears the retained MQTT topics and the Home Assistant entities.

## Cost on the Pi 4 (measured)

About 45% of one core (detection 20%, ffmpeg decode 25%) and about 370 MB RSS. The CPU runs
4–5 °C warmer (≈70 °C). The Pi 4's hardware H.264 decoder (`h264_v4l2m2m`) returns no frames for
this stream, so decoding is in software, with the loop filter skipped.
`stream.keyframes_only = true` cuts decode to almost nothing, but zone changes then lag about 2 s.

## Testing without the camera

```sh
ssh mainframe 'mosquitto_pub -h 127.0.0.1 -r -t clippy/vision/presence -m "{\"zone\": \"far\"}"'
ssh mainframe 'mosquitto_pub -h 127.0.0.1 -t clippy/vision/gesture -m "{\"gesture\": \"next\", \"at\": $(date +%s)}"'
```

A fake presence message stays on screen until the real service reports its next change.
Gestures older than 3 s are ignored.
