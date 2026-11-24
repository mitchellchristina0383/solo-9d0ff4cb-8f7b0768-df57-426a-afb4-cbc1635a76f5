"""卷内条目清单的解析与校验。

卷只含普通文件和目录（不允许软链接 / whiteout）。清单条目为相对路径，
相对于挂载后的卷根；为降低歧义，路径里不允许绝对路径与 ``..`` 分量，
且每个条目的父目录必须在清单中显式给出。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import InvalidInputError


@dataclass(frozen=True)
class VolumeEntry:
    entry_type: str  # "file" | "dir"


@dataclass
class VolumeTree:
    """一个卷的独立视图（路径分量相对于卷根）。"""

    entries: dict[tuple[str, ...], VolumeEntry]

    def lookup(self, parts: tuple[str, ...]) -> VolumeEntry | None:
        return self.entries.get(parts)


def _normalize_volume_path(raw: str) -> tuple[str, ...]:
    if not isinstance(raw, str) or not raw:
        raise InvalidInputError("卷内条目路径不能为空")
    if "\x00" in raw:
        raise InvalidInputError(f"卷内条目路径含 NUL 字节: {raw}")
    if raw.startswith("/"):
        raise InvalidInputError(
            f"卷内条目路径必须是相对卷根的相对路径（不要以 / 开头）: {raw}"
        )
    parts = tuple(p for p in raw.split("/") if p not in ("", "."))
    if not parts:
        raise InvalidInputError(f"卷内条目路径无效: {raw}")
    if any(p == ".." for p in parts):
        raise InvalidInputError(f"卷内条目路径不允许 '..' 分量: {raw}")
    return parts


def build_volume_tree(raw_entries: list[dict], mount_label: str) -> VolumeTree:
    """从 JSON 清单构建卷视图。"""

    entries: dict[tuple[str, ...], VolumeEntry] = {}
    for index, item in enumerate(raw_entries):
        if not isinstance(item, dict):
            raise InvalidInputError(f"{mount_label} 的第 {index} 个条目必须是对象")
        entry_type = item.get("type")
        if entry_type not in ("file", "dir"):
            raise InvalidInputError(
                f"{mount_label} 的条目 {item.get('path')!r} 类型非法：卷只支持 file/dir"
            )
        parts = _normalize_volume_path(item["path"])
        if parts in entries:
            raise InvalidInputError(
                f"{mount_label} 中存在重复条目: {'/'.join(parts)}"
            )
        entries[parts] = VolumeEntry(entry_type=entry_type)

    # 两遍法：先收集再统一校验父目录，这样清单顺序可以任意。
    for parts in entries:
        for depth in range(1, len(parts)):
            ancestor = parts[:depth]
            ancestor_entry = entries.get(ancestor)
            if ancestor_entry is None:
                raise InvalidInputError(
                    f"{mount_label} 条目 {'/'.join(parts)} 的父目录 "
                    f"{'/'.join(ancestor)} 未在清单中声明"
                )
            if ancestor_entry.entry_type != "dir":
                raise InvalidInputError(
                    f"{mount_label} 条目 {'/'.join(parts)} 的父路径 "
                    f"{'/'.join(ancestor)} 不是目录"
                )
    return VolumeTree(entries=entries)
