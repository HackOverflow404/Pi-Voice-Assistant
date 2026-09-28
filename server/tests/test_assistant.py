import asyncio
import datetime as dt
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo
import wave

from voice_assistant.audio import (Capture, Framer, FRAME_BYTES, is_cancel_phrase, normalize, rms,
                                   speech_threshold, wake_is_plausible)
from voice_assistant import agenda
from voice_assistant.deepgram import DeepgramListener, DeepgramVoice
from voice_assistant.engines import Engines
from voice_assistant.app import Session, drive_problems, system_stats
from voice_assistant.speech import chunks, speakable
from voice_assistant.store import Store
from voice_assistant.whatsapp import WhatsApp

AUDIO = dict(speech_rms=450, silence_seconds=0.16, start_timeout_seconds=0.24,
             max_utterance_seconds=0.8, wake_threshold=0.5)
SILENCE = bytes(FRAME_BYTES)
SPEECH = struct.pack('<1280h', *([2000] * 1280))


class AudioTests(unittest.TestCase):
    def test_fragmented_pcm(self):
        framer = Framer()
        self.assertEqual(list(framer.feed(SPEECH[:1024])), [])
        self.assertEqual(list(framer.feed(SPEECH[1024:] + SILENCE)), [SPEECH, SILENCE])

    def test_silence_ends_speech(self):
        capture = Capture(AUDIO)
        self.assertFalse(capture.feed(SPEECH))
        self.assertFalse(capture.feed(SILENCE))
        self.assertTrue(capture.feed(SILENCE))
        self.assertEqual(capture.pcm, SPEECH + SILENCE * 2)

    def test_no_speech_and_max_duration(self):
        capture = Capture(AUDIO)
        while not capture.feed(SILENCE):
            pass
        self.assertEqual(capture.pcm, b'')
        capture = Capture(AUDIO)
        for _ in range(9):
            self.assertFalse(capture.feed(SPEECH))
        self.assertTrue(capture.feed(SPEECH))


class ThresholdTests(unittest.TestCase):
    def test_scales_with_wake_phrase_within_bounds(self):
        self.assertEqual(speech_threshold(AUDIO, [600], [50] * 10), 180)    # quiet mic
        self.assertEqual(speech_threshold(AUDIO, [3000], [50] * 10), 450)   # capped by config
        self.assertEqual(speech_threshold(AUDIO, [600], [120] * 10), 300)   # above the noise floor
        self.assertEqual(speech_threshold(AUDIO, [], []), 30)               # never below 30
        self.assertAlmostEqual(speech_threshold(AUDIO, [102], [6] * 10), 30.6)  # measured Echo levels

    def test_normalize_boosts_quiet_and_leaves_loud(self):
        quiet = struct.pack('<1280h', *([150] * 1280))
        boosted, gain = normalize(quiet)
        self.assertAlmostEqual(gain, 4000 / 150)
        self.assertAlmostEqual(rms(boosted), 4000, delta=30)
        loud = struct.pack('<1280h', *([5000] * 1280)) + SILENCE
        self.assertEqual(normalize(loud), (loud, 1.0))  # already loud enough
        self.assertEqual(normalize(SILENCE), (SILENCE, 1.0))
        self.assertEqual(normalize(struct.pack('<1280h', *([10] * 1280)))[1], 40.0)  # gain is capped

    def test_wake_needs_phrase_well_above_room(self):
        audio = dict(AUDIO)
        self.assertTrue(wake_is_plausible(audio, [49], [4] * 40))      # quietest real wake: 12x
        self.assertFalse(wake_is_plausible(audio, [172], [42] * 40))   # TV chatter: 4x
        self.assertFalse(wake_is_plausible(audio, [7], [4] * 40))      # near-silence
        self.assertTrue(wake_is_plausible(audio, [150], []))           # right after a request
        self.assertFalse(wake_is_plausible(audio, [10], []))           # still needs an absolute minimum

    def test_cancel_phrases(self):
        self.assertTrue(is_cancel_phrase('Never mind.'))
        self.assertTrue(is_cancel_phrase('Cancel'))
        self.assertFalse(is_cancel_phrase('Cancel my three pm meeting'))

    def test_chime_at_start_is_not_speech(self):
        capture = Capture(dict(AUDIO, listen_grace_seconds=0.16))
        self.assertFalse(capture.feed(SPEECH) or capture.feed(SPEECH))  # chime frames
        self.assertFalse(capture.speech)
        self.assertEqual(len(capture.frames), 2)  # still recorded for transcription
        capture.feed(SPEECH)
        self.assertTrue(capture.speech)

    def test_quiet_speech_counts_with_lower_threshold(self):
        quiet = struct.pack('<1280h', *([300] * 1280))
        capture = Capture(AUDIO, threshold=180)
        capture.feed(quiet)
        self.assertTrue(capture.speech)
        default = Capture(AUDIO)  # the fixed 450 threshold misses the same speech
        default.feed(quiet)
        self.assertFalse(default.speech)


ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
X-WR-TIMEZONE:America/Chicago
BEGIN:VEVENT
UID:class
DTSTART;TZID=America/Chicago:20260907T090000
DTEND;TZID=America/Chicago:20260907T105000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE
SUMMARY:Analog Signal Processing
LOCATION:ECEB 4072
END:VEVENT
BEGIN:VEVENT
UID:holiday
DTSTART;VALUE=DATE:20260929
DTEND;VALUE=DATE:20260930
SUMMARY:Fall break
END:VEVENT
BEGIN:VEVENT
UID:cancelled
DTSTART:20260928T180000Z
DTEND:20260928T190000Z
STATUS:CANCELLED
SUMMARY:Cancelled meeting
END:VEVENT
BEGIN:VEVENT
UID:later
DTSTART:20261005T180000Z
DTEND:20261005T190000Z
SUMMARY:Next week
END:VEVENT
BEGIN:VEVENT
UID:demo
DTSTART:20260928T182000Z
SUMMARY:CS 425 MP2 Demo &amp; Q&amp;A
END:VEVENT
END:VCALENDAR
"""
CHICAGO = ZoneInfo('America/Chicago')
MONDAY = dt.datetime(2026, 9, 28, 1, 0, tzinfo=CHICAGO)


class AgendaTests(unittest.TestCase):
    def test_expands_recurrences_for_today_and_tomorrow(self):
        with patch.object(agenda, 'fetch', return_value=ICS):
            events = agenda.load(['https://calendar.test/private.ics'], MONDAY)
        titles = [(e['title'], e['all_day']) for e in events]
        self.assertEqual(titles, [('Fall break', True), ('Analog Signal Processing', False),
                                  ('CS 425 MP2 Demo & Q&A', False)])
        lesson = events[1]
        self.assertEqual(dt.datetime.fromtimestamp(lesson['begin'] / 1000, CHICAGO),
                         dt.datetime(2026, 9, 28, 9, 0, tzinfo=CHICAGO))
        self.assertEqual(lesson['location'], 'ECEB 4072')
        holiday = events[0]
        self.assertEqual(dt.datetime.fromtimestamp(holiday['begin'] / 1000, CHICAGO),
                         dt.datetime(2026, 9, 29, 0, 0, tzinfo=CHICAGO))  # local midnight
        self.assertEqual(events[2]['end'], events[2]['begin'])  # no DTEND: zero length

    def test_failed_calendars(self):
        def flaky(url):
            if 'bad' in url:
                raise OSError('unreachable')
            return ICS
        with patch.object(agenda, 'fetch', side_effect=flaky):
            self.assertEqual(len(agenda.load(['https://bad.test', 'https://good.test'], MONDAY)), 3)
            self.assertIsNone(agenda.load(['https://bad.test'], MONDAY))  # keep the old events
        shared = agenda.Shared()
        shared.update(None)
        self.assertEqual((shared.events, shared.version), (None, 0))
        shared.update([])
        shared.update([])
        self.assertEqual(shared.version, 1)


class SystemStatsTests(unittest.TestCase):
    def test_reports_memory_and_load(self):
        stats = system_stats()
        self.assertGreater(stats['mem_total_mb'], 0)
        self.assertLessEqual(stats['mem_available_mb'], stats['mem_total_mb'])
        self.assertGreaterEqual(stats['load1'], 0)
        self.assertTrue(0 <= stats['mem_percent'] <= 100)
        json.dumps(stats)
        time.sleep(0.05)
        self.assertTrue(0 <= system_stats()['cpu_percent'] <= 100)  # needs two samples


class DriveTests(unittest.TestCase):
    def test_missing_drives_reported_from_fstab(self):
        with tempfile.TemporaryDirectory() as temp:
            devices = Path(temp) / 'by-uuid'
            devices.mkdir()
            (devices / 'AAAA').touch()
            fstab = Path(temp) / 'fstab'
            fstab.write_text('\n'.join([
                'PARTUUID=917dde3f-02  /  ext4  defaults,noatime  0  1',
                'UUID=AAAA  /mnt/shodan   ntfs-3g  defaults,nofail,x-systemd.automount  0  0',
                'UUID=BBBB  /mnt/backups  ntfs-3g  defaults,nofail  0  0',
                '# UUID=CCCC  /mnt/old  ext4  defaults,nofail  0  2',
                'UUID=DDDD  /mnt/required  ext4  defaults  0  2',
            ]))
            self.assertEqual(drive_problems(str(fstab), str(devices)), ['backups missing'])


class SpeechTests(unittest.TestCase):
    def test_signoff_formatting_and_lists_become_sentences(self):
        self.assertEqual(speakable('Nothing else is due tonight.\r\n\r\nInstinct'), 'Nothing else is due tonight.')
        self.assertEqual(speakable('Due *tonight* at _11:59_ 🎉\n- Lab report\n- Pre-lab form\n\n-- \nSent by bot'),
                         'Due tonight at 11:59. Lab report. Pre-lab form.')
        self.assertEqual(speakable('See https://example.com/x for details.'), 'See a link for details.')
        self.assertEqual(speakable('Only one paragraph'), 'Only one paragraph.')

    def test_chunks_start_short_and_join_fragments(self):
        text = ('The lab report, the pre-lab form, and the Gradescope report are all due at 11:59 pm. '
                'Ok. Demo is tomorrow at 1:20 pm in Siebel. Nothing else is due tonight.')
        self.assertEqual(chunks(text), ['The lab report, the pre-lab form,',
                                        'and the Gradescope report are all due at 11:59 pm.',
                                        'Ok. Demo is tomorrow at 1:20 pm in Siebel.', 'Nothing else is due tonight.'])
        self.assertEqual(chunks(''), [])


class FakeBridge(BaseHTTPRequestHandler):
    """Minimal stand-in for the Go bridge's HTTP API."""
    sent, replies, polls, requests = [], [], 0, []

    def log_message(self, *args): pass

    def reply(self, body, code=200):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        FakeBridge.sent.append(body)
        self.reply({'id': 'ABC', 'timestamp': 1000})

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        FakeBridge.polls += 1
        FakeBridge.requests.append(q['request'][0])
        since, after = int(q['since'][0]), int(q['after'][0])
        self.reply({'messages': [m for m in FakeBridge.replies if m['timestamp'] >= since and m['seq'] > after]})


class WhatsAppTests(unittest.TestCase):
    def setUp(self):
        FakeBridge.sent, FakeBridge.replies, FakeBridge.polls, FakeBridge.requests = [], [], 0, []
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeBridge)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = WhatsApp(dict(bridge_url=f'http://127.0.0.1:{self.server.server_port}/', contact='Instinct',
                                    reply_settle_seconds=0.2))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_send_names_contact_and_returns_timestamp(self):
        self.assertEqual(self.client.send('What is due?'), {'id': 'ABC', 'timestamp': 1000})
        self.assertEqual(FakeBridge.sent, [{'contact': 'Instinct', 'text': 'What is due?'}])

    def test_send_appends_instruction(self):
        self.client.config['instruction'] = '(Reply in full sentences.)'
        self.client.send('What is due?')
        self.assertEqual(FakeBridge.sent[-1]['text'], 'What is due?\n\n(Reply in full sentences.)')

    def test_joins_multi_bubble_reply_and_ignores_older_messages(self):
        FakeBridge.replies = [dict(seq=1, timestamp=990, text='Old answer'),
                              dict(seq=2, timestamp=1001, text='Two things are due.'),
                              dict(seq=3, timestamp=1002, text='Lab report and pre-lab.')]
        reply = self.client.wait_reply('ABC', 1000, time.time() + 5, threading.Event())
        self.assertEqual(reply, 'Two things are due.\n\nLab report and pre-lab.')
        self.assertEqual(FakeBridge.requests[-1], 'ABC')

    def test_timeout_and_bridge_down(self):
        with self.assertRaises(TimeoutError):
            self.client.wait_reply('ABC', 1000, time.time() + 1.5, threading.Event())
        down = WhatsApp(dict(bridge_url='http://127.0.0.1:9', contact='Instinct', reply_settle_seconds=0))
        with self.assertRaises(TimeoutError):
            down.wait_reply('ABC', 1000, time.time() + 1, threading.Event())


class FakeDeepgram(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args): pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        if urlparse(self.path).path == '/v1/listen':
            FakeDeepgram.seen.append((self.headers['Authorization'], parse_qs(urlparse(self.path).query), len(raw)))
            data = json.dumps({'results': {'channels': [{'alternatives': [
                {'transcript': ' What assignments do I have left tonight? '}]}]}}).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers()
            self.wfile.write(data)
            return
        body = json.loads(raw)
        FakeDeepgram.seen.append((self.headers['Authorization'], parse_qs(urlparse(self.path).query), body))
        if body['text'] == 'fail':
            self.send_response(401); self.end_headers(); self.wfile.write(b'{"err_msg":"bad key"}')
            return
        pcm = struct.pack('<2400h', *([100] * 2400))  # 0.1 s at 24 kHz
        self.send_response(200)
        self.send_header('Content-Length', str(len(pcm)))
        self.end_headers()
        self.wfile.write(pcm)


class VoiceTests(unittest.TestCase):
    def test_deepgram_request_and_wav(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), FakeDeepgram)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            voice = DeepgramVoice('KEY', 'aura-2-thalia-en', url=f'http://127.0.0.1:{server.server_port}/v1/speak')
            data, duration = voice.synthesize('Hello there.')
            auth, query, body = FakeDeepgram.seen[-1]
            self.assertEqual(auth, 'Token KEY')
            self.assertEqual((query['model'], query['encoding'], query['container']),
                             (['aura-2-thalia-en'], ['linear16'], ['none']))
            self.assertEqual(body, {'text': 'Hello there.'})
            self.assertNotIn('speed', query)  # default rate is not sent
            DeepgramVoice('KEY', 'aura-2-orion-en', 1.2, url=voice.url).synthesize('Faster.')
            self.assertEqual(FakeDeepgram.seen[-1][1]['speed'], ['1.2'])
            self.assertAlmostEqual(duration, 0.1)
            with wave.open(io.BytesIO(data)) as wav:
                self.assertEqual((wav.getframerate(), wav.getnframes()), (24000, 2400))
            with self.assertRaisesRegex(RuntimeError, 'Deepgram 401'):
                voice.synthesize('fail')
            listener = DeepgramListener('KEY', url=f'http://127.0.0.1:{server.server_port}/v1/listen')
            self.assertEqual(listener.transcribe(SPEECH), 'What assignments do I have left tonight?')
            auth, query, size = FakeDeepgram.seen[-1]
            self.assertEqual((auth, query['model'], query['sample_rate'], size),
                             ('Token KEY', ['nova-3'], ['16000'], len(SPEECH)))
        finally:
            server.shutdown(); server.server_close()

    def test_cloud_failure_falls_back_to_piper(self):
        class Broken:
            def synthesize(self, text): raise RuntimeError('offline')
        class Piper:
            def synthesize_wav(self, text, wav):
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
                wav.writeframes(SILENCE)
        engines = Engines.__new__(Engines)
        engines.cloud, engines.piper = Broken(), Piper()
        data, duration = engines.synthesize('Hello')
        self.assertAlmostEqual(duration, 0.08)


class FakeSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.closed = None
    def __aiter__(self): return self
    async def __anext__(self): return await self.incoming.get()
    async def send(self, message): await self.outgoing.put(message)
    async def close(self, code, reason): self.closed = code


class FakeEngines:
    def predict(self, frame): return 1
    def reset(self): pass
    def transcribe(self, pcm): return 'What is the weather?'
    def synthesize(self, text):
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes(SILENCE)
        return output.getvalue(), 0.08


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'state.db')
        self.socket = FakeSocket()
        whatsapp = dict(bridge_url='http://127.0.0.1:9', contact='Instinct', reply_timeout_seconds=10,
                        reply_settle_seconds=0, max_reply_chars=4000)
        self.config = {'audio': dict(AUDIO), 'whatsapp': whatsapp, 'state_dir': self.temp.name}
        self.session = Session(self.socket, self.config, FakeEngines(), self.store)
        self.sent = []
        self.session.messenger.send = lambda text: self.sent.append(text) or {'id': 'WA1', 'timestamp': 1000}
        self.session.messenger.wait_reply = lambda *args: 'It is sunny.\r\n\r\nInstinct'
        self.task = None

    async def asyncTearDown(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError): await self.task
        self.store.db.close()
        self.temp.cleanup()

    async def event(self):
        message = await asyncio.wait_for(self.socket.outgoing.get(), 3)
        return json.loads(message) if isinstance(message, str) else message

    async def until(self, target):
        seen = []
        while True:
            event = await self.event()
            seen.append(event)
            if isinstance(event, dict) and (event.get('status') == target or event.get('type') == target):
                return seen

    async def test_full_flow_persists_id_streams_wav_waits_for_ack(self):
        self.task = asyncio.create_task(self.session.run())
        await self.until('idle')
        await self.socket.incoming.put(SPEECH)  # the wake phrase: loud enough to count
        await self.until('listening')
        await self.socket.incoming.put(SPEECH + SILENCE * 2)
        events = await self.until('audio_end')
        statuses = [e['status'] for e in events if isinstance(e, dict) and e.get('type') == 'status']
        self.assertEqual(statuses, ['transcribing', 'waiting', 'waiting', 'speaking'])
        data = b''.join(e for e in events if isinstance(e, bytes))
        with wave.open(io.BytesIO(data), 'rb') as wav:
            self.assertEqual(wav.getnframes(), 1280)
        self.assertEqual(self.sent, ['What is the weather?'])
        self.assertTrue(any(isinstance(e, dict) and e.get('type') == 'sent' and e['id'] == 'WA1' for e in events))
        self.assertEqual(self.store.latest()['message_id'], 'WA1')  # replies are matched to WhatsApp's ID
        self.assertEqual(self.store.latest()['status'], 'replied')
        self.assertEqual(self.store.latest()['reply'], 'It is sunny.')  # sign-off stripped
        await self.socket.incoming.put(json.dumps({'type': 'playback_done', 'id': 'wrong'}))
        await asyncio.sleep(0.01)
        self.assertEqual(self.session.status, 'speaking')
        await self.socket.incoming.put(json.dumps({'type': 'playback_done', 'id': self.session.message_id}))
        await self.until('idle')
        self.assertEqual(self.store.latest()['status'], 'done')

    async def test_reply_streams_one_segment_per_sentence(self):
        self.session.messenger.wait_reply = lambda *args: 'The first sentence is long enough. The second one too.'
        self.task = asyncio.create_task(self.session.run())
        await self.until('idle')
        await self.socket.incoming.put(SPEECH)  # the wake phrase: loud enough to count
        await self.until('listening')
        await self.socket.incoming.put(SPEECH + SILENCE * 2)
        events = await self.until('audio_end') + await self.until('audio_end')
        ends = [e for e in events if isinstance(e, dict) and e.get('type') == 'audio_end']
        self.assertEqual([(e['seq'], e['last']) for e in ends], [(0, False), (1, True)])
        starts = [e for e in events if isinstance(e, dict) and e.get('type') == 'audio_start']
        self.assertEqual([e['volume'] for e in starts], [1.0, 1.0])  # no tts config: full volume
        self.assertEqual(self.session.status, 'speaking')  # waits for one ack after the last segment
        await self.socket.incoming.put(json.dumps({'type': 'playback_done', 'id': self.session.message_id}))
        await self.until('idle')
        self.assertEqual(self.store.latest()['status'], 'done')

    async def test_debug_recordings_are_opt_in_and_capped(self):
        self.config['audio']['debug_save_utterances'] = True
        for _ in range(7):
            self.session.save_utterance(SPEECH)
            await asyncio.sleep(0.002)
        saved = list((Path(self.temp.name) / 'utterances').glob('*.wav'))
        self.assertEqual(len(saved), 5)
        self.config['audio']['debug_save_utterances'] = False

    async def test_uncertain_send_recovers_without_resending(self):
        self.store.create('<saved@test>', 'Existing request')
        self.task = asyncio.create_task(self.session.run())
        await self.until('audio_end')
        self.assertEqual(self.sent, [])
        self.assertEqual(self.session.transcript, 'Existing request')

    async def test_expired_request_is_closed_not_retried(self):
        self.store.create('<old@test>', 'Old request')
        self.store.update('<old@test>', 'sent')
        self.store.db.execute('UPDATE requests SET created = created - 3600')
        self.store.db.commit()
        self.session.messenger.wait_reply = lambda *args: self.fail('expired request was retried')
        self.task = asyncio.create_task(self.session.run())
        events = await self.until('idle')
        self.assertIsNone(events[-1]['error'])
        self.assertEqual(self.store.latest()['status'], 'expired')

    async def speak_request(self):
        self.task = asyncio.create_task(self.session.run())
        await self.until('idle')
        await self.socket.incoming.put(SPEECH)
        await self.until('listening')
        await self.socket.incoming.put(SPEECH + SILENCE * 2)

    async def test_cancel_during_grace_period_sends_nothing(self):
        self.config['whatsapp']['send_delay_seconds'] = 2
        await self.speak_request()
        events = await self.until('confirming')
        self.assertEqual(events[-1]['send_delay'], 2)
        await self.socket.incoming.put(json.dumps({'type': 'cancel'}))
        events = await self.until('idle')
        self.assertEqual(events[-1]['error'], 'Cancelled')
        self.assertEqual(self.sent, [])

    async def test_cancel_while_waiting_unsends(self):
        revoked = []
        def waiting(request, since, deadline, stop):
            stop.wait(5)
            raise InterruptedError('Reply watch cancelled')
        self.session.messenger.wait_reply = waiting
        self.session.messenger.revoke = revoked.append
        await self.speak_request()
        await self.until('sent')
        await self.until('waiting')
        await self.socket.incoming.put(json.dumps({'type': 'cancel'}))
        events = await self.until('idle')
        self.assertEqual(events[-1]['error'], 'Cancelled and unsent')
        self.assertEqual(revoked, ['WA1'])
        self.assertEqual(self.store.latest()['status'], 'cancelled')

    async def test_spoken_cancel_sends_nothing(self):
        self.session.engines.transcribe = lambda pcm: 'Never mind.'
        await self.speak_request()
        events = await self.until('idle')
        while events[-1].get('error') != 'Cancelled':
            events = await self.until('idle')
        self.assertEqual(self.sent, [])

    async def test_silent_capture_does_not_send(self):
        self.task = asyncio.create_task(self.session.run())
        await self.until('idle')
        await self.socket.incoming.put(SPEECH)  # the wake phrase: loud enough to count
        await self.until('listening')
        await self.socket.incoming.put(SILENCE * 3)
        events = await self.until('idle')
        self.assertIn('No speech', events[-1]['error'])
        self.assertEqual(self.sent, [])

    async def test_session_reports_system_stats(self):
        self.task = asyncio.create_task(self.session.run())
        events = await self.until('system')
        self.assertIn('mem_available_mb', events[-1])

    async def test_session_sends_calendar_when_it_changes(self):
        shared = agenda.Shared()
        shared.update([dict(title='Demo', begin=1, end=2, all_day=False, location='', color=0)])
        self.session.calendar = shared
        self.task = asyncio.create_task(self.session.run())
        events = await self.until('calendar')
        self.assertEqual(events[-1]['events'][0]['title'], 'Demo')

    async def test_invalid_pcm_closes_socket(self):
        await self.socket.incoming.put(b'\x01')
        await self.session.receive()
        self.assertEqual(self.socket.closed, 1003)

    async def test_disconnect_cancels_reply_worker(self):
        self.store.create('<saved@test>', 'Existing request')
        started = threading.Event()
        finished = threading.Event()
        def waiting(request, since, deadline, stop):
            started.set()
            stop.wait(5)
            finished.set()
            raise InterruptedError('Cancelled')
        self.session.messenger.wait_reply = waiting
        self.task = asyncio.create_task(self.session.run())
        await self.until('waiting')
        for _ in range(100):
            if started.is_set(): break
            await asyncio.sleep(0.01)
        self.assertTrue(started.is_set())
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(self.task, 2)
        self.assertTrue(finished.is_set())

    async def test_reply_timeout_returns_idle_with_error(self):
        self.store.create('<saved@test>', 'Existing request')
        def timeout(*args): raise TimeoutError('No reply')
        self.session.messenger.wait_reply = timeout
        self.task = asyncio.create_task(self.session.run())
        events = await self.until('idle')
        self.assertEqual(events[-1]['error'], 'No reply within 10 s')
        self.assertEqual(self.store.latest()['status'], 'expired')
        self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
