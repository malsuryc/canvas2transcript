import argparse
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lecture_transcripts.cli import (
    CliError,
    cached_model,
    main,
    parse_time,
    run_command,
    validate_transcript,
    validate_url,
)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source.m4a"
        self.source.write_bytes(b"fixture audio")
        self.output = self.root / "clip.m4a"

    def clip_arguments(self):
        return [
            "clip",
            "--input",
            str(self.source),
            "--start",
            "10",
            "--end",
            "12",
            "--out",
            str(self.output),
        ]

    def fake_media(self, arguments, failure_message, environment=None):
        if arguments[0] == "ffmpeg":
            Path(arguments[-1]).write_bytes(b"clipped audio")
            return ""
        return json.dumps(
            {
                "format": {"duration": "2.0", "size": "13"},
                "streams": [{"codec_type": "audio", "codec_name": "aac"}],
            }
        )

    def fake_asr(self, arguments, failure_message, environment=None):
        if Path(arguments[0]).name == "whisper-ctranslate2":
            self.assertEqual(environment["HF_HUB_OFFLINE"], "1")
            staging = Path(arguments[arguments.index("--output_dir") + 1])
            audio_name = Path(arguments[1]).stem
            transcript = {
                "language": "en",
                "text": "A capacitor stores energy.",
                "segments": [
                    {"start": 0, "end": 2, "text": "A capacitor stores energy."}
                ],
            }
            for extension in ("txt", "vtt", "srt", "tsv"):
                (staging / f"{audio_name}.{extension}").write_text(transcript["text"])
            (staging / f"{audio_name}.json").write_text(json.dumps(transcript))
            return ""
        return self.fake_media(arguments, failure_message, environment)

    def invoke(self, arguments):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return main(arguments)

    def test_clock_times_and_requested_pilot_duration(self):
        self.assertEqual(parse_time("1:02:11") - parse_time("33:56"), 1695)
        self.assertEqual(parse_time("90.5"), 90.5)
        self.assertEqual(parse_time("00:01:02.25"), 62.25)

    def test_invalid_times_are_rejected(self):
        for value in ["-1", "nan", "inf", "1:60", "1:60:00", "1.5:20", "1:2:3:4", ""]:
            with (
                self.subTest(value=value),
                self.assertRaises(argparse.ArgumentTypeError),
            ):
                parse_time(value)

    def test_invalid_range_does_not_run_ffmpeg(self):
        arguments = self.clip_arguments()
        arguments[arguments.index("--end") + 1] = "9"
        with patch("lecture_transcripts.cli.run_command") as command:
            self.assertEqual(self.invoke(arguments), 1)
            command.assert_not_called()

    def test_clip_saves_audio_and_source_offset(self):
        with patch("lecture_transcripts.cli.run_command", side_effect=self.fake_media):
            self.assertEqual(self.invoke(self.clip_arguments()), 0)
        self.assertEqual(self.output.read_bytes(), b"clipped audio")
        metadata = json.loads(self.output.with_suffix(".metadata.json").read_text())
        self.assertEqual(metadata["source_start_seconds"], 10)
        self.assertEqual(metadata["source_end_seconds"], 12)

    def test_local_clip_preserves_original_source_offset(self):
        with patch("lecture_transcripts.cli.run_command", side_effect=self.fake_media):
            self.assertEqual(
                self.invoke(self.clip_arguments() + ["--source-offset", "33:56"]), 0
            )
        metadata = json.loads(self.output.with_suffix(".metadata.json").read_text())
        self.assertEqual(metadata["source_start_seconds"], 2046)
        self.assertEqual(metadata["input_start_seconds"], 10)

    def test_existing_output_is_preserved(self):
        self.output.write_bytes(b"existing audio")
        with patch("lecture_transcripts.cli.run_command") as command:
            self.assertEqual(self.invoke(self.clip_arguments()), 1)
            command.assert_not_called()
        self.assertEqual(self.output.read_bytes(), b"existing audio")

    def test_failed_extraction_preserves_existing_output_even_with_overwrite(self):
        self.output.write_bytes(b"existing audio")
        with patch(
            "lecture_transcripts.cli.run_command",
            side_effect=CliError("Download failed."),
        ):
            self.assertEqual(self.invoke(self.clip_arguments() + ["--overwrite"]), 1)
        self.assertEqual(self.output.read_bytes(), b"existing audio")
        self.assertFalse(self.output.with_suffix(".metadata.json").exists())

    def test_partial_clip_is_not_published(self):
        arguments = self.clip_arguments()
        arguments[arguments.index("--end") + 1] = "20"
        with patch("lecture_transcripts.cli.run_command", side_effect=self.fake_media):
            self.assertEqual(self.invoke(arguments), 1)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_suffix(".metadata.json").exists())

    def test_explicit_overwrite_replaces_successful_clip(self):
        self.output.write_bytes(b"existing audio")
        with patch("lecture_transcripts.cli.run_command", side_effect=self.fake_media):
            self.assertEqual(self.invoke(self.clip_arguments() + ["--overwrite"]), 0)
        self.assertEqual(self.output.read_bytes(), b"clipped audio")

    def test_source_urls_reject_embedded_credentials(self):
        for url in [
            "https://user:password@media.invalid/",
            "https://:password@media.invalid/",
            "http://media.invalid/",
        ]:
            with self.subTest(url=url), self.assertRaises(CliError):
                validate_url(url)

    def test_signed_url_is_not_written_to_metadata(self):
        url_file = self.root / "private-url.txt"
        url_file.write_text(
            "https://media.example.invalid/movie/playlist.m3u8?token=SECRET_TOKEN"
        )
        arguments = [
            "clip",
            "--source-url-file",
            str(url_file),
            "--start",
            "10",
            "--end",
            "12",
            "--out",
            str(self.output),
        ]
        with patch("lecture_transcripts.cli.run_command", side_effect=self.fake_media):
            self.assertEqual(self.invoke(arguments), 0)
        metadata_text = self.output.with_suffix(".metadata.json").read_text()
        self.assertNotIn("SECRET_TOKEN", metadata_text)
        self.assertNotIn("playlist.m3u8", metadata_text)

    def test_raw_tool_errors_do_not_leak_credentials(self):
        result = subprocess.CompletedProcess(
            ["ffmpeg"],
            1,
            "",
            "https://media.invalid/_tkSECRET_TOKEN?token=SECRET_TOKEN",
        )
        with patch("lecture_transcripts.cli.subprocess.run", return_value=result):
            with self.assertRaises(CliError) as error:
                run_command(["ffmpeg"], "Extraction failed.")
        self.assertNotIn("SECRET_TOKEN", str(error.exception))

    def test_missing_model_requires_explicit_download_permission(self):
        with patch(
            "huggingface_hub.snapshot_download", side_effect=RuntimeError("missing")
        ) as download:
            with self.assertRaisesRegex(CliError, "--download-model"):
                cached_model("medium.en", False)
        self.assertEqual(download.call_count, 1)
        self.assertTrue(download.call_args.kwargs["local_files_only"])
        self.assertFalse(download.call_args.kwargs["token"])

    def test_permitted_model_download_fetches_complete_snapshot(self):
        with patch(
            "huggingface_hub.snapshot_download",
            side_effect=[RuntimeError("missing"), str(self.root)],
        ) as download:
            with contextlib.redirect_stdout(io.StringIO()):
                model_path, elapsed = cached_model("medium.en", True)
        self.assertEqual(model_path, self.root)
        self.assertGreaterEqual(elapsed, 0)
        self.assertNotIn("allow_patterns", download.call_args.kwargs)
        self.assertFalse(download.call_args.kwargs["token"])

    def test_transcription_maps_source_timestamps_and_records_model(self):
        output_dir = self.root / "transcripts"
        arguments = [
            "transcribe",
            str(self.source),
            "--source-offset",
            "33:56",
            "--output-dir",
            str(output_dir),
            "--model",
            "medium.en",
        ]
        with (
            patch("lecture_transcripts.cli.run_command", side_effect=self.fake_asr),
            patch(
                "lecture_transcripts.cli.cached_model", return_value=(self.root, 0.0)
            ),
        ):
            self.assertEqual(self.invoke(arguments), 0)
        transcript = json.loads((output_dir / "source.source.json").read_text())
        self.assertEqual(transcript["segments"][0]["source_start"], 2036)
        self.assertEqual(transcript["segments"][0]["source_end"], 2038)
        report = json.loads((output_dir / "source.benchmark.json").read_text())
        self.assertEqual(report["model"]["name"], "medium.en")
        self.assertFalse(report["quality"]["accuracy_measured"])

    def test_metadata_hash_mismatch_stops_before_model_loading(self):
        metadata = self.source.with_suffix(".metadata.json")
        metadata.write_text(json.dumps({"audio_sha256": "wrong hash"}))
        with (
            patch("lecture_transcripts.cli.run_command", side_effect=self.fake_media),
            patch("lecture_transcripts.cli.cached_model") as model,
        ):
            self.assertEqual(self.invoke(["transcribe", str(self.source)]), 1)
            model.assert_not_called()

    def test_clip_metadata_is_automatically_used_by_transcription(self):
        output_dir = self.root / "transcripts"
        with patch("lecture_transcripts.cli.run_command", side_effect=self.fake_media):
            self.assertEqual(
                self.invoke(self.clip_arguments() + ["--source-offset", "33:56"]), 0
            )
        with (
            patch("lecture_transcripts.cli.run_command", side_effect=self.fake_asr),
            patch(
                "lecture_transcripts.cli.cached_model", return_value=(self.root, 0.0)
            ),
        ):
            self.assertEqual(
                self.invoke(
                    ["transcribe", str(self.output), "--output-dir", str(output_dir)]
                ),
                0,
            )
        transcript = json.loads((output_dir / "clip.source.json").read_text())
        self.assertEqual(transcript["segments"][0]["source_start"], 2046)

    def test_invalid_metadata_filename_stops_cleanly(self):
        self.source.with_suffix(".metadata.json").write_text(
            json.dumps({"audio_file": ["invalid"]})
        )
        with patch("lecture_transcripts.cli.run_command", side_effect=self.fake_media):
            self.assertEqual(self.invoke(["transcribe", str(self.source)]), 1)

    def test_invalid_segment_timestamps_are_rejected(self):
        for segments in [
            [{"text": "missing timestamps"}],
            [{"start": 0, "end": 5, "text": "past end"}],
            [
                {"start": 1, "end": 2, "text": "later"},
                {"start": 0, "end": 1, "text": "earlier"},
            ],
        ]:
            with self.subTest(segments=segments), self.assertRaises(CliError):
                validate_transcript(
                    {"text": "Recognized speech.", "segments": segments}, 2
                )

    def test_small_asr_end_padding_is_reported(self):
        padding = validate_transcript(
            {
                "text": "Recognized speech.",
                "segments": [{"start": 1.5, "end": 2.67, "text": "Recognized speech."}],
            },
            2,
        )
        self.assertAlmostEqual(padding, 0.67)

    def test_derived_source_times_clamp_padding_without_changing_raw_json(self):
        output_dir = self.root / "transcripts"

        def padded_asr(arguments, failure_message, environment=None):
            result = self.fake_asr(arguments, failure_message, environment)
            if Path(arguments[0]).name == "whisper-ctranslate2":
                staging = Path(arguments[arguments.index("--output_dir") + 1])
                transcript_path = staging / "source.json"
                transcript = json.loads(transcript_path.read_text())
                transcript["segments"][0]["end"] = 2.67
                transcript_path.write_text(json.dumps(transcript))
            return result

        with (
            patch("lecture_transcripts.cli.run_command", side_effect=padded_asr),
            patch(
                "lecture_transcripts.cli.cached_model", return_value=(self.root, 0.0)
            ),
        ):
            self.assertEqual(
                self.invoke(
                    [
                        "transcribe",
                        str(self.source),
                        "--source-offset",
                        "33:56",
                        "--output-dir",
                        str(output_dir),
                    ]
                ),
                0,
            )
        raw = json.loads((output_dir / "source.json").read_text())
        mapped = json.loads((output_dir / "source.source.json").read_text())
        self.assertEqual(raw["segments"][0]["end"], 2.67)
        self.assertEqual(mapped["segments"][0]["source_end"], 2038)

    def test_existing_transcript_is_preserved_before_model_loading(self):
        output_dir = self.root / "transcripts"
        output_dir.mkdir()
        transcript = output_dir / "source.txt"
        transcript.write_text("Existing transcript.")
        with patch("lecture_transcripts.cli.cached_model") as model:
            self.assertEqual(
                self.invoke(
                    ["transcribe", str(self.source), "--output-dir", str(output_dir)]
                ),
                1,
            )
            model.assert_not_called()
        self.assertEqual(transcript.read_text(), "Existing transcript.")

    def test_failed_transcription_publishes_no_exports(self):
        output_dir = self.root / "transcripts"

        def failed_asr(arguments, failure_message, environment=None):
            if Path(arguments[0]).name == "whisper-ctranslate2":
                raise CliError("ASR failed.")
            return self.fake_media(arguments, failure_message, environment)

        with (
            patch("lecture_transcripts.cli.run_command", side_effect=failed_asr),
            patch(
                "lecture_transcripts.cli.cached_model", return_value=(self.root, 0.0)
            ),
        ):
            self.assertEqual(
                self.invoke(
                    ["transcribe", str(self.source), "--output-dir", str(output_dir)]
                ),
                1,
            )
        self.assertEqual(list(output_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
