"""Japanese punctuation restoration for the optional fast ASR backend."""
from __future__ import annotations

import os
import unicodedata

import numpy as np


_PUNCT = set("。、！？!?…「」『』（）()【】・,.\n")
_QUESTIONS = ("ですか", "ますか", "でしょうか", "かな", "かしら", "かい",
              "だろうか", "でしたか", "ましたか")


class JapanesePunctuator:
    def __init__(self, model_dir: str, threads: int = 4):
        import onnxruntime as ort

        model = os.path.join(model_dir, "punct_bert.onnx")
        vocab_path = os.path.join(model_dir, "vocab.txt")
        if not os.path.isfile(model) or not os.path.isfile(vocab_path):
            raise RuntimeError("日本語句読点モデルがありません。setup_fast_asr.py を実行してください")
        with open(vocab_path, encoding="utf-8") as f:
            self.vocab = {line.rstrip("\n"): i for i, line in enumerate(f)}
        self.unk = self.vocab["[UNK]"]
        self.cls = self.vocab["[CLS]"]
        self.sep = self.vocab["[SEP]"]
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(threads))
        self.session = ort.InferenceSession(
            model, sess_options=opts, providers=["CPUExecutionProvider"])

    def restore(self, text: str) -> str:
        text = text.strip()
        if not text:
            return text
        chars = list(unicodedata.normalize("NFKC", text))[:500]
        ids = [self.cls] + [self.vocab.get(c, self.unk) for c in chars] + [self.sep]
        arr = np.asarray([ids], dtype=np.int64)
        mask = np.ones_like(arr)
        logits = self.session.run(["logits"], {
            "input_ids": arr, "attention_mask": mask})[0][0]
        probs = 1.0 / (1.0 + np.exp(-logits))
        out = []
        for i, ch in enumerate(chars):
            out.append(ch)
            nxt = chars[i + 1] if i + 1 < len(chars) else ""
            if ch in _PUNCT or nxt in _PUNCT:
                continue
            comma, period = probs[i + 1]
            if period >= 0.5:
                out.append("。")
            elif comma >= 0.5:
                out.append("、")
        result = "".join(out)
        if result and result[-1] not in _PUNCT:
            result += "。"
        parts = []
        for sentence in result.split("。"):
            if sentence:
                parts.append(sentence + ("？" if sentence.endswith(_QUESTIONS) else "。"))
        return "".join(parts)
