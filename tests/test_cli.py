import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

from lecture_transcripts.browser import (
    BrowserSource,
    MediaDiscoveryError,
    get_browser_source,
    signed_master_url,
    wait_for_source,
)
from lecture_transcripts.cli import (
    CliError,
    cached_model,
    initial_audio_delay,
    main,
    parse_time,
    probe_audio,
    run_command,
    run_progress_command,
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

    def fake_media(self, arguments, failure_message, environment=None, progress=None):
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

    def test_zero_start_aac_preserves_initial_timestamp_gap(self):
        arguments = self.clip_arguments()
        arguments[arguments.index("--start") + 1] = "0"
        arguments[arguments.index("--end") + 1] = "2"
        with patch(
            "lecture_transcripts.cli.run_command", side_effect=self.fake_media
        ) as command:
            self.assertEqual(self.invoke(arguments), 0)
        extraction = next(
            call for call in command.call_args_list if call.args[0][0] == "ffmpeg"
        )
        self.assertIn("asetpts=PTS-STARTPTS,adelay=0.000000:all=1", extraction.args[0])
        self.assertEqual(
            extraction.args[0][extraction.args[0].index("-c:a") + 1], "aac"
        )
        self.assertEqual(extraction.kwargs["progress"], ("Converting audio", 2))
        metadata = json.loads(self.output.with_suffix(".metadata.json").read_text())
        self.assertEqual(metadata["audio_processing"], "aac_reencode")

    def test_initial_audio_delay_is_relative_to_recording_start(self):
        media = {
            "format": {"start_time": "1.4"},
            "streams": [
                {"codec_type": "video", "start_time": "1.4"},
                {"codec_type": "audio", "start_time": "2.235"},
            ],
        }
        self.assertAlmostEqual(initial_audio_delay(media), 0.835)

    def test_nonzero_aac_clip_still_uses_stream_copy(self):
        with patch(
            "lecture_transcripts.cli.run_command", side_effect=self.fake_media
        ) as command:
            self.assertEqual(self.invoke(self.clip_arguments()), 0)
        extraction = command.call_args_list[0]
        self.assertNotIn("-af", extraction.args[0])
        self.assertEqual(
            extraction.args[0][extraction.args[0].index("-c:a") + 1], "copy"
        )

    def test_ffmpeg_progress_uses_audio_seconds_without_raw_errors(self):
        process = MagicMock()
        process.stdout = io.StringIO(
            "out_time_us=500000\nout_time_us=1500000\nprogress=end\n"
        )
        process.wait.return_value = 0
        process.poll.return_value = 0
        bar = MagicMock()
        bar.n = 0.0
        bar.update.side_effect = lambda amount: setattr(bar, "n", bar.n + amount)
        with patch(
            "lecture_transcripts.cli.subprocess.Popen", return_value=process
        ) as launch:
            with patch("tqdm.tqdm") as progress:
                progress.return_value.__enter__.return_value = bar
                run_progress_command(
                    ["ffmpeg", "-i", "https://media.invalid/?token=SECRET"],
                    "Extraction failed.",
                    None,
                    "Downloading audio",
                    2,
                )
        self.assertEqual(bar.n, 2)
        self.assertEqual(launch.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertIn("pipe:1", launch.call_args.args[0])
        self.assertNotIn("SECRET", str(progress.call_args))

    def test_ffmpeg_progress_failure_does_not_complete_bar(self):
        process = MagicMock()
        process.stdout = io.StringIO("out_time_us=500000\n")
        process.wait.return_value = 1
        process.poll.return_value = 1
        bar = MagicMock()
        bar.n = 0.0
        bar.update.side_effect = lambda amount: setattr(bar, "n", bar.n + amount)
        with (
            patch("lecture_transcripts.cli.subprocess.Popen", return_value=process),
            patch("tqdm.tqdm") as progress,
        ):
            progress.return_value.__enter__.return_value = bar
            with self.assertRaisesRegex(CliError, "Extraction failed"):
                run_progress_command(
                    ["ffmpeg"], "Extraction failed.", None, "Downloading audio", 2
                )
        self.assertEqual(bar.n, 0.5)

    def test_ffmpeg_progress_cancellation_terminates_process(self):
        process = MagicMock()
        process.stdout.__iter__.side_effect = KeyboardInterrupt
        process.poll.return_value = None
        with (
            patch("lecture_transcripts.cli.subprocess.Popen", return_value=process),
            patch("tqdm.tqdm"),
            self.assertRaises(KeyboardInterrupt),
        ):
            run_progress_command(
                ["ffmpeg"], "Extraction failed.", None, "Downloading audio", 2
            )
        process.terminate.assert_called_once()
        process.stdout.close.assert_called_once()

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

    def test_browser_source_accepts_current_signed_master(self):
        url = "https://ptz143.ust.hk/rvcsecured/mp4:lecture.mp4/playlist.m3u8?rvctokenendtime=2000&rvctokenhash=SECRET_TOKEN"
        self.assertEqual(signed_master_url(url, now=1000), url)
        self.assertNotIn(
            "SECRET_TOKEN", repr(BrowserSource(url, "https://canvas.ust.hk/"))
        )

    def test_browser_source_rejects_expired_or_untrusted_urls(self):
        base = "https://ptz143.ust.hk/rvcsecured/mp4:lecture.mp4/playlist.m3u8?rvctokenendtime=2000&rvctokenhash=SECRET_TOKEN"
        for url in [
            base.replace("2000", "999"),
            base.replace("2000", "nan"),
            base.replace("2000", "inf"),
            base.replace("ptz143.ust.hk", "evilust.hk"),
            base.replace("ptz143.ust.hk", "ust.hk.example.invalid"),
            base.replace("https://", "http://"),
            base.replace("https://", "https://user:password@"),
            base.replace("playlist.m3u8", "chunklist_w123_tkSECRET.m3u8"),
            base.replace("rvctokenhash=SECRET_TOKEN", "rvctokenhash="),
            base + "&rvctokenendtime=3000",
            "https://login.microsoftonline.com/",
        ]:
            with self.subTest(url=url):
                self.assertIsNone(signed_master_url(url, now=1000))

    def browser_fixture(self):
        factory = MagicMock()
        context = factory.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
        page = context.new_page.return_value
        page.url = "https://canvas.ust.hk/courses/123/pages/lecture"
        page.is_closed.return_value = False
        frame = MagicMock()
        page.frames = [frame]
        url = "https://ptz143.ust.hk/rvcsecured/mp4:lecture.mp4/playlist.m3u8?rvctokenendtime=4102444800&rvctokenhash=SECRET_TOKEN"
        frame.evaluate.return_value = [url]
        return factory, context, page, frame, url

    def test_browser_waits_through_login_before_returning_source(self):
        factory, context, page, frame, url = self.browser_fixture()
        frame.evaluate.side_effect = [[], [], [url]]
        with contextlib.redirect_stdout(io.StringIO()):
            result = get_browser_source(
                profile_dir=self.root / "profile", sync_playwright_factory=factory
            )
        self.assertEqual(result.url, url)
        self.assertEqual(page.wait_for_timeout.call_count, 2)
        context.close.assert_called_once()
        factory.return_value.__enter__.return_value.chromium.launch_persistent_context.assert_called_once_with(
            user_data_dir=str(self.root / "profile"),
            headless=False,
        )
        self.assertEqual((self.root / "profile").stat().st_mode & 0o777, 0o700)

    def test_browser_cancellation_is_clear_and_closes_owned_context(self):
        factory, context, page, frame, url = self.browser_fixture()
        page.is_closed.return_value = True
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(MediaDiscoveryError, "closed"),
        ):
            get_browser_source(
                profile_dir=self.root / "profile", sync_playwright_factory=factory
            )
        context.close.assert_called_once()

    def test_browser_closed_during_event_wait_reports_cancellation(self):
        factory, context, page, frame, url = self.browser_fixture()
        frame.evaluate.return_value = []
        page.is_closed.side_effect = [False, True]
        page.wait_for_timeout.side_effect = RuntimeError("closed")
        with self.assertRaisesRegex(MediaDiscoveryError, "closed"):
            wait_for_source(context, page)

    def test_browser_timeout_does_not_succeed_on_login_state(self):
        factory, context, page, frame, url = self.browser_fixture()
        frame.evaluate.return_value = []
        with patch("lecture_transcripts.browser.time.monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(MediaDiscoveryError, "Timed out"):
                wait_for_source(context, page, timeout=1)

    def test_browser_can_capture_successful_media_response(self):
        factory, context, page, frame, url = self.browser_fixture()
        frame.evaluate.return_value = []
        response = MagicMock()
        response.status = 200
        response.url = url
        response.request.frame.page = page
        context.on.side_effect = lambda event, callback: callback(response)
        self.assertEqual(wait_for_source(context, page).url, url)

    def test_browser_observes_media_responses_during_initial_navigation(self):
        factory, context, page, frame, url = self.browser_fixture()
        frame.evaluate.return_value = []
        response = MagicMock()
        response.status = 200
        response.url = url
        response.request.frame.page = page
        listeners = {}
        context.on.side_effect = lambda event, callback: listeners.update(
            {event: callback}
        )
        page.goto.side_effect = lambda *arguments, **kwargs: listeners["response"](
            response
        )
        self.assertEqual(wait_for_source(context, page, start_url=page.url).url, url)

    def test_missing_explicit_source_uses_browser_and_keeps_token_out_of_metadata(self):
        factory, context, page, frame, url = self.browser_fixture()
        arguments = ["clip", "--start", "10", "--end", "12", "--out", str(self.output)]
        with patch(
            "lecture_transcripts.cli.get_browser_source",
            return_value=BrowserSource(url, page.url),
        ) as browser:
            with patch(
                "lecture_transcripts.cli.run_command", side_effect=self.fake_media
            ):
                self.assertEqual(self.invoke(arguments), 0)
        browser.assert_called_once()
        metadata_text = self.output.with_suffix(".metadata.json").read_text()
        self.assertNotIn("SECRET_TOKEN", metadata_text)
        self.assertEqual(json.loads(metadata_text)["source_page"], page.url)

    def test_existing_output_stops_before_browser_launch(self):
        self.output.write_bytes(b"existing audio")
        with patch("lecture_transcripts.cli.get_browser_source") as browser:
            self.assertEqual(
                self.invoke(
                    ["clip", "--start", "10", "--end", "12", "--out", str(self.output)]
                ),
                1,
            )
        browser.assert_not_called()

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


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "Real media tests require FFmpeg and ffprobe.",
)
class FFmpegClipTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "delayed-audio.ts"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=160x90:r=25:d=3",
                "-itsoffset",
                "0.835",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=44100:duration=2.165",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "mpeg2video",
                "-c:a",
                "aac",
                "-f",
                "mpegts",
                str(self.source),
            ],
            capture_output=True,
            check=True,
        )

    def invoke(self, output, end="2"):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return main(
                [
                    "clip",
                    "--input",
                    str(self.source),
                    "--start",
                    "0",
                    "--end",
                    end,
                    "--out",
                    str(output),
                ]
            )

    def test_zero_start_aac_with_delayed_audio_has_requested_duration(self):
        output = self.root / "clip.m4a"
        self.assertEqual(self.invoke(output), 0)
        self.assertAlmostEqual(
            float(probe_audio(output)["format"]["duration"]), 2.0, places=2
        )

    def test_wav_preserves_silence_before_first_source_audio(self):
        output = self.root / "clip.wav"
        self.assertEqual(self.invoke(output), 0)
        with wave.open(str(output), "rb") as audio:
            self.assertEqual(audio.getframerate(), 16000)
            self.assertEqual(audio.getnframes(), 32000)
            initial = audio.readframes(4000)
            self.assertEqual(initial, bytes(len(initial)))
            remaining = audio.readframes(28000)
            self.assertNotEqual(remaining, bytes(len(remaining)))

    def test_padding_does_not_hide_truncated_recording(self):
        output = self.root / "truncated.m4a"
        self.assertEqual(self.invoke(output, end="5"), 1)
        self.assertFalse(output.exists())
        self.assertFalse(output.with_suffix(".metadata.json").exists())


@unittest.skipUnless(
    os.environ.get("LECTURE_BROWSER_TESTS") == "1",
    "Opt-in real browser tests require Playwright and a graphical session.",
)
class BrowserHarnessTests(unittest.TestCase):
    def capture_fixture(self, mode):
        from playwright.sync_api import sync_playwright

        master = "https://ptz143.ust.hk/rvcsecured/mp4:fixture.mp4/playlist.m3u8?rvctokenendtime=4102444800&rvctokenhash=EXAMPLE_TOKEN"

        @contextlib.contextmanager
        def factory():
            with sync_playwright() as driver:
                launch = driver.chromium.launch_persistent_context

                def launch_fixture(**arguments):
                    context = launch(**arguments)

                    def respond(route):
                        if route.request.url == master:
                            route.fulfill(
                                status=200,
                                content_type="application/vnd.apple.mpegurl",
                                body="#EXTM3U\n#EXT-X-ENDLIST\n",
                            )
                        elif "/frame" in route.request.url:
                            body = (
                                "<script>setTimeout(() => { window.jwplayer = () => ({getPlaylist: () => [{sources: [{file: "
                                + json.dumps(master)
                                + "}]}]}); }, 600);</script>"
                            )
                            route.fulfill(
                                status=200, content_type="text/html", body=body
                            )
                        else:
                            body = (
                                '<iframe src="/frame"></iframe>'
                                if mode == "iframe-player"
                                else "<script>setTimeout(() => fetch("
                                + json.dumps(master)
                                + "), 600);</script>"
                            )
                            route.fulfill(
                                status=200, content_type="text/html", body=body
                            )

                    context.route("**/*", respond)
                    return context

                with patch.object(
                    driver.chromium,
                    "launch_persistent_context",
                    side_effect=launch_fixture,
                ):
                    yield driver

        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile"
            with contextlib.redirect_stdout(io.StringIO()):
                source = get_browser_source(
                    start_url="https://canvas.ust.hk/browser-fixture",
                    profile_dir=profile,
                    timeout=10,
                    sync_playwright_factory=factory,
                )
            self.assertEqual(source.url, master)
            self.assertNotIn("EXAMPLE_TOKEN", repr(source))
            self.assertEqual(profile.stat().st_mode & 0o777, 0o700)

    def test_delayed_iframe_source_in_real_browser(self):
        self.capture_fixture("iframe-player")

    def test_delayed_media_response_in_real_browser(self):
        self.capture_fixture("media-response")


if __name__ == "__main__":
    unittest.main()
