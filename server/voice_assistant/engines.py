import io
import logging
import wave

LOG = logging.getLogger('voice')


class Engines:
    def __init__(self, config, tts=None, stt=None):
        import numpy as np
        from openwakeword.model import Model as WakeModel
        from faster_whisper import WhisperModel
        from piper import PiperVoice
        self.np = np
        self.wake = WakeModel(wakeword_models=[config['wake']], inference_framework='onnx')
        # Whisper tiny.en transcribed the Echo's quiet, reverberant recordings correctly where
        # Vosk small did not, without inventing text from silence (unlike base.en), and fits
        # the Pi's memory.
        self.whisper = WhisperModel(config['whisper'], device='cpu', compute_type='int8', cpu_threads=4)
        self.piper = PiperVoice.load(config['piper'])  # also the fallback when the cloud voice fails
        self.cloud = self.cloud_stt = None
        tts, stt = tts or {}, stt or {}
        if tts.get('engine') == 'deepgram':
            from .deepgram import DeepgramVoice
            self.cloud = DeepgramVoice(tts['deepgram_api_key'], tts['deepgram_voice'])
        if stt.get('engine') == 'deepgram':
            from .deepgram import DeepgramListener
            self.cloud_stt = DeepgramListener(tts['deepgram_api_key'], stt['deepgram_model'])

    def predict(self, frame):
        return max(self.wake.predict(self.np.frombuffer(frame, dtype='<i2')).values(), default=0)

    def reset(self):
        self.wake.reset()

    def transcribe(self, pcm):
        if self.cloud_stt:
            try:
                return self.cloud_stt.transcribe(pcm)
            except Exception as exc:  # no internet, bad key, quota: transcribe locally instead
                LOG.warning('Cloud transcription failed (%s); using Whisper', exc)
        audio = self.np.frombuffer(pcm, dtype='<i2').astype(self.np.float32) / 32768
        segments, _ = self.whisper.transcribe(audio, language='en', beam_size=1, vad_filter=False,
                                              condition_on_previous_text=False, without_timestamps=True)
        return ' '.join(s.text.strip() for s in segments).strip()

    def synthesize(self, text):
        if self.cloud:
            try:
                return self.cloud.synthesize(text)
            except Exception as exc:  # no internet, bad key, quota: speak locally instead
                LOG.warning('Cloud voice failed (%s); using Piper', exc)
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            self.piper.synthesize_wav(text, wav)
        data = output.getvalue()
        with wave.open(io.BytesIO(data), 'rb') as wav:
            duration = wav.getnframes() / wav.getframerate()
        return data, duration
