import io
import json
from pathlib import Path
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from gijiroku.asr.fast import FastASREvent
from gijiroku.gui import CAPTION_MODES, MeetingRecorderGUI


class CaptionModeDisplayTests(unittest.TestCase):
    def setUp(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(root.destroy)
        root.withdraw()
        self.gui = gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        gui.root = root
        gui.transcript_area = tk.Text(root)
        gui.translation_area = tk.Text(root)
        gui.TRANSCRIBE_RATE = 16000
        gui._transcript_row_counter = 0
        gui._record_start = gui._transcribe_start = 0
        gui._rt_lang = "en"
        gui.TRANSLATION_ENABLED = True
        gui._translation_worker = Mock()
        gui._fast_final_segments = []
        gui._fast_refined_segments = []
        gui.transcription_file = io.StringIO()
        gui.refined_transcription_file = io.StringIO()

    def check_mode(self, mode):
        gui = self.gui
        gui.REALTIME_CAPTION_MODE = mode
        gui._record_fast_results([FastASREvent("partial", "Draft", "en")])
        gui.root.update()
        self.assertEqual("Draft" in gui.transcript_area.get("1.0", "end"), mode != "final_only")

        gui._record_fast_results([
            FastASREvent("final", "Finished", "en", 0, 16000),
            FastASREvent("refine", "Corrected", "en", 0, 16000),
        ])
        gui.root.update()
        original = gui.transcript_area.get("1.0", "end")
        self.assertEqual("Finished" in original, mode != "partial_only")
        self.assertEqual("Corrected" in original, mode == "both")
        self.assertIn("Finished", gui.transcription_file.getvalue())
        self.assertIn("Corrected", gui.refined_transcription_file.getvalue())
        self.assertNotIn("Draft", gui.transcription_file.getvalue())
        self.assertEqual([e.text for e in gui._fast_final_segments], ["Finished"])
        self.assertEqual([e.text for e in gui._fast_refined_segments], ["Corrected"])
        gui._translation_worker.submit.assert_called_once()
        self.assertEqual(gui._translation_worker.submit.call_args.args[:2], ("Finished", "en"))
        self.assertIn("翻訳中", gui.translation_area.get("1.0", "end"))

    def test_partial_only(self):
        self.check_mode("partial_only")

    def test_final_only(self):
        self.check_mode("final_only")

    def test_both(self):
        self.check_mode("both")

    def test_selecting_final_only_clears_drafts_and_restores_selection_on_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            with (patch("gijiroku.gui.SETTINGS_PATH", str(path)),
                  patch.object(MeetingRecorderGUI, "_enumerate_audio_devices"),
                  patch.object(MeetingRecorderGUI, "_populate_audio_listbox"),
                  patch.object(MeetingRecorderGUI, "_detect_monitors"),
                  patch.object(MeetingRecorderGUI, "_tick_level_meter")):
                root = self.gui.root
                gui = MeetingRecorderGUI(root)
                gui._show_partial_transcript("Unfinished")
                index = next(i for i, (_, mode) in enumerate(CAPTION_MODES) if mode == "final_only")
                gui.combo_caption.current(index)
                gui.combo_caption.event_generate("<<ComboboxSelected>>")
                root.update()
                self.assertNotIn("Unfinished", gui.transcript_area.get("1.0", "end"))
                self.assertEqual(json.loads(path.read_text("utf-8"))["realtime_caption_mode"],
                                 "final_only")
                reopened_root = tk.Toplevel(root)
                reopened = MeetingRecorderGUI(reopened_root)
                self.assertEqual(reopened.combo_caption.current(), index)


class CaptionModeBacklogTests(unittest.TestCase):
    def test_explicit_modes_remain_in_effect_as_backlog_changes(self):
        for mode in ("partial_only", "final_only"):
            with self.subTest(mode=mode):
                gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
                gui.root = Mock()
                gui.REALTIME_CAPTION_MODE = mode
                gui._asr_load_level = 0
                gui._asr_backlog_check = None
                queues = {"self": SimpleNamespace(pending_seconds=0)}
                session = SimpleNamespace(partials_enabled=True, defer_refinement=False)
                with patch("gijiroku.gui.time.monotonic", side_effect=[0, 1, 2, 3]):
                    for lag in (0, 1.5, 4, 0):
                        queues["self"].pending_seconds = lag
                        gui._update_asr_load(queues, {"self": session})
                        self.assertEqual(session.partials_enabled, mode == "partial_only")
