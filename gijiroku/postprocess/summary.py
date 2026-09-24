"""Optional AI meeting summary."""
import json
import re
import traceback
import urllib.request
from .common import PostProcessCancelled
from .render import fmt_timestamp
from ..settings import load_app_settings

# ------------------------------------------------------------------ AI summary

# The contract every provider must satisfy. Both back-ends are told to emit
# exactly this, so the report renderer never has to guess at free-form prose.
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "owner": {"type": ["string", "null"]},
                    "due": {"type": ["string", "null"]},
                },
                "required": ["task", "owner", "due"],
                "additionalProperties": False,
            },
        },
        "open_issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "decisions", "action_items", "open_issues"],
    "additionalProperties": False,
}

SUMMARY_SYSTEM = """あなたは会議の議事録作成者です。文字起こしから事実だけを抽出します。

- 推測で補わない。文字起こしにない内容は書かない
- 決定事項は「決まったこと」だけ。検討中の案は未決事項へ
- アクションアイテムは担当者と期限を文字起こしから読み取る。不明なら null
- 話者が「自分」「相手」と付いている場合、担当者の判断に使う
- ⭐ が付いた箇所は発言者が重要と判断した部分なので優先的に拾う
- 出力は日本語"""

SUMMARY_INSTRUCTION = """次の会議の文字起こしから、サマリ・決定事項・アクション\
アイテム・未決事項を抽出してください。"""


def transcript_for_summary(data, max_chars=None):
    """Flatten the transcript for an LLM, marking bookmarked moments with ⭐."""
    marks = [m.get("elapsed", 0.0) for m in data.get("markers", [])]
    out = []
    for line in data["lines"]:
        start = line["start"]
        star = "⭐ " if any(abs(start - m) <= 60.0 for m in marks) else ""
        speaker = f"{line['speaker']}: " if line.get("speaker") else ""
        out.append(f"[{fmt_timestamp(start)}] {star}{speaker}{line['text']}")
    text = "\n".join(out)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars]
    return text


def _parse_summary_json(text):
    """Parse a model's reply into the summary dict, degrading rather than failing.

    Tries strict JSON, then a fenced ```json block, then gives up and returns the
    raw text so the report can still show something.
    """
    if not text:
        return None
    try:
        return _coerce_summary(json.loads(text))
    except (json.JSONDecodeError, TypeError):
        pass
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if not match:
        match = re.search(r"(\{.*\})", text, re.S)
    if match:
        try:
            return _coerce_summary(json.loads(match.group(1)))
        except (json.JSONDecodeError, TypeError):
            pass
    print("[要約警告] JSON として読めなかったため原文を掲載します")
    return {"summary": "", "decisions": [], "action_items": [],
            "open_issues": [], "raw": text}


def _coerce_summary(obj):
    """Normalize a parsed object to the schema, tolerating loose model output."""
    if not isinstance(obj, dict):
        raise TypeError("summary must be an object")
    items = []
    for item in obj.get("action_items") or []:
        if isinstance(item, dict):
            items.append({"task": str(item.get("task", "")).strip(),
                          "owner": item.get("owner") or None,
                          "due": item.get("due") or None})
        elif isinstance(item, str):
            items.append({"task": item, "owner": None, "due": None})
    as_list = lambda v: [str(x) for x in v] if isinstance(v, list) else []
    return {
        "summary": str(obj.get("summary") or "").strip(),
        "decisions": as_list(obj.get("decisions")),
        "action_items": [i for i in items if i["task"]],
        "open_issues": as_list(obj.get("open_issues")),
    }


def _merge_summaries(parts):
    """Concatenate map-stage results, de-duplicating while preserving order."""
    merged = {"summary": "", "decisions": [], "action_items": [], "open_issues": []}
    seen = {"decisions": set(), "open_issues": set(), "action_items": set()}
    texts = []
    for part in parts:
        if not part:
            continue
        if part.get("summary"):
            texts.append(part["summary"])
        for key in ("decisions", "open_issues"):
            for value in part.get(key, []):
                if value not in seen[key]:
                    seen[key].add(value)
                    merged[key].append(value)
        for item in part.get("action_items", []):
            key = (item["task"], item.get("owner"))
            if key not in seen["action_items"]:
                seen["action_items"].add(key)
                merged["action_items"].append(item)
    merged["summary"] = "\n\n".join(texts)
    return merged


def _chunk_transcript(text, max_chars):
    """Split on line boundaries so an utterance is never cut in half."""
    chunks, current = [], []
    size = 0
    for line in text.splitlines():
        if size + len(line) + 1 > max_chars and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _summarize_claude(transcript, cfg, report):
    """One request to the Claude API — a meeting fits in the context window."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic パッケージが未導入です（pipenv install anthropic）")

    model = cfg.get("summary_model") or "claude-opus-5"
    api_key = cfg.get("anthropic_api_key") or None
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    report(None, f"要約を生成中（Claude API / {model}）— 文字起こしを外部送信します")

    params = dict(
        model=model,
        max_tokens=32000,
        system=SUMMARY_SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
        messages=[{"role": "user",
                   "content": f"{SUMMARY_INSTRUCTION}\n\n<transcript>\n{transcript}\n</transcript>"}],
    )

    def _run(with_fallback):
        # Claude Opus 5's safety classifiers can decline a request; the
        # server-side fallback re-runs it on another model in the same call.
        if with_fallback:
            with client.beta.messages.stream(
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default", **params) as stream:
                return stream.get_final_message()
        with client.messages.stream(**params) as stream:
            return stream.get_final_message()

    try:
        message = _run(True)
    except anthropic.BadRequestError as e:
        # Older account/SDK without the fallback beta — proceed without it.
        print(f"[要約] フォールバック無しで再試行: {e}")
        message = _run(False)

    if message.stop_reason == "refusal":
        raise RuntimeError("安全性判定により要約が拒否されました")
    text = next((b.text for b in message.content if b.type == "text"), "")
    return _parse_summary_json(text)


def _ollama_chat(url, model, system, user, report):
    """One Ollama /api/chat call with JSON mode forced."""
    payload = json.dumps({
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/api/chat", data=payload,
        headers={"Content-Type": "application/json"})
    # No default timeout on urllib, and a CPU-hosted model takes minutes.
    with urllib.request.urlopen(request, timeout=900) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body.get("message", {}).get("content", "")


def _summarize_ollama(transcript, cfg, report):
    """Map-reduce over the transcript — local models have small context windows."""
    url = cfg.get("ollama_url") or "http://localhost:11434"
    model = cfg.get("summary_model") or "qwen3"
    chunks = _chunk_transcript(transcript, int(cfg.get("summary_chunk_chars", 6000)))
    report(None, f"要約を生成中（Ollama / {model}） 分割 {len(chunks)}件")

    schema_hint = ('必ず次の JSON だけを返してください: '
                   '{"summary": "...", "decisions": ["..."], '
                   '"action_items": [{"task": "...", "owner": null, "due": null}], '
                   '"open_issues": ["..."]}')
    parts = []
    for i, chunk in enumerate(chunks, 1):
        report(None, f"要約 {i}/{len(chunks)}")
        reply = _ollama_chat(url, model, SUMMARY_SYSTEM,
                             f"{SUMMARY_INSTRUCTION}\n{schema_hint}\n\n"
                             f"（会議の一部 {i}/{len(chunks)}）\n{chunk}", report)
        parts.append(_parse_summary_json(reply))

    merged = _merge_summaries(parts)
    if len(chunks) == 1:
        return merged
    # Reduce: fold the per-chunk extracts into one coherent set.
    report(None, "要約を統合中")
    reply = _ollama_chat(url, model, SUMMARY_SYSTEM,
                         f"次は同じ会議を分割して抽出した結果です。重複を除いて"
                         f"1つにまとめてください。\n{schema_hint}\n\n"
                         f"{json.dumps(merged, ensure_ascii=False, indent=1)}", report)
    return _parse_summary_json(reply) or merged


def summarize_meeting(data, report):
    """Attach an AI summary to the meeting data, or leave it None.

    A summarization failure never fails post-processing — the transcript and
    report are the deliverable; the summary is an addition to them.
    """
    cfg = load_app_settings()
    provider = str(cfg.get("summary_provider", "none")).lower()
    if provider in ("", "none", "off"):
        return None
    if not data["lines"]:
        report(None, "発言がないため要約をスキップ")
        return None

    transcript = transcript_for_summary(data)
    try:
        if provider == "claude":
            summary = _summarize_claude(transcript, cfg, report)
        elif provider == "ollama":
            summary = _summarize_ollama(transcript, cfg, report)
        else:
            report(None, f"[要約] 未知のプロバイダ: {provider}")
            return None
    except PostProcessCancelled:
        raise
    except Exception as e:
        report(None, f"[要約エラー] {e}（議事録の生成は続行します）")
        traceback.print_exc()
        return None

    if summary:
        summary["provider"] = provider
        data["summary"] = summary
        report(None, f"要約完了: 決定事項{len(summary.get('decisions', []))}件 "
                     f"/ アクション{len(summary.get('action_items', []))}件")
    return summary
