"""Orchestrate transcription, OCR, summary, and report export."""
import os
import traceback
from .common import _Reporter, PostProcessCancelled
from .data import collect_meeting_data
from ..settings import load_app_settings
from .transcribe import transcribe_meeting
from .ocr import ocr_images
from .summary import summarize_meeting
from .render import render_markdown, render_html, render_docx

def post_process_folder(folder, progress=None, cancel=None, backend=None):
    """Transcribe a meeting folder and generate the report files.

    progress: optional callable(fraction | None, message) for UI feedback.
              Post-processing a one-hour meeting takes tens of minutes on CPU,
              so every phase reports where it is.
    cancel:   optional callable() -> bool, polled at each checkpoint. When it
              returns True, PostProcessCancelled is raised.
    """
    report = _Reporter(progress, cancel)
    report(0.0, f"後処理開始: {os.path.basename(folder)}")
    data = collect_meeting_data(folder)
    report(0.02, f"音声: {data['audio_name']} / 画像: {len(data['images'])}枚")

    selected_backend = backend or load_app_settings().get("postprocess_backend", "whisper")
    report(None, f"後処理ASR: {selected_backend}")
    transcribe_meeting(data, report, span=(0.05, 0.80), backend=selected_backend)

    report(0.82, "スライドOCR")
    try:
        ocr_images(data, report)
    except PostProcessCancelled:
        raise
    except Exception as e:
        report(None, f"[OCRエラー] {e}")

    report(0.88, "要約フェーズ")
    summarize_meeting(data, report)
    report(0.95, "議事録を出力中")

    md_path = os.path.join(folder, "meeting_report.md")
    render_markdown(data, md_path)

    cfg = load_app_settings()
    if cfg.get("export_html", True):
        try:
            html_path = os.path.join(folder, "meeting_report.html")
            _, size_mb = render_html(
                data, html_path,
                embed_audio=bool(cfg.get("html_embed_audio", False)),
                embed_images=bool(cfg.get("html_embed_images", True)))
            report(0.98, f"HTML を出力しました ({size_mb:.1f}MB)")
        except Exception as e:
            report(None, f"[HTML出力エラー] {e}")
            traceback.print_exc()
    if cfg.get("export_docx", False):
        try:
            render_docx(data, os.path.join(folder, "meeting_report.docx"))
            report(0.99, "DOCX を出力しました")
        except Exception as e:
            report(None, f"[DOCX出力エラー] {e}")

    report(1.0, f"議事録を出力しました: {os.path.basename(md_path)}")
    print(f"  - 発言 {len(data['lines'])}行 / 画像 {len(data['images'])}枚")
    return md_path
