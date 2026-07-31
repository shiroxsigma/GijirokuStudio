# 超軽量リモート会議記録システム 開発仕様書
### リソース最適化型 画面・音声キャプチャ及び文字起こしアーキテクチャ（v3）

| 項目 | 内容 |
| :--- | :--- |
| **対象環境** | Windows 10 / 11 |
| **作成日** | 2026年7月6日 |
| **設計方針** | faster-whisper 統一・語彙バイアス・多デバイス対応 |
| **前バージョン** | v2（2026-05-31）— moonshine-voice ベース |
| **著者** | システム開発担当 |

---

## 1. システム概要
本システムは、リモート会議（Microsoft Teams, Zoom 等）の内容をバックグラウンドで高効率に記録する Windows 向けデスクトップアプリケーションである。音声の常時 MP3 録音、5秒間隔での画面変更検知（dHash）によるスライド自動キャプチャ、そして **faster-whisper** による文字起こし（リアルタイム／後処理）を統合し、画像と発言を時系列に並べた議事録（Markdown）を生成する。すべてローカルで動作し、会議音声が外部に送信されることはない。

### 1.1 実装形態
- **GUIアプリケーション**: Python `tkinter` ベースのデスクトップGUI（620×860px、リサイズ不可）
- **CLIモード**: `--post-process FOLDER` 引数によるバッチ後処理（音声の高精度文字起こし ＋ Markdown 生成）

### 1.2 v2（旧仕様）からの主な変更点
- 文字起こしエンジンを **moonshine-voice → faster-whisper** に統一（リアルタイム・後処理とも）。後処理は faster-whisper 未導入時のみ moonshine-voice にフォールバック
- **用語辞書**（`glossary.csv`）による語彙バイアス（`initial_prompt`）と事後補正（別名→正規形置換）を追加
- **自動言語検出**（日本語 / 英語）モードを追加
- 音声ソース選択を 3択 → **任意の複数デバイス同時選択（N入力ミキシング）** に拡張
- リアルタイム文字起こしを **5秒チャンク ＋ 1秒オーバーラップ方式** に変更（旧: 1秒リングバッファ）
- `language_segments.jsonl` / `snapshots.jsonl` の構造化ログを追加
- `whisper_*` / `realtime_whisper_model` 設定を追加（settings.json で管理、read-modify-write 永続化）
- ウィンドウサイズを 620×750 → **620×860** に拡張、会議名機能を追加

---

## 2. 基本要件及び制約条件

- **音声取得**
  `ffmpeg` は音声キャプチャには使用せず、**PCMストリームのMP3エンコード専用**として利用する。音声キャプチャ自体は以下のPythonライブラリで行う：
  - **スピーカー音声**: `pyaudiowpatch` ライブラリを用いて WASAPI ループバックデバイスから直接キャプチャ
  - **マイク音声**: `sounddevice` ライブラリを用いてデフォルト入力デバイスからキャプチャ
  - キャプチャした PCM データは `subprocess.PIPE` 経由で ffmpeg に送信し、リアルタイム MP3 エンコードを行う（Captura パターン）

- **音声ソース選択（v2 → 拡張）**
  GUI 上で複数のオーディオデバイスを同時選択可能（Listbox、拡張選択モード）。スピーカー・マイクを問わず任意の組み合わせ・任意の数を選択できる。選択デバイス数に応じて動作が切り替わる：
  - **1デバイス**: ミキシング不要（`_single_writer` が ffmpeg に直接書き込み）
  - **2デバイス以上**: N入力ミキシング（`_mixer_loop_n`）してから ffmpeg に送信
  - いずれの経路でも、高負荷モード時は同時に文字起こしキューへ転送

- **ミキシング処理**
  ffmpeg の `amix` フィルターではなく、Python/numpy によるソフトウェアミキシングを実装。1024フレーム（4096バイト）の固定サイズチャンク単位で各ソースのバッファから読み出し、float32 加算 → ライブソース数で平均化 → int16 クリッピングして出力する。片方のソースが停止・遅延した場合のオーバーフロー対策（~1.2秒バッファ上限で単独ソースを出力）を実装。

- **画面キャプチャ**
  対象は「全画面（Virtual Screen）」をデフォルトとするが、**GUI上で各ディスプレイを個別に選択可能**。

- **キャプチャ間隔**: 5秒に1回（タイマードリフト補正付き定周期実行）

- **画像保存形式**: JPEG形式（画質は設定ダイアログで 50〜100、既定 85）

- **手動キャプチャ機能**: 録音中に「📷 手動キャプチャ」ボタンで任意タイミングのスナップショットを保存（自動検知とは別枠で `manual_` プレフィックスで保存）

- **入力レベルメーター**: GUI 上にリアルタイム音声レベルメーター（80ms 更新、緑<0.6 / 黄<0.85 / 赤）。チェックボックスで表示切替可

- **省電力・スリープ**: OS標準設定に従う（介入なし）

---

## 3. 動作モード仕様

### 3.1 低負荷モード（録音のみ）
会議中の CPU プレッシャーを最小化するモード。
- **会議中:** 音声のストリーム録音（MP3 エンコード・ファイル書き出し）と、5秒間隔の画面 dHash 比較・変更検知時の JPEG 保存のみを実行。文字起こし処理は一切行わない。

### 3.2 高負荷モード（リアルタイム文字起こし）
会議進行と並行して、テキスト化された議論をリアルタイムに確認するモード。
- **会議中:** 音声を常時録音しつつ、`faster-whisper`（既定モデル `base`）を用いてチャンクベースのストリーミング文字起こしを非同期実行。音声 PCM をトランスクリプションキューに転送し、5秒分蓄積後に `transcribe()` で文字起こし、結果を GUI と `transcription.txt` に出力。
- **モデル事前読み込み**: 高負荷モード選択時（または言語変更時）にバックグラウンドスレッドで `base` モデルをロードし、録音開始までに初期化を完了。

---

## 4. 技術スタック及びコンポーネント選定

| コンポーネント | 選定技術 | 理由・特徴 |
| :--- | :--- | :--- |
| **音声キャプチャ（スピーカー）** | **pyaudiowpatch** | WASAPI ループバックデバイスに直接アクセス可能な PyAudio 拡張 |
| **音声キャプチャ（マイク）** | **sounddevice** | PortAudio の薄いラッパー。デフォルトマイク検出・キャプチャがシンプル |
| **音声エンコード** | **ffmpeg (CLI)** | stdin の PCM (s16le, 44100Hz, stereo) を `libmp3lame` で 192kbps MP3 にリアルタイムエンコード |
| **PCM正規化・リサンプル** | **NumPy + numpy.interp** | キャプチャ時のチャンネル数/サンプルレート差異を Python 内で吸収。ステレオ s16le 44100Hz に統一 |
| **画面キャプチャ** | **mss** | OS のグラフィック API を直接呼び出す軽量ライブラリ |
| **差分検知** | **ImageHash (dHash)** | 9×8 グレースケールの 64bit ハッシュ。閾値は設定可変（1〜30、既定 10） |
| **文字起こし（リアルタイム・後処理）** | **faster-whisper** | Whisper 系・語彙バイアス（initial_prompt）対応・VAD 対応。リアルタイムは `base`、後処理は `large-v3-turbo`（既定） |
| **リアルタイムリサンプル** | **scipy.signal.resample** | 44100Hz → 16000Hz へのチャンクダウンサンプル |
| **フォールバック（後処理のみ）** | **moonshine-voice** | faster-whisper 未導入時に後処理のみフォールバック |

---

## 5. 詳細設計及びアルゴリズム

### 5.1 音声キャプチャ・ミキシング・エンコード アーキテクチャ

#### 5.1.1 デバイス列挙（`_enumerate_audio_devices`）
統一的なデバイスリストを生成。各エントリ: `{key, kind, native_idx, name, channels, rate}`
- **スピーカー**: `pyaudiowpatch` の `get_loopback_device_info_generator()` で WASAPI loopback を列挙。`key = "S<native_idx>"`
- **マイク**: `sounddevice.query_devices()` で入力デバイスを列挙。ループバック重複は名前で除外。`key = "M<native_idx>"`
- GUI の Listbox で `🔊`（スピーカー）/ `🎤`（マイク）プレフィックス付きで表示

#### 5.1.2 キャプチャ層（`_capture_device`）
選択された各デバイスごとに専用スレッド・専用キュー（maxsize=200）を起動。
- **スピーカー**（`_capture_speaker_dev`）: `pyaudio` の `stream_callback` で非同期取得 → `_to_stereo_s16le()` 正規化 → キュー投入
- **マイク**（`_capture_mic_dev`）: `sd.InputStream` のコールバックで取得 → 正規化 → キュー投入
- 終了時: `_EOF` センチネルをキューに投入し、ミキサー/ライターに終端を通知（キュー満杯時は古い PCM を破棄して再試行）

#### 5.1.3 PCM正規化処理（`_to_stereo_s16le`）
- モノラル → ステレオ（チャンネル複製）
- 3ch 以上 → ステレオ（最初の 2チャンネルのみ抽出）
- サンプルレート変換: `numpy.interp` による線形補間で 44100Hz へリサンプル
- `AUDIO_GAIN` 乗算 → int16 クリッピング（-32768〜32767）

#### 5.1.4 単一ソース書き込み（`_single_writer`）
デバイス 1 つ選択時はミキシングせず、キャプチャキューから直接読み出して ffmpeg に書き込み、同時に文字起こしキューへ転送。レベルメーター更新もここで行う。

#### 5.1.5 N入力ミキシング（`_mixer_loop_n`）
デバイス 2 つ以上選択時に動作。任意の N 入力に一般化。
- 各入力キューからデータを `bytearray` バッファに蓄積
- `MIX_FRAMES = 1024`（`MIX_BYTES = 4096` バイト）固定チャンク単位で **ライブ（EOF 未到達）ソース** のバッファから読み出し
- float32 加算 → `1.0 / len(live)` で平均化（ソース数が増えても振幅 ≤1.0 を維持） → int16 クリッピング
- ライブソース全てが十分なデータを持つ間だけ整列ミックスを実行
- **オーバーフロー対策**（`OVERFLOW = MIX_BYTES * 50` ≒ 1.2秒）: あるソースが上限を超え他が遅れている場合、そのソース単独を出力
- **終了条件**: 停止要求かつ全ソース EOF。EOF 到達ソースはサイレント扱いし、残りライブソースで mixing を継続
- 終了時に残余のフレーム整合データ（`FRAME_BYTES` 境界）をフラッシュ

#### 5.1.6 ffmpegエンコード（`_ffmpeg_start`）
```
ffmpeg -f s16le -acodec pcm_s16le -ar 44100 -ac 2 -i - -c:a libmp3lame -b:a 192k -y output.mp3
```
Windows では `subprocess.CREATE_NO_WINDOW` でコンソールウィンドウを非表示。

### 5.2 タイマードリフト（累積遅延）排除ロジック
キャプチャ処理時間が毎回累積してズレるのを防ぐため、「次回実行すべき絶対時刻」を基準に逆算ウェイトする自動補正タイマーを実装。
```python
INTERVAL = 5.0
next_target = time.time() + INTERVAL
while recording:
    dt = next_target - time.time()
    if dt > 0:
        time.sleep(dt)
    execute_capture_and_diff()
    next_target += INTERVAL
```

### 5.3 dHash（Difference Hash）による画面変化判定
1. キャプチャ画像を 9×8 ピクセルに縮小、グレースケール化
2. 隣接ピクセルの輝度比較で 64bit ハッシュ生成
3. 前回ハッシュ `H_prev` とのハミング距離 `D_H` を計算
4. **判定基準:** `D_H > DHASH_THRESHOLD`（設定ダイアログで 1〜30 可変、既定 10）で「変化あり」とみなし JPEG 保存。閾値を小さくするほど小さな変化も検知、大きくするほど大きな変化のみ検知。録画開始時に値が確定し、録画中は変更不可。

---

## 6. 文字起こし仕様

### 6.1 使用エンジン: faster-whisper
リアルタイム・後処理とも **faster-whisper** を使用。モデルを用途別に分離：
- **リアルタイム**: `realtime_whisper_model`（既定 `base`、低遅延優先）
- **後処理**: `whisper_model`（既定 `large-v3-turbo`、精度優先）

### 6.2 対応言語
| 表示名 | 言語コード |
| :--- | :--- |
| 自動（日本語/英語） | `auto` |
| 日本語 | `ja` |
| English | `en` |
| 中文 | `zh` |
| 한국어 | `ko` |

**録音中の動的切替に対応**。`auto` 以外は単一モデルで多言語対応のため、言語切替時のモデル再読み込みは不要（`_rt_lang` を更新するのみ）。

### 6.3 リアルタイム文字起こし（高負荷モード / `_transcribe_loop`）
- **入力**: `transcribe_queue`（maxsize=400）の 44100Hz stereo int16 PCM
- **前処理**: モノラル化 → `scipy.signal.resample` で 16000Hz にダウンサンプル
- **チャンク化**: 5秒（`TRANSCRIBE_CHUNK_SECONDS = 5.0`）蓄積で `transcribe()` に供給
- **オーバーラップ**: チャンク境界で単語が切れないよう、末尾 1秒（`TRANSCRIBE_OVERLAP_SECONDS = 1.0`）を次チャンクに持ち越し
- **言語決定**: `_rt_lang is None`（`auto`）なら `_transcribe_chunk` 内で `detect_ja_en()` を呼び ja/en を判定。言語変化時に `language_segments.jsonl` へイベント記録
- **transcribe オプション**: `vad_filter=True`, `beam_size=1`
- **出力**: 確定テキストを GUI（`_log_transcript`）と `transcription.txt`（`[経過秒] テキスト`）に書き込み・flush
- **バックプレッシャー**（`_drop_backlog`）: キューの qsize が 200 を超えたら古いデータを破棄（`_queue_overflow_count` に計上）し、リアルタイム追従を維持
- **終了時フラッシュ**: 残バッファが 0.5秒分以上あれば最終チャンクを文字起こし

### 6.4 バッチ後処理（`post_process_folder`）
GUI の「📄 議事録を生成（後処理）」ボタン、または CLI `--post-process FOLDER` で起動。録画前後を問わず任意フォルダを指定可能。

処理フロー:
1. **音声ファイル特定**: `audio_main.mp3`（なければ `audio_main.wav`）
2. **メタデータ読込**: `metadata.txt`（会議名・開始時刻・AUDIO_SOURCE・言語 など）
3. **言語セグメント読込**: `language_segments.jsonl`（録音中の言語切替記録）
4. **画像エントリ読込**: `snapshots.jsonl` を優先（なければファイル名からタイムスタンプ解析でフォールバック）
5. **音声デコード**: ffmpeg で 16kHz mono WAV に変換 → NumPy float32 配列に読込
6. **用語辞書ロード**: `load_glossary()` → `build_whisper_prompt()` で `initial_prompt` 生成
7. **文字起こし**:
   - **auto モード**: `iter_speech_windows()` で VAD ベースの発話区間（最大25秒）に分割 → 各ウィンドウで `detect_ja_en()` で言語判定 → `transcribe()`。録音時の lang_segments は無視して各区間独立に検出
   - **固定言語モード**: lang_segments があれば各区間をその言語で、なければ全体を `default_lang` で `transcribe`
   - いずれも `initial_prompt`（語彙バイアス）・`vad_filter=True`・`beam_size=5`（faster-whisper 時）
8. **事後補正**: `apply_glossary()` で別名 → 正規形に最長一致置換
9. **Markdown生成**: 発言と画像を開始時刻で時系列マージし `meeting_report.md` を出力
10. **フォールバック**: faster-whisper 未導入時は moonshine-voice で文字起こし（auto は `ja` 扱い、語彙バイアスなし）

### 6.5 言語検出（`detect_ja_en`）
- `model.detect_language()` の確率を ja/en で比較し大きい方を採用
- 古い faster-whisper で `detect_language` がない場合は `transcribe(language=None)` の `info.language` で代替（`en` 以外は `ja` に丸め）
- 完全失敗時は `ja` をデフォルト

### 6.6 VAD 発話ウィンドウ（`iter_speech_windows`）
- faster-whisper 同梱の Silero VAD（`get_speech_timestamps`）で発話区間を取得
- 連続する発話区間を最大 25秒（`window_s`）のウィンドウに貪欲にグループ化
- 失敗時・無発話時は全体を固定 25秒ウィンドウに分割
- 0.5秒未満のウィンドウは除外

---

## 7. 用語辞書仕様（`glossary.csv`）

固有名詞・専門用語・人名の誤認識を正しい表記に寄せる辞書。プロジェクト直下の `glossary.csv` を編集。

```csv
正規形,読み,別名1,別名2
開発定例会議,カイハツテイレカイギ,開発定例会,
プロダクト戦略室,プロダクトセンリャクシツ,プロダクト線略室,プロダクト戦略しつ
山田太郎,ヤマダタロウ,,
```

- **1行 ＝ 1用語**: 1列目=正規形（議事録に残したい表記）、2列目=読み、3列目以降=別名（誤認識されやすい表記）
- `#` で始まる行・空行・ヘッダ行（`正規形`/`form`/`用語`/`term`/`word`/`name`）はスキップ
- **正規形 → `initial_prompt`**: `build_whisper_prompt()` が最大 80用語 / 400文字 でプロンプトを生成し、認識時にバイアスをかける
- **別名 → 正規形**: `apply_glossary()` が最長一致の完全一致置換を後処理で実行
- UTF-8（BOM 許容: `utf-8-sig`）で保存
- 辞書がない・空の場合は通常の文字起こしとして動作（エラーなし）

---

## 8. 成果物およびファイル管理仕様
すべてのデータは会議ごとの専用ディレクトリ（`YYYYMMDD_HHMMSS_<会議名>`、会議名空欄時は `_Meeting`）内に格納。

### 8.1 ファイル命名規則
| 種別 | ファイル名 | 備考 |
| :--- | :--- | :--- |
| 音声ファイル | `audio_main.mp3` | 192kbps MP3 |
| 自動スナップショット | `snapshot_HHMMSS_mmm.jpg` | ミリ秒付き（例: `snapshot_130520_123.jpg`） |
| 手動スナップショット | `manual_HHMMSS_mmm.jpg` | 手動キャプチャボタンで保存 |
| 会議メタデータ | `metadata.txt` | 構造化キーバリュー（後述） |
| スナップショットログ | `snapshots.jsonl` | ★キャプチャのタイミング・種別・差分 |
| 言語セグメントログ | `language_segments.jsonl` | ★言語切替のタイミング |
| 文字起こしファイル | `transcription.txt` | 高負荷モード時のみ。`[経過秒] テキスト` |
| 議事録Markdown | `meeting_report.md` | 後処理で生成 |

### 8.2 メタデータ形式（`metadata.txt`）
```
MEETING_NAME=<会議名>
START_TIME_EPOCH=<開始Epoch>
START_TIME_STR=YYYY-MM-DD HH:MM:SS
AUDIO_SOURCE=<後述>
MODE=light|full
SNAPSHOT_COUNT=<自動キャプチャ（スライド変化検知）の枚数>
AUDIO_FILE=audio_main.mp3
LANGUAGE=<言語コード または auto>
TRANSCRIPTION_FILE=transcription.txt   ← 高負荷モード時のみ
```
> ※ `SNAPSHOT_COUNT` は自動検知によるキャプチャ数のみ。手動キャプチャは含まない（`snapshots.jsonl` には両方記録される）。

### 8.3 AUDIO_SOURCE 文字列（`_audio_source_label_n`）
メタデータ互換性のため、代表的な組み合わせは従来値を維持：
- スピーカー1 / マイク0 → `speaker_loopback`
- スピーカー0 / マイク1 → `microphone`
- スピーカー1 / マイク1 → `both_mixed`
- それ以外（複数同時選択）→ `speaker_loopback x{n} + microphone x{m}` の可読形式

### 8.4 構造化ログ
**`snapshots.jsonl`**（1行1キャプチャ）:
```json
{"file": "snapshot_130520_123.jpg", "epoch": 1748300000.123, "elapsed": 12.345, "type": "auto", "diff": 14}
{"file": "manual_130522_045.jpg", "epoch": 1748300022.045, "elapsed": 34.045, "type": "manual", "diff": null}
```

**`language_segments.jsonl`**（1行1切替）:
```json
{"elapsed": 0.0, "lang": "ja", "label": "日本語"}
{"elapsed": 45.2, "lang": "en", "label": "English"}
```

### 8.5 タイムスタンプ紐付けの考え方
文字起こしテキストの会議開始からの経過秒 `S_rel` と、画像エントリの `elapsed`（開始からの経過秒）を直接比較して時系列マージする。両者とも「会議開始からの経過秒」で統一されているため、`T_target = T_start + S_rel` により発言と画像を完全に同期できる。

---

## 9. GUI仕様

### 9.1 ウィンドウ構成
- **タイトル**: GijirokuStudio v2 - リモート会議記録システム
- **サイズ**: 620×860px（リサイズ不可）
- **フレーム構成**: システム概要 / 録画設定 / レベルメーター / コントロール / 動作ログ / 文字起こし

### 9.2 録画設定項目
| 項目 | UI形式 | 備考 |
| :--- | :--- | :--- |
| 会議名 | Entry | フォルダ名・メタデータ・議事録タイトルに反映（空欄可） |
| 対象画面 | コンボボックス（読取専用） | 全画面 / 各ディスプレイ（自動検出） |
| 音声デバイス | Listbox（拡張選択） | 複数選択可（Ctrl/Shift+クリック、スピーカー・マイク混在可）。「🔄 デバイス再検出」付き |
| 動作モード | コンボボックス | 軽量（録音のみ） / 高負荷（リアルタイム文字起こし） |
| 文字起こし言語 | コンボボックス | 自動(ja/en) / 日本語 / English / 中文 / 한국어。**録音中も切替可** |

### 9.3 メニューバー
| メニュー | 項目 | 機能 |
| :--- | :--- | :--- |
| 設定 | 設定を開く... | モーダル設定ダイアログを表示 |
| 設定 | 設定をリセット | dHash/ゲイン/JPEG を既定値に戻す |

### 9.4 設定ダイアログ
| 項目 | ウィジェット | 範囲 | ステップ | デフォルト |
| :--- | :--- | :--- | :--- | :--- |
| 差分感度（dHash閾値） | Scale | 1〜30 | 1 | 10 |
| 音声ゲイン | Scale | 1.0〜5.0 | 0.1 | 2.0 |
| JPEG画質 | Scale | 50〜100 | 1 | 85 |

> `whisper_model` / `whisper_device` / `whisper_compute` / `realtime_whisper_model` は GUI から編集不可。`settings.json` を直接編集する。

### 9.5 設定ファイル永続化（`settings.json`）
```json
{
  "dhash_threshold": 10,
  "audio_gain": 2.0,
  "jpeg_quality": 85,
  "whisper_model": "large-v3-turbo",
  "whisper_device": "cpu",
  "whisper_compute": "int8",
  "realtime_whisper_model": "base"
}
```
- **保存**: read-modify-write 方式（手動追加キーも保持）。設定ダイアログで「OK」時、または「設定をリセット」時
- **読込**: アプリ起動時（GUI: `_load_settings`）・後処理時（`load_app_settings` 共用）
- ファイル不存在・破損時は既定値を使用

### 9.6 操作ボタン
| ボタン | 機能 | 状態制御 |
| :--- | :--- | :--- |
| ▶ 会議記録を開始 | 録音開始 | 録音中は「■ 記録を停止して保存」に切替 |
| 📷 手動キャプチャ | 現在画面を JPEG 保存 | 録音中のみ有効 |
| 📄 議事録を生成（後処理） | フォルダ選択 → 後処理（`_postprocess_worker`） | 常に有効 |

### 9.7 ステータス表示
- 停止中: 「ステータス: 停止中」（灰色）
- 録音中: 「ステータス: 記録中 HH:MM:SS」（赤色、500ms 更新）
- 保存処理中: 「ステータス: 保存処理中...」（黄色）
- 議事録生成中: 「ステータス: 議事録生成中...」（黄色）

### 9.8 非同期終了処理（`_async_cleanup`）
録音停止時、UI は即座に反応しバックグラウンドスレッドで以下を実行：
1. パイプラインスレッド完了待ち（10秒タイムアウト）
2. 全キャプチャスレッド停止（3秒タイムアウト）・ミキサー/ライター join
3. ffmpeg stdin クローズ → MP3 エンコード完了待ち（10秒タイムアウト）
4. 文字起こし停止（`transcription.txt` クローズ）
5. `metadata.txt` 書き出し
6. 完了メッセージボックス表示 → UI リセット

---

## 10. 仕様変更履歴（v2 → v3）

| # | 変更箇所 | v2（旧） | v3（新） |
| :--- | :--- | :--- | :--- |
| 1 | リアルタイム文字起こしエンジン | moonshine-voice | faster-whisper（`base`） |
| 2 | 後処理文字起こしエンジン | moonshine-voice | faster-whisper（`large-v3-turbo`）。未導入時のみ moonshine フォールバック |
| 3 | リアルタイム方式 | 1秒リングバッファ | 5秒チャンク ＋ 1秒オーバーラップ ＋ scipy リサンプル |
| 4 | 音声ソース選択 | 3択（スピーカー/マイク/両方） | 任意の複数デバイス（N入力ミキシング） |
| 5 | ミキシング | 2入力固定 | N入力に一般化・平均ミックス（振幅 ≤1.0 を維持） |
| 6 | 言語モード | ja/en/zh/ko | ＋ 自動（ja/en）検出モード |
| 7 | 用語辞書 | なし | glossary.csv（`initial_prompt` ＋ 別名置換） |
| 8 | スナップショットログ | なし | `snapshots.jsonl`（種別・差分付き） |
| 9 | 言語セグメントログ | なし | `language_segments.jsonl` |
| 10 | 後処理言語処理 | 単一パス | auto=VAD区間ごと検出 / 固定=セグメントごと指定 |
| 11 | VAD | なし | Silero VAD（リアルタイム・後処理とも） |
| 12 | リアルタイムバックプレッシャー | なし | キュー溢れ検知で古いデータ破棄（`_drop_backlog`） |
| 13 | 設定項目 | dHash/ゲイン/JPEG | ＋ `whisper_model`/`device`/`compute`/`realtime_whisper_model` |
| 14 | 設定永続化 | settings.json（3項目・全上書き） | read-modify-write（7項目 ＋ 手動キー保持） |
| 15 | ウィンドウサイズ | 620×750 | 620×860 |
| 16 | 会議名 | なし | フォルダ名・メタデータ・議事録タイトルへ反映 |
| 17 | 後処理Markdown | 画像＋発言 | メタデータテーブル（言語・検出言語・音声ソース等）追加 |
| 18 | `ctypes` import | あり | 削除 |
