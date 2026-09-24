import io
import json
import wave


class Engines:
    def __init__(self, config):
        import numpy as np
        from openwakeword.model import Model as WakeModel
        from vosk import Model as VoskModel
        from piper import PiperVoice
        self.np = np
        self.wake = WakeModel(wakeword_models=[config['wake']], inference_framework='onnx')
        self.vosk = VoskModel(config['vosk'])
        self.piper = PiperVoice.load(config['piper'])

    def predict(self, frame):
        return max(self.wake.predict(self.np.frombuffer(frame, dtype='<i2')).values(), default=0)

    def reset(self):
        self.wake.reset()

    def transcribe(self, pcm):
        from vosk import KaldiRecognizer
        recognizer = KaldiRecognizer(self.vosk, 16000)
        parts = []
        for offset in range(0, len(pcm), 8000):
            if recognizer.AcceptWaveform(pcm[offset:offset + 8000]):
                parts.append(json.loads(recognizer.Result()).get('text', ''))
        parts.append(json.loads(recognizer.FinalResult()).get('text', ''))
        return ' '.join(x for x in parts if x).strip()

    def synthesize(self, text):
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            self.piper.synthesize_wav(text, wav)
        data = output.getvalue()
        with wave.open(io.BytesIO(data), 'rb') as wav:
            duration = wav.getnframes() / wav.getframerate()
        return data, duration
