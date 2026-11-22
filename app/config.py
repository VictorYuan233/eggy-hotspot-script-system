"""读取配置：设定库、规则库、数据源授权清单、提示词。"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PROMPT_DIR = ROOT / "prompts"
DATA_DIR = ROOT / "data"


def _load(name: str) -> dict:
    return json.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))


@lru_cache
def rules() -> dict:
    return _load("rules.json")


@lru_cache
def game_setting() -> dict:
    return _load("game_setting.json")


@lru_cache
def sources() -> dict:
    return _load("sources.json")


@lru_cache
def prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def render(template_name: str, **values: str) -> str:
    """用 {name} 占位符填充提示词。不用 str.format，因为模板里有 JSON 花括号。"""
    text = prompt(template_name)
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text
