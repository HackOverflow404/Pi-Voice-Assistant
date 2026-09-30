import argparse
import datetime as dt
import asyncio
import re
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

from . import agenda, controls, skills
from .audio import (FRAME_SECONDS, Capture, Framer, is_cancel_phrase, is_noise_transcript, normalize, rms,
                    speech_threshold, wake_is_plausible)
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
    w.setdefault('instruction', '')
    w.setdefault('send_delay_seconds', 3)
    w.setdefault('reaction_phrases', {'👀': 'Looking into it.', '👍': 'On it.'})
    if not (w.get('contact') and w.get('bridge_url') and 1 <= w['reply_timeout_seconds'] <= 3600 and
            1 <= w['max_reply_chars'] <= 10000 and 0 <= w['reply_settle_seconds'] <= 30):
        raise ValueError('Invalid whatsapp contact, bridge URL, timeouts or reply length')
    tts = c.setdefault('tts', {}) or {}
    c['tts'] = tts
    tts.setdefault('engine', 'piper')
    tts.setdefault('deepgram_voice', 'aura-2-thalia-en')
    tts.setdefault('speech_volume', 0.3)
    tts.setdefault('deepgram_speed', 1.0)
    if not 0.7 <= tts['deepgram_speed'] <= 1.5:
        raise ValueError('tts.deepgram_speed must be between 0.7 and 1.5')
    if not 0.01 <= tts['speech_volume'] <= 1:
        raise ValueError('tts.speech_volume must be between 0.01 and 1')
    if tts['engine'] not in ('piper', 'deepgram') or (tts['engine'] == 'deepgram' and not tts.get('deepgram_api_key')):
        raise ValueError('tts.engine must be piper or deepgram; deepgram needs tts.deepgram_api_key')
    stt = c.setdefault('stt', {}) or {}
    c['stt'] = stt
    stt.setdefault('engine', 'whisper')
    stt.setdefault('deepgram_model', 'nova-3')
    if stt['engine'] not in ('whisper', 'deepgram') or (stt['engine'] == 'deepgram' and not tts.get('deepgram_api_key')):
        raise ValueError('stt.engine must be whisper or deepgram; deepgram needs tts.deepgram_api_key')
    echo = c.setdefault('echo', {}) or {}
    c['echo'] = echo
    echo.setdefault('adb_serial', None)
    echo.setdefault('pairing_seconds', 120)
    if not 10 <= echo['pairing_seconds'] <= 600:
        raise ValueError('echo.pairing_seconds must be between 10 and 600')
    jev = c.setdefault('jev', {}) or {}
    c['jev'] = jev
    jev.setdefault('api_key', '')
    jev.setdefault('min_probability', 0.6)
    jev.setdefault('timeout_seconds', 3)
    if not (0.34 <= jev['min_probability'] <= 1 and 0.5 <= jev['timeout_seconds'] <= 15):
        raise ValueError('jev.min_probability must be 0.34-1 and jev.timeout_seconds 0.5-15')
    cal = c.setdefault('calendar', {}) or {}
    c['calendar'] = cal
    cal.setdefault('ical_urls', [])
    cal.setdefault('refresh_minutes', 5)
    if not (isinstance(cal['ical_urls'], list) and 1 <= cal['refresh_minutes'] <= 1440):
        raise ValueError('calendar.ical_urls must be a list and refresh_minutes 1-1440')
    c['state_dir'] = str((path.parent / c['state_dir']).resolve())
    for key in ('tls_cert', 'tls_key'):
        if c['server'].get(key):
            c['server'][key] = str((path.parent / c['server'][key]).resolve())
    if bool(c['server'].get('tls_cert')) != bool(c['server'].get('tls_key')):
        raise ValueError('Set both TLS certificate and key')
    return c


def canonical_emoji(text):
    """Emoji without skin-tone modifiers or variation selectors, so 👍🏽 matches 👍."""
    return re.sub('[\U0001F3FB-\U0001F3FF\uFE0E\uFE0F]', '', text or '')


class NoReply(Exception):
    """No reply arrived before the request deadline."""


class Cancelled(Exception):
    """The user cancelled a request that was already sent."""


class AnyEvent:
    """is_set()/wait() over several threading.Events: session shutdown or a user cancel."""
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(e.is_set() for e in self.events)

    def wait(self, timeout):
        end = time.monotonic() + timeout
        while not self.is_set() and time.monotonic() < end:
            time.sleep(min(0.1, max(0.0, end - time.monotonic())))
        return self.is_set()


_last_cpu = None  # (busy, total) jiffies at the previous system_stats() call


def cpu_percent():
    """CPU busy share since the previous call, from /proc/stat; None on the first call."""
    global _last_cpu
    fields = [int(x) for x in Path('/proc/stat').read_text().split('\n', 1)[0].split()[1:]]
    idle = fields[3] + fields[4]  # idle + iowait
    total = sum(fields[:8])       # excludes guest time, already counted in user/nice
    previous, _last_cpu = _last_cpu, (total - idle, total)
    if previous is None or total <= previous[1]:
        return None
    return round(100 * (total - idle - previous[0]) / (total - previous[1]), 1)


def max_temperature():
    """Hottest reading across all thermal zones and hwmon sensors, in degrees C."""
    readings = []
    for pattern in ('/sys/class/thermal/thermal_zone*/temp', '/sys/class/hwmon/hwmon*/temp*_input'):
        for path in Path('/').glob(pattern.lstrip('/')):
            with suppress(OSError, ValueError):
                readings.append(int(path.read_text()) / 1000)
    return round(max(readings), 1) if readings else None


def system_stats():
    """Pi health for the dashboard; any unreadable source is omitted."""
    stats = {}
    with suppress(OSError, ValueError, KeyError):
        info = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        mb = lambda key: int(info[key].split()[0]) // 1024
        stats.update(mem_total_mb=mb('MemTotal'), mem_available_mb=mb('MemAvailable'),
                     swap_used_mb=mb('SwapTotal') - mb('SwapFree'))
        stats['mem_percent'] = round(100 * (1 - mb('MemAvailable') / mb('MemTotal')), 1)
    with suppress(OSError, ValueError, IndexError):
        stats['cpu_percent'] = cpu_percent()
    with suppress(OSError, ValueError, IndexError):
        stats['load1'] = float(Path('/proc/loadavg').read_text().split()[0])
    with suppress(OSError, ValueError, IndexError):
        stats['uptime_s'] = int(float(Path('/proc/uptime').read_text().split()[0]))
    stats['temp_c'] = max_temperature()
    return {k: v for k, v in stats.items() if v is not None}


class Session:
    def __init__(self, ws, config, engines, store, calendar=None, levels=None, echo=None, jev=None,
                 clock=None, weather=None):
        self.ws, self.config, self.engines, self.store = ws, config, engines, store
        self.levels, self.echo, self.jev = levels, echo, jev  # device commands; jev is optional
        self.clock, self.weather = clock, weather  # built-in skills; the clock is shared
        self.calendar = calendar  # agenda.Shared, or None when no calendars are configured
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
        self.audio_lock = asyncio.Lock()  # one WAV transfer at a time: replies and announcements
        self.phrases = {}  # cached announcement audio
        self.cancel = asyncio.Event()  # the Echo's Cancel tap, before sending
        self.abort = threading.Event()  # the same tap while waiting for the reply
        self.volume_reply = None  # the Echo's answer to a media volume change
        self.pairing_timer = None

    async def event(self, **payload):
        await self.ws.send(json.dumps(payload))

    async def set_status(self, status, error=None):
        self.status, self.error = status, error
        await self.event(type='status', status=status, last_transcript=self.transcript,
                         last_reply=self.reply, message_id=self.message_id, error=error,
                         speech_volume=self.config.get('tts', {}).get('speech_volume', 1.0),
                         send_delay=self.config.get('whatsapp', {}).get('send_delay_seconds', 0))

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
                    if event.get('type') == 'clock' and self.clock:  # a tap on the timer tile
                        self.clock.act(event.get('id'), event.get('action'))
                    if event.get('type') == 'cancel' and self.status in ('confirming', 'waiting'):
                        self.cancel.set()
                        self.abort.set()
                    if (event.get('type') == 'media_volume_state' and self.volume_reply and
                            not self.volume_reply.done()):
                        self.volume_reply.set_result(float(event['level']))
                    if (event.get('type') in ('playback_done', 'playback_error') and
                            self.playback_id and event.get('id') == self.playback_id):
                        self.playback_error = event.get('type') == 'playback_error'
                        self.played.set()
                except (ValueError, AttributeError):
                    await self.ws.close(1003, 'Invalid JSON control message')
                    return

    async def speak(self, text, audio_id=None):
        """Synthesize and stream one sentence at a time: the client queues the WAV segments
        and starts playing the first while later ones are still being synthesized, then
        acknowledges once after the segment marked last. A reply to a request is recorded as
        done; a device command's confirmation passes its own audio_id and isn't stored."""
        await self.set_status('speaking')
        self.playback_id = audio_id or self.message_id
        self.playback_error = False
        self.played.clear()
        parts = chunks(text) or ['I got an empty reply.']
        started, total = time.monotonic(), 0.0
        for seq, part in enumerate(parts):
            async with self.audio_lock:
                wav, duration = await self.blocking(self.engines.synthesize, part)
                if seq == 0:
                    LOG.info('First of %d speech segments ready after %.1f s', len(parts), time.monotonic() - started)
                total += duration
                await self.send_wav(self.playback_id, seq, seq == len(parts) - 1, wav, duration)
        await asyncio.wait_for(self.played.wait(), timeout=total + 30)
        if self.playback_error:
            raise RuntimeError('Client could not play the reply')
        self.playback_id = None
        if audio_id is None:
            self.store.update(self.message_id, 'done')

    async def send_wav(self, audio_id, seq, last, wav, duration):
        if len(wav) > 32 * 1024 * 1024:
            raise ValueError('Synthesized audio exceeds 32 MiB limit')
        await self.event(type='audio_start', id=audio_id, seq=seq, last=last, format='wav',
                         bytes=len(wav), duration_seconds=duration,
                         volume=self.config.get('tts', {}).get('speech_volume', 1.0))
        for offset in range(0, len(wav), 32768):
            await self.ws.send(wav[offset:offset + 32768])
        await self.event(type='audio_end', id=audio_id, seq=seq, last=last)

    async def announce(self, phrase):
        """Speak a short status phrase (e.g. for Instinct's 👀 reaction) while still waiting.
        It has its own audio ID, so it neither needs nor satisfies the reply's playback ack."""
        try:
            async with self.audio_lock:
                if phrase not in self.phrases:
                    self.phrases[phrase] = await asyncio.to_thread(self.engines.synthesize, phrase)
                wav, duration = self.phrases[phrase]
                await self.send_wav(f'announce-{uuid.uuid4().hex[:8]}', 0, True, wav, duration)
            LOG.info('Announced "%s"', phrase)
        except Exception as exc:
            LOG.warning('Announcement failed: %s', type(exc).__name__)

    def reaction_handler(self):
        """Callback for the reply wait (a worker thread): each configured reaction emoji is
        announced once per request. Skin tones and variation selectors are ignored."""
        loop, announced = asyncio.get_running_loop(), set()
        phrases = {canonical_emoji(k): v for k, v in self.config['whatsapp'].get('reaction_phrases', {}).items()}

        def on_reaction(emoji):
            emoji = canonical_emoji(emoji)
            if emoji in phrases and emoji not in announced:
                announced.add(emoji)
                asyncio.run_coroutine_threadsafe(self.announce(phrases[emoji]), loop)
        return on_reaction

    def no_reply_message(self):
        seconds = self.config['whatsapp']['reply_timeout_seconds']
        return f'No reply within {seconds // 60} min' if seconds >= 60 else f'No reply within {seconds} s'

    async def wait_and_speak(self, created, since=None):
        """Wait for the contact's reply to request self.message_id, created at `created`.
        `since` is WhatsApp's timestamp for the sent message; after a restart only the local
        creation time is known, so allow a few seconds of clock skew."""
        await self.set_status('waiting')
        w = self.config['whatsapp']
        try:
            raw = await self.blocking(self.messenger.wait_reply, self.message_id, since or created - 5,
                                      created + w['reply_timeout_seconds'], AnyEvent(self.stop, self.abort),
                                      self.reaction_handler())
        except TimeoutError as exc:
            self.store.update(self.message_id, 'expired')
            raise NoReply from exc
        except InterruptedError as exc:
            if self.abort.is_set() and not self.stop.is_set():
                raise Cancelled from exc
            raise
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
        except Cancelled:
            await self.retract()
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
                    before = recent[:-15]
                    if not wake_is_plausible(self.config['audio'], recent[-15:], before):
                        LOG.info('Ignored wake word (score %.2f): phrase peak %d, room %d', score,
                                 max(recent[-15:]), sorted(before)[len(before) // 5] if before else 0)
                        self.engines.reset()
                        continue
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
                if is_noise_transcript(self.transcript):
                    LOG.info('Ignored likely accidental trigger: transcript was non-speech')
                    await self.set_status('idle')
                    continue
                if is_cancel_phrase(self.transcript):
                    LOG.info('Request cancelled by voice')
                    await self.set_status('idle', 'Cancelled')
                    continue
                decision = await self.route(self.transcript)
                if decision == 'cancel':
                    await self.set_status('idle', 'Cancelled')
                    continue
                if decision == 'accidental':
                    await self.set_status('idle')
                    continue
                if isinstance(decision, (controls.Command, skills.Skill)):
                    await self.control(decision)
                    await self.set_status('idle', self.error)
                    continue
                # A short window to discard a false trigger before anything is sent.
                self.cancel.clear()
                self.abort.clear()
                delay = self.config['whatsapp'].get('send_delay_seconds', 0)
                if delay:
                    await self.set_status('confirming')
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.cancel.wait(), delay)
                    if self.cancel.is_set():
                        LOG.info('Request cancelled before sending')
                        await self.set_status('idle', 'Cancelled')
                        continue
                self.reply = ''
                self.message_id = f'<{uuid.uuid4()}@pi-voice>'
                self.store.create(self.message_id, self.transcript)
                await self.set_status('waiting')
                sent = await self.blocking(self.messenger.send, self.transcript)
                # Keep WhatsApp's ID: replies are matched to it, including after a restart.
                self.store.rename(self.message_id, sent['id'])
                self.message_id = sent['id']
                self.store.update(self.message_id, 'sent')
                await self.event(type='sent', id=self.message_id)  # the Echo chimes
                await self.wait_and_speak(self.store.latest()['created'], sent['timestamp'])
                await self.set_status('idle')
            except NoReply:
                LOG.info('No reply to %s before the deadline', self.message_id)
                await self.set_status('idle', self.no_reply_message())
            except Cancelled:
                await self.retract()
            except Exception as exc:
                LOG.warning('Request failed: %s', type(exc).__name__)
                await self.set_status('idle', f'{type(exc).__name__}: request failed; check WhatsApp/network')
            finally:
                self.flush()

    async def route(self, text):
        """A device command from the grammar, else Jev's decision when it is configured,
        else 'instinct'. A Jev failure never loses the request: it goes to Instinct."""
        command = controls.parse(text) or skills.parse(text, ringing=bool(self.clock and self.clock.ringing()))
        if command or not self.jev:
            return command or 'instinct'
        started = time.monotonic()
        try:
            decision, probability = await asyncio.to_thread(self.jev.decide, text)
        except Exception as exc:
            LOG.warning('Jev failed (%s); sending to Instinct', exc)
            return 'instinct'
        LOG.info('Jev routed to %s (p=%.2f) in %.2f s', decision, probability, time.monotonic() - started)
        return decision

    async def control(self, command):
        """Carry out a device command or built-in skill and confirm it aloud."""
        LOG.info('Device command %s', command)
        self.reply, self.error = '', None
        if isinstance(command, skills.Skill):
            try:
                self.reply = await self.run_skill(command)
            except Exception as exc:
                LOG.warning('Skill %s failed: %s', command.action, exc)
                self.reply = "Sorry, I couldn't do that right now."
            if self.reply:  # a dismissed alarm needs no answer
                await self.speak(self.reply, audio_id=f'control-{uuid.uuid4().hex[:8]}')
            return
        try:
            if command.action == 'pairing_on':
                before = await self.blocking(self.echo.bonded)
                name = await self.blocking(self.start_pairing)
                seconds = self.config['echo']['pairing_seconds']
                duration = f'{seconds // 60} minutes' if seconds % 60 == 0 and seconds > 60 else f'{seconds} seconds'
                self.schedule_pairing_stop(before)
                self.reply = f'Pairing mode is on. Connect to {name} within {duration}.'
            elif command.action == 'pairing_off':
                if self.pairing_timer:
                    self.pairing_timer.cancel()
                await self.blocking(self.echo.stop_pairing)
                self.reply = 'Pairing mode is off.'
            elif command.action.startswith('ai_'):
                current = self.levels.speech_volume
                level = {'ai_up': current + controls.STEP, 'ai_down': current - controls.STEP}.get(
                    command.action, command.level)
                self.set_speech_volume(controls.clamp(level, lowest=0.05))
                self.reply = f'My voice is at {controls.percent(self.levels.speech_volume)}.'
            else:
                level = await self.media_volume(command.action.split('_')[1], command.level)
                self.reply = 'The speaker is muted.' if level == 0 else f'Volume {controls.percent(level)}.'
        except Exception as exc:
            LOG.warning('Device command %s failed: %s', command.action, exc)
            self.error = 'Could not control the Echo'
            self.reply = 'Sorry, I could not change that on the Echo.'
        await self.speak(self.reply, audio_id=f'control-{uuid.uuid4().hex[:8]}')

    def start_pairing(self):
        self.echo.start_pairing()
        with suppress(Exception):
            name = self.echo.adb('shell', 'settings', 'get', 'secure', 'bluetooth_name')
            if name and name != 'null':
                return name
        return 'this speaker'

    def schedule_pairing_stop(self, before):
        """Back to the dashboard as soon as a new device has paired (the Echo lists it among
        its bonded devices), or when pairing_seconds run out; stop watching if the user has
        already left the pairing screen."""
        if self.pairing_timer:
            self.pairing_timer.cancel()

        async def watch():
            deadline = time.monotonic() + self.config['echo']['pairing_seconds']
            while time.monotonic() < deadline:
                await asyncio.sleep(2)
                with suppress(Exception):
                    if not await asyncio.to_thread(self.echo.pairing_open):
                        return
                    new = {a: n for a, n in (await asyncio.to_thread(self.echo.bonded)).items() if a not in before}
                    if new:
                        await asyncio.sleep(2)  # let the pairing dialog finish
                        await asyncio.to_thread(self.echo.stop_pairing)
                        name = next(iter(new.values())) or 'the new device'
                        LOG.info('Paired with %s; back to the dashboard', name)
                        await self.announce(f'Paired with {name}.')
                        return
            with suppress(Exception):
                await asyncio.to_thread(self.echo.stop_pairing)
                LOG.info('Pairing mode timed out')
        self.pairing_timer = asyncio.create_task(watch())

    async def run_skill(self, skill):
        """The answer to say for a built-in skill."""
        clock, now = self.clock, dt.datetime.now().astimezone()
        a = skill.action
        if a == 'timer_start':
            if not skill.seconds:
                return 'How long should the timer be? Say, for example, set a timer for ten minutes.'
            return clock.start_timer(skill.seconds, skill.label)
        if a in ('timer_pause', 'timer_resume', 'timer_stop'):
            method = {'timer_pause': clock.pause_timer, 'timer_resume': clock.resume_timer,
                      'timer_stop': clock.stop_timer}[a]
            # A bare "pause"/"resume" with no timer means the stopwatch.
            if not clock.timers and clock.stopwatch and a != 'timer_stop':
                return clock.pause_stopwatch() if a == 'timer_pause' else clock.resume_stopwatch()
            return method(skill.label, skill.all)
        if a == 'timer_query':
            return clock.timer_status(skill.label)
        if a == 'dismiss':
            clock.dismiss()
            return ''
        if a.startswith('stopwatch_'):
            return {'stopwatch_start': clock.start_stopwatch, 'stopwatch_pause': clock.pause_stopwatch,
                    'stopwatch_resume': clock.resume_stopwatch, 'stopwatch_stop': clock.stop_stopwatch,
                    'stopwatch_query': clock.stopwatch_status}[a]()
        if a == 'time_query':
            return skills.say_time(now)
        if a == 'date_query':
            return skills.say_date(now)
        if a == 'math':
            return skills.math_reply(skill)
        if a == 'weather_query':
            return await asyncio.to_thread(self.weather.report, skill.day)
        if a == 'calendar_query':
            return skills.calendar_report(self.calendar.events if self.calendar else None, now, skill.day)
        return ''

    def clock_changed(self):
        """Push timers and the stopwatch to the Echo, and say when a timer finishes."""
        loop = self.loop
        ringing = {t.id for t in self.clock.timers if t.rang_at}
        finished = [t for t in self.clock.timers if t.id in ringing - self.rang]
        self.rang = ringing
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(
            self.event(type='timers', items=self.clock.snapshot())))
        for t in finished:
            phrase = f'Your {t.label} timer is done.' if t.label else 'Your timer is done.'
            loop.call_soon_threadsafe(lambda p=phrase: asyncio.ensure_future(self.announce(p)))

    def set_speech_volume(self, level):
        self.levels.speech_volume = level
        self.config['tts']['speech_volume'] = level  # read for every reply and chime

    async def media_volume(self, change, level=None):
        """The Echo app owns the media volume (it pins it against Bluetooth sources), so ask
        it to move its pinned level; it answers with the level actually applied."""
        self.volume_reply = asyncio.get_running_loop().create_future()
        await self.event(type='media_volume', change=change, level=level, step=controls.STEP)
        try:
            return await asyncio.wait_for(self.volume_reply, 5)
        finally:
            self.volume_reply = None

    async def retract(self):
        """Delete the cancelled request from the chat for everyone, and stop waiting."""
        self.store.update(self.message_id, 'cancelled')
        try:
            await self.blocking(self.messenger.revoke, self.message_id)
            LOG.info('Cancelled and unsent %s', self.message_id)
            await self.set_status('idle', 'Cancelled and unsent')
        except Exception as exc:
            LOG.warning('Could not unsend %s: %s', self.message_id, type(exc).__name__)
            await self.set_status('idle', 'Cancelled; could not unsend the message')

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
        """Pi health every 30 s; calendar events whenever they change (checked every 5 s)."""
        sent_version, tick = 0, 0
        while True:
            if tick % 6 == 0:
                await self.event(type='system', **system_stats())
            if self.calendar and self.calendar.events is not None and self.calendar.version != sent_version:
                sent_version = self.calendar.version
                await self.event(type='calendar', events=self.calendar.events)
            tick += 1
            await asyncio.sleep(5)

    async def run(self):
        if self.clock:
            self.loop, self.rang = asyncio.get_running_loop(), {t.id for t in self.clock.timers if t.rang_at}
            self.clock.listeners.append(self.clock_changed)
            await self.event(type='timers', items=self.clock.snapshot())
        receiver = asyncio.create_task(self.receive())
        processor = asyncio.create_task(self.process())
        telemetry = asyncio.create_task(self.telemetry())
        try:
            done, _ = await asyncio.wait([receiver, processor], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.stop.set()
            if self.clock and self.clock_changed in self.clock.listeners:
                self.clock.listeners.remove(self.clock_changed)
            if self.pairing_timer:
                self.pairing_timer.cancel()
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
    levels = controls.Levels(state, config['tts']['speech_volume'])
    config['tts']['speech_volume'] = levels.speech_volume  # a spoken change outlives restarts
    echo = controls.Echo(config['echo'])
    jev = controls.Jev(config['jev']) if config['jev'].get('api_key') else None
    clock, weather = skills.Clock(state), skills.Weather()

    async def ticker():  # finished timers start ringing, even with no Echo connected
        while True:
            with suppress(Exception):
                clock.tick()
            await asyncio.sleep(0.5)
    clock_task = asyncio.create_task(ticker())  # noqa: F841 (kept referenced)
    LOG.info('Device commands: grammar%s', ' + Jev fallback' if jev else ' only (no jev.api_key)')
    current = None  # (connection, finished event) of the connected microphone
    calendar = refresher = None  # keep a reference so the task isn't garbage-collected
    if config['calendar']['ical_urls']:
        calendar = agenda.Shared()

        async def refresh():
            urls, minutes = config['calendar']['ical_urls'], config['calendar']['refresh_minutes']
            while True:
                calendar.update(await asyncio.to_thread(agenda.load, urls))
                await asyncio.sleep(minutes * 60)
        refresher = asyncio.create_task(refresh())

    async def handler(ws):
        nonlocal current
        auth = ws.request.headers.get('Authorization', '')
        if not hmac.compare_digest(auth.encode(), ('Bearer ' + config['server']['token']).encode()):
            await ws.close(1008, 'Unauthorized')
            return
        if current:
            # One microphone at a time. After a Wi-Fi blip the Echo reconnects before the old
            # connection has timed out here, so a new authenticated connection replaces the old
            # one instead of being refused until it does.
            old_ws, old_done = current
            LOG.info('Replacing the previous microphone connection')
            try:
                await asyncio.wait_for(old_ws.close(1012, 'Replaced by a new connection'), 2)
            except Exception:
                with suppress(Exception):
                    old_ws.transport.abort()  # the old peer is gone; don't wait for its handshake
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(old_done.wait(), 10)
        done = asyncio.Event()
        current = (ws, done)
        try:
            await Session(ws, config, engines, store, calendar, levels, echo, jev, clock=clock, weather=weather).run()
        except ConnectionClosed:
            pass
        except Exception:
            LOG.exception('Session ended')
        finally:
            done.set()
            if current and current[0] is ws:
                current = None

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
    if refresher:
        refresher.cancel()
    store.db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--check', action='store_true', help='Load config and all models, then exit')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = load_config(args.config)
    from .engines import Engines
    engines = Engines(config['models'], config['tts'], config['stt'])
    if args.check:
        LOG.info('Configuration and model loading OK')
    else:
        asyncio.run(serve(config, engines))


if __name__ == '__main__':
    main()
