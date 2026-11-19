import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image, ImageTk

from gijiroku.window_preview import WindowPreview


class SnapshotFeedbackTests(unittest.TestCase):
    def test_indicator_fades_and_restarts_without_changing_capture(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(root.destroy)
        root.withdraw()
        preview = WindowPreview(root, lambda: None)
        captured = Image.new("RGB", (320, 180), "white")
        pixels = captured.tobytes()
        preview.latest_image = captured
        canvas = preview.canvas
        dot = preview._snapshot_item
        self.assertEqual(canvas.itemcget(dot, "state"), "hidden")

        # Drive animation frames without relying on the host's Tk timer resolution.
        with (patch.object(canvas, "winfo_width", return_value=320),
              patch.object(preview, "after", side_effect=["frame1", "frame2", "frame3"]) as schedule,
              patch.object(preview, "after_cancel") as cancel,
              patch("gijiroku.window_preview.time.monotonic", return_value=0) as clock):
            preview.flash_snapshot()
            self.assertEqual(canvas.itemcget(dot, "state"), "normal")
            self.assertEqual(canvas.itemcget(dot, "anchor"), "ne")
            x, y = canvas.coords(dot)
            self.assertTrue(300 <= x < 320 and 0 < y <= 20)
            color = ImageTk.getimage(preview._snapshot_photo).getpixel((6, 6))
            self.assertGreater(color[0], color[1])

            clock.return_value = 0.35
            schedule.call_args.args[1]()
            faded = ImageTk.getimage(preview._snapshot_photo).getpixel((6, 6))
            self.assertGreater(faded[3], 0)
            self.assertLess(faded[3], color[3])

            preview.flash_snapshot()
            cancel.assert_called_once_with("frame2")
            restarted = ImageTk.getimage(preview._snapshot_photo).getpixel((6, 6))
            self.assertEqual(restarted[3], color[3])
            self.assertIs(preview.latest_image, captured)
            self.assertEqual(captured.tobytes(), pixels)
            clock.return_value = 2
            schedule.call_args.args[1]()
            self.assertEqual(canvas.itemcget(dot, "state"), "hidden")
            self.assertIsNone(preview._snapshot_timer)
