# 超軽量リモート会議記録システム 変更仕様書（v3 → v4）

| 項目 | 内容 |
| :--- | :--- |
| **対象環境** | Windows 10 / 11 |
| **作成日** | 2026年7月31日 |
| **設計方針** | 話者分離・AI要約・共有可能な成果物 |
| **前バージョン** | v3（2026-07-06）— faster-whisper 統一・語彙バイアス |

本書は v3 からの差分のみを記述する。v3 で規定した録音・キャプチャ・文字起こしの基本アーキテクチャは維持されている。

---

## 1. 変更サマリ

| # | 領域 | v3 | v4 |
| :--- | :--- | :--- | :--- |
| 1 | 音声キャプチャの安定性 | 複数デバイス選択で破綻 | COM初期化・飢餓検出・デバイス個別スキップで安定化 |
| 2 | 話者 | ミックスのみ（話者不明） | マイク=自分 / スピーカー=相手 の役割別トラック |
| 3 | 後処理の構造 | 単一関数 | 収集 → 文字起こし → OCR → 要約 → 描画 に分割 |
| 4 | 後処理のUI | 完了までログなし | 進捗バー・フェーズ表示・中断 |
| 5 | 重要箇所 | なし | ⭐ マーカー（グローバルホットキー対応） |
| 6 | スライド | 画像のみ | Windows内蔵OCRでテキスト抽出 |
| 7 | 要約 | なし | Ollama / Claude API による構造化要約 |
| 8 | 出力形式 | Markdown | ＋ 単一HTML（音声シーク・検索）、DOCX |
| 9 | 過去記録の操作 | フォルダ選択ダイアログ | 記録一覧ブラウザ（検索・再後処理） |

---

## 2. 音声キャプチャの安定化

### 2.1 COM のスレッド別初期化（`_com_initialize`）

WASAPI は COM ベースであり、COM のアパートメントはスレッド単位である。PortAudio は**最初に `Pa_Initialize` を呼んだスレッドでのみ** COM を初期化し、以降の呼び出しは参照カウントの空振りとなる。

このため v3 では、2台目以降のキャプチャスレッドで `p.open()` は成功するが `Pa_StartStream` が `-9999 (Unanticipated host error)` で失敗していた。

**v4:** 各キャプチャスレッドの先頭で `ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)` を実行し、ストリームのクローズ後に `CoUninitialize` する。`S_OK`/`S_FALSE` を返した場合のみ解放責任を持つ。

### 2.2 飢餓ソースの非ブロック化（`_mixer_loop_n`）

v3 のミキサーは「**全**ライブソースが `MIX_BYTES` 以上を持つまで」待機していた。Windows は再生していないループバックエンドポイントに対してコールバックを一切呼ばないため、無音のスピーカーを選択するとミキシングが永久に停止し、バッファが無限に膨張していた。

**v4:** ソースごとに最終受信時刻を保持し、`STARVE_SECONDS`（0.5秒）データが来ないソースは「無音」と見なして待機対象から外す。

- 混合はその時点で `MIX_BYTES` を満たすソースのみで行う
- 平均化の分母は**寄与したソース数**（全ライブ数ではない）
- バッファ上限 `BUF_CAP`（約9秒）を超えたら古いデータを破棄
- 停止要求から3秒経過しても EOF が揃わない場合は待たずに終了

### 2.3 デバイスの個別スキップ

- `_format_candidates` により native → 2ch/48k/44.1k の順でフォーマットを試行
- 開けないデバイスはログに出して**そのデバイスのみ除外**し、残りで録音を継続
- PortAudio の `open` は `_open_lock` で直列化（スレッド安全でないため）
- オーディオコールバック内の例外を遮断（C コールバックに例外が抜けるとプロセスが落ちる）

### 2.4 タイムライン保全

全ライブソースが飢餓状態のとき、経過時間との差分だけ無音を挿入する。音声長が実時間より短くなると、スナップショットの `elapsed` と発言時刻が食い違い議事録が崩れるため。

### 2.5 デバイス一覧の整理

- 同一マイクが MME / DirectSound / WDM-KS / WASAPI で重複列挙されるため、正規化名（先頭30文字）でグループ化し WASAPI > WDM-KS > DirectSound > MME の優先順で1つに集約
- 「Microsoft サウンド マッパー」「プライマリ サウンド キャプチャ ドライバー」等の仮想入力は除外（既定マイクの多重取得になるため）
- 「🎚 すべて選択」ボタンを追加

---

## 3. 話者分離

### 3.1 役割の定義

| 役割 | ソース | ファイル |
| :--- | :--- | :--- |
| 自分 | マイク（`kind == "mic"`） | `audio_self.mp3` |
| 相手 | スピーカーループバック | `audio_other.mp3` |

**マイク1台以上かつスピーカー1台以上**を選択した場合のみ有効。1デバイスのみの場合は分離する対象が存在しないため無効。

### 3.2 不変条件（最重要）

> **`audio_main` に N バイト出力するたび、各ロールトラックにも必ず N バイト出力する。**
> そのロールの寄与がない場合は同量の無音を書く。

ミキサーの4つの出力経路すべてでこれを維持する:

1. 整列ミックス — ロール別にサブミックスし、寄与のないロールには無音
2. オーバーフロー時の単独出力 — 出したソースのロールに実データ、他は無音
3. 飢餓時の無音パディング — 全ストリームに無音
4. 終端フラッシュ — 同上

破ると3ファイルの時刻がずれ、話者タグ付きの発言が誤った時刻にマージされる。

### 3.3 エンコード

ロールトラックは文字起こし専用のため 16kHz mono 64kbps。入力は `audio_main` と同じ 44.1kHz ステレオ PCM を与え、ffmpeg 側でダウンサンプルする（同一バイト数＝同一時間長を保証するため）。

### 3.4 後処理

- ロールトラックが存在する場合、**ミックスは文字起こししない**（3重に同じ発話を処理する意味がない）
- 各トラックを `iter_speech_windows()` で発話区間に絞ってから文字起こし。ロールトラックは大半が無音のため処理量は増えない
- 各行に `speaker` を付与し、開始時刻でマージ。同時刻は `相手 → 自分` の固定順で安定ソート

### 3.5 既知の限界

スピーカー出力（非ヘッドセット）使用時、マイクが相手の音声を拾い `audio_self.mp3` に混入する。同じ発言が両話者に現れる可能性がある。ヘッドセットの使用を推奨する。

---

## 4. 後処理の再構成

### 4.1 関数分割

```
post_process_folder(folder, progress=None, cancel=None)
  ├ collect_meeting_data(folder)     → 中間データ dict
  ├ transcribe_meeting(data, report)
  ├ ocr_images(data, report)
  ├ summarize_meeting(data, report)
  └ render_markdown / render_html / render_docx
```

**すべてのレンダラは同一の dict から描画する。** ある出力形式が別の出力形式をパースする設計は取らない。

### 4.2 中間データ構造

```python
{
  "folder", "meta", "meeting_name", "start_time_str", "audio_source",
  "language", "auto_mode", "audio_file", "audio_name",
  "role_tracks": {"自分": path, "相手": path},
  "lang_segments": [...], "images": [{file, time, type, ocr}],
  "markers": [{elapsed, epoch, label}],
  "duration", "detected_langs", "engine",
  "lines": [{start, text, speaker}],
  "summary": {...} | None,
}
```

### 4.3 進捗と中断

- `progress(fraction | None, message)` コールバック。CLI は引数なしで従来動作
- `cancel()` は発話区間の境界と faster-whisper のセグメント消費ごとに判定。数秒で反応する
- `PostProcessCancelled` を送出。GUI 側で捕捉して「中断」表示
- v3 の `sys.stdout` 差し替えを廃止（プロセスグローバルのため、録音スレッドの print まで横取りしていた）

### 4.4 文字起こしの統一

全経路で VAD 発話区間ベースに統一。無音を投げなくなり幻覚が減る（既存録音での比較: 24行/2079字 → 27行/2224字）。

---

## 5. マーカー（`markers.jsonl`）

```json
{"elapsed": 1234.5, "epoch": 1785431808.077, "label": "重要"}
```

- ⭐ ボタン（録音中のみ有効）とグローバルホットキー（既定 `ctrl+shift+m`）
- 会議中は Teams/Zoom が前面のため tkinter の `bind` では届かない。Win32 `RegisterHotKey` を専用スレッドで登録し、同一スレッドでメッセージポンプを回す（登録スレッドのメッセージキューに配送されるため）
- 他アプリが同じ組み合わせを使用中の場合はログに出して録音は継続
- 書き込みはロックで保護（ホットキースレッドとUIスレッドの双方から呼ばれる）
- 議事録では引用ブロックとして表示。AI要約には ±60秒の発言を「重要」として渡す

---

## 6. スライドOCR（`ocr.jsonl`）

### 6.1 エンジン選定

`winocr` は依存する `winsdk` に Python 3.13 用 wheel が存在せず使用不可。代わりに `Windows.Media.Ocr`（WinRT）を **PowerShell 経由**で呼ぶ。追加インストール不要で OS の日本語 OCR をそのまま利用できる。

フォールバックは `rapidocr-onnxruntime`。ただし既定モデルが中国語であり漢字が簡体字に化ける。

同一スライドでの実測比較:

| エンジン | 出力 |
| :--- | :--- |
| Windows | `2026年度第3四半期売上報告` / `売上高:1,240百万円(前年比118%)` / `・営業利益:186百万円` |
| RapidOCR | `2026年度第3四半期壳上報告` / `·壳上高：1,240百万（前年比118%)` / `·當業利益：186百万` |

### 6.2 実装上の要点

- 全画像を1つの PowerShell プロセスで処理し、WinRT の初期化コストを1回に抑える（1枚あたり約0.5秒）
- **`StorageFile.GetFileFromPathAsync` はバックスラッシュ区切りのみ受け付ける。** tkinter の `askdirectory()` はスラッシュ区切りを返すため、`os.path.normpath(os.path.abspath(...))` で正規化する
- Windows OCR は日本語をグリフ単位で分割するため `売 上 高 : 1 , 240` と返る。片側が日本語の空白、数値区切り、括弧・記号まわりのみ詰め、英単語間の空白は保持する
- 画像単位の失敗は理由をログに出す（無言の空文字は「白紙のスライド」と区別できない）
- 結果は `ocr.jsonl` にキャッシュ。空文字も記録するため、読めない画像を毎回試し直さない
- 実行は後処理フェーズのみ（録画中のCPUは会議のために空ける）

---

## 7. AI要約

### 7.1 出力契約

```json
{
  "summary": "string",
  "decisions": ["string"],
  "action_items": [{"task": "string", "owner": "string|null", "due": "string|null"}],
  "open_issues": ["string"]
}
```

- Ollama: `/api/chat` の `format: "json"`
- Claude API: `output_config.format` の `json_schema`

### 7.2 パースのフォールバック階層

1. `json.loads`
2. ` ```json ` フェンス内の抽出
3. 裸の中括弧の抽出
4. 失敗時は原文を `raw` として保持し、「AI要約（未整形）」節に掲載

さらに緩い型（`decisions` が文字列、`action_items` が文字列配列など）を矯正する。

### 7.3 プロバイダ別の方針

| | Ollama | Claude API |
| :--- | :--- | :--- |
| 分割 | map-reduce（既定6000字・行境界） | なし（1時間の会議でも1リクエストに収まる） |
| モデル | `qwen3`（既定） | `claude-opus-5`（既定） |
| タイムアウト | 900秒（urllib は既定タイムアウトなし） | SDK 既定 |
| 拒否 | — | `stop_reason == "refusal"` を判定 |
| フォールバック | — | サーバサイド `fallbacks: "default"` を既定で有効化。未対応環境では自動で再試行 |

### 7.4 失敗時の扱い

**要約の失敗で後処理全体を落とさない。** 文字起こしと議事録が成果物であり、要約は付加物である。例外を捕捉してログに出し、`summary` を `None` のままレポートを生成する。

### 7.5 外部送信の明示

`summary_provider="claude"` は文字起こし全文を外部に送信する。設定ダイアログと実行ログの双方に明示する。既定は `none`。

---

## 8. 出力形式

### 8.1 単一HTML（`meeting_report.html`）

- 画像は base64 埋め込み（単体で共有可能）
- **音声は既定で相対パス参照。** 1時間の 192kbps MP3 は base64 で約115MB になりブラウザが扱えない。`html_embed_audio=true` のときのみ 64kbps mono に再エンコードして埋め込む（1時間で約37MB）
- 時刻クリックで `audio.currentTime` をシークして再生
- 発言のインクリメンタル検索とハイライト（外部ライブラリ非依存）
- 出力は全て `html.escape`。会議名・発言・OCR・要約に HTML が混ざっても文字として表示される
- ライト/ダーク両対応、印刷時はプレイヤーの固定を解除

### 8.2 DOCX（`meeting_report.docx`）

`python-docx`（任意依存）。`export_docx=true` のときのみ出力。音声リンクとタイムスタンプのシークは持てないため、HTML の簡易版という位置づけ。

---

## 9. 記録一覧ブラウザ

- アプリ直下と年月フォルダ（`20260727` 等）を走査し、`audio_main` を持つフォルダを列挙
- 開始時刻・会議名・画像数・マーカー数・話者分離の有無・議事録の生成状況を表示
- 検索は会議名と開始時刻。「議事録の本文も検索」で `meeting_report.md` / `transcription.txt` の全文検索に切り替え
- 選択して再後処理（既存の進捗・中断UIを経由）、Markdown / HTML を開く、フォルダを開く
- 走査はウィンドウを開いた時のみ（起動を遅くしないため）

---

## 10. metadata.txt の追加項目

```
MARKER_COUNT=<マーカー数>
ROLE_TRACKS=自分,相手          ← 話者分離時のみ
AUDIO_SELF_FILE=audio_self.mp3
AUDIO_OTHER_FILE=audio_other.mp3
```

---

## 11. settings.json の追加項目

```json
{
  "marker_hotkey": "ctrl+shift+m",
  "summary_provider": "none",
  "summary_model": "",
  "ollama_url": "http://localhost:11434",
  "summary_chunk_chars": 6000,
  "anthropic_api_key": "",
  "ocr_enabled": true,
  "ocr_backend": "auto",
  "ocr_lang": "ja",
  "export_html": true,
  "export_docx": false,
  "html_embed_images": true,
  "html_embed_audio": false
}
```

---

## 12. 後方互換性

- ロールトラックを持たない既存フォルダは従来どおり単一トラックで処理する
- `snapshots.jsonl` を持たないフォルダはファイル名からのタイムスタンプ解析にフォールバックする（v3 と同じ）
- ICレコーダー等から取り込んだ `audio_main.mp3` ＋ `metadata.txt` のみのフォルダも従来どおり処理できる
- CLI `--post-process` は引数なしで従来と同じ動作
