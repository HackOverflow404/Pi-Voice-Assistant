"""Removes the Echo's listening chime from a recorded request.

The Echo plays a rising two-note chime as it starts listening, and its microphone records it,
usually on top of the first words. The chime is synthesized (android/.../Chimes.kt), so the
Pi renders the same waveform, finds it in the recording by cross-correlation, fits the
speaker-to-microphone path as a short FIR filter by regularized least squares, and subtracts
the predicted chime. Speech doesn't correlate with the chime, so it survives. On the saved
recordings this removed 8-18 dB of chime and turned "turn on my load" into "turn on my lamp";
it also keeps the chime from setting normalize()'s gain, which it otherwise always did.
"""
import numpy as np

RATE = 16000
LISTENING = ((329.6, 0.0, 0.26), (493.9, 0.11, 0.34))  # Chimes.kt LISTENING: E4 -> B4
TAPS = 96          # 6 ms of speaker/room/microphone response
SEARCH_SECONDS = 2.5  # the chime plays within this long of the recording starting
MIN_REMOVED_DB = 1.0  # less than this means no chime was found; leave the audio alone


def render(notes=LISTENING, rate=RATE):
    """Chimes.kt's synthesis: sine notes gliding up from 3% flat, soft attack, exponential
    decay, a trace of the octave. Scaled to a peak of 1."""
    total = max(start + length for _, start, length in notes)
    mix = np.zeros(int(total * rate))
    for freq, start, length in notes:
        t = np.arange(int(length * rate)) / rate
        phase = np.cumsum(2 * np.pi * freq * (1 - 0.03 * np.exp(-t / 0.008)) / rate)
        tone = np.minimum(1.0, t / 0.006) * np.exp(-t / (length * 0.3)) * (np.sin(phase) + 0.12 * np.sin(2 * phase))
        offset = int(start * rate)
        mix[offset:offset + len(tone)] += tone[:len(mix) - offset]
    return mix / np.abs(mix).max()


REFERENCE = render()


def correlate(signal, kernel):
    """np.correlate(signal, kernel, 'valid'), by FFT."""
    n = len(signal) + len(kernel)
    size = 1 << (n - 1).bit_length()
    full = np.fft.irfft(np.fft.rfft(signal, size) * np.conj(np.fft.rfft(kernel, size)), size)
    return full[:len(signal) - len(kernel) + 1]


def remove(pcm, reference=REFERENCE, taps=TAPS):
    """(PCM with the chime subtracted, dB removed over the chime's span). Returns the input
    unchanged, and 0.0, when there is no chime to remove."""
    x = np.frombuffer(pcm, dtype='<i2').astype(float)
    span = len(reference) + taps
    search = x[:int(SEARCH_SECONDS * RATE) + span]
    if len(search) < span:
        return pcm, 0.0
    start = max(int(np.argmax(np.abs(correlate(search, reference)))) - taps // 4, 0)  # room for pre-delay
    y = x[start:start + span]
    if len(y) < span:
        return pcm, 0.0
    # Least squares for the path h in y ~ reference * h. Every shift up to `taps` keeps the whole
    # reference inside the span, so the normal equations are exactly Toeplitz in its autocorrelation.
    auto = correlate(np.concatenate([reference, np.zeros(taps)]), reference)[:taps]
    gram = auto[np.abs(np.subtract.outer(np.arange(taps), np.arange(taps)))]
    path = np.linalg.solve(gram + 1e-3 * auto[0] * np.eye(taps), correlate(y, reference)[:taps])
    predicted = np.zeros(span)
    predicted[:span - 1] = np.convolve(reference, path)  # len(reference) + taps - 1 samples
    residual = y - predicted
    before, after = np.sqrt(np.mean(y ** 2)), np.sqrt(np.mean(residual ** 2))
    if not before or not after:
        return pcm, 0.0
    removed = 20 * np.log10(before / after)
    if removed < MIN_REMOVED_DB:
        return pcm, 0.0
    x[start:start + span] = residual
    return np.clip(np.round(x), -32768, 32767).astype('<i2').tobytes(), float(removed)
