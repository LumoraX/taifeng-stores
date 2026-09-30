"""文档不能悄悄过期：站内链接要指得到东西，接口参考要覆盖公开接口与迁移命令。"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

import taifeng_stores
from taifeng_stores import postgres, redis
from taifeng_stores.postgres import PgvectorMemoryStore
from taifeng_stores.postgres.cli import build_parser
from taifeng_stores.postgres.migrate import COMPONENTS

REPO = Path(__file__).resolve().parents[1]

_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.MULTILINE)
_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def _documents() -> list[Path]:
    """仓库里的全部 Markdown 文档。"""
    return [REPO / "README.md", REPO / "AGENTS.md", *sorted((REPO / "docs").rglob("*.md"))]


def _anchors(path: Path) -> set[str]:
    """一份文档里各级标题对应的锚点（与代码托管平台的生成规则一致）。"""
    text = _FENCE.sub("", path.read_text(encoding="utf-8"))
    anchors: set[str] = set()
    for heading in _HEADING.findall(text):
        plain = re.sub(r"[`*]", "", heading).strip().lower()
        anchors.add(re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", plain)))
    return anchors


def _broken_links(path: Path) -> list[str]:
    """一份文档里指不到东西的站内链接。"""
    broken: list[str] = []
    text = _FENCE.sub("", path.read_text(encoding="utf-8"))
    for target in _LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        file_part, _, anchor = target.partition("#")
        resolved = (path.parent / file_part).resolve() if file_part else path
        if not resolved.exists():
            broken.append(f"{target}（文件不存在）")
        elif anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
            broken.append(f"{target}（没有这个标题）")
    return broken


def _missing(names: set[str], document: str) -> list[str]:
    """哪些名字没有作为完整的词出现在文档里。"""
    return sorted(
        name
        for name in names
        if not re.search(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_])", document)
    )


def _reference() -> str:
    """接口参考的全文。"""
    return (REPO / "docs" / "architecture" / "reference.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", _documents(), ids=lambda path: str(path.relative_to(REPO)))
def test_document_links_resolve(path: Path) -> None:
    """文档里的相对链接都指向存在的文件与标题。"""
    assert _broken_links(path) == []


def test_reference_covers_public_names() -> None:
    """接口参考覆盖各模块公开导出的全部名字。"""
    names: set[str] = set()
    for module in (taifeng_stores, postgres, redis):
        names |= {name for name in module.__all__ if not name.startswith("__")}
    assert {"SqlNamespace", "PgvectorMemoryStore", "RedisThreadDirectory"} <= names
    assert _missing(names, _reference()) == []


def test_reference_covers_memory_store_parameters() -> None:
    """接口参考覆盖向量记忆的全部构造参数。"""
    parameters = set(inspect.signature(PgvectorMemoryStore.__init__).parameters) - {"self", "pool"}
    assert {"dimensions", "scope", "min_similarity"} <= parameters
    assert _missing(parameters, _reference()) == []


def test_reference_covers_migrate_command() -> None:
    """接口参考覆盖迁移命令的全部选项与组件。"""
    document = _reference()
    options = {
        option
        for action in build_parser()._actions
        for option in action.option_strings
        if option.startswith("--") and option != "--help"
    }
    assert "--embedding-dimensions" in options
    assert [option for option in sorted(options) if f"`{option}`" not in document] == []
    assert [name for name in COMPONENTS if f"`{name}`" not in document] == []
