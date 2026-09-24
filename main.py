"""Compatibility entry point for the GijirokuStudio GUI and CLI."""
import sys
import tkinter as tk
from gijiroku.gui import MeetingRecorderGUI
from gijiroku.postprocess.video import import_video_file
from gijiroku.postprocess.pipeline import post_process_folder

# ======================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="GijirokuStudio v2")
    parser.add_argument("--post-process", metavar="FOLDER",
        help="指定フォルダの音声を高精度文字起こしし、スクリーンショットと統合したMarkdownを出力")
    parser.add_argument("--postprocess-backend", choices=("fast_ja_en", "whisper"),
        help="後処理の文字起こし方式（未指定なら設定画面の選択を使用）")
    parser.add_argument("--import-video", metavar="VIDEO",
        help="動画から音声を抽出し、文字起こしと議事録を生成")
    parser.add_argument("--video-snapshots", action="store_true",
        help="動画入力時に画面変化を画像として保存し、議事録に表示")
    args = parser.parse_args()

    if args.import_video:
        try:
            imported_folder = import_video_file(
                args.import_video, capture_scenes=args.video_snapshots)
            post_process_folder(imported_folder, backend=args.postprocess_backend)
            print(f"  - 保存先: {imported_folder}")
        except Exception as e:
            print(f"[ERROR] {e}")
            sys.exit(1)
    elif args.post_process:
        try:
            post_process_folder(args.post_process, backend=args.postprocess_backend)
        except Exception as e:
            print(f"[ERROR] {e}")
            sys.exit(1)
    else:
        root = tk.Tk()
        app = MeetingRecorderGUI(root)

        def _on_close():
            if app.is_recording:
                app.is_recording = False
                app.stop_event.set()
                root.after(2000, root.destroy)
            else:
                root.destroy()

        root.protocol("WM_DELETE_WINDOW", _on_close)
        root.mainloop()
