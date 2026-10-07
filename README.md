# Lecture recordings to transcripts

Clip HKUST Canvas lectures and turn the audio into English transcripts locally.

[Install](#install) | [Clip](#clip-a-lecture) | [Transcribe](#transcribe) | [Models](#models-and-tested-hardware) | [Other commands](#other-commands)

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and
[FFmpeg](https://ffmpeg.org/download.html) first. Run these commands from the project folder:

```bash
uv venv --python 3.12 .venv
uv pip sync --python .venv/bin/python requirements.txt
uv pip install --python .venv/bin/python -e '.[browser]'
.venv/bin/python -m playwright install chromium
source .venv/bin/activate
```

In a new terminal, activate the environment again:

```bash
source .venv/bin/activate
```

## Clip a lecture

Change the start and end times to the section you want:

```bash
lecture-transcripts clip \
  --start 33:56 --end 1:02:11 \
  --out data/lecture.m4a
```

1. Sign in and complete MFA in the browser that opens.
2. Open the lecture recording. Press Play if needed.
3. Wait for capture. The browser closes and the audio downloads automatically.

To open a lecture page directly, add `--source-page 'YOUR_CANVAS_LECTURE_URL'`.
Times accept seconds, `MM:SS`, or `HH:MM:SS`. Press Ctrl+C to cancel.

## Transcribe

```bash
lecture-transcripts transcribe data/lecture.m4a --download-model
```

The first run downloads the model. Your audio stays on your computer.

Open the text transcript at `transcripts/small.en/lecture/lecture.txt`.
Subtitles and timestamped JSON are saved alongside it.

### Try a larger model

`small.en` is the default. Try `medium.en` when you want to compare recognition:

```bash
lecture-transcripts transcribe data/lecture.m4a \
  --model medium.en --download-model
```

Its text transcript is saved at `transcripts/medium.en/lecture/lecture.txt`.
Review equations and technical terms against the audio and slides.

## Models and tested hardware

Uses pretrained English [Whisper](https://github.com/openai/whisper) models ([ctranslate2](https://github.com/Softcatala/whisper-ctranslate2)) through
`faster-whisper`, running locally on CPU with `int8`. No GPU is required.

- `small.en`: default, faster option; tested on lecture audio.
- `medium.en`: larger alternative that may improve recognition; not yet benchmarked here.

**Tested example:** Fedora, AMD Ryzen AI 7 PRO 450 (8 cores,
16 threads), and about 27 GiB usable RAM. Using `small.en` with 8 CPU threads,
28:15 of audio took about **2:45** and peaked at **1.8 GiB process memory**.
Note that this is one measured example, not a minimum
hardware requirement. Speed and accuracy depend on the machine and recording.

## Other commands

### Clip a local file

```bash
lecture-transcripts clip --input data/lecture.m4a \
  --start 1:00 --end 5:00 --source-offset 33:56 \
  --out data/short-section.m4a
```

For an input that is already a clip, set `--source-offset` to its original lecture
start time. Omit it for a full recording.

### Use a saved media URL

```bash
lecture-transcripts clip \
  --source-url-file "$HOME/.cache/lecture-access-pilot/source-url.txt" \
  --start 33:56 --end 1:02:11 \
  --out data/from-url.m4a
```

Use your own file containing a current signed media URL. An expired URL needs
refreshing; browser mode avoids copying it manually.

### Show all options

```bash
lecture-transcripts clip --help
lecture-transcripts transcribe --help
```

If an output already exists, choose a new destination or add `--overwrite` to
replace it deliberately.

Only use recordings you are allowed to access. Do not share signed URLs, cookies,
or the helper browser's login profile.
