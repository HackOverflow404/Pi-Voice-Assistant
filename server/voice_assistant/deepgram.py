"""Deepgram speech services: Aura-2 text to speech and Nova-3 speech recognition. Both
run off the Pi (reply text and the recorded request are sent to Deepgram); local voices
this natural run 5-13x slower than real time on a Pi 4, and Whisper tiny takes ~5 s."""
import io
import json
import urllib.error
import urllib.parse
import urllib.request
import wave

API = 'https://api.deepgram.com/v1/speak'
LISTEN_API = 'https://api.deepgram.com/v1/listen'
RATE = 24000


def post(url, key, body, content_type, timeout):
    request = urllib.request.Request(url, data=body, headers={'Authorization': f'Token {key}',
                                                              'Content-Type': content_type})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors='replace').strip()[:200]
        raise RuntimeError(f'Deepgram {exc.code}: {detail}') from exc


class DeepgramListener:
    """Transcribes one recorded request (16 kHz mono int16 PCM) with Nova-3."""
    def __init__(self, api_key, model='nova-3', url=LISTEN_API, timeout=8):
        self.api_key, self.model, self.url, self.timeout = api_key, model, url, timeout

    def transcribe(self, pcm):
        query = urllib.parse.urlencode({'model': self.model, 'language': 'en', 'smart_format': 'true',
                                        'encoding': 'linear16', 'sample_rate': 16000, 'channels': 1})
        result = json.loads(post(f'{self.url}?{query}', self.api_key, pcm, 'application/octet-stream', self.timeout))
        return result['results']['channels'][0]['alternatives'][0]['transcript'].strip()


class DeepgramVoice:
    def __init__(self, api_key, voice, speed=1.0, url=API, timeout=8):
        self.api_key, self.voice, self.speed, self.url, self.timeout = api_key, voice, speed, url, timeout

    def synthesize(self, text):
        """WAV bytes and duration. Raw PCM is requested and wrapped locally so the WAV header
        always states the true length."""
        params = {'model': self.voice, 'encoding': 'linear16', 'sample_rate': RATE, 'container': 'none'}
        if self.speed != 1.0:
            params['speed'] = self.speed
        query = urllib.parse.urlencode(params)
        pcm = post(f'{self.url}?{query}', self.api_key, json.dumps({'text': text}).encode(),
                   'application/json', self.timeout)
        if len(pcm) < 2:
            raise RuntimeError('Deepgram returned no audio')
        pcm = pcm[:len(pcm) // 2 * 2]
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(RATE)
            wav.writeframes(pcm)
        return output.getvalue(), len(pcm) / 2 / RATE
