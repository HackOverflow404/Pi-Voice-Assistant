"""16 kHz, mono, signed little-endian PCM framing and endpoint detection."""
from array import array
import re
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


def wake_is_plausible(config, wake_levels, before_levels):
    """A wake-word hit counts only if the phrase was clearly louder than the room: the model
    also fires on near-silence and on TV chatter. Real wakes measured 12-43x the noise
    floor; false ones 1.5-4x. The room level comes from before the phrase, so the phrase
    can't raise it; with no history yet, only the absolute minimum applies."""
    peak = max(wake_levels, default=0.0)
    noise = sorted(before_levels)[len(before_levels) // 5] if before_levels else 0.0
    return peak >= max(20.0, config.get('wake_min_ratio', 6.0) * noise)


# A spoken cancel is one or more of these words, padded with any of the fillers:
# "actually cancel", "ignore the request", "I said never mind", "no no, stop".
CANCEL_WORDS = {'cancel', 'nevermind', 'stop', 'forget', 'ignore', 'disregard', 'scratch', 'abort',
                'nothing', 'no', 'nope'}
CANCEL_FILLERS = {'actually', 'wait', 'sorry', 'oh', 'um', 'uh', 'hmm', 'okay', 'ok', 'please', 'just',
                  'hey', 'clippy', 'i', 'said', 'mean', 'that', 'it', 'this', 'about', 'the', 'my', 'last',
                  'request', 'message', 'question', 'one'}
# The same cancel said at the end of a request, as a correction: "what's the weather,
# actually never mind". It must follow punctuation or a correction word, so a request that
# merely ends in the words ("tell Sam to forget it") is still sent.
TRAILING_CANCEL = re.compile(
    r'(?:[,.?!;]|\b(?:actually|wait|sorry|no))[\s,]*(?:(?:actually|wait|sorry|oh|no|um|uh)[\s,]+)*'
    r'(?:cancel(?: that| it| this| the request| my request)?|never ?mind(?: that)?|nvm|'
    r'forget (?:it|that|about it)|ignore (?:that|this|it|the request|my request)|scratch that|'
    r'disregard(?: that| it| this)?)[\s.!]*$')


def is_cancel_phrase(text):
    """The request was a spoken "cancel", alone or as a closing correction: discard it."""
    lowered = re.sub(r'\b(?:never ?mind|nvm)\b', 'nevermind', text.lower())
    words = re.sub(r'[^a-z ]', ' ', lowered).split()
    rest = [w for w in words if w not in CANCEL_FILLERS]
    if rest and all(w in CANCEL_WORDS for w in rest):
        return True
    return bool(TRAILING_CANCEL.search(text.lower().strip()))


# What Whisper writes when it hears no speech (TV, a cough, the room), per its well-known
# hallucinations on non-speech audio. A request that is only this is an accidental trigger.
NOISE_TRANSCRIPTS = {'you', 'thank you', 'thanks', 'thank you very much', 'thanks for watching',
                     'thank you for watching', 'thank you so much for watching', 'bye', 'bye bye',
                     'oh', 'um', 'uh', 'hmm', 'mm', 'ah', 'huh', 'so', 'okay', 'the',
                     'subtitles by the amaraorg community', 'please subscribe'}


def is_noise_transcript(text):
    """True when the transcript is non-speech: only bracketed sound tags like [Music] or
    (upbeat music), or one of Whisper's stock phrases for silence."""
    spoken = re.sub(r'\[[^\]]*\]|\([^)]*\)|[♪*]', ' ', text.lower())
    words = re.sub(r'[^a-z ]', '', spoken).split()
    return not words or ' '.join(words) in NOISE_TRANSCRIPTS


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
