import io
import queue
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from gijiroku.gui import MeetingRecorderGUI


class WhisperCaptionTests(unittest.TestCase):
    def run_recording(self, mode):
        gui = object.__new__(MeetingRecorderGUI)
        gui.TARGET_RATE = gui.TRANSCRIBE_RATE = 16000
        gui.TRANSCRIBE_CHUNK_SECONDS = 5
        gui.TRANSCRIBE_OVERLAP_SECONDS = 1
        gui.REALTIME_CAPTION_MODE = mode
        gui.is_recording = True
        gui.transcribe_queue = queue.Queue()
        for _ in range(8):
            gui.transcribe_queue.put(bytes(16000 * 4))
        gui._audio_finished = Mock()
        gui._audio_finished.is_set.side_effect = gui.transcribe_queue.empty
        gui._rt_lang = "en"
        gui._transcribe_start = 0
        gui.transcription_file = io.StringIO()
        gui.root = Mock()
        gui.root.after.side_effect = lambda delay, callback, *args: callback(*args)
        gui._show_partial_transcript = Mock()
        gui._publish_transcript = Mock()
        gui._log = Mock()
        gui.transcriber = Mock()

        def decode(clip, **kwargs):
            length = len(clip) // gui.TRANSCRIBE_RATE
            return iter([SimpleNamespace(text=f"{length} seconds", start=0)]), None

        gui.transcriber.transcribe.side_effect = decode
        gui._transcribe_loop()
        gui._log.assert_not_called()
        return gui

    def test_drafts_use_growing_audio_and_only_complete_chunks_are_saved(self):
        gui = self.run_recording("partial_only")
        drafts = [call.args[0] for call in gui._show_partial_transcript.call_args_list]
        self.assertEqual(drafts, ["2 seconds", "4 seconds", "3 seconds"])
        self.assertEqual([call.args[0] for call in gui._publish_transcript.call_args_list],
                         ["5 seconds", "4 seconds"])
        self.assertEqual(len(gui.transcription_file.getvalue().splitlines()), 2)

    def test_final_only_decodes_just_complete_chunks_and_the_tail(self):
        gui = self.run_recording("final_only")
        gui._show_partial_transcript.assert_not_called()
        self.assertEqual(gui.transcriber.transcribe.call_count, 2)
