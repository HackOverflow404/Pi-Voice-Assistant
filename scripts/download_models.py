"""Download official small STT / low TTS models; never overwrite a custom wake model."""
from pathlib import Path
import urllib.request
import zipfile
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
    name = 'vosk-model-small-en-us-0.15'
    if not (MODELS / name).exists():
        archive = MODELS / (name + '.zip')
        download(f'https://alphacephei.com/vosk/models/{name}.zip', archive)
        staging = MODELS / '.vosk-extract'
        staging.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                resolved = (staging / member.filename).resolve()
                if not resolved.is_relative_to(staging.resolve()):
                    raise ValueError('Unsafe model archive path')
            source.extractall(staging)
        (staging / name).rename(MODELS / name)
        staging.rmdir()
        archive.unlink()
    base = 'https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/'
    for name in ('en_US-amy-low.onnx', 'en_US-amy-low.onnx.json', 'MODEL_CARD'):
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
