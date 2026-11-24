"""只读取未压缩 OCI 层 tar 的条目元数据，不落盘、不提取内容。

输出为有序事件列表，顺序与 tar 内条目顺序一致，交由 :mod:`app.imagefs`
做「底到顶」合并。支持的条目：

* 普通文件、目录、软链接；
* whiteout 文件：同层目录里名为 ``.wh.<name>`` 的条目；
* 不透明目录标记：``.wh..wh..opq``，表示该目录在下层已有的内容全部被遮蔽。
"""

from __future__ import annotations

import io
import os
import tarfile
from dataclasses import dataclass

from .errors import InvalidInputError, TarReadError

# 单条目路径最大长度与单层最大条目数，纯属防御性上限。
MAX_NAME_LENGTH = 4096
MAX_MEMBERS_PER_LAYER = 50_000

# 与 Linux 内核循环路径解析上限保持一致（ELOOP 常见阈值）。
MAX_SYMLINK_TARGET_LENGTH = 4095

OPAQUE_MARKER = ".wh..wh..opq"
WHITEOUT_PREFIX = ".wh."


@dataclass(frozen=True)
class OpaqueEvent:
    """某目录上出现不透明标记 ``.wh..wh..opq``。"""

    dir_parts: tuple[str, ...]


@dataclass(frozen=True)
class WhiteoutEvent:
    """出现 ``.wh.<name>`` 删除标记。"""

    parent_parts: tuple[str, ...]
    name: str


@dataclass(frozen=True)
class NodeEvent:
    """普通文件 / 目录 / 软链接条目。"""

    parts: tuple[str, ...]
    entry_type: str  # "file" | "dir" | "symlink"
    link_target: str | None


LayerEvent = OpaqueEvent | WhiteoutEvent | NodeEvent


def _normalize_member_name(raw_name: str) -> tuple[str, ...] | None:
    """把 tar 条目名规整为纯相对路径分量。

    返回 None 表示该条目应跳过（裸 ``.`` / 含 ``..`` 的逃逸路径）。
    """
    name = raw_name
    # tarfile 会用 pax 头还原路径，但再防御一次 NUL 字节。
    if "\x00" in name:
        raise InvalidInputError(f"tar 条目名包含 NUL 字节: {name!r}")
    if name.startswith("/"):
        raise InvalidInputError(f"OCI 层条目必须是相对路径，发现绝对路径: {name!r}")
    if len(name) > MAX_NAME_LENGTH:
        raise InvalidInputError(f"tar 条目路径过长（>{MAX_NAME_LENGTH}）: {name!r}")

    normalized = os.path.normpath(name)
    if normalized == ".":
        return None
    parts = tuple(p for p in normalized.split("/") if p not in ("", "."))
    if any(p == ".." for p in parts):
        # 层 tar 内的路径逃逸一律拒绝，避免污染合并树。
        raise InvalidInputError(f"tar 条目路径包含 '..' 逃逸分量: {name!r}")
    return parts


def load_layer(data: bytes, layer_index: int, display_name: str = "") -> list[LayerEvent]:
    """解析一层未压缩 tar，返回按 tar 顺序排列的事件。"""

    if not data:
        raise TarReadError(f"第 {layer_index} 层为空，不是合法的 tar 文件")

    # 压缩流的魔数：gzip(1f8b) / xz(fd377a) / zstd(28b52ffd) / bzip2(BZh)。
    # 本接口按需求只接收未压缩 OCI 层，压缩流要明确报错而不是隐式解压。
    if data[:2] == b"\x1f\x8b":
        raise TarReadError(f"第 {layer_index} 层是 gzip 压缩流，请先解压后再上传未压缩 tar")
    if data[:6] == b"\xfd7zXZ\x00":
        raise TarReadError(f"第 {layer_index} 层是 xz 压缩流，请先解压后再上传未压缩 tar")
    if data[:4] == b"\x28\xb5\x2f\xfd":
        raise TarReadError(f"第 {layer_index} 层是 zstd 压缩流，请先解压后再上传未压缩 tar")
    if data[:3] == b"BZh":
        raise TarReadError(f"第 {layer_index} 层是 bzip2 压缩流，请先解压后再上传未压缩 tar")

    label = display_name or f"layer[{layer_index}]"
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
            events: list[LayerEvent] = []
            seen: set[tuple[str, ...]] = set()
            count = 0
            for member in tf:
                count += 1
                if count > MAX_MEMBERS_PER_LAYER:
                    raise InvalidInputError(
                        f"{label} 条目数超过上限 {MAX_MEMBERS_PER_LAYER}"
                    )

                parts = _normalize_member_name(member.name)
                if parts is None:
                    continue
                if parts in seen:
                    raise InvalidInputError(
                        f"{label} 中存在重复路径条目: {'/'.join(parts)}"
                    )
                seen.add(parts)

                parent, name = parts[:-1], parts[-1]

                # 1) 不透明目录标记：<dir>/.wh..wh..opq
                if name == OPAQUE_MARKER:
                    events.append(OpaqueEvent(dir_parts=parent))
                    continue

                # 2) whiteout：.wh.<被删除的名称>
                if name.startswith(WHITEOUT_PREFIX):
                    removed = name[len(WHITEOUT_PREFIX):]
                    if not removed:
                        raise InvalidInputError(f"{label} 中 whiteout 缺少被删除名称: {member.name}")
                    events.append(WhiteoutEvent(parent_parts=parent, name=removed))
                    continue

                # 3) 普通条目（先判类型，再记事件；whiteout 名称优先于类型检查）
                if member.isdir():
                    entry_type = "dir"
                    link_target = None
                elif member.isreg():
                    entry_type = "file"
                    link_target = None
                elif member.issym():
                    entry_type = "symlink"
                    link_target = member.linkname or ""
                    if "\x00" in link_target:
                        raise InvalidInputError(
                            f"{label} 软链接目标含 NUL 字节: {member.name}"
                        )
                    if len(link_target) > MAX_SYMLINK_TARGET_LENGTH:
                        raise InvalidInputError(
                            f"{label} 软链接目标过长（>{MAX_SYMLINK_TARGET_LENGTH}）: {member.name}"
                        )
                else:
                    raise InvalidInputError(
                        f"{label} 含不支持的条目类型（仅支持普通文件/目录/软链接）: {member.name}"
                    )

                events.append(
                    NodeEvent(parts=parts, entry_type=entry_type, link_target=link_target)
                )
            return events
    except tarfile.ReadError as exc:
        raise TarReadError(f"{label} 不是合法的未压缩 tar 文件: {exc}") from exc
