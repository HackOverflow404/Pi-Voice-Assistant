import argparse
import asyncio
from collections import deque
from contextlib import suppress
import hmac
import json
import logging
from pathlib import Path
import signal
import ssl
import threading
import time
import uuid
import wave

from .audio import FRAME_SECONDS, Capture, Framer, normalize, rms, speech_threshold
from .speech import chunks, speakable
from .store import Store
from .whatsapp import WhatsApp

LOG = logging.getLogger('voice')


def load_config(path):
    import yaml
    path = Path(path).resolve()
    c = yaml.safe_load(path.read_text())
    token = c['server']['token']
    if not isinstance(token, str) or len(token) < 32 or token.startswith('REPLACE'):
        raise ValueError('Set server.token to a random secret of at least 32 characters')
    for key, value in c['models'].items():
        target = (path.parent / value).resolve()
        if not target.exists():
            raise ValueError(f'Missing {key} model: {target}')
        c['models'][key] = str(target)
    a = c['audio']
    if not (0 < a['wake_threshold'] <= 1 and a['speech_rms'] > 0 and
            0 < a['silence_seconds'] < a['max_utterance_seconds'] <= 60 and
            0 < a['start_timeout_seconds'] <= a['max_utterance_seconds']):
        raise ValueError('Invalid audio thresholds or durations')
    w = c['whatsapp']
    if not (w.get('contact') and w.get('bridge_url') and 1 <= w['reply_timeout_seconds'] <= 3600 and
            1 <= w['max_reply_chars'] <= 10000 and 0 <= w['reply_settle_seconds'] <= 30):
        raise ValueError('Invalid whatsapp contact, bridge URL, timeouts or reply length')
    c['state_dir'] = str((path.parent / c['state_dir']).resolve())
    for key in ('tls_cert', 'tls_key'):
        if c['server'].get(key):
            c['server'][key] = str((path.parent / c['server'][key]).resolve())
    if bool(c['server'].get('tls_cert')) != bool(c['server'].get('tls_key')):
        raise ValueError('Set both TLS certificate and key')
    return c


class NoReply(Exception):
    """No reply arrived before the request deadline."""


def system_stats():
    """Pi health for the dashboard; any unreadable source is omitted."""
    stats = {}
    with suppress(OSError, ValueError, KeyError):
        info = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        mb = lambda key: int(info[key].split()[0]) // 1024
        stats.update(mem_total_mb=mb('MemTotal'), mem_available_mb=mb('MemAvailable'),
                     swap_used_mb=mb('SwapTotal') - mb('SwapFree'))
    with suppress(OSError, ValueError, IndexError):
        stats['load1'] = float(Path('/proc/loadavg').read_text().split()[0])
    with suppress(OSError, ValueError, IndexError):
        stats['uptime_s'] = int(float(Path('/proc/uptime').read_text().split()[0]))
    with suppress(OSError, ValueError):
        stats['temp_c'] = round(int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000, 1)
    return stats


class Session:
    def __init__(self, ws, config, engines, store):
        self.ws, self.config, self.engines, self.store = ws, config, engines, store
        self.messenger = WhatsApp(config['whatsapp'])
        self.queue = asyncio.Queue(maxsize=25)  # 2 seconds maximum backlog
        self.stop = threading.Event()
        self.played = asyncio.Event()
        self.playback_id = None
        self.status = 'waiting'  # discard PCM until recovery is complete
        self.transcript = ''
        self.reply = ''
        self.message_id = None
        self.error = None

    async def event(self, **payload):
        await self.ws.send(json.dumps(payload))

    async def set_status(self, status, error=None):
        self.status, self.error = status, error
        await self.event(type='status', status=status, last_transcript=self.transcript,
                         last_reply=self.reply, message_id=self.message_id, error=error)

    async def blocking(self, function, *args):
        # Do not release the shared engines to another session while a native call is running.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self.stop.set()
            with suppress(Exception):
                await task
            raise

    def flush(self):
        while not self.queue.empty():
            self.queue.get_nowait()
        self.engines.reset()

    async def receive(self):
        framer = Framer()
        async for data in self.ws:
            if isinstance(data, bytes):
                if len(data) % 2:
                    await self.ws.close(1003, 'PCM must contain complete int16 samples')
                    return
                if self.status not in ('idle', 'listening'):
                    framer = Framer()
                    continue
                for frame in framer.feed(data):
                    try:
                        self.queue.put_nowait(frame)
                    except asyncio.QueueFull:
                        await self.ws.close(1013, 'Audio processing cannot keep up')
                        return
            else:
                try:
                    event = json.loads(data)
                    if (event.get('type') in ('playback_done', 'playback_error') and
                            self.playback_id and event.get('id') == self.playback_id):
                        self.playback_error = event.get('type') == 'playback_error'
                        self.played.set()
                except (ValueError, AttributeError):
                    await self.ws.close(1003, 'Invalid JSON control message')
                    return

    async def speak(self, text):
        """Synthesize and stream one sentence at a time: the client queues the WAV segments
        and starts playing the first while later ones are still being synthesized, then
        acknowledges once after the segment marked last."""
        await self.set_status('speaking')
        self.playback_id = self.message_id
        self.playback_error = False
        self.played.clear()
        parts = chunks(text) or ['I got an empty reply.']
        started, total = time.monotonic(), 0.0
        for seq, part in enumerate(parts):
            wav, duration = await self.blocking(self.engines.synthesize, part)
            if len(wav) > 32 * 1024 * 1024:
                raise ValueError('Synthesized audio exceeds 32 MiB limit')
            if seq == 0:
                LOG.info('First of %d speech segments ready after %.1f s', len(parts), time.monotonic() - started)
            total += duration
            last = seq == len(parts) - 1
            await self.event(type='audio_start', id=self.playback_id, seq=seq, last=last, format='wav',
                             bytes=len(wav), duration_seconds=duration)
            for offset in range(0, len(wav), 32768):
                await self.ws.send(wav[offset:offset + 32768])
            await self.event(type='audio_end', id=self.playback_id, seq=seq, last=last)
        await asyncio.wait_for(self.played.wait(), timeout=total + 30)
        if self.playback_error:
            raise RuntimeError('Client could not play the reply')
        self.playback_id = None
        self.store.update(self.message_id, 'done')

    def no_reply_message(self):
        seconds = self.config['whatsapp']['reply_timeout_seconds']
        return f'No reply within {seconds // 60} min' if seconds >= 60 else f'No reply within {seconds} s'

    async def wait_and_speak(self, created, since=None):
        """Wait for the contact's reply to the request created at `created`. `since` is
        WhatsApp's timestamp for the sent message; after a restart only the local creation
        time is known, so allow a few seconds of clock skew."""
        await self.set_status('waiting')
        w = self.config['whatsapp']
        try:
            raw = await self.blocking(self.messenger.wait_reply, since or created - 5,
                                      created + w['reply_timeout_seconds'], self.stop)
        except TimeoutError as exc:
            self.store.update(self.message_id, 'expired')
            raise NoReply from exc
        self.reply = speakable(raw)[:w['max_reply_chars']]
        self.store.update(self.message_id, 'replied', self.reply)
        await self.speak(self.reply)

    async def recover(self):
        latest = self.store.latest()
        if not latest:
            return
        self.message_id = latest['message_id']
        self.transcript, self.reply = latest['transcript'], latest['reply']
        expired = time.time() >= latest['created'] + self.config['whatsapp']['reply_timeout_seconds']
        if latest['status'] in ('sending', 'sent') and expired:
            # Past its deadline (e.g. the client was away): close it out instead of retrying.
            self.store.update(self.message_id, 'expired')
        elif latest['status'] in ('sending', 'sent'):
            # A crash between sending and commit is ambiguous. Keep watching for the reply;
            # never resend automatically, which could cause duplicate external actions.
            await self.wait_and_speak(latest['created'])
        elif latest['status'] == 'replied':
            await self.speak(self.reply)

    async def process(self):
        try:
            await self.recover()
        except NoReply:
            await self.set_status('idle', self.no_reply_message())
        except Exception as exc:
            LOG.warning('Recovery failed: %s', type(exc).__name__)
            await self.set_status('idle', f'Recovery failed: {type(exc).__name__}')
        self.flush()
        await self.set_status('idle', self.error)
        capture = None
        levels = deque(maxlen=50)  # last 4 s of frame loudness while waiting for the wake word
        while True:
            try:
                frame = await asyncio.wait_for(self.queue.get(), 15)
            except asyncio.TimeoutError:
                await self.ws.close(1008, 'No microphone audio received for 15 seconds')
                return
            if capture is None:
                levels.append(rms(frame))
                score = await self.blocking(self.engines.predict, frame)
                if score >= self.config['audio']['wake_threshold']:
                    recent = list(levels)
                    threshold = speech_threshold(self.config['audio'], recent[-15:], recent)
                    LOG.info('Wake word (score %.2f): phrase peak %d, noise %d, speech threshold %d', score,
                             max(recent[-15:]), sorted(recent)[len(recent) // 5], threshold)
                    capture = Capture(self.config['audio'], threshold)
                    await self.set_status('listening')
                continue
            if not capture.feed(frame):
                continue
            LOG.info('Utterance ended after %.1f s: speech=%s, peak %d', len(capture.frames) * FRAME_SECONDS,
                     capture.speech, capture.peak)
            levels.clear()
            pcm, capture = capture.pcm, None
            try:
                await self.set_status('transcribing')
                self.transcript = await self.blocking(self.transcribe, pcm) if pcm else ''
                if not self.transcript:
                    await self.set_status('idle', 'No speech recognized; say the wake phrase again')
                    continue
                self.reply = ''
                self.message_id = f'<{uuid.uuid4()}@pi-voice>'
                self.store.create(self.message_id, self.transcript)
                await self.set_status('waiting')
                sent_at = await self.blocking(self.messenger.send, self.transcript)
                self.store.update(self.message_id, 'sent')
                await self.wait_and_speak(self.store.latest()['created'], sent_at)
                await self.set_status('idle')
            except NoReply:
                LOG.info('No reply to %s before the deadline', self.message_id)
                await self.set_status('idle', self.no_reply_message())
            except Exception as exc:
                LOG.warning('Request failed: %s', type(exc).__name__)
                await self.set_status('idle', f'{type(exc).__name__}: request failed; check WhatsApp/network')
            finally:
                self.flush()

    def save_utterance(self, pcm):
        """Opt-in (audio.debug_save_utterances) copy of the last five raw recordings in
        state/utterances, for tuning speech recognition on real microphone audio."""
        folder = Path(self.config['state_dir']) / 'utterances'
        folder.mkdir(mode=0o700, exist_ok=True)
        name = time.strftime('%Y%m%d-%H%M%S') + f'-{int(time.time() * 1000) % 1000:03d}.wav'
        with wave.open(str(folder / name), 'wb') as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
            out.writeframes(pcm)
        for old in sorted(folder.glob('*.wav'))[:-5]:
            old.unlink()

    def transcribe(self, pcm):
        if self.config['audio'].get('debug_save_utterances'):
            self.save_utterance(pcm)
        pcm, gain = normalize(pcm)
        text = self.engines.transcribe(pcm)
        LOG.info('Transcribed %.1f s at gain %.1fx: %d words', len(pcm) / 32000, gain, len(text.split()))
        return text

    async def telemetry(self):
        while True:
            await self.event(type='system', **system_stats())
            await asyncio.sleep(30)

    async def run(self):
        receiver = asyncio.create_task(self.receive())
        processor = asyncio.create_task(self.process())
        telemetry = asyncio.create_task(self.telemetry())
        try:
            done, _ = await asyncio.wait([receiver, processor], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.stop.set()
            receiver.cancel()
            processor.cancel()
            telemetry.cancel()
            await asyncio.gather(receiver, processor, telemetry, return_exceptions=True)


async def serve(config, engines):
    from websockets.asyncio.server import serve as ws_serve
    from websockets.exceptions import ConnectionClosed
    state = Path(config['state_dir'])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = Store(state / 'requests.sqlite3')
    active = False

    async def handler(ws):
        nonlocal active
        auth = ws.request.headers.get('Authorization', '')
        if not hmac.compare_digest(auth.encode(), ('Bearer ' + config['server']['token']).encode()):
            await ws.close(1008, 'Unauthorized')
            return
        if active:
            await ws.close(1013, 'Another microphone client is connected')
            return
        active = True
        try:
            await Session(ws, config, engines, store).run()
        except ConnectionClosed:
            pass
        except Exception:
            LOG.exception('Session ended')
        finally:
            active = False

    tls = None
    c = config['server']
    if c.get('tls_cert'):
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(c['tls_cert'], c['tls_key'])
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)
    async with ws_serve(handler, c['host'], c['port'], ssl=tls, max_size=32768,
                        max_queue=8, compression=None, ping_interval=20, ping_timeout=20):
        LOG.info('Listening on %s:%s', c['host'], c['port'])
        await shutdown.wait()
    store.db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--check', action='store_true', help='Load config and all models, then exit')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = load_config(args.config)
    from .engines import Engines
    engines = Engines(config['models'])
    if args.check:
        LOG.info('Configuration and model loading OK')
    else:
        asyncio.run(serve(config, engines))


if __name__ == '__main__':
    main()
