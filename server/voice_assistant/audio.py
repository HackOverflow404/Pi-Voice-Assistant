"""16 kHz, mono, signed little-endian PCM framing and endpoint detection."""
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


class Capture:
    def __init__(self, config):
        self.config = config
        self.frames = []
        self.speech = False
        self.silence = 0.0

    def feed(self, frame):
        self.frames.append(frame)
        samples = struct.unpack('<' + 'h' * (len(frame) // 2), frame)
        rms = math.sqrt(sum(x * x for x in samples) / len(samples))
        if rms >= self.config['speech_rms']:
            self.speech = True
            self.silence = 0.0
        else:
            self.silence += FRAME_SECONDS
        elapsed = len(self.frames) * FRAME_SECONDS
        return (elapsed >= self.config['max_utterance_seconds'] or
                (self.speech and self.silence >= self.config['silence_seconds']) or
                (not self.speech and elapsed >= self.config['start_timeout_seconds']))

    @property
    def pcm(self):
        return b''.join(self.frames) if self.speech else b''
