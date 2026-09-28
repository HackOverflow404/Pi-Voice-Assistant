"""Deepgram Aura-2 text to speech. Natural-sounding voices at no CPU cost on the Pi; the
reply text is sent to Deepgram. Local Piper voices this natural run 5-13x slower than real
time on a Pi 4."""
import io
import json
import urllib.error
import urllib.parse
import urllib.request
import wave

API = 'https://api.deepgram.com/v1/speak'
RATE = 24000


class DeepgramVoice:
    def __init__(self, api_key, voice, url=API, timeout=15):
        self.api_key, self.voice, self.url, self.timeout = api_key, voice, url, timeout

    def synthesize(self, text):
        """WAV bytes and duration. Raw PCM is requested and wrapped locally so the WAV header
        always states the true length."""
        query = urllib.parse.urlencode({'model': self.voice, 'encoding': 'linear16',
                                        'sample_rate': RATE, 'container': 'none'})
        request = urllib.request.Request(f'{self.url}?{query}', data=json.dumps({'text': text}).encode(),
                                         headers={'Authorization': f'Token {self.api_key}',
                                                  'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                pcm = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors='replace').strip()[:200]
            raise RuntimeError(f'Deepgram {exc.code}: {detail}') from exc
        if len(pcm) < 2:
            raise RuntimeError('Deepgram returned no audio')
        pcm = pcm[:len(pcm) // 2 * 2]
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(RATE)
            wav.writeframes(pcm)
        return output.getvalue(), len(pcm) / 2 / RATE
