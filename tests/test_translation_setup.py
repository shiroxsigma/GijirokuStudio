import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import setup_translation


class TranslationSetupTests(unittest.TestCase):
    def check_isolation_and_cleanup(self, fail):
        environments = []
        commands = []

        def create(environment):
            environment.mkdir(parents=True)
            (environment / "temporary-dependency").write_text("converter")
            environments.append(environment)

        def run(command):
            commands.append(command)
            self.assertNotEqual(Path(command[0]), Path(sys.executable))
            self.assertTrue(Path(command[0]).is_relative_to(environments[0]))
            if fail and "--_convert" in command:
                raise subprocess.CalledProcessError(1, command)

        with tempfile.TemporaryDirectory() as output:
            with patch.object(setup_translation.venv.EnvBuilder, "create", side_effect=create), \
                    patch.object(setup_translation.subprocess, "check_call", side_effect=run):
                if fail:
                    with self.assertRaises(subprocess.CalledProcessError):
                        setup_translation.convert_in_temporary_environment("elan-tiny", Path(output))
                else:
                    setup_translation.convert_in_temporary_environment("elan-tiny", Path(output))
        self.assertEqual(len(commands), 3)
        self.assertFalse(environments[0].parent.exists())

    def test_conversion_dependencies_are_isolated_and_removed(self):
        self.check_isolation_and_cleanup(False)

    def test_failed_conversion_also_removes_temporary_environment(self):
        self.check_isolation_and_cleanup(True)
