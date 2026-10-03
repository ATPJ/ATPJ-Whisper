# ATPJ Whisper

Offline **hold-to-talk dictation for Windows**. Hold a key, speak, release — the text is typed into whatever app has focus. Speech recognition runs locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper); nothing is sent to any server.

**A tool tuned for the Persian (Farsi) language.** Out of the box it is set up for Persian speech with English words mixed in (e.g. *«این پروژه رو روی GitHub آپلود کردم»*): English words stay in English, Persian stays in Persian, and a Persian-aware prompt, term list and correction dictionary improve accuracy. Other languages work too — set `language` in the config — but the special tuning is for Persian.

> Inspired by tools like Wispr Flow. Not affiliated with them or with OpenAI.

## Features

- **Hold-to-talk or toggle** with a global hotkey (default `F9`); `Esc` cancels. A small floating pill shows *Recording / Transcribing / Pasted* without stealing focus.
- **Pastes into any app** (clipboard + Ctrl+V, so Persian text is never mangled).
- **Persian + English in one sentence.** A mixed-language prompt, a built-in list of common tech terms, and your own dictionary keep English words in Latin letters.
- **Learns from your fixes.** Edit a transcript and press *Save edit*: single-word corrections (typos, `پایتون` → `Python`) are remembered and applied next time. Unknown words are snapped to the nearest word in your dictionary.
- **History** with search, audio playback, copy, edit, delete. Right-to-left aware editor.
- **Analytics** tab: words per day, speaking speed (words/min), time saved vs typing, day streak, and how often you did *not* need to correct a transcript.
- **Lives in the tray**, can **start with Windows**, only one copy runs at a time.
- **Frees the GPU when idle.** The model runs in its own process that is closed after a few idle minutes (100 % of its VRAM is released) and restarted the next time you talk.

## Requirements

- Windows 10/11 (uses Windows-only APIs: global key hooks, the Run registry key, `winsound`)
- A microphone
- Python 3.13 via [uv](https://docs.astral.sh/uv/) (uv installs the right Python for you)
- Recommended: an NVIDIA GPU with CUDA 12 + cuDNN 9 (see the [faster-whisper GPU notes](https://github.com/SYSTRAN/faster-whisper#gpu)). The default model uses about 1.6 GB of VRAM (measured on an RTX 3060). Without a usable GPU it falls back to the CPU, which works but is much slower.
- ~3 GB of disk space for the `large-v3` model (downloaded once)

## Install & run

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (one time). In PowerShell: `winget install --id=astral-sh.uv -e`
2. Download and start the app:

```
git clone https://github.com/ATPJ/ATPJ-Whisper.git
cd ATPJ-Whisper
uv sync
uv run app.py
```

`uv sync` creates the virtual environment and installs everything (including Python 3.13), so no manual setup is needed.

The first start downloads the model from Hugging Face; after that everything works **fully offline**. If you need a proxy for the download, put it in `config.json` (`"proxy": "http://127.0.0.1:8080"`).

Run the window-less background version (what *Start with Windows* uses): `.venv\Scripts\pythonw.exe app.py --hidden`.

## Usage

1. Wait until the status bar says **Ready** (the first start downloads the model, which can take a few minutes).
2. Click into any app, hold **F9**, speak, release.
3. The text is pasted there and saved to the **History** tab.
4. Closing the window keeps the app running in the tray. Right-click the tray icon for **Show**, **Start with Windows**, **Unload model now** and **Quit**. (Windows may hide new tray icons under the `^` arrow — drag it out to keep it visible.)

**Fixing mistakes:** select a recording in History, edit the text, press **Save edit** (or Ctrl+S). Use **Add a word Whisper should know** for names and jargon.

## Configuration

`config.json` is created next to the code on first run (and is git-ignored). Restart the app after changing it.

| Key | Default | Meaning |
|---|---|---|
| `hotkey` | `"f9"` | A single key name from the [`keyboard`](https://github.com/boppreh/keyboard) library. Key combinations are not supported. |
| `mode` | `"hold"` | `"hold"` = talk while pressed, `"toggle"` = press to start, press to stop |
| `language` | `"fa"` | Whisper language code (`"en"`, `"de"`, …) or `null` to auto-detect. The Persian prompt is only used for `"fa"`. |
| `model` | `"large-v3"` | Any faster-whisper model: `large-v3` (most accurate), `large-v3-turbo` (faster, less accurate in Persian), `medium`, `small`, … |
| `fuzzy` | `0.8` | Nearest-word cutoff (0–1). Lower = snaps more words to your dictionary, higher = fewer false corrections. |
| `restore_clipboard` | `false` | Restore your previous clipboard after pasting. Off by default so the transcript stays on the clipboard if auto-paste fails. |
| `idle_unload_minutes` | `5` | Close the model process after this many idle minutes. `0` = never. |
| `typing_wpm` | `40` | Your typing speed, used for the *time saved* estimate. |
| `proxy` | `""` | HTTP(S) proxy used only to download the model. |

## Privacy

- Audio and text never leave your machine. The only network use is downloading the model from Hugging Face.
- Your data lives next to the code and is git-ignored: `history.db` (transcripts, dictionary), `recordings/` (WAV files), `config.json`, `app.log`. Deleting a recording in the app also deletes its WAV file. To wipe everything, delete those files.
- Windows requires a global keyboard hook to detect a hotkey while another app is focused. The app only reacts to your hotkey and `Esc`; it never stores other keystrokes (see `on_press`/`on_release` in `app.py`).

## How it works

```
hotkey ──▶ app.py (PySide6 UI, tray, recording) ──audio──▶ worker.py (separate process: faster-whisper model)
                │                                            │ raw text
                ▼                                            ▼
        history.db  ◀── core.py (corrections, dictionary, stats) ◀──┘
                │
                └──▶ clipboard + Ctrl+V into the focused app
```

- `app.py` — UI, hotkey, recording, paste, tray, autostart
- `worker.py` — runs the model in its own process; killing it frees all GPU memory
- `core.py` — config, SQLite (history, dictionary), word corrections, transcription, analytics
- Self-check for the logic (corrections, learning, analytics): `uv run core.py --test`

## Troubleshooting

- **Hotkey or paste doesn't work in some apps** — apps running as administrator ignore input from a normal process. Run the terminal (or the app) as administrator.
- **CUDA / cuDNN errors, or it is slow** — it silently fell back to the CPU. Install CUDA 12 and cuDNN 9 as described in the faster-whisper docs, or use a smaller `model`.
- **English words still come out in Persian letters** — fix them once in History and save; they are remembered. You can also add the English word with *Add a word*.
- **Nothing happens at startup** — check `app.log` next to the code (tray mode has no console).
- **Model download fails** — set `proxy` in `config.json`, or download the model once on another network.

## Limitations

- Windows only. Text is inserted with the clipboard + Ctrl+V, so apps that block paste won't receive it (it stays on the clipboard).
- No live streaming: text appears after you release the key.
- The built-in prompt and term list target Persian/English; other language pairs work but get no special handling.

## Credits & license

Built on [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT) and OpenAI's Whisper models (MIT), with [PySide6](https://doc.qt.io/qtforpython-6/) (Qt for Python, LGPL) for the UI.

Licensed under the **MIT License** (see `LICENSE`).

---

## خلاصه به فارسی

برنامه‌ی دیکته‌ی صوتی **کاملاً آفلاین** برای ویندوز که **مخصوص زبان فارسی تنظیم و بهینه شده است**: کلید F9 را نگه دار، حرف بزن، رها کن؛ متن در هر برنامه‌ای که باز است نوشته می‌شود. کلمات انگلیسی لابه‌لای فارسی به همان انگلیسی نوشته می‌شوند. تاریخچه با پخش صدا، ویرایش و حذف، بخش آمار (سرعت، زمان صرفه‌جویی‌شده)، اجرا در ترای و شروع خودکار با ویندوز دارد. وقتی چند دقیقه استفاده نشود، مدل به‌طور کامل از کارت گرافیک خارج می‌شود. هیچ داده‌ای از سیستم تو بیرون نمی‌رود.

نصب: ابتدا uv را نصب کن (`winget install --id=astral-sh.uv -e`)، سپس `git clone https://github.com/ATPJ/ATPJ-Whisper.git`، وارد پوشه شو و `uv sync` و بعد `uv run app.py` (بار اول مدل حدود ۳ گیگابایت دانلود می‌شود).
