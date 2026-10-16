# airplay-relay

Makes the Pi an AirPlay speaker that plays on the Echo Show. shairport-sync writes the audio
to stdout, `relay.py` streams the raw PCM (44.1 kHz, stereo, s16le) to the Echo's voice app over
USB (`adb reverse tcp:8767`, kept up by `scripts/echo-autostart.sh`), and the app plays it as
media (`android/.../AirPlay.kt`).

On the Echo, AirPlay takes audio focus like any media player. Starting it pauses Spotify and the
laptop's Bluetooth stream, starting either of them silences AirPlay, and the assistant's speech
interrupts it. AirPlay keeps streaming while silenced, because the phone can't be paused from
the Echo.

## Install

```sh
ssh -t mainframe 'sudo systemctl disable --now shairport-sync'   # once: the stock service
./install          # from this machine; PI_HOST overrides mainframe
./uninstall        # then re-enable the stock service to get AirPlay on the Pi itself
```

The Echo app (`../deploy-apk.sh`) and `echo-autostart.sh` (`../install` on the Pi) must also
be current.

## Tuning

- Delay: about 0.3 s from shairport-sync to the speaker, which `shairport-sync.conf` offsets
  (`audio_backend_latency_offset_in_seconds`) so AirPlay video stays in sync.
- Logs: `journalctl --user -u airplay-relay` on the Pi gives connects, and a per-minute
  streamed/dropped summary. On the Echo, `adb logcat -s AirPlay`.
