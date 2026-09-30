"""命名空间校验：标识符会拼进 SQL 与键名，必须从字符集上杜绝注入。"""

from __future__ import annotations

import pytest

from taifeng_stores import SqlNamespace, StoreConfigError
from taifeng_stores.namespace import validate_key_prefix


def test_default_namespace() -> None:
    """默认落在 public schema，表名带 taifeng_ 前缀。"""
    assert SqlNamespace().table("thread") == '"public"."taifeng_thread"'


def test_custom_namespace_and_empty_prefix() -> None:
    """schema 与前缀可定制；前缀可以为空。"""
    assert SqlNamespace(schema="tenant_a", prefix="tf_").table("message") == '"tenant_a"."tf_message"'
    assert SqlNamespace(schema="s", prefix="").table("thread") == '"s"."thread"'


@pytest.mark.parametrize(
    "schema", ["", "Public", "a-b", 'a"b', "a;drop", "1abc", "a b", "x" * 64, "模式"]
)
def test_invalid_schema_rejected(schema: str) -> None:
    """schema 只接受小写字母、数字、下划线。"""
    with pytest.raises(StoreConfigError):
        SqlNamespace(schema=schema)


@pytest.mark.parametrize("prefix", ["A_", "a-", 'a"', "a;", "9a", "x" * 33])
def test_invalid_prefix_rejected(prefix: str) -> None:
    """表名前缀同样受字符集约束。"""
    with pytest.raises(StoreConfigError):
        SqlNamespace(prefix=prefix)


def test_invalid_object_name_rejected() -> None:
    """对象名也要校验，且拼上前缀后不能超过 63 字符。"""
    namespace = SqlNamespace(prefix="p" * 32)
    with pytest.raises(StoreConfigError):
        namespace.table('thread"; drop')
    with pytest.raises(StoreConfigError):
        namespace.table("t" * 40)


@pytest.mark.parametrize("prefix", ["taifeng", "app:prod", "a.b-c_d", "T1"])
def test_valid_key_prefix(prefix: str) -> None:
    """键前缀允许字母、数字与少数分隔符。"""
    assert validate_key_prefix(prefix) == prefix


@pytest.mark.parametrize("prefix", ["", "a b", "a*", "a\nb", ":lead", "x" * 129, "a{b}"])
def test_invalid_key_prefix_rejected(prefix: str) -> None:
    """含空白、通配符、换行的键前缀被拒绝。"""
    with pytest.raises(StoreConfigError):
        validate_key_prefix(prefix)
