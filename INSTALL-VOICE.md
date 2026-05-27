# Voice Pipeline Install (agent B03)

Target: Windows 11 (acer host). Linux/macOS notes inline.

## 1. Python environment

```powershell
py -3.11 -m venv D:\interview\copilot-v2-2026-05-27\.venv
D:\interview\copilot-v2-2026-05-27\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r D:\interview\copilot-v2-2026-05-27\requirements.txt
```

Python 3.11 recommended (pywhispercpp wheels). 3.12 works for everything except
some pywhispercpp builds — fall back to 3.11 if the wheel doesn't resolve.

## 2. whisper.cpp native library

`pywhispercpp` on Linux/macOS bundles the native `libwhisper`. On **Windows**
you need `whisper.dll` on PATH (or alongside the Python install).

Option A (recommended, vendored):
1. Download a release zip from https://github.com/ggerganov/whisper.cpp/releases
   (pick `whisper-bin-x64.zip` for AVX2/AVX-512 CPUs).
2. Extract `whisper.dll` into `D:\interview\copilot-v2-2026-05-27\bin\`.
3. Add that dir to `PATH` for the venv: in `.venv\Scripts\activate.bat`
   append `set PATH=D:\interview\copilot-v2-2026-05-27\bin;%PATH%`.

Option B (build from source — only if you want CUDA/Vulkan):
1. Clone `https://github.com/ggerganov/whisper.cpp`.
2. `cmake -B build -DWHISPER_CUBLAS=ON` (or `-DWHISPER_VULKAN=ON`).
3. `cmake --build build --config Release`.
4. Copy `build\bin\Release\whisper.dll` to the same `bin\` dir.

## 3. Whisper model files

```powershell
mkdir D:\interview\copilot-v2-2026-05-27\models\whisper
# base.en — recommended default, 142 MB
curl -L -o D:\interview\copilot-v2-2026-05-27\models\whisper\ggml-base.en.bin `
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin

# small.en — slower but more accurate, 466 MB (optional)
curl -L -o D:\interview\copilot-v2-2026-05-27\models\whisper\ggml-small.en.bin `
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin

# base multilingual — for PT-EN mixed interviews, 142 MB (optional)
curl -L -o D:\interview\copilot-v2-2026-05-27\models\whisper\ggml-base.bin `
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin
```

Configure `STTConfig.whisper_model_path` to the absolute `.bin` path. If you
leave `whisper_model_path=None`, pywhispercpp downloads on first use into its
own cache — fine for dev, but vendor the file for offline runs.

## 4. Audio device check

Run once to confirm WASAPI loopback is reachable:

```powershell
python -c "import pyaudiowpatch; pa = pyaudiowpatch.PyAudio(); print(pa.get_default_wasapi_loopback())"
```

You should see a dict with the default speakers' loopback device. If you get
"no loopback found", confirm Windows has audio output enabled (a real speaker
or virtual cable selected as the default playback device).

## 5. Optional: Google Cloud fallback

Only needed if you want cloud STT as a backup engine.

1. Create a GCP service account with role `Cloud Speech Client`.
2. Download the JSON key to e.g. `D:\interview\creds\gcp-stt.json`.
3. Either set `GOOGLE_APPLICATION_CREDENTIALS=D:\interview\creds\gcp-stt.json`
   in your shell, or pass `STTConfig(google_credentials_json="...")`.

Privacy gate: interviewer audio (`StreamTag.SYSTEM`) is **refused** by the
cloud engine unless `STTConfig.allow_cloud_for_system_audio=True`. Default is
local-only.

## 6. Optional: silero-vad

```powershell
mkdir D:\interview\copilot-v2-2026-05-27\models\vad
curl -L -o D:\interview\copilot-v2-2026-05-27\models\vad\silero_vad.onnx `
  https://github.com/snakers4/silero-vad/raw/master/files/silero_vad.onnx
pip install onnxruntime
```

Then set `CaptureConfig(vad_backend="silero")`.

## 7. Run the unit tests

```powershell
cd D:\interview\copilot-v2-2026-05-27
python -m unittest discover -s tests -v
```

No real audio device or whisper model is touched — all hardware and STT
clients are mocked. Tests should pass on any Windows / Linux / macOS host with
Python 3.11+ and `pip install webrtcvad-wheels numpy` (the only test-time
imports).
