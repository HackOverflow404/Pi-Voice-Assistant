import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from logic import GestureConfig, Gestures, Hand, ZoneConfig, ZoneTracker  # noqa: E402

FPS = 15


def run(tracker, start, seconds, hz=5, **seen):
    """Feed the same observation for a while; return [(time, zone)] for each change."""
    changes, t = [], start
    while t < start + seconds:
        zone = tracker.observe(t, **seen)
        if zone:
            changes.append((round(t, 2), zone))
        t += 1 / hz
    return changes


class ZoneTests(unittest.TestCase):
    def setUp(self):
        self.c = ZoneConfig()
        self.z = ZoneTracker(self.c, now=0)

    def test_first_sighting_after_a_short_dwell(self):
        changes = run(self.z, 0, 2, face_width=0.2)
        self.assertEqual(changes[0][1], 'near')
        self.assertAlmostEqual(changes[0][0], self.c.to_back, delta=0.25)

    def test_hysteresis_band_holds_the_zone(self):
        run(self.z, 0, 2, face_width=0.2)  # near
        between = (self.c.near_enter + self.c.near_exit) / 2
        self.assertEqual(run(self.z, 2, 10, face_width=between), [])  # leaning back a little: still near
        changes = run(self.z, 12, 3, face_width=0.05)
        self.assertEqual([z for _, z in changes], ['far'])
        self.assertEqual(run(self.z, 15, 10, face_width=between), [])  # and the band holds far too

    def test_a_flicker_shorter_than_the_dwell_changes_nothing(self):
        run(self.z, 0, 3, face_width=0.05)  # far
        self.assertEqual(run(self.z, 3, 0.4, face_width=0.2), [])
        self.assertEqual(run(self.z, 3.4, 5, face_width=0.05), [])

    def test_turning_away_keeps_near_then_becomes_far(self):
        run(self.z, 0, 2, face_width=0.2)
        lost = self.c.face_lost
        self.assertEqual(run(self.z, 2, lost - 0.5, person=0.57), [])  # back turned: still near for now
        changes = run(self.z, 1.5 + lost, 3, person=0.57)  # but the body is across-the-room sized
        self.assertEqual([z for _, z in changes], ['far'])
        self.assertLess(changes[0][0] - 2, lost + 0.3)  # no extra dwell after face_lost

    def test_walking_out_of_frame_goes_far_then_away(self):
        run(self.z, 0, 2, face_width=0.2)  # near, then nothing at all is seen
        changes = run(self.z, 2, self.c.away_after + 5)
        self.assertEqual([z for _, z in changes], ['far', 'away'])
        self.assertLess(changes[0][0] - 2, self.c.face_lost + 0.3)  # not held near until away

    def test_sitting_down_is_quick(self):
        run(self.z, 0, 3, face_width=0.05)  # far
        changes = run(self.z, 3, 2, face_width=0.2)
        self.assertEqual([z for _, z in changes], ['near'])
        self.assertLess(changes[0][0] - 3, 0.7)

    def test_looking_down_at_the_desk_stays_near(self):
        run(self.z, 0, 2, face_width=0.18)
        self.assertEqual(run(self.z, 2, 60, hz=0.67, person=1.0), [])  # face hidden, body fills the frame
        changes = run(self.z, 62, 12, hz=0.67, person=0.57)  # then walks off
        self.assertEqual([z for _, z in changes], ['far'])

    def test_a_close_body_alone_never_enters_near(self):
        run(self.z, 0, 3, face_width=0.05)  # far
        self.assertEqual(run(self.z, 3, 20, hz=0.67, person=1.0), [])

    def test_away_only_after_the_full_wait_and_back_quickly(self):
        run(self.z, 0, 2, face_width=0.05)
        self.assertEqual(run(self.z, 2, self.c.away_after - 1), [])
        changes = run(self.z, 2 + self.c.away_after - 1, 3)
        self.assertEqual([z for _, z in changes], ['away'])
        back = run(self.z, 200, 2, person=0.6)
        self.assertEqual(back[0][1], 'far')
        self.assertLess(back[0][0] - 200, 1)

    def test_empty_room_from_the_start_goes_away(self):
        self.assertEqual([z for _, z in run(self.z, 0, self.c.away_after + 1)], ['away'])

    def test_lost_video_is_unknown(self):
        run(self.z, 0, 2, face_width=0.2)
        self.assertEqual(self.z.video_lost(), 'unknown')
        self.assertIsNone(self.z.video_lost())
        self.z.video_back(100)
        self.assertEqual(run(self.z, 100, self.c.away_after - 5), [])  # the outage doesn't count


def palm(x=0.5, y=0.4, score=0.9, scale=0.12, upright=True):
    return Hand(x, y, scale, score, upright)


def feed(g, start, frames):
    """frames: a list of Hand/None at 15 fps. Returns [(frame index, gesture)]."""
    out = []
    for i, hand in enumerate(frames):
        name = g.observe(start + i / FPS, hand)
        if name:
            out.append((i, name))
    return out


def sweep(x0, x1, seconds, y=0.4):
    n = max(2, round(seconds * FPS))
    return [palm(x0 + (x1 - x0) * i / (n - 1), y, score=0.3) for i in range(n)]


class GestureTests(unittest.TestCase):
    def setUp(self):
        self.g = Gestures(GestureConfig())

    def test_still_palm_fires_once_after_the_hold(self):
        out = feed(self.g, 0, [palm()] * 45)  # held for 3 s
        self.assertEqual([n for _, n in out], ['palm'])
        self.assertAlmostEqual(out[0][0] / FPS, 0.35, delta=0.1)

    def test_palm_rearms_only_after_lowering_it(self):
        frames = [palm()] * 15 + [None] * 3 + [palm()] * 15 + [None] * 10 + [palm()] * 15
        self.assertEqual([n for _, n in feed(self.g, 0, frames)], ['palm', 'palm'])

    def test_a_dropped_frame_does_not_restart_the_hold(self):
        frames = [palm()] * 3 + [None] + [palm()] * 3
        self.assertEqual([n for _, n in feed(self.g, 0, frames)], ['palm'])

    def test_palm_needs_upright_close_and_still(self):
        self.assertEqual(feed(self.g, 0, [palm(upright=False)] * 30), [])
        self.assertEqual(feed(self.g, 10, [palm(scale=0.03)] * 30), [])
        waving = [palm(x=0.4 + 0.1 * (i % 2)) for i in range(30)]
        self.assertEqual(feed(self.g, 20, waving), [])

    def test_swipes_map_to_the_users_left_and_right(self):
        # The camera isn't mirrored: moving a hand to the user's left moves it to the image's right.
        self.assertEqual([n for _, n in feed(self.g, 0, sweep(0.3, 0.7, 0.3))], ['next'])
        self.assertEqual([n for _, n in feed(self.g, 10, sweep(0.7, 0.3, 0.3))], ['previous'])

    def test_the_return_stroke_is_ignored(self):
        frames = sweep(0.3, 0.75, 0.3) + sweep(0.75, 0.3, 0.35)
        self.assertEqual([n for _, n in feed(self.g, 0, frames)], ['next'])

    def test_slow_drift_and_diagonal_motion_are_not_swipes(self):
        self.assertEqual(feed(self.g, 0, sweep(0.2, 0.8, 2.0)), [])  # a hand wandering across
        diagonal = [palm(0.3 + 0.4 * i / 5, 0.2 + 0.4 * i / 5, score=0.3) for i in range(6)]
        self.assertEqual(feed(self.g, 10, diagonal), [])

    def test_a_swipe_ending_in_an_open_palm_is_not_also_a_palm(self):
        frames = sweep(0.3, 0.7, 0.3) + [palm(x=0.7)] * 4  # stops briefly, then drops the hand
        self.assertEqual([n for _, n in feed(self.g, 0, frames + [None] * 5)], ['next'])

    def test_configurable_direction(self):
        g = Gestures(GestureConfig(swipe_left_is='previous'))
        self.assertEqual([n for _, n in feed(g, 0, sweep(0.3, 0.7, 0.3))], ['previous'])


if __name__ == '__main__':
    unittest.main()
