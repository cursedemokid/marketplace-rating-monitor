"""Скачать закреплённую версию MiniLM до запуска изолированного сервера."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("models/minilm"))
    args = parser.parse_args()
    target = args.output.resolve()
    target.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=MODEL,
        revision=REVISION,
        local_dir=target,
        allow_patterns=["*.json", "*.safetensors", "*.model", "*.bpe"],
    )
    required = ("modules.json", "config.json", "model.safetensors", "tokenizer.json")
    missing = [name for name in required if not (target / name).is_file()]
    if missing:
        raise RuntimeError(f"MiniLM загружена не полностью: {', '.join(missing)}")
    sys.stdout.write(f"MiniLM {REVISION} подготовлена в {target}\n")


if __name__ == "__main__":
    main()
