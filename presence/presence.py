#!/usr/bin/env python3
"""echo-presence: who is in front of the Echo Show, and (optionally) what their hand is doing.

Reads the SECUR-T hub's Annex-B H.264 stream (localhost:7002), runs MediaPipe on the Pi and
publishes over MQTT, where Clippy's voice server relays it to the Echo's dashboard:

  clippy/vision/presence      retained  {"zone": "near"|"far"|"away"|"unknown", "since": epoch}
  clippy/vision/gesture                 {"gesture": "palm"|"next"|"previous", "event_type": ..., "at": epoch}
  clippy/vision/availability  retained  online | offline (last will)

It also answers echo-unlock's face checks from the laptop (faceauth.py, `[faceauth]`).

Presence runs face detection a few times a second, plus a person detector every couple of
seconds while no face is visible (someone facing away). Hands are off by default: a Pi 4
can't track them fast enough next to Clippy and Home Assistant (see README).
"""
import argparse
import json
import logging
import os
import signal
import subprocess
import threading
import time
import tomllib
from pathlib import Path

import numpy as np

import faceauth
from logic import GestureConfig, Gestures, Hand, ZoneConfig, ZoneTracker

LOG = logging.getLogger('presence')
HERE = Path(__file__).resolve().parent
BASE = 'clippy/vision'
DEVICE = {'identifiers': ['clippy'], 'name': 'Clippy'}  # the voice server's Home Assistant device
THERMAL = '/sys/class/thermal/thermal_zone0/temp'


class Frames:
    """Decodes the hub's stream with ffmpeg in a thread and keeps only the newest frame, so a
    slow consumer always sees the present instead of a backlog."""

    def __init__(self, config):
        self.url, self.w, self.h = config['url'], config['width'], config['height']
        self.keyframes_only = config.get('keyframes_only', False)
        self.cond = threading.Condition()
        self.frame, self.seq, self.at = None, 0, 0.0
        self.proc = None
        self.stopping = False

    def command(self):
        skip = ['-skip_frame', 'nokey'] if self.keyframes_only else []
        # Software decode: the Pi 4's v4l2m2m decoder returns no frames for this stream.
        # Skipping the loop filter roughly halves decode time and doesn't matter to detection.
        return ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-fflags', 'nobuffer',
                '-flags', 'low_delay', '-c:v', 'h264', '-skip_loop_filter', 'all', '-threads', '2', *skip,
                '-f', 'h264', '-i', self.url, '-vf', f'scale={self.w}:{self.h}', '-pix_fmt', 'rgb24',
                '-f', 'rawvideo', '-']

    def run(self):
        size, backoff = self.w * self.h * 3, 1
        while not self.stopping:
            started = time.monotonic()
            self.proc = subprocess.Popen(self.command(), stdout=subprocess.PIPE)
            try:
                while True:
                    buf = self.proc.stdout.read(size)
                    if len(buf) < size:
                        break
                    with self.cond:
                        self.frame, self.at = buf, time.monotonic()
                        self.seq += 1
                        self.cond.notify_all()
            finally:
                self.proc.kill()
                self.proc.wait()
            if self.stopping:
                return
            LOG.warning('Stream ended (ffmpeg exit %s); reconnecting in %d s', self.proc.returncode, backoff)
            time.sleep(backoff)
            backoff = 1 if time.monotonic() - started > 60 else min(backoff * 2, 30)

    def next(self, after, timeout):
        """The newest frame after sequence `after` as (seq, rgb array, arrival time), or None."""
        with self.cond:
            if not self.cond.wait_for(lambda: self.seq > after, timeout):
                return None
            arr = np.frombuffer(self.frame, np.uint8).reshape(self.h, self.w, 3)
            return self.seq, arr, self.at

    def stop(self):
        self.stopping = True
        if self.proc:
            self.proc.kill()


class Models:
    def __init__(self, config, hands):
        from mediapipe.tasks.python import BaseOptions, vision
        models = Path(config.get('dir', HERE / 'models'))
        video = vision.RunningMode.VIDEO
        self.face = vision.FaceDetector.create_from_options(vision.FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(models / 'blaze_face_short_range.tflite')),
            running_mode=video, min_detection_confidence=config.get('face_confidence', 0.6)))
        # IMAGE mode: it runs on a different crop each time, so there's nothing to track.
        self.person = vision.ObjectDetector.create_from_options(vision.ObjectDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(models / 'efficientdet_lite0.tflite')),
            category_allowlist=['person'], max_results=3,
            score_threshold=config.get('person_confidence', 0.5)))
        self.hands = None
        if hands:
            self.hands = vision.GestureRecognizer.create_from_options(vision.GestureRecognizerOptions(
                base_options=BaseOptions(model_asset_path=str(models / 'gesture_recognizer.task')),
                running_mode=video, num_hands=2))
        self.last_ts = {}

    def _ts(self, name, at):
        # VIDEO mode needs strictly increasing timestamps per model.
        ts = max(int(at * 1000), self.last_ts.get(name, -1) + 1)
        self.last_ts[name] = ts
        return ts

    @staticmethod
    def image(arr):
        import mediapipe as mp
        return mp.Image(image_format=mp.ImageFormat.SRGB, data=arr)

    def face_width(self, img, at):
        """The largest face's width as a fraction of the frame, or None."""
        result = self.face.detect_for_video(img, self._ts('face', at))
        if not result.detections:
            return None
        return max(d.bounding_box.width for d in result.detections) / img.width

    @staticmethod
    def crops(arr):
        """Square crops covering the frame: the left and right halves (overlapping for 16:9)."""
        h, w = arr.shape[:2]
        return [arr[:, :h], arr[:, w - h:]] if w > h else [arr]

    def person_height(self, arr, crop):
        """The tallest person's height as a fraction of the frame, or 0.0 for nobody.
        The detector squeezes its input into 320x320: a whole 16:9 frame squashes a person
        across the room below its threshold (0.11 measured), while a square crop of the same
        frame scores 0.8-0.9. So it looks at one square crop per call, alternating."""
        crops = self.crops(arr)
        img = self.image(np.ascontiguousarray(crops[crop % len(crops)]))
        found = self.person.detect(img).detections
        return max((d.bounding_box.height for d in found), default=0) / img.height

    def nearest_hand(self, img, at):
        result = self.hands.recognize_for_video(img, self._ts('hands', at))
        best = None
        aspect = img.width / img.height
        for landmarks, gestures in zip(result.hand_landmarks, result.gestures):
            wrist, knuckle, tip = landmarks[0], landmarks[9], landmarks[12]
            scale = float(np.hypot((knuckle.x - wrist.x) * aspect, knuckle.y - wrist.y))
            top = gestures[0] if gestures else None
            palm = top.score if top and top.category_name == 'Open_Palm' else 0.0
            centre = [landmarks[i] for i in (0, 5, 9, 13, 17)]
            hand = Hand(x=sum(p.x for p in centre) / 5, y=sum(p.y for p in centre) / 5, scale=scale,
                        open_palm=palm, upright=tip.y < wrist.y)
            if best is None or hand.scale > best.scale:
                best = hand
        return best


class Publisher:
    def __init__(self, config):
        import paho.mqtt.client as mqtt
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='echo-presence')
        self.client.will_set(f'{BASE}/availability', 'offline', retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.zone = None
        self.handlers = {}
        if config.get('username'):
            self.client.username_pw_set(config['username'], config.get('password'))
        self.client.connect_async(config.get('host', '127.0.0.1'), config.get('port', 1883), keepalive=30)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason, properties):
        if reason.is_failure:
            LOG.warning('MQTT connect failed: %s', reason)
            return
        state = {'state_topic': f'{BASE}/presence', 'availability_topic': f'{BASE}/availability', 'device': DEVICE}
        entities = [
            ('sensor', 'echo_presence_zone', {**state, 'name': 'Presence', 'icon': 'mdi:account-eye',
                                              'value_template': '{{ value_json.zone }}'}),
            ('binary_sensor', 'echo_occupancy', {**state, 'name': 'Someone at the Echo', 'device_class': 'occupancy',
                                                 'value_template': "{{ 'ON' if value_json.zone in ['near', 'far'] "
                                                                   "else 'OFF' }}"}),
            ('event', 'echo_gesture', {'state_topic': f'{BASE}/gesture', 'availability_topic': f'{BASE}/availability',
                                       'device': DEVICE, 'name': 'Gesture', 'icon': 'mdi:hand-wave',
                                       'event_types': ['palm', 'next', 'previous']}),
        ]
        for component, uid, fields in entities:
            client.publish(f'homeassistant/{component}/{uid}/config',
                           json.dumps({**fields, 'unique_id': uid, 'object_id': uid}), retain=True)
        client.publish(f'{BASE}/availability', 'online', retain=True)
        if self.zone:
            self.presence(self.zone)
        for topic in self.handlers:
            client.subscribe(topic)
        LOG.info('MQTT connected')

    def _on_message(self, client, userdata, msg):
        handler = self.handlers.get(msg.topic)
        if handler:
            handler(msg.payload)

    def subscribe(self, topic, handler):
        self.handlers[topic] = handler
        if self.client.is_connected():
            self.client.subscribe(topic)

    def publish(self, topic, payload):
        self.client.publish(topic, payload)

    def presence(self, zone):
        self.zone = zone
        self.client.publish(f'{BASE}/presence', json.dumps({'zone': zone, 'since': round(time.time())}), retain=True)

    def gesture(self, name):
        self.client.publish(f'{BASE}/gesture', json.dumps({'gesture': name, 'event_type': name,
                                                           'at': round(time.time(), 3)}))

    def close(self):
        self.client.publish(f'{BASE}/availability', 'offline', retain=True).wait_for_publish(timeout=5)
        self.client.loop_stop()
        self.client.disconnect()


def temperature():
    try:
        return int(Path(THERMAL).read_text()) / 1000
    except (OSError, ValueError):
        return None


def run(config, debug=False):
    stream, cfg_p, cfg_h = config['stream'], config.get('presence', {}), config.get('hands', {})
    hands_on = cfg_h.get('enabled', False)
    frames = Frames(stream)
    models = Models(config.get('models', {}), hands_on)
    logging.root.setLevel(logging.INFO)  # importing MediaPipe (absl) lowers it to WARNING
    pub = Publisher(config.get('mqtt', {}))
    zones = ZoneTracker(ZoneConfig(**cfg_p.get('zones', {})), time.monotonic())
    auth = faceauth.load(config.get('faceauth', {}), Path(config.get('models', {}).get('dir', HERE / 'models')), pub)
    gestures = Gestures(GestureConfig(**cfg_h.get('gestures', {}))) if hands_on else None
    face_every = 1 / cfg_p.get('face_hz', 5)
    person_every = cfg_p.get('person_every', 1.5)
    hands_every = 1 / cfg_h.get('max_fps', 15)
    hot = cfg_p.get('throttle_temp', 78)

    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    reader = threading.Thread(target=frames.run, daemon=True)
    reader.start()

    seq, next_face, next_person, next_hands = 0, 0.0, 0.0, 0.0
    person_crop = 0
    last_face_seen = -1e9
    stats = {'frames': 0, 'face_ms': 0.0, 'faces': 0, 'person_ms': 0.0, 'persons': 0, 'hand_ms': 0.0, 'hands': 0,
             'lag_ms': 0.0, 'since': time.monotonic()}
    video_ok = False
    while not stopping.is_set():
        got = frames.next(seq, timeout=10)
        now = time.monotonic()
        if got is None:
            if video_ok:
                LOG.warning('No video for 10 s')
                video_ok = False
                if zones.video_lost():
                    pub.presence('unknown')
            continue
        seq, arr, arrived = got
        if not video_ok:
            LOG.info('Video %s', 'started' if seq == 1 else 'back')
            video_ok = True
            zones.video_back(now)
        stats['frames'] += 1
        img = None
        temp = temperature()
        slow = 2 if temp is not None and temp >= hot else 1  # back off before the Pi throttles

        face_width = None
        person = None
        if now >= next_face:
            next_face = now + face_every * slow
            img = models.image(arr) if img is None else img
            t = time.perf_counter()
            face_width = models.face_width(img, arrived)
            stats['face_ms'] += (time.perf_counter() - t) * 1000
            stats['faces'] += 1
            stats['lag_ms'] += (time.monotonic() - arrived) * 1000
            if face_width is not None:
                last_face_seen = now
            elif now >= next_person and now - last_face_seen > 1.0:
                # No face: someone facing away, or nobody. The person detector is ~15x the cost.
                next_person = now + person_every * slow
                t = time.perf_counter()
                person = models.person_height(arr, person_crop)
                person_crop += 1
                stats['person_ms'] += (time.perf_counter() - t) * 1000
                stats['persons'] += 1
            zone = zones.observe(now, face_width=face_width, person=person)
            if debug:
                LOG.info('face %s person %s -> zone %s', f'{face_width:.3f}' if face_width else '-',
                         f'{person:.2f}' if person is not None else '-', zones.zone)
            if zone:
                LOG.info('Zone: %s', zone)
                pub.presence(zone)

        # A face check the laptop asked for, only while one is open (~0.2 s CPU per check).
        if auth and auth.due(now):
            auth.step(now, arr, zones.zone == 'near')

        # Hands only while someone is here; MediaPipe tracks between frames, so run every frame.
        if gestures and zones.zone in ('near', 'far') and now >= next_hands:
            next_hands = now + hands_every * slow
            img = models.image(arr) if img is None else img
            t = time.perf_counter()
            hand = models.nearest_hand(img, arrived)
            stats['hand_ms'] += (time.perf_counter() - t) * 1000
            stats['hands'] += 1
            name = gestures.observe(arrived, hand)
            if debug and hand:
                LOG.info('hand x %.2f y %.2f scale %.3f palm %.2f upright %s', hand.x, hand.y, hand.scale,
                         hand.open_palm, hand.upright)
            if name:
                LOG.info('Gesture: %s (%.0f ms after the frame arrived)', name, (time.monotonic() - arrived) * 1000)
                pub.gesture(name)

        if now - stats['since'] >= 300:
            span = now - stats['since']
            avg = lambda k, n: stats[k] / stats[n] if stats[n] else 0  # noqa: E731
            LOG.info('%.1f fps in, face %.0f ms x%.1f/s, person %.0f ms x%.2f/s, hands %.0f ms x%.1f/s, '
                     'lag %.0f ms, zone %s, %s', stats['frames'] / span, avg('face_ms', 'faces'), stats['faces'] / span,
                     avg('person_ms', 'persons'), stats['persons'] / span, avg('hand_ms', 'hands'),
                     stats['hands'] / span, avg('lag_ms', 'faces'), zones.zone,
                     f'{temp:.0f} C' if temp is not None else 'temp n/a')
            stats = dict.fromkeys(stats, 0.0)
            stats['since'] = now
    frames.stop()
    pub.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default=str(HERE / 'config.toml'))
    parser.add_argument('--debug', action='store_true', help='log every detection (for calibrating distances)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    os.environ.setdefault('GLOG_minloglevel', '2')  # MediaPipe's per-frame chatter
    with open(args.config, 'rb') as f:
        config = tomllib.load(f)
    run(config, debug=args.debug)


if __name__ == '__main__':
    main()
