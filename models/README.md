Supply an openWakeWord-compatible custom classifier named `custom.onnx` here.
An arbitrary ONNX model will not work. Train/export with openWakeWord 0.6-compatible
feature embeddings; tune `audio.wake_threshold` on the actual Echo microphone.

`setup.sh` downloads Vosk `vosk-model-small-en-us-0.15` and Piper `en_US-amy-low`
(ONNX + JSON + model card). It also downloads openWakeWord's shared feature models
into the project's virtual environment. The custom wake model is not generated
or downloaded because no wake phrase / trained model was supplied.

Models are excluded from version control. Preserve the downloaded Piper model
card and review each model's license before redistribution.
