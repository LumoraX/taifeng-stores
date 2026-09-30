"""测试辅助：脚本化的模型客户端与最小 skill 目录。

只用 taifeng 稳定层的类型搭建。内核自带的模拟器不在稳定层，适配包不能依赖它。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import taifeng
from taifeng import ResponseEvent, TokenUsage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

ENTRY_SKILL = """---
name: helper
description: 回答问题的助手
version: 1.0.0
type: composite
entry: true
model: scripted-model
child_skills: [notes]
tool_names: []
---
# 助手
简短地回答用户的问题。
"""

CHILD_SKILL = """---
name: notes
description: 回答时可以参考的注意事项
version: 1.0.0
type: atomic
---
# 注意事项
回答要简短。
"""


def write_skills(root: Path) -> Path:
    """写一个最小的 skill 目录：一个入口 skill 加一个子 skill。"""
    for name, body in (("helper", ENTRY_SKILL), ("notes", CHILD_SKILL)):
        skill_dir = root / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return root / "skills"


class _Session:
    """一次 turn 的会话：吐出脚本里的下一段回复。"""

    def __init__(self, replies: list[str], requests: list[Any]) -> None:
        """绑定共享的回复队列与请求记录。"""
        self._replies = replies
        self._requests = requests

    async def stream(self, request: Any) -> AsyncIterator[ResponseEvent]:
        """按内核要求的顺序产出事件，最后一个一定是 completed。"""
        self._requests.append(request)
        text = self._replies.pop(0) if self._replies else "（脚本已用完）"
        yield ResponseEvent(kind="created")
        yield ResponseEvent(kind="text_delta", data={"text": text})
        usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
        yield ResponseEvent(
            kind="completed",
            data={
                "response_id": None,
                "usage": usage.model_dump(),
                "end_turn": True,
                "request_id": None,
                "stop_reason": "stop",
            },
        )

    async def __aenter__(self) -> _Session:
        """进入会话。"""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """退出会话。"""
        return


class ScriptedModelClient:
    """按脚本依次回复的模型客户端。"""

    def __init__(self, replies: list[str]) -> None:
        """``replies`` 是每次采样依次返回的文本。"""
        self._replies = list(replies)
        self.requests: list[Any] = []

    def session(self, *, cancel: taifeng.CancellationToken, model: str | None = None) -> _Session:
        """创建 turn 级会话。"""
        return _Session(self._replies, self.requests)


async def run_turn(engine: taifeng.AgentEngine, text: str) -> list[taifeng.EventMsg]:
    """提交一条用户消息并等到该 turn 结束，返回期间的事件。"""
    submission_id = await engine.submit(taifeng.UserMessage(text=text))
    events: list[taifeng.EventMsg] = []

    async def _collect() -> None:
        async for event in engine.subscribe(submission_id):
            events.append(event)
            if event.msg.kind in ("turn_completed", "turn_failed", "turn_suspended"):
                return

    await asyncio.wait_for(_collect(), timeout=30)
    return events
