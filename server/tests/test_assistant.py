import asyncio
from contextlib import suppress
from email.message import EmailMessage
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from voice_assistant.audio import Capture, Framer, FRAME_BYTES
from voice_assistant.app import Session
from voice_assistant.mail import Mail, plain_reply
from voice_assistant.store import Store

AUDIO = dict(speech_rms=450, silence_seconds=0.16, start_timeout_seconds=0.24,
             max_utterance_seconds=0.8, wake_threshold=0.5)
SILENCE = bytes(FRAME_BYTES)
SPEECH = struct.pack('<1280h', *([2000] * 1280))


def reply_bytes(message_id='<request@test>', text='Here is your answer.'):
    mail = EmailMessage()
    mail['In-Reply-To'] = message_id
    mail.set_content(text)
    return mail.as_bytes()


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


class MailTests(unittest.TestCase):
    def test_exact_id_not_substring_or_subject(self):
        raw = reply_bytes('<prefix-request@test>')
        self.assertIsNone(plain_reply(raw, '<request@test>', 100))
        self.assertEqual(plain_reply(reply_bytes(), '<request@test>', 4), 'Here')

    def test_plain_alternative_decoding_and_skip_attachments(self):
        message = EmailMessage()
        message['In-Reply-To'] = '<other@test> <request@test>'
        message.set_content('Café answer.')
        message.add_alternative('<p>HTML answer</p>', subtype='html')
        message.add_attachment(b'Unwanted attachment', maintype='text', subtype='plain', filename='note.txt')
        self.assertEqual(plain_reply(message.as_bytes(), '<request@test>', 100), 'Café answer.')

    def test_html_only_ignored(self):
        message = EmailMessage()
        message['In-Reply-To'] = '<request@test>'
        message.set_content('<b>Not plain text</b>', subtype='html')
        self.assertIsNone(plain_reply(message.as_bytes(), '<request@test>', 100))

    def test_idle_then_reply_without_marking_read(self):
        class FakeIMAP:
            Error = OSError
            def __init__(self, *args, **kwargs): self.searches = 0
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def login(self, *args): pass
            def select_folder(self, folder, readonly): assert readonly
            def capabilities(self): return [b'IDLE']
            def search(self, criteria):
                self.searches += 1
                return [] if self.searches == 1 else [123]
            def fetch(self, uids, parts):
                if parts == ['RFC822.SIZE']: return {123: {b'RFC822.SIZE': 100}}
                assert parts == ['BODY.PEEK[]']
                return {123: {b'BODY[]': reply_bytes()}}
            def idle(self): pass
            def idle_check(self, timeout): return [(1, b'EXISTS')]
            def idle_done(self): pass
        config = dict(imap_host='test', imap_port=993, username='a@test', app_password='secret',
                      mailbox='INBOX', max_reply_chars=100)
        with patch('imapclient.IMAPClient', FakeIMAP):
            result = Mail(config).wait_reply('<request@test>', time.time() + 1, threading.Event())
        self.assertEqual(result, 'Here is your answer.')


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
        self.session = Session(self.socket, {'audio': AUDIO, 'mail': {'reply_timeout_seconds': 10}},
                               FakeEngines(), self.store)
        self.sent = []
        self.session.mail.new_id = lambda: '<saved@test>'
        self.session.mail.send = lambda text, mid: self.sent.append((text, mid))
        self.session.mail.wait_reply = lambda *args: 'It is sunny.'
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
        await self.socket.incoming.put(SILENCE)
        await self.until('listening')
        await self.socket.incoming.put(SPEECH + SILENCE * 2)
        events = await self.until('audio_end')
        statuses = [e['status'] for e in events if isinstance(e, dict) and e.get('type') == 'status']
        self.assertEqual(statuses, ['transcribing', 'waiting', 'waiting', 'speaking'])
        data = b''.join(e for e in events if isinstance(e, bytes))
        with wave.open(io.BytesIO(data), 'rb') as wav:
            self.assertEqual(wav.getnframes(), 1280)
        self.assertEqual(self.sent, [('What is the weather?', '<saved@test>')])
        self.assertEqual(self.store.latest()['status'], 'replied')
        await self.socket.incoming.put(json.dumps({'type': 'playback_done', 'id': 'wrong'}))
        await asyncio.sleep(0.01)
        self.assertEqual(self.session.status, 'speaking')
        await self.socket.incoming.put(json.dumps({'type': 'playback_done', 'id': '<saved@test>'}))
        await self.until('idle')
        self.assertEqual(self.store.latest()['status'], 'done')

    async def test_uncertain_send_recovers_without_resending(self):
        self.store.create('<saved@test>', 'Existing request')
        self.task = asyncio.create_task(self.session.run())
        await self.until('audio_end')
        self.assertEqual(self.sent, [])
        self.assertEqual(self.session.transcript, 'Existing request')

    async def test_silent_capture_does_not_send_mail(self):
        self.task = asyncio.create_task(self.session.run())
        await self.until('idle')
        await self.socket.incoming.put(SILENCE)
        await self.until('listening')
        await self.socket.incoming.put(SILENCE * 3)
        events = await self.until('idle')
        self.assertIn('No speech', events[-1]['error'])
        self.assertEqual(self.sent, [])

    async def test_invalid_pcm_closes_socket(self):
        await self.socket.incoming.put(b'\x01')
        await self.session.receive()
        self.assertEqual(self.socket.closed, 1003)

    async def test_disconnect_cancels_imap_worker(self):
        self.store.create('<saved@test>', 'Existing request')
        started = threading.Event()
        finished = threading.Event()
        def waiting(message_id, deadline, stop):
            started.set()
            stop.wait(5)
            finished.set()
            raise InterruptedError('Cancelled')
        self.session.mail.wait_reply = waiting
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
        self.session.mail.wait_reply = timeout
        self.task = asyncio.create_task(self.session.run())
        events = await self.until('idle')
        self.assertIn('TimeoutError', events[-1]['error'])
        self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
