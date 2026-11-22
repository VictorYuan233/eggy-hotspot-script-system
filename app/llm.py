"""大模型调用层。

- AnthropicLLM：真实调用 Claude API（需要环境变量 ANTHROPIC_API_KEY）。
  生成和评审默认用不同的模型，避免模型给自己的稿子打高分。
- ReplayLLM：离线回放预先生成的输出，用于演示和测试，保证结果可复现。

所有调用都要求模型只输出 JSON，并在这里统一解析。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Protocol

from .config import DATA_DIR


class LLMError(RuntimeError):
    pass


def parse_json(text: str) -> dict:
    """从模型输出里取出 JSON。兼容被 ``` 包裹或前后有说明文字的情况。"""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match:
            raise LLMError(f"模型输出不是 JSON：{text[:200]}")
        return json.loads(match.group(0))


class LLM(Protocol):
    def complete_json(self, task: str, key: str, prompt: str, role: str = "default") -> dict: ...


class AnthropicLLM:
    MODELS = {
        "default": os.environ.get("EGGY_MODEL", "claude-sonnet-5-5"),
        "generator": os.environ.get("EGGY_GEN_MODEL", "claude-sonnet-5-5"),
        "judge": os.environ.get("EGGY_JUDGE_MODEL", "claude-haiku-5-5"),
    }

    def __init__(self) -> None:
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:
            raise LLMError("请先 pip install anthropic") from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMError("缺少环境变量 ANTHROPIC_API_KEY")
        self.client = anthropic.Anthropic()

    def complete_json(self, task: str, key: str, prompt: str, role: str = "default") -> dict:
        model = self.MODELS.get(role, self.MODELS["default"])
        msg = self.client.messages.create(
            model=model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
        return parse_json(text)


class ReplayLLM:
    """按 (task, key) 回放预先生成的输出。找不到时报错，避免静默编造。"""

    def __init__(self, path: Path | None = None):
        path = path or DATA_DIR / "demo_llm_outputs.json"
        self.outputs: dict = json.loads(Path(path).read_text(encoding="utf-8"))["outputs"]
        self.calls: list[tuple[str, str, str]] = []

    def complete_json(self, task: str, key: str, prompt: str, role: str = "default") -> dict:
        self.calls.append((task, key, role))
        try:
            return json.loads(json.dumps(self.outputs[task][key]))  # 深拷贝
        except KeyError as exc:
            raise LLMError(f"回放数据里没有 {task}/{key}") from exc
