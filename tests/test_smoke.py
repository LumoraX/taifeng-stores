"""冒烟测试：版本来源正确，所依赖的 taifeng 协议与类型可见。"""

from __future__ import annotations

from importlib.metadata import version

import taifeng

import taifeng_stores
from taifeng_stores import _kernel_types


def test_version_from_metadata_matches_distribution() -> None:
    """``__version__`` 必须等于发行元数据版本（pyproject 是唯一来源）。"""
    assert taifeng_stores.__version__ == version("taifeng-stores")


def test_stable_api_store_protocols_exported() -> None:
    """本包要实现的存储类协议必须在 taifeng 稳定层 ``__all__`` 中。

    红线：适配包只依赖稳定层（ADR 0001 决策 2）。taifeng 若把它们移出 ``__all__``，
    这里先红，而不是等到实现代码 import 失败才发现。
    """
    required = {"MessageStore", "MessageWriter", "ThreadDirectory", "IndexHook", "MemoryStore"}
    assert required <= set(taifeng.__all__)


def test_stable_api_data_types_exported() -> None:
    """实现里用到的数据类型也必须在稳定层。"""
    required = {
        "DirectoryError",
        "ResponseItem",
        "ThreadFilter",
        "ThreadMetadata",
        "ThreadNotFoundError",
        "ThreadPage",
    }
    assert required <= set(taifeng.__all__)


def test_signature_types_pinned() -> None:
    """经单点模块引入的签名类型（ADR 0003）：钉住其存在与字段。

    taifeng 把 ``ThreadInfo`` 提到顶层后，这条断言会提醒把引入方式改掉。
    """
    assert set(_kernel_types.__all__) == {"ThreadInfo"}
    fields = set(_kernel_types.ThreadInfo.model_fields)
    assert {"thread_id", "cwd", "created_at", "last_activity_at", "entry_skill_id"} <= fields
    assert "ThreadInfo" not in taifeng.__all__, (
        "ThreadInfo 已进入 taifeng 稳定层：请改为从顶层 import，并删除 _kernel_types 里的条目"
    )
