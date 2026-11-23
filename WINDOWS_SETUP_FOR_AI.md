# Task: set up and run Caption Studio on this Windows laptop

You are an AI assistant with a terminal on a Windows laptop. Follow these steps in order. Run every
command yourself and check its output before you move on. Report back to the user at the end
(section 10).

## 1. What this project is

The folder `AndroidRecorder` was copied here from a Mac. It contains a few tools. **Only one of them
should run on Windows: Caption Studio**, a local web page that:

1. takes a video file (.mp4 / .mov / .m4v),
2. turns its speech into captions on this computer (no cloud upload),
3. lets the user style the captions with a live preview,
4. exports a new video with the captions drawn on.

Files that matter for Caption Studio:

| File | Purpose |
|------|---------|
| `caption_studio.py` | Web server and page. **This is the program you start.** |
| `transcribe.py` | Speech-to-text, caption styles, fonts, video export |
| `caption_effects.py` | Letter and word animation effects |
| `effects.js` | Browser-side preview effects (served by `caption_studio.py`) |
| `vocabulary.txt` | Optional word list that helps speech-to-text |
| `windows_selftest.py` | Self-test with 23 checks that prints PASS/FAIL for each part |
| `README.md` | Short human instructions |

**Mac-only files. Do not run them and do not try to make them work:** `app.py`, `multi_record.py`
(Android screen recorder), `ios_app.py`, `ios_record.swift` (iPhone recorder).

The code already supports Windows. It swaps the Mac fonts for Windows fonts, saves text files as
UTF-8, finds the Python in `.venv\Scripts\python.exe`, and handles the port correctly. **You should
not need to change any code.** Only change code if the self-test in step 7 fails. Follow the rules
in section 9.

## 2. Rules

- Use **PowerShell**. Put quotes around every path, because paths may contain spaces.
- Run Python through the project environment: `.venv\Scripts\python ...`. You don't need to
  activate the environment, so PowerShell's script execution policy doesn't matter.
- **Do not** change `HOST = "127.0.0.1"` in `caption_studio.py`. The server must only be reachable
  from this laptop. Do not open firewall ports and do not bind to `0.0.0.0`.
- **Do not** delete anything inside `recordings\`. It holds the user's videos.
- Don't install anything that isn't listed here without asking the user first.

## 3. Copy the folder to a local disk

If the folder is on a network share (a path starting with `\\`, a mapped drive, or a Mac share),
copy it to a local folder first. Running it from a share is slow, and Windows file locking on shares
can break the video export.

```powershell
robocopy "<SHARED_FOLDER_PATH>" "C:\AndroidRecorder" /E /XD .venv __pycache__ .build
cd "C:\AndroidRecorder"
```

Replace `<SHARED_FOLDER_PATH>` with the real path. If the folder is already on a local disk, just
`cd` into it. The `recordings` folder is optional and can be large. Add `recordings` to `/XD` to skip
it, but then create an empty one with `mkdir recordings`.

From now on, run every command **inside this folder**.

## 4. Remove the Mac leftovers

The Mac's Python environment (`.venv`) does not work on Windows and has to be rebuilt.

```powershell
if (Test-Path ".venv") { Remove-Item -Recurse -Force ".venv" }
if (Test-Path "__pycache__") { Remove-Item -Recurse -Force "__pycache__" }
if (Test-Path ".build") { Remove-Item -Recurse -Force ".build" }
Get-ChildItem -Filter "._*" -Recurse -File | Remove-Item -Force   # macOS metadata files, if any
Get-ChildItem -Filter ".DS_Store" -Recurse -File | Remove-Item -Force
```

Keep the `.backup` folder. It holds older copies of the code.

## 5. Install Python, ffmpeg and the C++ runtime

### 5a. Python 3.12

The Mac uses Python 3.10. Versions 3.10, 3.11 and 3.12 are all fine. **Avoid 3.13 or newer**,
because some speech-to-text packages may not have Windows builds for it yet.

```powershell
py -0p          # lists the installed Python versions, if the "py" launcher exists
```

If 3.10, 3.11 or 3.12 is already installed, use it. Otherwise:

```powershell
winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
```

If `winget` doesn't exist, download the Windows installer for 3.12 from
https://www.python.org/downloads/windows/ and tick **"Add python.exe to PATH"** during setup.

### 5b. ffmpeg (includes ffprobe)

```powershell
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
```

If `winget` doesn't exist, download the "release full" build from https://www.gyan.dev/ffmpeg/builds/,
unzip it to `C:\ffmpeg`, and add `C:\ffmpeg\bin` to the user PATH:

```powershell
[Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path","User") + ";C:\ffmpeg\bin", "User")
```

### 5c. Microsoft Visual C++ runtime

The speech-to-text engine (ctranslate2) and onnxruntime need this. It's often already installed.

```powershell
winget install --id Microsoft.VCRedist.2015+.x64 -e --accept-source-agreements --accept-package-agreements
```

### 5d. Open a NEW PowerShell window

PATH changes only apply to new windows. Open a new window, `cd` back into the project folder, and
check:

```powershell
py -3.12 --version         # or: python --version
ffmpeg -version            # first line starts with "ffmpeg version"
ffprobe -version
ffmpeg -hide_banner -encoders | Select-String libx264   # must print a line with libx264
```

The export needs all four to work. If `libx264` is missing, install the Gyan "full" build of ffmpeg.

## 6. Create the project's Python environment

```powershell
py -3.12 -m venv .venv            # if there is no "py" launcher: python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install faster-whisper pillow fonttools numpy
```

Check that everything imports:

```powershell
.venv\Scripts\python -c "import faster_whisper, PIL, fontTools, numpy, ctranslate2; print('imports OK', PIL.__version__)"
```

If the install or the import fails, try the exact versions that work on the Mac:

```powershell
.venv\Scripts\python -m pip install "faster-whisper==1.2.1" "pillow==12.3.0" "fonttools==4.65.0" "numpy==2.2.6" "ctranslate2==4.9.0"
```

If you see `DLL load failed`, go back to step 5c and install the Visual C++ runtime.

## 7. Run the self-test

```powershell
.venv\Scripts\python windows_selftest.py
```

It takes about 1 to 5 minutes. The first run downloads a small speech model (about 75 MB) from
huggingface.co. It works in a temp folder and cleans up after itself.

**Success** means the last lines are:

```
23 passed, 0 failed
ALL GOOD - Caption Studio works on this machine.
```

The checks include `platform` (it must show `win32`), `ffmpeg / ffprobe on PATH`, the imports,
every style's font, the colour emoji, non-English subtitles, 11 kinds of export, speech-to-text,
and the web server. Warnings about `HF_TOKEN` or `unauthenticated requests` are harmless.

If any line says `FAIL`, use section 8. Run the self-test again after every fix.

## 8. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ffmpeg not found on PATH` | Do step 5b, then open a **new** PowerShell window. |
| `No module named ...` | Run step 6 again. Make sure you use `.venv\Scripts\python`, not plain `python`. |
| `DLL load failed` (ctranslate2 / onnxruntime / av) | Step 5c, then restart PowerShell. |
| `speech-to-text` fails with a network, SSL or proxy error | The model download from huggingface.co is blocked. Try another network, or set `$env:HTTPS_PROXY="http://proxy:port"` and run it again. The cache is in `%USERPROFILE%\.cache\huggingface`. |
| `every style's font file exists` fails | Windows is missing even the fallback fonts (Arial, Segoe UI). Tell the user which fonts are listed. Don't edit code to hide it. |
| `colour emoji` fails | `C:\Windows\Fonts\seguiemj.ttf` is missing or old. Check `Test-Path C:\Windows\Fonts\seguiemj.ttf`. Report it; it only affects the optional emoji feature. |
| Export fails with `Unknown encoder 'libx264'` | Install the Gyan "full" ffmpeg build (step 5b). |
| Export fails with `PermissionError` / `WinError 32` | Another program (antivirus, OneDrive, a video player) has the file open. Close it, or move the folder out of OneDrive, then try again. |
| Web server check fails with `WinError 10048` | Another program uses that port. Run the self-test again (it picks a free port each time). |

## 9. If you really must change code

Only change code if a FAIL can't be fixed by installing or configuring something.

1. Back up first: create the next free folder number under `.backup`, for example
   `.backup\v25`, and copy `transcribe.py`, `caption_studio.py` and `caption_effects.py` into it.
2. Keep the Mac working: put any Windows-only behaviour behind `if WINDOWS:` (this flag already
   exists in `transcribe.py` and `caption_studio.py`, and means `sys.platform == "win32"`).
3. Make the smallest change that fixes the FAIL. Don't refactor or reformat.
4. Never change `HOST = "127.0.0.1"`, the Host/Origin checks, or the upload size and type limits in
   `caption_studio.py`.
5. Run the self-test again until it says `ALL GOOD`, and tell the user exactly what you changed and why.

## 10. Start Caption Studio for the user

```powershell
.venv\Scripts\python caption_studio.py
```

Expected output: `Caption Studio running at http://127.0.0.1:8765  (this PC only; Ctrl+C to stop)`.
Then open http://127.0.0.1:8765 in the browser (it doesn't open by itself).

- Leave the PowerShell window open while using the studio. **Ctrl+C** stops it.
- If port 8765 is taken, use another port:
  ```powershell
  $env:CAPTION_STUDIO_PORT = "8766"; .venv\Scripts\python caption_studio.py
  ```
- If Windows Firewall asks for network access, click **Cancel** / don't allow. The studio only uses
  this laptop (127.0.0.1) and doesn't need the network.
- **The first real transcription downloads the full speech model ("large-v3-turbo", about 1.6 GB).**
  It happens once and can take several minutes. The page shows progress. Later runs are fast.
- Uploaded videos and exports are stored in `recordings\uploads\`.

Optionally, create a desktop shortcut so the user can start it with a double-click:

```powershell
$folder = (Get-Location).Path
$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut("$env:USERPROFILE\Desktop\Caption Studio.lnk")
$lnk.TargetPath = "$folder\.venv\Scripts\python.exe"
$lnk.Arguments = "`"$folder\caption_studio.py`""
$lnk.WorkingDirectory = $folder
$lnk.Save()
```

## 11. Report back to the user

Tell the user:

1. the folder it runs from, the Python version and the ffmpeg version,
2. the self-test summary line (`N passed, M failed`) and any FAIL lines,
3. any code changes you made (file, what changed, why) and the backup folder name, or "no code changes",
4. how to start it (command or desktop shortcut) and the address http://127.0.0.1:8765,
5. that the first transcription downloads about 1.6 GB once.

## Known, expected differences from the Mac

- Styles that use Mac fonts (Avenir Next, Futura, Menlo, Marker Felt, Chalkduster, Bodoni, SF)
  are drawn with the closest Windows font (Segoe UI, Century Gothic, Consolas, Ink Free,
  Segoe Print, Georgia ...). They look close, not identical. This is expected, not a bug.
- If the user re-exports while the browser is still playing the previous export, Windows may refuse
  to overwrite the file. Close the preview and export again.
