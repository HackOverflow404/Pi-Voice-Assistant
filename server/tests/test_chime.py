import unittest

import numpy as np

from voice_assistant import chime


def recording(with_chime=True, seed=0):
    """3 s of speech-like noise with the chime played through a short echoey path at 1 s."""
    rng = np.random.default_rng(seed)
    speech = np.convolve(rng.normal(0, 60, 48000), np.ones(8) / 8, mode='same')
    if with_chime:
        path = np.zeros(40); path[5], path[18], path[33] = 0.8, -0.3, 0.1
        echo = np.convolve(chime.REFERENCE * 200, path)
        speech[16000:16000 + len(echo)] += echo
    return np.clip(speech, -32768, 32767).astype('<i2').tobytes()


class ChimeTests(unittest.TestCase):
    def test_reference_matches_chimes_kt(self):
        self.assertEqual(len(chime.REFERENCE), int(0.45 * chime.RATE))
        self.assertAlmostEqual(float(np.abs(chime.REFERENCE).max()), 1.0)

    def test_removes_the_chime_and_keeps_the_speech(self):
        pcm = recording()
        clean, removed = chime.remove(pcm)
        self.assertGreater(removed, 6)
        speech = np.frombuffer(recording(with_chime=False), dtype='<i2').astype(float)
        error = np.frombuffer(clean, dtype='<i2').astype(float) - speech
        self.assertLess(np.sqrt(np.mean(error ** 2)), 0.25 * np.sqrt(np.mean(speech ** 2)))

    def test_no_chime_leaves_audio_unchanged(self):
        pcm = recording(with_chime=False)
        self.assertEqual(chime.remove(pcm), (pcm, 0.0))

    def test_too_short(self):
        self.assertEqual(chime.remove(b'\0' * 100), (b'\0' * 100, 0.0))
