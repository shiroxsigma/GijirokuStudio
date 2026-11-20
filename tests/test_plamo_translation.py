from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import setup_translation
from gijiroku.gui import MeetingRecorderGUI
from gijiroku.translation import create_translation_model, plamo_ollama_tag


MODELS = {
    "plamo-3-translate-0.8b": "hf.co/pfnet/plamo-3-translate-0.8b-GGUF:Q4_K_M",
    "plamo-3-translate-2b": "hf.co/pfnet/plamo-3-translate-2b-GGUF:Q4_K_M",
}


class PlamoSetupTests(unittest.TestCase):
    def test_both_models_pull_the_selected_quantization_to_local_ollama(self):
        for name, tag in MODELS.items():
            with (self.subTest(name=name),
                  patch.object(sys, "argv", ["setup_translation.py", "--model", name]),
                  patch.dict(os.environ, {"OLLAMA_HOST": "http://remote.invalid:11434"}),
                  patch("setup_translation.subprocess.run") as run,
                  redirect_stdout(io.StringIO())):
                setup_translation.main()
                run.assert_called_once()
                self.assertEqual(run.call_args.args, (["ollama", "pull", tag],))
                self.assertTrue(run.call_args.kwargs["check"])
                self.assertEqual(run.call_args.kwargs["env"]["OLLAMA_HOST"], "http://127.0.0.1:11434")
                self.assertEqual(plamo_ollama_tag(name), tag)

    def test_missing_ollama_or_failed_pull_aborts_setup(self):
        name = "plamo-3-translate-2b"
        for error in (FileNotFoundError(), subprocess.CalledProcessError(1, "ollama")):
            with (self.subTest(error=error),
                  patch.object(sys, "argv", ["setup_translation.py", "--model", name]),
                  patch("setup_translation.subprocess.run", side_effect=error),
                  redirect_stderr(io.StringIO()) as stderr,
                  self.assertRaises(SystemExit) as raised):
                setup_translation.main()
            self.assertEqual(raised.exception.code, 1)
            self.assertIn("Ollama", stderr.getvalue())


class PlamoTranslationTests(unittest.TestCase):
    def test_recording_uses_selected_local_model_and_saves_its_translation(self):
        gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        gui.root = Mock()
        gui._translation_worker = None
        gui._translation_model = None
        gui._translation_model_key = None
        gui.TRANSLATION_ENABLED = True
        gui.TRANSLATION_THREADS = 2
        for name, tag in MODELS.items():
            with (self.subTest(name=name), tempfile.TemporaryDirectory() as folder,
                  patch("gijiroku.translation.http.client.HTTPConnection") as connect):
                connection = connect.return_value
                response = connection.getresponse.return_value
                response.status = 200
                response.read.return_value = json.dumps({"message": {"content": "訳文"}}).encode()
                gui.TRANSLATION_MODEL = name
                gui._start_translation(folder)
                try:
                    connect.assert_not_called()
                    self.assertTrue(gui._translation_worker.submit("Good morning.", "en", speaker="相手"))
                finally:
                    gui._stop_translation()
                connect.assert_called_once()
                self.assertEqual(connect.call_args.args, ("127.0.0.1", 11434))
                request = connection.request.call_args
                self.assertEqual(request.args[:2], ("POST", "/api/chat"))
                payload = json.loads(request.kwargs["body"])
                self.assertEqual(payload["model"], tag)
                self.assertFalse(payload["stream"])
                self.assertEqual(payload["messages"], [
                    {"role": "system", "content": "[plamo-translate]\ntarget_lang=ja\nsource_lang=en"},
                    {"role": "user", "content": "Good morning."},
                ])
                connection.close.assert_called_once()
                row = json.loads((Path(folder) / "translation_en_ja.jsonl").read_text("utf-8"))
                self.assertEqual((row["model"], row["translation"], row["speaker"]),
                                 (name, "訳文", "相手"))

    def test_missing_model_names_the_tag_to_install(self):
        name = "plamo-3-translate-0.8b"
        with patch("gijiroku.translation.http.client.HTTPConnection") as connect:
            response = connect.return_value.getresponse.return_value
            response.status = 404
            response.read.return_value = b'{"error":"model not found"}'
            with self.assertRaises(RuntimeError) as raised:
                create_translation_model(name).translate("Hello")
        self.assertIn(f"ollama pull {MODELS[name]}", str(raised.exception))

    def test_unknown_model_rejected_before_opening_connection(self):
        with patch("gijiroku.translation.http.client.HTTPConnection") as connect:
            with self.assertRaises(ValueError):
                create_translation_model("unknown-model")
            connect.assert_not_called()
