"""本包的异常类型。"""

from __future__ import annotations


class StoreConfigError(ValueError):
    """构造参数不合法（命名空间含非法字符、维度不是正整数等）。"""


class MigrationError(RuntimeError):
    """迁移脚本执行失败，或迁移记录与脚本对不上。"""


class SchemaOutOfDateError(RuntimeError):
    """数据库表结构落后于代码要求。

    本包不在运行时隐式建表改表（红线 6）；遇到这个异常应先运行迁移。

    Attributes:
        component: 落后的迁移组件名。
        current: 数据库当前版本（从未迁移过为 0）。
        required: 代码要求的版本。
    """

    def __init__(self, component: str, current: int, required: int) -> None:
        """记录版本差异并生成提示。"""
        super().__init__(
            f"组件 {component!r} 的表结构版本为 {current}，代码要求 {required}；"
            "请先运行 taifeng-stores-migrate"
        )
        self.component = component
        self.current = current
        self.required = required


__all__ = ["MigrationError", "SchemaOutOfDateError", "StoreConfigError"]
