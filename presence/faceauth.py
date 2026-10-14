#!/usr/bin/env python3
"""Face verification for echo-unlock: proves to the laptop that its owner is at the desk.

The laptop asks, the Pi answers; nothing is broadcast on its own:

  clippy/faceauth/challenge   laptop → Pi   {"nonce": <32-64 hex>, "host": "<laptop>"}
  clippy/faceauth/verdict     Pi → laptop   {"nonce", "host", "at": epoch ms, "ok", "score", "sig"}

While a challenge is open (`window` seconds), the Pi runs YuNet + SFace on the newest frames. It
signs a verdict only when the largest face matches the enrolled one on `matches` consecutive
checks and presence says `near`. The Ed25519 signature covers `signed_message()`, so a verdict
can't be replayed for another nonce or host, or forged by anyone else on the broker.

  faceauth.py keygen     create faceauth/ed25519.pem (once; install does this)
  faceauth.py pubkey     print the public key the laptop pins
  faceauth.py enroll     sit at the desk and move your head a little for ~20 s
  faceauth.py test       print live match scores (for choosing `threshold`)
"""
import argparse
import base64
import json
import logging
import re
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

LOG = logging.getLogger('faceauth')
HERE = Path(__file__).resolve().parent
STATE = HERE / 'faceauth'
KEY = STATE / 'ed25519.pem'
ENROLLED = STATE / 'enrolled.npy'
YUNET = 'face_detection_yunet_2023mar.onnx'
SFACE = 'face_recognition_sface_2021dec.onnx'
CHALLENGE = 'clippy/faceauth/challenge'
VERDICT = 'clippy/faceauth/verdict'
NONCE = re.compile(r'[0-9a-f]{32,64}')
HOST = re.compile(r'[A-Za-z0-9._-]{1,64}')


def signed_message(host, nonce, at_ms):
    """The exact bytes signed for an ok verdict. echo-unlock on the laptop rebuilds them."""
    return f'echo-faceauth/v1\n{host}\n{nonce}\n{at_ms}\nok'.encode()


@dataclass
class AuthConfig:
    enabled: bool = True
    threshold: float = 0.6   # SFace cosine similarity to the enrolled face (OpenCV suggests 0.363)
    matches: int = 2         # consecutive matching checks needed
    window: float = 6.0      # seconds a challenge stays open
    hz: float = 3.0          # checks per second while a challenge is open (~0.2 s CPU each)
    min_gap: float = 1.0     # a new challenge sooner than this after the last one is ignored


class Challenges:
    """One open challenge at a time, decided from per-check observations. No OpenCV or I/O."""

    def __init__(self, config: AuthConfig):
        self.c = config
        self.lock = threading.Lock()
        self.current = None  # dict(nonce, host, deadline, hits, best)
        self.last_offer = -1e9
        self.next_check = 0.0

    def offer(self, now, nonce, host):
        """A challenge from the broker (any thread). True if it was accepted."""
        if not (isinstance(nonce, str) and NONCE.fullmatch(nonce)
                and isinstance(host, str) and HOST.fullmatch(host)):
            return False
        with self.lock:
            if self.current or now - self.last_offer < self.c.min_gap:
                return False
            self.last_offer = now
            self.current = {'nonce': nonce, 'host': host, 'deadline': now + self.c.window, 'hits': 0, 'best': None}
            self.next_check = now
            return True

    def due(self, now):
        with self.lock:
            return self.current is not None and now >= self.next_check

    def observe(self, now, near, score):
        """One check: near is the presence zone, score the largest face's similarity (None: no
        face). Returns (True|False, challenge) when the challenge is decided, else None."""
        with self.lock:
            ch = self.current
            if ch is None:
                return None
            self.next_check = now + 1 / self.c.hz
            if score is not None:
                ch['best'] = score if ch['best'] is None else max(ch['best'], score)
            ch['hits'] = ch['hits'] + 1 if near and score is not None and score >= self.c.threshold else 0
            if ch['hits'] >= self.c.matches:
                self.current = None
                return True, ch
            if now >= ch['deadline']:
                self.current = None
                return False, ch
            return None


class FaceID:
    """YuNet finds the face and its landmarks; SFace turns the aligned crop into a 128-d feature."""

    DETECT_WIDTH = 320  # YuNet at 640x360 takes ~160 ms on the Pi 4, at 320x180 ~37 ms

    def __init__(self, models, enrolled=None):
        import cv2
        cv2.setNumThreads(2)  # leave cores for Clippy's wake word
        self.cv2 = cv2
        self.detector = cv2.FaceDetectorYN.create(str(Path(models) / YUNET), '', (320, 320), 0.8, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(Path(models) / SFACE), '')
        self.enrolled = np.load(enrolled) if enrolled is not None else None
        self.size = None

    def faces(self, rgb):
        """The frame as BGR, and the faces YuNet finds in it, in full-frame coordinates."""
        h, w = rgb.shape[:2]
        scale = min(1.0, self.DETECT_WIDTH / w)
        size = (round(w * scale), round(h * scale))
        if self.size != size:
            self.detector.setInputSize(size)
            self.size = size
        bgr = self.cv2.cvtColor(rgb, self.cv2.COLOR_RGB2BGR)
        small = bgr if scale == 1 else self.cv2.resize(bgr, size, interpolation=self.cv2.INTER_AREA)
        _, found = self.detector.detect(small)
        if found is None:
            return bgr, []
        found[:, :14] /= scale  # box and five landmarks; the last column is the score
        return bgr, list(found)

    def feature(self, bgr, face):
        f = self.recognizer.feature(self.recognizer.alignCrop(bgr, face)).flatten()
        return f / np.linalg.norm(f)

    def largest(self, rgb):
        """(feature, face width as a fraction of the frame, face count) for the largest face, or None."""
        bgr, found = self.faces(rgb)
        if not found:
            return None
        face = max(found, key=lambda f: f[2] * f[3])
        return self.feature(bgr, face), float(face[2]) / rgb.shape[1], len(found)

    def score(self, feature):
        """Mean cosine similarity to the best few enrolled features."""
        sims = np.sort(self.enrolled @ feature)[::-1]
        return float(sims[:min(5, len(sims))].mean())


class Signer:
    def __init__(self, path=KEY):
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        self.key = load_pem_private_key(Path(path).read_bytes(), None)

    def sign(self, message):
        return base64.b64encode(self.key.sign(message)).decode()


class FaceAuth:
    """Glue for presence.py: takes challenges from MQTT, checks frames from the main loop."""

    def __init__(self, config: AuthConfig, models, publisher):
        self.challenges = Challenges(config)
        self.faceid = FaceID(models, ENROLLED)
        self.signer = Signer()
        self.pub = publisher
        publisher.subscribe(CHALLENGE, self._on_challenge)

    def _on_challenge(self, payload):
        try:
            msg = json.loads(payload)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if self.challenges.offer(time.monotonic(), msg.get('nonce'), msg.get('host')):
            LOG.info('Challenge from %s', msg['host'])

    def due(self, now):
        return self.challenges.due(now)

    def step(self, now, rgb, near):
        got = self.faceid.largest(rgb)
        score = self.faceid.score(got[0]) if got else None
        decided = self.challenges.observe(now, near, score)
        if decided is None:
            return
        ok, ch = decided
        at = int(time.time() * 1000)
        verdict = {'nonce': ch['nonce'], 'host': ch['host'], 'at': at, 'ok': ok,
                   'score': None if ch['best'] is None else round(ch['best'], 3), 'sig': None}
        if ok:
            verdict['sig'] = self.signer.sign(signed_message(ch['host'], ch['nonce'], at))
        LOG.info('Verdict for %s: %s (best score %s)', ch['host'], 'match' if ok else 'no match', verdict['score'])
        self.pub.publish(VERDICT, json.dumps(verdict))


def load(config, models, publisher):
    """FaceAuth from the [faceauth] config table, or None if it's off or not set up yet."""
    c = AuthConfig(**config)
    if not c.enabled:
        return None
    missing = [p for p in (KEY, ENROLLED, Path(models) / YUNET, Path(models) / SFACE) if not p.exists()]
    if missing:
        LOG.warning('Face auth off: missing %s', ', '.join(str(p) for p in missing))
        return None
    return FaceAuth(c, models, publisher)


# --- command line ---------------------------------------------------------------------------

def keygen():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
    if KEY.exists():
        print(f'{KEY} already exists')
        return
    STATE.mkdir(mode=0o700, exist_ok=True)
    pem = Ed25519PrivateKey.generate().private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    KEY.touch(mode=0o600)
    KEY.write_bytes(pem)
    print(f'Created {KEY}')


def pubkey():
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    raw = Signer().key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    print(base64.b64encode(raw).decode())


def frames(config):
    from presence import Frames
    f = Frames(config['stream'])
    threading.Thread(target=f.run, daemon=True).start()
    return f


def enroll(config, models, seconds, count):
    f, faceid = frames(config), FaceID(models)
    feats, seq, last, end = [], 0, 0.0, time.monotonic() + seconds
    print(f'Sit at the desk facing the Echo and turn your head a little; collecting {count} samples...')
    while len(feats) < count and time.monotonic() < end:
        got = f.next(seq, timeout=30)  # a new connection waits for a keyframe (~10 s)
        if got is None:
            raise SystemExit('No video from the hub')
        seq, rgb, _ = got
        if time.monotonic() - last < seconds / count / 2:
            continue
        bgr, found = faceid.faces(rgb)
        if len(found) != 1 or found[0][-1] < 0.9 or found[0][2] / rgb.shape[1] < 0.08:
            continue  # exactly one clear face, at desk distance
        feats.append(faceid.feature(bgr, found[0]))
        last = time.monotonic()
        print(f'  {len(feats)}/{count}', end='\r', flush=True)
    f.stop()
    if len(feats) < count // 2:
        raise SystemExit(f'\nOnly {len(feats)} usable samples; check the light and sit closer')
    arr = np.array(feats, np.float32)
    STATE.mkdir(mode=0o700, exist_ok=True)
    np.save(ENROLLED, arr)
    sims = arr @ arr.T
    print(f'\nSaved {len(arr)} samples to {ENROLLED}; self-similarity median {np.median(sims[np.triu_indices(len(arr), 1)]):.3f}')
    print('Restart the service to load them: systemctl --user restart echo-presence')


def test(config, models, seconds):
    f, faceid = frames(config), FaceID(models, ENROLLED)
    seq, end = 0, time.monotonic() + seconds
    while time.monotonic() < end:
        got = f.next(seq, timeout=30)  # a new connection waits for a keyframe (~10 s)
        if got is None:
            raise SystemExit('No video from the hub')
        seq, rgb, _ = got
        t = time.perf_counter()
        r = faceid.largest(rgb)
        ms = (time.perf_counter() - t) * 1000
        print(f'score {faceid.score(r[0]):.3f}  width {r[1]:.3f}  faces {r[2]}  {ms:.0f} ms' if r
              else f'no face  {ms:.0f} ms')
        time.sleep(0.25)
    f.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['keygen', 'pubkey', 'enroll', 'test'])
    parser.add_argument('--config', default=str(HERE / 'config.toml'))
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--samples', type=int, default=40)
    args = parser.parse_args()
    if args.command == 'keygen':
        return keygen()
    if args.command == 'pubkey':
        return pubkey()
    with open(args.config, 'rb') as fh:
        config = tomllib.load(fh)
    models = Path(config.get('models', {}).get('dir', HERE / 'models'))
    if args.command == 'enroll':
        enroll(config, models, args.seconds, args.samples)
    else:
        test(config, models, args.seconds)


if __name__ == '__main__':
    main()
