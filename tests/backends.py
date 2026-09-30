"""测试用的真实后端：优先用环境变量指定的服务，否则用 Docker 临时拉起一个。

红线：存储行为一律对真实后端验证，不用 mock 代替（AGENTS.md）。两种来源都没有时跳过。

- ``TAIFENG_STORES_TEST_PG_DSN``：已有的 Postgres（须装有 pgvector 扩展）；
- ``TAIFENG_STORES_TEST_REDIS_URL``：已有的 Redis。

测试只在自己新建的 schema / 键前缀里读写，结束时清理，不碰库里已有的数据。
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass

PG_IMAGE = "pgvector/pgvector:pg16"
REDIS_IMAGE = "redis:7-alpine"
_READY_TIMEOUT_SECONDS = 90.0


@dataclass
class Backend:
    """一个可用的后端服务。"""

    url: str
    container: str | None = None

    def stop(self) -> None:
        """销毁临时容器；环境变量指定的外部服务不动。"""
        if self.container is not None:
            _docker("rm", "--force", "--volumes", self.container, check=False)


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """运行 docker 命令。"""
    docker = shutil.which("docker")
    assert docker is not None
    return subprocess.run(  # noqa: S603 —— 参数全部来自测试代码
        [docker, *args], capture_output=True, text=True, timeout=300, check=check
    )


def docker_available() -> bool:
    """本机是否有可用的 Docker 守护进程。"""
    if shutil.which("docker") is None:
        return False
    try:
        return _docker("info", "--format", "{{.ServerVersion}}", check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _published_port(container: str, port: int) -> int:
    """容器端口映射到宿主机的哪个端口。"""
    mapping = _docker("port", container, f"{port}/tcp").stdout.strip().splitlines()[0]
    return int(mapping.rsplit(":", 1)[1])


def _wait_until(container: str, probe: list[str], description: str) -> None:
    """轮询容器内的探活命令直到成功。"""
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _docker("exec", container, *probe, check=False).returncode == 0:
            return
        time.sleep(0.5)
    logs = _docker("logs", "--tail", "30", container, check=False)
    _docker("rm", "--force", container, check=False)
    raise RuntimeError(f"{description} 在 {_READY_TIMEOUT_SECONDS:.0f}s 内未就绪：\n{logs.stderr}")


def start_postgres() -> Backend | None:
    """取得一个 Postgres；拿不到返回 None。"""
    dsn = os.environ.get("TAIFENG_STORES_TEST_PG_DSN")
    if dsn:
        return Backend(url=dsn)
    if not docker_available():
        return None
    name = "tfstores-pg-" + secrets.token_hex(4)
    _docker(
        "run", "--detach", "--rm", "--name", name,
        "--env", "POSTGRES_PASSWORD=test", "--env", "POSTGRES_DB=test",
        "--publish", "127.0.0.1::5432",
        "--label", "taifeng-stores.test=1",
        PG_IMAGE,
    )  # fmt: skip
    # 官方镜像初始化期间会先起一个临时实例再重启；对 TCP 端口探活才能避开这个窗口
    _wait_until(
        name, ["pg_isready", "--host", "127.0.0.1", "--username", "postgres"], "Postgres"
    )
    port = _published_port(name, 5432)
    return Backend(url=f"postgresql://postgres:test@127.0.0.1:{port}/test", container=name)


def start_redis() -> Backend | None:
    """取得一个 Redis；拿不到返回 None。"""
    url = os.environ.get("TAIFENG_STORES_TEST_REDIS_URL")
    if url:
        return Backend(url=url)
    if not docker_available():
        return None
    name = "tfstores-redis-" + secrets.token_hex(4)
    _docker(
        "run", "--detach", "--rm", "--name", name,
        "--publish", "127.0.0.1::6379",
        "--label", "taifeng-stores.test=1",
        REDIS_IMAGE,
    )  # fmt: skip
    _wait_until(name, ["redis-cli", "ping"], "Redis")
    port = _published_port(name, 6379)
    return Backend(url=f"redis://127.0.0.1:{port}/0", container=name)
