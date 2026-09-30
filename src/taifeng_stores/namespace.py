"""存储命名空间：把一组表（或一组键）圈在指定的 schema / 前缀之下。

本包不认识租户（红线 3）。上层要按租户、按环境隔离数据时，给每个隔离单元传不同的命名空间
即可——是分库、分 schema 还是分前缀，由上层决定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from taifeng_stores.errors import StoreConfigError

# 只允许小写字母、数字、下划线，且以字母或下划线开头：标识符会拼进 SQL，必须从字符集上杜绝注入
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_PREFIX = re.compile(r"^[a-z_][a-z0-9_]{0,31}$")
_KEY_PREFIX = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:\-]{0,127}$")


@dataclass(frozen=True)
class SqlNamespace:
    """关系库里的命名空间。

    Attributes:
        schema: schema 名。
        prefix: 表名前缀；空串表示不加前缀。
    """

    schema: str = "public"
    prefix: str = "taifeng_"

    def __post_init__(self) -> None:
        """校验标识符字符集。"""
        if not _IDENTIFIER.match(self.schema):
            raise StoreConfigError(f"schema 名非法：{self.schema!r}")
        if self.prefix and not _PREFIX.match(self.prefix):
            raise StoreConfigError(f"表名前缀非法：{self.prefix!r}")

    def table(self, name: str) -> str:
        """带 schema 与前缀、已加引号的完整表名。"""
        return f'"{self.schema}"."{self.object_name(name)}"'

    def object_name(self, name: str) -> str:
        """带前缀、不带 schema 的对象名（用于索引名等）。"""
        if not _IDENTIFIER.match(name):
            raise StoreConfigError(f"对象名非法：{name!r}")
        full = f"{self.prefix}{name}"
        if len(full) > 63:
            raise StoreConfigError(f"对象名超过 63 字符：{full!r}")
        return full


def validate_key_prefix(prefix: str) -> str:
    """校验键值库的键前缀。"""
    if not _KEY_PREFIX.match(prefix):
        raise StoreConfigError(f"键前缀非法：{prefix!r}")
    return prefix


__all__ = ["SqlNamespace", "validate_key_prefix"]
