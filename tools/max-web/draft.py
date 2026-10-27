"""Черновик ответа той же моделью, что у Помогайки. Саму Помогайку не вызывает.

На stdin — JSON {"text": "..."}. На stdout — один JSON с черновиком.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pomogaika.services.copilot import classify_and_draft  # noqa: E402


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        json.dump({"ok": False, "error": "на stdin пришёл не JSON"}, sys.stdout, ensure_ascii=False)
        return
    text = str(payload.get("text") or "").strip()
    if not text:
        json.dump({"ok": False, "error": "пустой текст"}, sys.stdout, ensure_ascii=False)
        return
    try:
        result = classify_and_draft(text)
    except Exception as exc:  # noqa: BLE001 — наружу уходит текст ошибки, процесс Node жив
        json.dump({"ok": False, "error": str(exc)}, sys.stdout, ensure_ascii=False)
        return
    json.dump(
        {
            "ok": True,
            "question_class": result.question_class,
            "source": result.source,
            "matched_faq": result.matched_faq,
            "draft": result.draft,
        },
        sys.stdout,
        ensure_ascii=False,
    )


if __name__ == "__main__":
    main()
