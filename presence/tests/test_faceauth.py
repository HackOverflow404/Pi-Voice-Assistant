import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from faceauth import AuthConfig, Challenges, signed_message  # noqa: E402

NONCE = 'ab' * 16


class ChallengeTests(unittest.TestCase):
    def setUp(self):
        self.c = AuthConfig(threshold=0.5, matches=2, window=6.0, hz=4, min_gap=1.0)
        self.ch = Challenges(self.c)

    def test_rejects_malformed(self):
        for nonce, host in [('xyz', 'laptop'), ('ab' * 8, 'laptop'), (NONCE, 'bad host'), (NONCE, None), (5, 'l')]:
            self.assertFalse(self.ch.offer(0, nonce, host))
        self.assertFalse(self.ch.due(0))

    def test_consecutive_matches_while_near(self):
        self.assertTrue(self.ch.offer(0, NONCE, 'laptop'))
        self.assertTrue(self.ch.due(0))
        self.assertIsNone(self.ch.observe(0, True, 0.7))
        self.assertFalse(self.ch.due(0.1))   # 4 Hz
        ok, ch = self.ch.observe(0.25, True, 0.6)
        self.assertTrue(ok)
        self.assertEqual((ch['nonce'], ch['host'], ch['best']), (NONCE, 'laptop', 0.7))
        self.assertFalse(self.ch.due(1))

    def test_a_miss_resets_the_streak(self):
        self.ch.offer(0, NONCE, 'laptop')
        for t, near, score in [(0, True, 0.7), (0.25, True, 0.3), (0.5, True, 0.7), (0.75, True, None)]:
            self.assertIsNone(self.ch.observe(t, near, score))

    def test_not_near_never_matches(self):
        self.ch.offer(0, NONCE, 'laptop')
        t, result = 0.0, None
        while result is None:
            result = self.ch.observe(t, False, 0.9)
            t += 0.25
        self.assertFalse(result[0])
        self.assertGreaterEqual(t, self.c.window)

    def test_one_at_a_time_and_rate_limited(self):
        self.assertTrue(self.ch.offer(0, NONCE, 'laptop'))
        self.assertFalse(self.ch.offer(0.1, 'cd' * 16, 'laptop'))  # one already open
        self.ch.observe(0, True, 0.9)
        self.ch.observe(0.25, True, 0.9)                          # decided
        self.assertFalse(self.ch.offer(0.5, 'cd' * 16, 'laptop'))  # within min_gap of the last offer
        self.assertTrue(self.ch.offer(1.1, 'cd' * 16, 'laptop'))

    def test_signed_message_binds_host_nonce_time(self):
        self.assertEqual(signed_message('h', NONCE, 123), f'echo-faceauth/v1\nh\n{NONCE}\n123\nok'.encode())


if __name__ == '__main__':
    unittest.main()
