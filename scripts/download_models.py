"""Download Whisper tiny.en STT / Piper medium TTS models; never overwrite a custom wake model."""
from pathlib import Path
import urllib.request
import shutil

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / 'models'


def download(url, target):
    if target.exists():
        return
    print(f'Downloading {target.name}', flush=True)
    temporary = target.with_suffix(target.suffix + '.part')
    try:
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open('wb') as output:
            shutil.copyfileobj(response, output)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    MODELS.mkdir(exist_ok=True)
    whisper = MODELS / 'whisper-tiny.en'
    if not (whisper / 'model.bin').exists():
        print('Downloading Whisper tiny.en', flush=True)
        from faster_whisper import download_model
        download_model('tiny.en', output_dir=str(whisper))
    base = 'https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/'
    for name in ('en_US-lessac-medium.onnx', 'en_US-lessac-medium.onnx.json', 'MODEL_CARD'):
        download(base + name, MODELS / name)
    # Includes the shared melspectrogram / embedding ONNX feature models required
    # by a custom classifier. These live inside this runtime's openwakeword package.
    import openwakeword
    features = Path(openwakeword.__file__).parent / 'resources' / 'models'
    features.mkdir(parents=True, exist_ok=True)
    for model in openwakeword.FEATURE_MODELS.values():
        url = model['download_url'].replace('.tflite', '.onnx')
        download(url, features / url.rsplit('/', 1)[-1])


if __name__ == '__main__':
    main()
