"""稳定协议签名里出现、但 taifeng 尚未从顶层导出的类型（ADR 0003）。

红线 1 要求只 import ``taifeng.__all__``。但 ``MessageStore.list_threads`` 的返回类型
``ThreadInfo`` 不在其中——不拿到这个类型，就无法实现这个已经公开的协议。这类「稳定协议
签名的组成部分」视为稳定契约的一部分，集中在本模块单点引入：

- 只允许引入出现在稳定协议方法签名里的类型，不得借此引入任何实现；
- 每个名字都由 ``tests/test_smoke.py`` 钉住其存在与形状；
- taifeng 补上顶层导出后，改为从顶层 import 并删除对应条目。
"""

from __future__ import annotations

from taifeng.conversation import ThreadInfo

__all__ = ["ThreadInfo"]
