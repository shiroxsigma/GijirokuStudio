"""Slide OCR and result caching."""
import json
import os
import re
import shutil
import subprocess
import tempfile
from ..settings import load_app_settings
from .data import _read_jsonl

# ----------------------------------------------------------------- Slide OCR

# Windows ships a Japanese OCR engine (Windows.Media.Ocr). Reaching it needs
# WinRT, and no WinRT binding installs on Python 3.13 — winsdk, which winocr
# depends on, has no 3.13 wheel — so we drive it through PowerShell instead.
# One process handles every image so the WinRT setup cost is paid once.
OCR_PS_SCRIPT = r"""
param([Parameter(Mandatory=$true)][string]$ListPath,
      [Parameter(Mandatory=$true)][string]$OutPath,
      [string]$Lang = 'ja')

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType=WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

function Await($task, $type) {
    $m = $asTaskGeneric.MakeGenericMethod($type)
    $t = $m.Invoke($null, @($task))
    $t.Wait(-1) | Out-Null
    $t.Result
}

$engine = $null
try {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
        [Windows.Globalization.Language]::new($Lang))
} catch {}
if ($null -eq $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}
if ($null -eq $engine) { Write-Error 'NO_OCR_ENGINE'; exit 2 }

$writer = New-Object System.IO.StreamWriter($OutPath, $false, (New-Object System.Text.UTF8Encoding($false)))
foreach ($path in [System.IO.File]::ReadAllLines($ListPath, [System.Text.Encoding]::UTF8)) {
    if ([string]::IsNullOrWhiteSpace($path)) { continue }
    $text = ''
    $err = ''
    try {
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $sb = New-Object System.Text.StringBuilder
        foreach ($line in $result.Lines) { [void]$sb.AppendLine($line.Text) }
        $text = $sb.ToString()
        $stream.Dispose()
    } catch {
        # Surfaced to the caller — a silently empty result looks like a blank
        # slide, which is a very different problem from an unreadable file.
        $err = $_.Exception.Message
    }
    $writer.WriteLine((@{ file = $path; text = $text; error = $err } | ConvertTo-Json -Compress))
}
$writer.Close()
"""

# The Windows engine emits one "word" per glyph for Japanese, so its output
# arrives as "売 上 高 : 1 , 240". Those spaces are segmentation artifacts, not
# slide content — but Latin words genuinely are space-separated, so the cleanup
# only closes gaps where at least one side is Japanese, plus digit-group and
# punctuation artifacts.
_CJK = r"぀-ヿ㐀-䶿一-鿿＀-￯"
_SP = r"[ 　]"
_OCR_FIXES = [
    (re.compile(f"(?<=[{_CJK}]){_SP}+(?=[{_CJK}])"), ""),          # 売 上 -> 売上
    (re.compile(f"(?<=[{_CJK}]){_SP}+(?=[0-9A-Za-z(（\\[])"), ""),  # 第 3 -> 第3
    (re.compile(f"(?<=[0-9A-Za-z)）\\]%％]){_SP}+(?=[{_CJK}])"), ""),  # 42 社 -> 42社
    (re.compile(rf"(?<=\d){_SP}*([,.]){_SP}*(?=\d)"), r"\1"),      # 1 , 240 -> 1,240
    (re.compile(f"{_SP}+(?=[,.:;%％)）\\]])"), ""),                  # 118 % -> 118%
    (re.compile(f"(?<=[(（\\[]){_SP}+"), ""),                       # ( 前年比 -> (前年比
    (re.compile(f"(?<=[:：]){_SP}+(?=[0-9{_CJK}])"), ""),           # 高: 1 -> 高:1
]


def tidy_ocr_text(text):
    """Clean OCR output: close glyph-level gaps and drop empty lines."""
    lines = []
    for line in (text or "").splitlines():
        for pattern, repl in _OCR_FIXES:
            line = pattern.sub(repl, line)
        line = line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _ocr_windows(paths, lang):
    """Run the Windows OCR engine over a batch of images. {path: text}."""
    if os.name != "nt":
        raise RuntimeError("Windows OCR は Windows 専用です")
    # StorageFile.GetFileFromPathAsync rejects forward slashes, and tkinter's
    # askdirectory() hands back exactly that on Windows.
    paths = [os.path.normpath(os.path.abspath(p)) for p in paths]
    tmpdir = tempfile.mkdtemp(prefix="gjs_ocr_")
    try:
        script = os.path.join(tmpdir, "ocr.ps1")
        listing = os.path.join(tmpdir, "images.txt")
        result = os.path.join(tmpdir, "out.jsonl")
        with open(script, "w", encoding="utf-8-sig") as f:
            f.write(OCR_PS_SCRIPT)
        with open(listing, "w", encoding="utf-8") as f:
            f.write("\n".join(paths))
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", script,
             "-ListPath", listing, "-OutPath", result, "-Lang", lang],
            capture_output=True, text=True, timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if not os.path.exists(result):
            raise RuntimeError((proc.stderr or "PowerShell 実行失敗").strip()[:200])
        out = {}
        with open(result, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("error"):
                    print(f"[OCR警告] {os.path.basename(row['file'])}: {row['error']}")
                out[row["file"]] = row.get("text", "")
        return out
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _ocr_rapidocr(paths, _lang):
    """Fallback engine. Its default model is Chinese, so kanji can come back as
    simplified variants — usable for numbers and Latin text, weaker on Japanese."""
    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR()
    out = {}
    for path in paths:
        try:
            result, _elapsed = engine(path)
            out[path] = "\n".join(item[1] for item in (result or []))
        except Exception as e:
            print(f"[OCR警告] {os.path.basename(path)}: {e}")
            out[path] = ""
    return out


OCR_BACKENDS = {"windows": _ocr_windows, "rapidocr": _ocr_rapidocr}


def ocr_images(data, report):
    """Extract slide text into data["images"][*]["ocr"], caching to ocr.jsonl.

    Runs in post-processing, never during recording — the CPU belongs to the
    meeting. Results are cached so re-running post-processing never redoes it.
    """
    cfg = load_app_settings()
    if not cfg.get("ocr_enabled", False):
        return 0
    folder = data["folder"]
    done = {e.get("file") for e in _read_jsonl(os.path.join(folder, "ocr.jsonl"))}
    todo = [e for e in data["images"] if e["file"] not in done]
    if not todo:
        return 0

    paths = [os.path.join(folder, e["file"]) for e in todo]
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return 0

    wanted = str(cfg.get("ocr_backend", "auto")).lower()
    order = ([wanted] if wanted in OCR_BACKENDS else ["windows", "rapidocr"])
    lang = str(cfg.get("ocr_lang", "ja"))

    results = None
    for name in order:
        try:
            report(None, f"スライドOCR ({name}): {len(paths)}枚")
            results = OCR_BACKENDS[name](paths, lang)
            break
        except Exception as e:
            report(None, f"[OCR] {name} 利用不可: {e}")
    if not results:
        report(None, "OCR をスキップしました")
        return 0

    by_name = {e["file"]: e for e in data["images"]}
    hits = 0
    with open(os.path.join(folder, "ocr.jsonl"), "a", encoding="utf-8") as f:
        for path, raw in results.items():
            fname = os.path.basename(path)
            text = tidy_ocr_text(raw)
            if fname in by_name:
                by_name[fname]["ocr"] = text
            f.write(json.dumps({"file": fname, "text": text},
                               ensure_ascii=False) + "\n")
            if text:
                hits += 1
    report(None, f"OCR 完了: {hits}/{len(paths)}枚から文字を抽出")
    return hits
