"""Decisions from detections, kept free of MediaPipe and I/O so they can be tested.

Every class is fed observations with a monotonic timestamp in seconds and returns a
decision (a zone, or a gesture name) only when one is final. All the tuning that keeps
false triggers down lives here: dwell times, hysteresis bands, cooldowns, re-arming.
"""
from collections import deque
from dataclasses import dataclass


@dataclass
class ZoneConfig:
    near_enter: float = 0.115  # face width / frame width at which someone counts as near
    near_exit: float = 0.095   # ...and below which they stop counting (hysteresis band)
    to_near: float = 0.4       # seconds a new zone must hold before it is reported
    to_far: float = 1.0
    to_back: float = 0.5       # from away (or unknown) to near/far
    face_lost: float = 3.5     # near with no face this long (back turned, walked off) becomes far, with
                               # no further dwell since this was the wait...
    person_near: float = 0.85  # ...unless a body this tall (fraction of frame height) is still seen:
                               # at the desk looking down at a laptop, the body fills the frame (1.0);
                               # across the room it is ~0.57
    person_hold: float = 6.0   # a person-detector hit keeps someone present this long (each half of
                               # the frame is checked every 2 x person_every seconds)
    away_after: float = 90.0   # nobody seen this long is away


class ZoneTracker:
    """near / far / away from face size, with hysteresis and dwell so the dashboard doesn't
    flicker. `observe` returns the new zone when it changes, else None."""

    def __init__(self, config: ZoneConfig, now: float):
        self.c = config
        self.zone = 'unknown'
        self.candidate, self.candidate_since = None, now
        self.last_face = self.last_person = self.last_close = None
        self.started = now

    def observe(self, now, face_width=None, person=None):
        """face_width: the largest face's width as a fraction of the frame, or None.
        person: the tallest person's height as a fraction of the frame when the person
        detector ran and found someone (0 or None otherwise)."""
        if face_width is not None:
            self.last_face = self.last_person = now
        elif person:
            self.last_person = now
            if person >= self.c.person_near:
                self.last_close = now
        return self._decide(now, face_width)

    def _decide(self, now, face_width):
        c, zone = self.c, self.zone
        seen = self.last_person is not None and now - self.last_person < c.person_hold
        close = max((t for t in (self.last_face, self.last_close) if t is not None), default=None)
        lost = False  # far because a near face is gone: face_lost was already the wait
        if face_width is not None:
            near = face_width >= (c.near_exit if zone == 'near' else c.near_enter)
            want = 'near' if near else 'far'
        elif zone == 'near' and (close is None or now - close >= c.face_lost):
            # Near without a face (or a desk-filling body) for a while: stepped back, turned away
            # or walked off. Far now, whether or not the person detector still sees them.
            want, lost = 'far', True
        elif seen or (self.last_face is not None and now - self.last_face < c.face_lost):
            want = zone if zone in ('near', 'far') else 'far'  # here, face hidden: keep the zone
        else:
            last = self.last_person if self.last_person is not None else self.started
            want = 'away' if now - last >= c.away_after else zone
        if want == zone or want == 'unknown':
            self.candidate = None
            return None
        if want != self.candidate:
            self.candidate, self.candidate_since = want, now
        if want == 'away' or lost:
            dwell = 0  # away_after or face_lost was already the wait
        elif zone in ('away', 'unknown'):
            dwell = c.to_back
        else:
            dwell = c.to_near if want == 'near' else c.to_far
        if now - self.candidate_since >= dwell:
            self.zone, self.candidate = want, None
            return want
        return None

    def video_lost(self):
        """No frames: nothing can be said about the room."""
        changed = self.zone != 'unknown'
        self.zone, self.candidate = 'unknown', None
        self.last_face = self.last_person = self.last_close = None
        return 'unknown' if changed else None

    def video_back(self, now):
        self.started = now  # away needs a full away_after of watching, not of outage


@dataclass
class Hand:
    """One tracked hand in a frame, in frame-normalised coordinates (0..1, x to the image's
    right, y down). `scale` is wrist to middle-finger knuckle, as a fraction of frame height."""
    x: float
    y: float
    scale: float
    open_palm: float     # the recogniser's Open_Palm score, 0 when it chose another gesture
    upright: bool        # fingers above the wrist


@dataclass
class GestureConfig:
    min_scale: float = 0.07     # smaller hands are too far away (or someone else's)
    palm_score: float = 0.6
    palm_hold: float = 0.35     # seconds of a still, upright open palm
    palm_still: float = 0.035   # max wrist travel during the hold, as a fraction of frame width
    palm_gap: float = 0.15      # a dropped frame shorter than this doesn't restart the hold
    palm_rearm: float = 0.5     # the palm must be gone this long before it can fire again
    swipe_distance: float = 0.22
    swipe_window: float = 0.6   # the travel has to happen within this
    swipe_min_speed: float = 0.6  # frame widths per second; return strokes are slower
    swipe_straightness: float = 0.6  # |dy| at most this times |dx|
    swipe_cooldown: float = 0.9
    swipe_opposite_lock: float = 1.3  # no swipe back the other way this soon (the return stroke)
    swipe_left_is: str = 'next'  # the user's own left, i.e. the image's right


class Gestures:
    """Open palm and horizontal swipes from per-frame hand observations."""

    def __init__(self, config: GestureConfig):
        self.c = config
        self.palm_since = self.palm_last = None
        self.palm_start_x = None
        self.palm_armed = True
        self.palm_gone_since = None
        self.track = deque()  # (t, x, y)
        self.swipe_at, self.swipe_dir = -1e9, 0

    def observe(self, now, hand):
        """hand: the nearest Hand, or None. Returns 'palm', 'next', 'previous' or None."""
        if hand is not None and hand.scale < self.c.min_scale:
            hand = None
        swipe = self._swipe(now, hand)
        if swipe:
            self._reset_palm(now)
            return swipe
        return self._palm(now, hand)

    def _reset_palm(self, now):
        self.palm_since = self.palm_start_x = None
        self.palm_armed = False
        self.palm_gone_since = now

    def _palm(self, now, hand):
        c = self.c
        palm = hand is not None and hand.open_palm >= c.palm_score and hand.upright
        if not palm:
            if self.palm_gone_since is None:
                self.palm_gone_since = now
            if now - self.palm_gone_since >= c.palm_rearm:
                self.palm_armed = True
            if self.palm_since is not None and now - (self.palm_last or now) > c.palm_gap:
                self.palm_since = self.palm_start_x = None
            return None
        self.palm_gone_since = None
        if self.palm_since is None or abs(hand.x - self.palm_start_x) > c.palm_still:
            self.palm_since, self.palm_start_x = now, hand.x  # (re)start the hold where it is now
        self.palm_last = now
        if self.palm_armed and now - self.palm_since >= c.palm_hold:
            self.palm_armed = False
            self.palm_since = None
            return 'palm'
        return None

    def _swipe(self, now, hand):
        c = self.c
        if hand is None:
            # Swipes are judged frame by frame as they happen; a lost hand starts the track over.
            self.track.clear()
            return None
        self.track.append((now, hand.x, hand.y))
        while self.track and now - self.track[0][0] > c.swipe_window:
            self.track.popleft()
        if now - self.swipe_at < c.swipe_cooldown or len(self.track) < 3:
            return None
        t0, x0, y0 = self.track[0]
        dx, dy, dt = hand.x - x0, hand.y - y0, max(now - t0, 1e-3)
        if abs(dx) < c.swipe_distance or abs(dy) > c.swipe_straightness * abs(dx) or abs(dx) / dt < c.swipe_min_speed:
            return None
        direction = 1 if dx > 0 else -1  # +1: image right, which is the user's left
        if direction == -self.swipe_dir and now - self.swipe_at < c.swipe_opposite_lock:
            return None
        self.swipe_at, self.swipe_dir = now, direction
        self.track.clear()
        left = c.swipe_left_is
        right = 'previous' if left == 'next' else 'next'
        return left if direction == 1 else right
