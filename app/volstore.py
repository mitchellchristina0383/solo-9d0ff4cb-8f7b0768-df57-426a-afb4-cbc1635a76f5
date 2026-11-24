"""持久卷初始化与按名读取。

场景：新容器挂卷前，值班员把镜像里某个源目录的默认配置预先放进命名卷。
本模块负责：

* 复用 :mod:`app.tarloader` / :mod:`app.imagefs` 的「底到顶」合并结果，
  按逐分量可达规则找出源目录下**最终可见**的普通文件与目录；
* 可见软链接一律报错（卷只含 file/dir）；空目录保留；
* 文件字节从「可见条目所在的层」的 tar 里取出，分块复制落盘；
* 提交协议：先写 ``<root>/.staging/<name>.<uuid>``，fsync 后
  ``os.rename`` 到 ``<root>/<name>``。rename 是唯一的生效点——
  写盘失败或进程中断只会留下暂存残骸，卷名不会被半成品占用，
  之后可以用同名重新初始化；启动时清理残留暂存目录；
* 每个卷内写一份 ``.volume-manifest.json`` 清单（随 rename 一起生效），
  供排障接口的 ``volume_entries`` 直接使用；
* 同名并发：按卷名的互斥锁保证只有一份生效，重复请求返回已生效清单；
  不同卷名互不阻塞，可以同时初始化。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tarfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    VolumeStorageError,
)
from .imagefs import ImageTree, Node, Tomb, build_image_tree
from .tarloader import _normalize_member_name, load_layer

# 卷内清单文件名（保留名，源目录里不允许出现同名条目）。
MANIFEST_NAME = ".volume-manifest.json"
# 暂存目录名：与最终卷目录同一文件系统，保证 rename 原子。
STAGING_DIRNAME = ".staging"

# 与 Docker 卷名习惯对齐：字母数字开头，可含 . _ -。
VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

DEFAULT_CHUNK_SIZE = 1024 * 1024  # 大文件分块复制：1 MiB / 块


@dataclass(frozen=True)
class VolumeInfo:
    """一份已生效卷的清单与统计。"""

    volume_name: str
    source_dir: str
    initialized_at: str
    entries: list[dict]  # [{"path": 相对卷根, "type": "file|dir"}]，可直接给排障接口
    file_count: int
    dir_count: int
    total_bytes: int


@dataclass(frozen=True)
class InitOutcome:
    info: VolumeInfo
    created: bool  # True=本次请求生效；False=卷此前已生效，返回既有清单


@dataclass(frozen=True)
class _CopyPlan:
    """从合并树收集到的落盘计划（路径均为相对源目录的分量）。"""

    dirs: list[tuple[str, ...]]
    files: list[tuple[tuple[str, ...], int]]  # (相对路径, 来源层索引)


def validate_volume_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise InvalidInputError("volume_name 不能为空")
    if "\x00" in name:
        raise InvalidInputError("volume_name 含 NUL 字节")
    if not VOLUME_NAME_RE.match(name):
        raise InvalidInputError(
            "volume_name 只能含字母、数字、'.'、'_'、'-'，且必须以字母或数字开头: "
            f"{name!r}"
        )
    return name


def _join(parts: tuple[str, ...]) -> str:
    return "/" + "/".join(parts) if parts else "/"


def _collect_visible(tree: ImageTree, src_parts: tuple[str, ...]) -> _CopyPlan:
    """按逐分量可达规则收集源目录下最终可见的 file/dir。

    与 :mod:`app.resolver` 的可见性一致：只有从源目录出发、沿途每一级
    都是可见目录的条目才算可见；被 whiteout / 不透明标记遮蔽的分支
    不会进入计划。遇到可见软链接直接报错。
    """

    if src_parts:
        node = tree.lookup(src_parts)
        if node is None:
            raise InvalidInputError(
                f"源目录 {_join(src_parts)} 在镜像的任何层中都未出现过"
            )
        if isinstance(node, Tomb):
            what = "不透明标记" if node.reason == "opaque" else "whiteout 删除标记"
            raise InvalidInputError(
                f"源目录 {_join(src_parts)} 被第 {node.layer_index} 层的{what}遮蔽，"
                "不是可用目录"
            )
        if node.entry_type == "symlink":
            raise InvalidInputError(
                f"源目录 {_join(src_parts)} 是软链接（-> {node.link_target}），"
                "卷只支持普通文件和目录"
            )
        if node.entry_type != "dir":
            raise InvalidInputError(
                f"源路径 {_join(src_parts)} 不是目录（实际为 {node.entry_type}）"
            )
    # src_parts == () 表示镜像根，视作天然存在的目录。

    # 父路径 -> 直接子条目，DFS 时按子树实际大小展开。
    children: dict[tuple[str, ...], list[tuple[str, Node | Tomb]]] = {}
    for parts, entry in tree.subtree_items(src_parts):
        children.setdefault(parts[:-1], []).append((parts[-1], entry))

    dirs: list[tuple[str, ...]] = []
    files: list[tuple[tuple[str, ...], int]] = []
    stack = [src_parts]
    while stack:
        cur = stack.pop()
        for name, entry in children.get(cur, ()):
            child = cur + (name,)
            rel = child[len(src_parts):]
            if isinstance(entry, Tomb):
                continue  # 被遮蔽：自身与后代都不可见，不再下钻
            if entry.entry_type == "symlink":
                raise InvalidInputError(
                    f"源目录内存在可见软链接 {_join(child)} -> {entry.link_target}，"
                    "卷只支持普通文件和目录"
                )
            if entry.entry_type == "dir":
                dirs.append(rel)
                stack.append(child)
            else:
                files.append((rel, entry.layer_index))
    dirs.sort()
    files.sort()
    return _CopyPlan(dirs=dirs, files=files)


def _copy_chunked(src, dst, chunk_size: int) -> int:
    """分块复制，返回字节数。大文件不会一次性读进内存。"""

    total = 0
    while True:
        chunk = src.read(chunk_size)
        if not chunk:
            return total
        dst.write(chunk)
        total += len(chunk)


class VolumeStore:
    """命名卷的磁盘存储（同一进程内共享，线程安全）。"""

    def __init__(self, root: str | os.PathLike, chunk_size: int = DEFAULT_CHUNK_SIZE):
        self._root = Path(root)
        self._staging = self._root / STAGING_DIRNAME
        self._chunk_size = chunk_size
        self._root.mkdir(parents=True, exist_ok=True)
        self._staging.mkdir(parents=True, exist_ok=True)
        # 上次进程中断留下的暂存残骸：未生效，直接清理。
        for leftover in self._staging.iterdir():
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover, ignore_errors=True)
            else:
                leftover.unlink(missing_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _lock_for(self, name: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._locks[name] = lock
            return lock

    def _manifest_path(self, name: str) -> Path:
        return self._root / name / MANIFEST_NAME

    def _read_info(self, name: str) -> VolumeInfo | None:
        """读取已生效卷的清单；未生效（无清单或清单损坏）返回 None。"""

        try:
            raw = self._manifest_path(name).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return None
        try:
            data = json.loads(raw)
            return VolumeInfo(
                volume_name=data["volume_name"],
                source_dir=data["source_dir"],
                initialized_at=data["initialized_at"],
                entries=list(data["entries"]),
                file_count=int(data["file_count"]),
                dir_count=int(data["dir_count"]),
                total_bytes=int(data["total_bytes"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def get_info(self, name: str) -> VolumeInfo:
        validate_volume_name(name)
        info = self._read_info(name)
        if info is None:
            raise NotFoundError(
                f"卷 {name} 尚未初始化完成（不存在或上次写入未生效），"
                "请先调用 POST /api/volumes/init"
            )
        return info

    def list_ready(self) -> list[VolumeInfo]:
        """列出全部已生效（写齐）的卷；半成品与暂存残骸不出现。"""

        ready: list[VolumeInfo] = []
        for child in sorted(self._root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if not VOLUME_NAME_RE.match(child.name):
                continue
            info = self._read_info(child.name)
            if info is not None:
                ready.append(info)
        return ready

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def initialize(
        self,
        name: str,
        src_parts: tuple[str, ...],
        layer_blobs: list[bytes],
    ) -> InitOutcome:
        """初始化命名卷；同名并发只生效一份，重复请求返回已生效清单。"""

        validate_volume_name(name)
        existing = self._read_info(name)
        if existing is not None:
            return InitOutcome(info=existing, created=False)
        lock = self._lock_for(name)
        with lock:
            # 拿到锁后再查一次：并发的同名请求可能刚刚写齐。
            existing = self._read_info(name)
            if existing is not None:
                return InitOutcome(info=existing, created=False)
            return InitOutcome(
                info=self._initialize_locked(name, src_parts, layer_blobs),
                created=True,
            )

    def _initialize_locked(
        self,
        name: str,
        src_parts: tuple[str, ...],
        layer_blobs: list[bytes],
    ) -> VolumeInfo:
        # 1) 纯内存阶段：解析层、合并、收集可见计划。输入错误在此抛出，
        #    尚未触碰磁盘，自然不会留下半成品。
        events = [
            load_layer(blob, index, f"layer[{index}]")
            for index, blob in enumerate(layer_blobs)
        ]
        tree = build_image_tree(events)
        plan = _collect_visible(tree, src_parts)
        if (MANIFEST_NAME,) in plan.dirs or any(
            rel == (MANIFEST_NAME,) for rel, _ in plan.files
        ):
            raise InvalidInputError(
                f"源目录内含有保留文件名 {MANIFEST_NAME}，无法初始化"
            )

        # 2) 写盘阶段：暂存目录写齐 + fsync，最后 rename 生效。
        staging = self._staging / f"{name}.{uuid.uuid4().hex}"
        final = self._root / name
        try:
            if os.path.lexists(final):
                raise ConflictError(
                    f"卷名 {name} 被未完成的残留目录占用，请人工清理 "
                    f"{final} 后重试"
                )
            staging.mkdir()
            file_count, total_bytes = self._write_plan(staging, src_parts, plan, layer_blobs)
            info = VolumeInfo(
                volume_name=name,
                source_dir=_join(src_parts),
                initialized_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                entries=(
                    [{"path": "/".join(rel), "type": "dir"} for rel in plan.dirs]
                    + [{"path": "/".join(rel), "type": "file"} for rel, _ in plan.files]
                ),
                file_count=file_count,
                dir_count=len(plan.dirs),
                total_bytes=total_bytes,
            )
            self._write_manifest(staging, info)
            _fsync_dir(staging)
            os.rename(staging, final)  # 唯一的生效点：原子提交
            _fsync_dir(self._root)
            return info
        except (InvalidInputError, ConflictError):
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except OSError as exc:
            # 写盘失败：清理暂存，卷未生效，同名可立即重新初始化。
            shutil.rmtree(staging, ignore_errors=True)
            raise VolumeStorageError(
                f"卷 {name} 写盘失败（未生效，可重新初始化）: {exc}"
            ) from exc
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _write_plan(
        self,
        staging: Path,
        src_parts: tuple[str, ...],
        plan: _CopyPlan,
        layer_blobs: list[bytes],
    ) -> tuple[int, int]:
        """把计划落盘到暂存目录，返回 (文件数, 总字节数)。"""

        # 目录先建（含空目录）；按深度排序保证父目录先于子目录。
        for rel in sorted(plan.dirs, key=lambda p: (len(p), p)):
            staging.joinpath(*rel).mkdir()

        # 文件按来源层分组：每层 tar 只顺序扫一遍，命中即分块写出。
        needed: dict[int, dict[tuple[str, ...], tuple[str, ...]]] = {}
        for rel, layer_index in plan.files:
            member_parts = src_parts + rel
            needed.setdefault(layer_index, {})[member_parts] = rel

        total_bytes = 0
        for layer_index, want in sorted(needed.items()):
            remaining = dict(want)
            with tarfile.open(fileobj=io.BytesIO(layer_blobs[layer_index]), mode="r:") as tf:
                for member in tf:
                    if not remaining:
                        break
                    if not member.isreg():
                        continue
                    parts = _normalize_member_name(member.name)
                    if parts is None or parts not in remaining:
                        continue
                    rel = remaining.pop(parts)
                    src = tf.extractfile(member)
                    if src is None:  # 防御：普通文件必然可取流
                        raise VolumeStorageError(
                            f"第 {layer_index} 层的 {'/'.join(parts)} 无法读取内容"
                        )
                    with src, open(staging.joinpath(*rel), "wb") as dst:
                        total_bytes += _copy_chunked(src, dst, self._chunk_size)
                        dst.flush()
                        os.fsync(dst.fileno())
            if remaining:  # 与 load_layer 读的是同一份字节，理论上不会缺
                missing = ", ".join("/".join(p) for p in sorted(remaining))
                raise VolumeStorageError(f"第 {layer_index} 层中缺少文件内容: {missing}")
        return len(plan.files), total_bytes

    def _write_manifest(self, staging: Path, info: VolumeInfo) -> None:
        payload = json.dumps(
            {
                "volume_name": info.volume_name,
                "source_dir": info.source_dir,
                "initialized_at": info.initialized_at,
                "entries": info.entries,
                "file_count": info.file_count,
                "dir_count": info.dir_count,
                "total_bytes": info.total_bytes,
            },
            ensure_ascii=False,
            indent=2,
        )
        manifest = staging / MANIFEST_NAME
        with open(manifest, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
