"""Check language gating, async failure isolation, and recording shutdown."""
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from gijiroku.asr.fast import FastASREvent
from gijiroku.translation import (EnglishTranslationWorker, LocalEnglishJapaneseTranslator,
                                 TranslationRequest)


class EnglishTranslationTests(unittest.TestCase):
    def test_disabled_worker_never_loads_model(self):
        factory = Mock()
        with tempfile.TemporaryDirectory() as folder:
            worker = EnglishTranslationWorker(factory, folder, Mock(), Mock())
            worker.set_enabled(False)
            self.assertFalse(worker.submit("Hello", "en"))
            worker.close()
            factory.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_switching_off_skips_waiting_jobs_and_can_be_reenabled(self):
        entered, release = threading.Event(), threading.Event()
        skipped = []

        def translate(text):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timeout")
            return "訳:" + text

        model = SimpleNamespace(translate=Mock(side_effect=translate))
        with tempfile.TemporaryDirectory() as folder:
            worker = EnglishTranslationWorker(lambda: model, folder, Mock(), Mock(),
                on_skipped=lambda request, error: skipped.append(request.source))
            try:
                self.assertTrue(worker.submit("First", "en"))
                self.assertTrue(entered.wait(5))
                self.assertTrue(worker.submit("Pending", "en"))
                worker.set_enabled(False)
                release.set()
                worker._queue.join()
                self.assertEqual(skipped, ["Pending"])
                self.assertEqual([call.args[0] for call in model.translate.call_args_list], ["First"])
                worker.set_enabled(True)
                self.assertTrue(worker.submit("After reenable", "en"))
            finally:
                release.set()
                worker.close()
            self.assertEqual([call.args[0] for call in model.translate.call_args_list],
                             ["First", "After reenable"])

    def test_japanese_and_drafts_do_not_load_model_or_create_output(self):
        factory = Mock()
        with tempfile.TemporaryDirectory() as folder:
            worker = EnglishTranslationWorker(factory, folder, Mock(), Mock())
            try:
                self.assertFalse(worker.submit("日本語です", "ja"))
                self.assertFalse(worker.submit("Hello", "en", kind="partial"))
                self.assertFalse(worker.submit("Hello", "en", kind="refine"))
                self.assertFalse(worker.submit("Hello", "fr"))
                self.assertFalse(worker.submit("  ", "en"))
            finally:
                worker.close()
            factory.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_english_is_translated_once_and_close_drains_pending_jobs(self):
        model = SimpleNamespace(translate=Mock(side_effect=lambda x: f"訳:{x}"))
        factory = Mock(return_value=model)
        results, errors = [], []
        with tempfile.TemporaryDirectory() as folder:
            worker = EnglishTranslationWorker(factory, folder,
                lambda request, text: results.append((request, text)), errors.append)
            self.assertTrue(worker.submit("Hello", "en", seconds=1.5, speaker="相手"))
            self.assertFalse(worker.submit("こんにちは", "ja"))
            self.assertTrue(worker.submit("Thank you", "en", seconds=4.0, speaker="自分"))
            worker.close()
            self.assertFalse(worker.submit("After close", "en"))
            factory.assert_called_once()
            self.assertEqual(model.translate.call_count, 2)
            self.assertEqual(errors, [])
            self.assertEqual([x[1] for x in results], ["訳:Hello", "訳:Thank you"])
            saved = [json.loads(line) for line in
                     (Path(folder) / "translation_en_ja.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(saved[0]["speaker"], "相手")
            self.assertEqual(saved[0]["seconds"], 1.5)
            self.assertEqual([row["source"] for row in saved], ["Hello", "Thank you"])

    def test_missing_model_reports_once_without_repeated_load_attempts(self):
        factory = Mock(side_effect=RuntimeError("model missing"))
        errors = []
        with tempfile.TemporaryDirectory() as folder:
            worker = EnglishTranslationWorker(factory, folder, Mock(), errors.append)
            worker.submit("Hello", "en")
            worker.submit("Thanks", "en")
            worker.close()
        factory.assert_called_once()
        self.assertEqual(errors, ["model missing"])

    def test_inference_failure_does_not_discard_following_english(self):
        model = SimpleNamespace(translate=Mock(side_effect=[RuntimeError("bad input"), "成功"]))
        errors, results = [], []
        with tempfile.TemporaryDirectory() as folder:
            worker = EnglishTranslationWorker(lambda: model, folder,
                lambda request, text: results.append(text), errors.append)
            worker.submit("First", "en")
            worker.submit("Second", "en")
            worker.close()
        self.assertEqual(errors, ["bad input"])
        self.assertEqual(results, ["成功"])

    def test_queue_backpressure_never_blocks_asr(self):
        entered, release = threading.Event(), threading.Event()

        def translate(text):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timeout")
            return text

        errors = []
        with tempfile.TemporaryDirectory() as folder:
            worker = EnglishTranslationWorker(lambda: SimpleNamespace(translate=translate),
                folder, Mock(), errors.append, max_pending=1)
            try:
                self.assertTrue(worker.submit("First", "en"))
                self.assertTrue(entered.wait(5))
                self.assertTrue(worker.submit("Second", "en"))
                self.assertFalse(worker.submit("Third", "en"))
            finally:
                release.set()
                worker.close()
            self.assertEqual(len(errors), 1)
            self.assertIn("上限", errors[0])

    def test_long_input_is_not_silently_truncated(self):
        translator = object.__new__(LocalEnglishJapaneseTranslator)
        translator._normalize = lambda text: text
        translator.source = SimpleNamespace(encode=lambda text, out_type: text.split())
        translator.target = SimpleNamespace(decode=lambda pieces: " ".join(pieces))
        batches = []

        def translate_batch(source, **kwargs):
            batches.append(source[0])
            return [SimpleNamespace(hypotheses=[source[0][:-1]])]

        translator.model = SimpleNamespace(translate_batch=translate_batch)
        text = " ".join(f"word{i}" for i in range(600))
        self.assertEqual(translator.translate(text), text)
        self.assertEqual([len(x) for x in batches], [257, 257, 89])
        self.assertTrue(all(x[-1] == "</s>" for x in batches))


class TranslationRoutingTests(unittest.TestCase):
    def make_gui(self, worker):
        from gijiroku.gui import MeetingRecorderGUI
        gui = object.__new__(MeetingRecorderGUI)
        gui._translation_worker = worker
        gui.TRANSLATION_ENABLED = True
        gui._transcript_row_counter = 0
        gui._record_start = 1700000000
        gui.root = SimpleNamespace(after=Mock())
        gui._leakage_guard = SimpleNamespace(remember_other=Mock(), is_leakage=lambda text: False)
        gui._fast_final_segments = []
        gui._fast_refined_segments = []
        gui.transcription_file = io.StringIO()
        gui.refined_transcription_file = io.StringIO()
        gui.TRANSCRIBE_RATE = 16000
        gui._transcribe_start = 0
        gui._rt_lang = None
        gui._rt_detected_lang = None
        gui._write_language_event = Mock()
        return gui

    def test_unchecked_gui_does_not_submit_english_for_translation(self):
        worker = Mock()
        gui = self.make_gui(worker)
        gui.TRANSLATION_ENABLED = False
        gui._record_fast_results([FastASREvent("final", "Hello", "en", 16000, 32000)])
        worker.submit.assert_not_called()
        self.assertIn("Hello", gui.transcription_file.getvalue())

    def test_fast_backend_routes_per_event_language_not_last_global_language(self):
        model = SimpleNamespace(translate=Mock(return_value="日本語訳"))
        errors = []
        with tempfile.TemporaryDirectory() as folder:
            worker = EnglishTranslationWorker(lambda: model, folder, Mock(), errors.append)
            gui = self.make_gui(worker)
            try:
                gui._record_fast_results([
                    FastASREvent("partial", "Draft", "en"),
                    FastASREvent("final", "Hello", "en", 16000, 32000),
                    FastASREvent("final", "こんにちは", "ja", 32000, 48000),
                    FastASREvent("refine", "Hello again", "en", 16000, 32000),
                    FastASREvent("final", "Thanks", "en", 48000, 64000),
                ], "相手")
            finally:
                worker.close()
            self.assertEqual([x.args[0] for x in model.translate.call_args_list], ["Hello", "Thanks"])
            self.assertIn("こんにちは", gui.transcription_file.getvalue())
            self.assertEqual(errors, [])

    def test_whisper_backend_uses_detected_language_for_translation(self):
        import numpy as np
        from unittest.mock import patch
        model = SimpleNamespace(translate=Mock(return_value="日本語訳"))
        with tempfile.TemporaryDirectory() as folder:
            worker = EnglishTranslationWorker(lambda: model, folder, Mock(), Mock())
            gui = self.make_gui(worker)
            gui.transcriber = SimpleNamespace(transcribe=Mock(side_effect=[
                ([SimpleNamespace(text="Hello", start=0.0)], None),
                ([SimpleNamespace(text="こんにちは", start=0.0)], None),
            ]))
            try:
                with patch("gijiroku.gui.detect_ja_en", side_effect=["en", "ja"]):
                    self.assertEqual(gui._transcribe_chunk(np.zeros(16000), None), "en")
                    self.assertEqual(gui._transcribe_chunk(np.zeros(16000), "en"), "ja")
            finally:
                worker.close()
            model.translate.assert_called_once_with("Hello")


class BilingualDisplayTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from gijiroku.gui import MeetingRecorderGUI
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.gui = object.__new__(MeetingRecorderGUI)
        self.gui.transcript_area = tk.Text(self.root)
        self.gui.translation_area = tk.Text(self.root)

    def test_checkbox_starts_off_even_with_legacy_saved_enabled_setting(self):
        from unittest.mock import patch
        from gijiroku.gui import MeetingRecorderGUI
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text('{"translation_enabled": true}', encoding="utf-8")
            with patch("gijiroku.gui.SETTINGS_PATH", str(path)), \
                 patch.object(MeetingRecorderGUI, "_enumerate_audio_devices"), \
                 patch.object(MeetingRecorderGUI, "_populate_audio_listbox"), \
                 patch.object(MeetingRecorderGUI, "_detect_monitors"), \
                 patch.object(MeetingRecorderGUI, "_tick_level_meter"):
                gui = MeetingRecorderGUI(self.root)
            self.assertFalse(gui.TRANSLATION_ENABLED)
            self.assertFalse(gui._translation_enabled_var.get())
            self.assertEqual(len(gui._transcript_panes.panes()), 1)
            gui._translation_worker = Mock()
            gui._translation_enabled_var.set(True)
            gui._on_translation_toggle()
            self.assertTrue(gui.TRANSLATION_ENABLED)
            self.assertEqual(len(gui._transcript_panes.panes()), 2)
            gui._translation_worker.set_enabled.assert_called_once_with(True)
            gui._translation_enabled_var.set(False)
            gui._on_translation_toggle()
            self.assertFalse(gui.TRANSLATION_ENABLED)
            self.assertEqual(len(gui._transcript_panes.panes()), 1)
            gui._translation_worker.set_enabled.assert_called_with(False)

    def test_off_only_updates_original_column(self):
        gui = self.gui
        gui._log_transcript("日本語です。", "自分", 1, "ja", "row1", "12:30:01",
                            False, False)
        self.assertIn("日本語です。", gui.transcript_area.get("1.0", "end-1c"))
        self.assertEqual(gui.translation_area.get("1.0", "end-1c"), "")

    def test_enabling_translation_opens_both_columns_after_layout(self):
        from unittest.mock import patch
        from gijiroku.gui import MeetingRecorderGUI
        with patch.object(MeetingRecorderGUI, "_enumerate_audio_devices"), \
             patch.object(MeetingRecorderGUI, "_populate_audio_listbox"), \
             patch.object(MeetingRecorderGUI, "_detect_monitors"), \
             patch.object(MeetingRecorderGUI, "_tick_level_meter"):
            gui = MeetingRecorderGUI(self.root)
        gui.chk_translation.invoke()
        self.root.update_idletasks()
        self.root.deiconify()
        self.root.update()
        panes = gui._transcript_panes
        self.assertGreater(gui._translation_frame.winfo_width(), panes.winfo_width() * .4)
        gui.chk_translation.invoke()
        self.root.update()
        for _ in range(3):
            gui.chk_translation.invoke()
            self.root.update()
            width = panes.winfo_width()
            self.assertGreater(gui._translation_frame.winfo_width(), width * .4)
            self.assertLess(panes.sashpos(0), width * .6)
            self.assertGreater(panes.sashpos(0), width * .4)
            # A later resize must preserve a user's manual column adjustment.
            panes.sashpos(0, int(width * .65))
            self.root.geometry(f"{self.root.winfo_width() - 10}x{self.root.winfo_height()}")
            self.root.update()
            self.assertGreater(panes.sashpos(0), panes.winfo_width() * .6)
            gui.chk_translation.invoke()
            self.root.update()
            self.assertEqual(len(panes.panes()), 1)

    def test_late_translation_keeps_order_timestamp_and_speaker(self):
        gui = self.gui
        gui._log_transcript("Hello", "相手", 1, "en", "row1", "12:30:01", True)
        gui._log_transcript("承知しました。", "自分", 2, "ja", "row2", "12:30:02", False)
        gui._log_transcript("Thanks", "相手", 3, "en", "row3", "12:30:03", True)
        source = gui.transcript_area.get("1.0", "end-1c")
        gui._log_translation(TranslationRequest("Thanks", 3, "相手", "row3", "12:30:03"), "ありがとう。")
        gui._log_translation(TranslationRequest("Hello", 1, "相手", "row1", "12:30:01"), "こんにちは。")
        self.assertEqual(gui.translation_area.get("1.0", "end-1c"),
            "[12:30:01] [相手] こんにちは。\n"
            "[12:30:02] [自分] 承知しました。\n"
            "[12:30:03] [相手] ありがとう。\n")
        self.assertEqual(gui.transcript_area.get("1.0", "end-1c"), source)

    def test_translation_failure_replaces_only_its_pending_row(self):
        gui = self.gui
        gui._log_transcript("Hello", "相手", 1, "en", "row1", "12:30:01", True)
        gui._log_transcript("日本語です。", "自分", 2, "ja", "row2", "12:30:02", False)
        request = TranslationRequest("Hello", 1, "相手", "row1", "12:30:01")
        gui._replace_translation_row(request, "翻訳できませんでした", "unavailable")
        displayed = gui.translation_area.get("1.0", "end-1c")
        self.assertNotIn("翻訳中", displayed)
        self.assertIn("[12:30:01] [相手] 翻訳できませんでした", displayed)
        self.assertIn("[12:30:02] [自分] 日本語です。", displayed)


if __name__ == "__main__":
    unittest.main()
