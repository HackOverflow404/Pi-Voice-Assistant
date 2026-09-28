"""16 kHz, mono, signed little-endian PCM framing and endpoint detection."""
from array import array
import math
import struct

RATE = 16000
FRAME_SAMPLES = 1280  # openWakeWord's native 80 ms window
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_SECONDS = FRAME_SAMPLES / RATE


class Framer:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        while len(self.buffer) >= FRAME_BYTES:
            frame = bytes(self.buffer[:FRAME_BYTES])
            del self.buffer[:FRAME_BYTES]
            yield frame


def rms(frame):
    samples = struct.unpack('<' + 'h' * (len(frame) // 2), frame)
    return math.sqrt(sum(x * x for x in samples) / len(samples)) if samples else 0.0


def speech_threshold(config, wake_levels, idle_levels):
    """Speech level scaled to how loudly the wake phrase was just said: the request
    follows at the same distance and volume, and mic gain varies by device. Kept above
    the room's noise floor, never below 30, and capped by config speech_rms."""
    peak = max(wake_levels, default=0.0)
    noise = sorted(idle_levels)[len(idle_levels) // 5] if idle_levels else 0.0
    return min(config['speech_rms'], max(0.3 * peak, 2.5 * noise, 30.0))


def normalize(pcm, target=4000.0, max_gain=40.0):
    """Boost a quiet recording so its loudest 80 ms frame reaches about `target` RMS.
    Some microphones (the Echo's VOICE_RECOGNITION source) deliver speech near -50 dBFS,
    where Vosk drops words. Assumes a little-endian host, like the PCM itself."""
    peak = max((rms(pcm[i:i + FRAME_BYTES]) for i in range(0, len(pcm), FRAME_BYTES)), default=0.0)
    gain = min(max_gain, target / peak) if peak else 1.0
    if gain <= 1.0:
        return pcm, 1.0
    boosted = array('h', (max(-32768, min(32767, int(x * gain))) for x in array('h', pcm)))
    return boosted.tobytes(), gain


class Capture:
    def __init__(self, config, threshold=None):
        self.config = config
        self.threshold = threshold or config['speech_rms']
        self.frames = []
        self.speech = False
        self.silence = 0.0
        self.peak = 0.0
        # The Echo plays a chime as listening starts; don't let it count as speech or silence.
        self.grace = config.get('listen_grace_seconds', 0.0)

    def feed(self, frame):
        self.frames.append(frame)
        elapsed = len(self.frames) * FRAME_SECONDS
        if elapsed <= self.grace:
            return False
        level = rms(frame)
        self.peak = max(self.peak, level)
        if level >= self.threshold:
            self.speech = True
            self.silence = 0.0
        else:
            self.silence += FRAME_SECONDS
        return (elapsed >= self.config['max_utterance_seconds'] or
                (self.speech and self.silence >= self.config['silence_seconds']) or
                (not self.speech and elapsed >= self.config['start_timeout_seconds']))

    @property
    def pcm(self):
        return b''.join(self.frames) if self.speech else b''
