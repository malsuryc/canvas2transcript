import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class CliError(Exception):
    pass


def parse_time(value: str) -> float:
    parts = value.strip().split(":")
    message = "Use nonnegative seconds, MM:SS, or HH:MM:SS."
    if not 1 <= len(parts) <= 3:
        raise argparse.ArgumentTypeError(message)
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", parts[-1]):
        raise argparse.ArgumentTypeError(message)
    if any(not re.fullmatch(r"[0-9]+", part) for part in parts[:-1]):
        raise argparse.ArgumentTypeError(message)
    seconds = float(parts[-1])
    if len(parts) > 1 and seconds >= 60:
        raise argparse.ArgumentTypeError(
            "Seconds must be less than 60 in a clock time."
        )
    if len(parts) == 3 and int(parts[1]) >= 60:
        raise argparse.ArgumentTypeError("Minutes must be less than 60 in HH:MM:SS.")
    for index, part in enumerate(reversed(parts[:-1]), start=1):
        seconds += int(part) * 60**index
    if not math.isfinite(seconds):
        raise argparse.ArgumentTypeError(message)
    return seconds


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CliError("Could not read a valid JSON metadata file.") from None
    if not isinstance(value, dict):
        raise CliError("JSON metadata must be an object.")
    return value


def write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def audio_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as audio:
        while block := audio.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def run_command(
    arguments: list[str], failure_message: str, environment: dict | None = None
) -> str:
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, check=False, env=environment
        )
    except FileNotFoundError:
        raise CliError(f"Required tool not found: {Path(arguments[0]).name}.") from None
    except OSError:
        raise CliError(failure_message) from None
    if result.returncode:
        raise CliError(failure_message)
    return result.stdout


def probe_audio(path: Path) -> dict:
    output = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_name,codec_type,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        "Could not inspect the audio with ffprobe.",
    )
    try:
        media = json.loads(output)
        duration = float(media["format"]["duration"])
        has_audio = any(stream["codec_type"] == "audio" for stream in media["streams"])
    except (ValueError, KeyError, TypeError):
        raise CliError("ffprobe did not return usable audio metadata.") from None
    if not has_audio or not math.isfinite(duration) or duration <= 0:
        raise CliError("The input must contain audio with a known positive duration.")
    return media


def check_outputs(paths: list[Path], overwrite: bool) -> None:
    if any(path.is_dir() for path in paths):
        raise CliError("An output path is a directory.")
    if not overwrite and any(path.exists() or path.is_symlink() for path in paths):
        raise CliError(
            "Output already exists. Choose another destination or use --overwrite."
        )


def publish_files(pairs: list[tuple[Path, Path]], overwrite: bool) -> None:
    check_outputs([target for _, target in pairs], overwrite)
    created = []
    try:
        for staged, target in pairs:
            if overwrite:
                os.replace(staged, target)
            else:
                os.link(staged, target)
                created.append(target)
    except OSError:
        for target in created:
            target.unlink(missing_ok=True)
        raise CliError(
            "Could not save outputs; no existing file was implicitly overwritten."
        ) from None


def validate_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        )
        parsed.port
    except ValueError:
        valid = False
    if not valid or any(character.isspace() for character in value):
        raise CliError(
            "Source URLs must be HTTPS URLs without embedded login credentials."
        )
    return value


def clean_source_page(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CliError("The source page must be a stable HTTPS page URL.")
    parsed = urlsplit(validate_url(value))
    if "_tk" in parsed.path or parsed.path.lower().endswith((".m3u8", ".mpd")):
        raise CliError(
            "Use a stable lecture page for --source-page, not a signed media URL."
        )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def clip_audio(options: argparse.Namespace) -> None:
    if options.end <= options.start:
        raise CliError("--end must be later than --start.")
    output = options.out.resolve()
    if output.suffix.lower() not in {".m4a", ".wav"}:
        raise CliError(
            "Choose an .m4a output for AAC stream copy, or .wav for conversion."
        )
    metadata_path = output.with_suffix(".metadata.json")
    check_outputs([output, metadata_path], options.overwrite)
    if options.source_url_file:
        try:
            source = validate_url(
                options.source_url_file.read_text(encoding="utf-8").strip()
            )
        except (OSError, UnicodeError):
            raise CliError("Could not read --source-url-file.") from None
        source_identity = {"kind": "remote", "host": urlsplit(source).hostname}
    else:
        if not options.input.is_file():
            raise CliError("--input must be an existing local media file.")
        source = str(options.input.resolve())
        if output == options.input.resolve():
            raise CliError("Input and output must be different files.")
        source_identity = {"kind": "local", "filename": options.input.name}
    source_page = clean_source_page(options.source_page)
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(
        prefix=".lecture-clip-", dir=output.parent
    ) as directory:
        staged = Path(directory) / output.name
        codec = (
            ["-c:a", "copy"]
            if output.suffix.lower() == ".m4a"
            else [
                "-c:a",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
            ]
        )
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-n",
                "-rw_timeout",
                "20000000",
                "-ss",
                str(options.start),
                "-i",
                source,
                "-t",
                str(options.end - options.start),
                "-map",
                "0:a:0",
                "-vn",
                *codec,
                str(staged),
            ],
            "Audio extraction failed. Refresh expired signed URLs; try WAV if the source audio is not AAC.",
        )
        media = probe_audio(staged)
        duration = float(media["format"]["duration"])
        if abs(duration - (options.end - options.start)) > 0.25:
            raise CliError(
                "Clip duration did not match the requested range. No completed clip was saved."
            )
        staged_metadata = Path(directory) / metadata_path.name
        write_json(
            staged_metadata,
            {
                "source": source_identity,
                "source_page": source_page,
                "input_start_seconds": options.start,
                "input_end_seconds": options.end,
                "source_start_seconds": options.source_offset + options.start,
                "source_end_seconds": options.source_offset + options.end,
                "audio_file": output.name,
                "audio_sha256": audio_hash(staged),
                "audio": media,
                "clip_wall_seconds": round(time.perf_counter() - started, 3),
                "timestamp_note": "Requested source offsets; AAC stream-copy boundaries are approximate.",
            },
        )
        publish_files(
            [(staged, output), (staged_metadata, metadata_path)], options.overwrite
        )
    print(f"Saved {output} ({duration:.3f} seconds)")
    print(f"Metadata: {metadata_path}")


def cached_model(model: str, allow_download: bool) -> tuple[Path, float]:
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        raise CliError(
            "Install the project's ASR dependencies before transcription."
        ) from None
    repository = f"Systran/faster-whisper-{model}"
    try:
        return Path(
            snapshot_download(repository, local_files_only=True, token=False)
        ), 0.0
    except Exception:
        if not allow_download:
            raise CliError(
                f"{model} is not completely cached. Re-run with --download-model to allow its pretrained weights to download."
            ) from None
    print(
        f"Downloading public pretrained {model} weights from Hugging Face. Audio stays local; no fine-tuning.",
        flush=True,
    )
    started = time.perf_counter()
    try:
        model_path = Path(snapshot_download(repository, token=False))
    except Exception:
        raise CliError(
            "Model download failed. Check connectivity and whether HF_HUB_OFFLINE is enabled."
        ) from None
    return model_path, time.perf_counter() - started


def source_metadata(
    options: argparse.Namespace, audio_path: Path, digest: str
) -> tuple[dict, float | None]:
    metadata_path = options.metadata
    if metadata_path is None:
        candidate = audio_path.with_suffix(".metadata.json")
        metadata_path = candidate if candidate.exists() else None
    metadata = read_json(metadata_path) if metadata_path is not None else {}
    if metadata.get("audio_sha256") and metadata["audio_sha256"] != digest:
        raise CliError("Metadata audio hash does not match this input.")
    if metadata.get("audio_file"):
        if (
            not isinstance(metadata["audio_file"], str)
            or Path(metadata["audio_file"]).name != audio_path.name
        ):
            raise CliError("Metadata belongs to a different or invalid audio filename.")
    offset = options.source_offset
    if offset is None:
        offset = metadata.get(
            "source_start_seconds", metadata.get("requested_source_start_seconds")
        )
    if offset is not None:
        try:
            offset = float(offset)
        except (TypeError, ValueError):
            raise CliError(
                "Metadata source offset must be a nonnegative number."
            ) from None
        if not math.isfinite(offset) or offset < 0:
            raise CliError("Metadata source offset must be a nonnegative number.")
    return {
        "audio_file": audio_path.name,
        "audio_sha256": digest,
        "source_page": clean_source_page(metadata.get("source_page")),
        "source_offset_seconds": offset,
    }, offset


def validate_transcript(transcript: dict, duration: float) -> float:
    segments = transcript.get("segments")
    if (
        not isinstance(transcript.get("text"), str)
        or not transcript["text"].strip()
        or not isinstance(segments, list)
        or not segments
    ):
        raise CliError(
            "No usable speech transcript was produced; no completed exports were saved."
        )
    previous_start = -1.0
    for segment in segments:
        try:
            start, end = float(segment["start"]), float(segment["end"])
            valid = (
                isinstance(segment["text"], str)
                and math.isfinite(start)
                and math.isfinite(end)
            )
        except (KeyError, TypeError, ValueError):
            raise CliError(
                "ASR returned malformed segment timestamps; no completed exports were saved."
            ) from None
        if (
            not valid
            or start < previous_start
            or start < 0
            or start > duration
            or end < start
            or end > duration + 1.0
        ):
            raise CliError(
                "ASR returned invalid or out-of-range segment timestamps; no completed exports were saved."
            )
        previous_start = start
    return max(0.0, max(float(segment["end"]) for segment in segments) - duration)


def transcribe_audio(options: argparse.Namespace) -> None:
    audio_path = options.audio.resolve()
    if not audio_path.is_file():
        raise CliError("The audio input must be an existing local file.")
    output_dir = (
        options.output_dir or Path("transcripts") / options.model / audio_path.stem
    ).resolve()
    names = [
        f"{audio_path.stem}.{extension}"
        for extension in (
            "txt",
            "json",
            "vtt",
            "srt",
            "tsv",
            "source.json",
            "benchmark.json",
        )
    ]
    check_outputs([output_dir / name for name in names], options.overwrite)
    media = probe_audio(audio_path)
    duration = float(media["format"]["duration"])
    source, offset = source_metadata(options, audio_path, audio_hash(audio_path))
    model_path, download_seconds = cached_model(options.model, options.download_model)
    executable_name = (
        "whisper-ctranslate2.exe" if os.name == "nt" else "whisper-ctranslate2"
    )
    backend = Path(sys.executable).parent / executable_name
    if not backend.is_file():
        raise CliError(
            "The ASR CLI was not found in this Python environment. Reinstall the project."
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(
        os.environ,
        HF_HUB_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
    )
    print(
        f"Transcribing with {options.model}, CPU int8, {options.threads} threads; inference is offline.",
        flush=True,
    )
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(
        prefix=".lecture-asr-", dir=output_dir
    ) as directory:
        staging = Path(directory)
        run_command(
            [
                str(backend),
                str(audio_path),
                "--model",
                options.model,
                "--model_directory",
                str(model_path),
                "--device",
                "cpu",
                "--compute_type",
                "int8",
                "--threads",
                str(options.threads),
                "--language",
                "en",
                "--task",
                "transcribe",
                "--beam_size",
                "5",
                "--vad_filter",
                str(options.vad),
                "--batched",
                "False",
                "--verbose",
                "False",
                "--local_files_only",
                "True",
                "--output_format",
                "all",
                "--pretty_json",
                "True",
                "--output_dir",
                str(staging),
            ],
            "Transcription failed. Check available memory, audio validity, and the installed dependency versions.",
            environment=environment,
        )
        wall_seconds = time.perf_counter() - started
        if any(
            not (staging / name).is_file() or (staging / name).stat().st_size == 0
            for name in names[:5]
        ):
            raise CliError(
                "ASR did not produce all expected nonempty exports; no completed output was saved."
            )
        transcript = read_json(staging / f"{audio_path.stem}.json")
        timestamp_padding = validate_transcript(transcript, duration)
        segments = [dict(segment) for segment in transcript["segments"]]
        if offset is not None:
            for segment in segments:
                segment["source_start"] = float(segment["start"]) + offset
                segment["source_end"] = min(float(segment["end"]), duration) + offset
        write_json(
            staging / f"{audio_path.stem}.source.json",
            {
                **transcript,
                "source": source,
                "segments": segments,
                "timestamp_basis": "start/end preserve approximate raw audio-relative times; source_start/source_end are original-source seconds when known, with source_end clamped to the audio boundary.",
            },
        )
        versions = {}
        for package in (
            "lecture-vids-to-transcripts",
            "whisper-ctranslate2",
            "faster-whisper",
            "ctranslate2",
            "av",
        ):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = None
        write_json(
            staging / f"{audio_path.stem}.benchmark.json",
            {
                "source": source,
                "audio": media,
                "model": {
                    "name": options.model,
                    "repository": f"Systran/faster-whisper-{options.model}",
                    "revision": model_path.name,
                    "fine_tuned": False,
                },
                "settings": {
                    "device": "cpu",
                    "compute_type": "int8",
                    "threads": options.threads,
                    "language": "en",
                    "beam_size": 5,
                    "vad": options.vad,
                    "batched": False,
                },
                "versions": versions,
                "runtime": {
                    "wall_seconds": round(wall_seconds, 3),
                    "real_time_factor": wall_seconds / duration,
                    "audio_seconds_per_wall_second": duration / wall_seconds,
                },
                "model_download_wall_seconds": round(download_seconds, 3),
                "result": {
                    "word_count": len(
                        (staging / f"{audio_path.stem}.txt")
                        .read_text(encoding="utf-8")
                        .split()
                    ),
                    "segment_count": len(segments),
                },
                "validation": {
                    "nonempty_exports": True,
                    "ordered_plausible_timestamps": True,
                    "allowed_end_padding_seconds": 1.0,
                    "actual_end_padding_seconds": round(timestamp_padding, 3),
                },
                "quality": {"accuracy_measured": False, "human_review_required": True},
            },
        )
        publish_files(
            [(staging / name, output_dir / name) for name in names], options.overwrite
        )
    print(f"Transcript: {output_dir / names[0]}")
    print(
        f"ASR wall time: {wall_seconds:.2f}s for {duration:.2f}s of audio (model download excluded)"
    )
    if timestamp_padding:
        print(
            f"Note: raw ASR end times exceed audio by {timestamp_padding:.3f}s; derived source times are clamped."
        )


def positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a positive integer.") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("Use a positive integer.")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clip authorized lecture audio and transcribe it locally."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    clip = commands.add_parser(
        "clip", help="Extract an audio section from signed media or a local file."
    )
    source = clip.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--source-url-file",
        type=Path,
        help="Private file containing one current HTTPS media URL.",
    )
    source.add_argument(
        "--input",
        type=Path,
        help="Local media file; its timeline is used for --start/--end.",
    )
    clip.add_argument(
        "--start", type=parse_time, required=True, help="Seconds, MM:SS, or HH:MM:SS."
    )
    clip.add_argument(
        "--end",
        type=parse_time,
        required=True,
        help="Exclusive end on the source timeline.",
    )
    clip.add_argument(
        "--out",
        type=Path,
        required=True,
        help="AAC .m4a or mono 16 kHz .wav destination.",
    )
    clip.add_argument(
        "--source-page",
        help="Stable lecture page URL; query and fragment are not retained.",
    )
    clip.add_argument(
        "--source-offset",
        type=parse_time,
        default=0.0,
        help="Original-source offset of a local input clip; default 0.",
    )
    clip.add_argument(
        "--overwrite",
        action="store_true",
        help="Explicitly replace the clip and its metadata.",
    )
    clip.set_defaults(handler=clip_audio)
    transcribe = commands.add_parser(
        "transcribe",
        help="Transcribe local audio using pretrained English models on CPU.",
    )
    transcribe.add_argument("audio", type=Path)
    transcribe.add_argument(
        "--model",
        choices=("small.en", "medium.en"),
        default="small.en",
        help="small.en is faster; medium.en may improve recognition.",
    )
    transcribe.add_argument(
        "--output-dir", type=Path, help="Default: transcripts/MODEL/AUDIO_NAME/."
    )
    transcribe.add_argument("--threads", type=positive_integer, default=8)
    transcribe.add_argument(
        "--metadata",
        type=Path,
        help="Clip metadata; defaults to AUDIO_NAME.metadata.json if present.",
    )
    transcribe.add_argument(
        "--source-offset",
        type=parse_time,
        help="Original-lecture offset; overrides metadata when supplied.",
    )
    transcribe.add_argument(
        "--download-model",
        action="store_true",
        help="Allow downloading the selected pretrained weights if not cached.",
    )
    transcribe.add_argument(
        "--vad",
        action="store_true",
        help="Filter non-speech audio; may discard quiet speech. Off by default.",
    )
    transcribe.add_argument(
        "--overwrite",
        action="store_true",
        help="Explicitly replace this input's transcript exports and report.",
    )
    transcribe.set_defaults(handler=transcribe_audio)
    return parser


def main(argv: list[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    try:
        options.handler(options)
    except CliError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except OSError:
        print(
            "Error: Could not access the input or output files. Check paths and permissions.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print(
            "Interrupted. No completed output was saved for the interrupted operation.",
            file=sys.stderr,
        )
        return 130
    return 0
