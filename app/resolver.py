"""容器内路径解析：软链接跟随、跨挂载点、成环检测。

设计要点：

* 镜像区域与所有挂载卷被抽象成一个统一的 :class:`ContainerView`，
  解析过程始终使用容器绝对路径分量，挂载点的卷路由在视图内部完成，
  因此软链接天然可以指向其他挂载点；
* 卷内不存在软链接，所以每次跟随的软链接都位于镜像区域，
  用「容器绝对路径」作为循环检测键即可精确捕获成环；
* 中断（找不到）时逐分量报告断在哪一段、父路径是什么、为何中断。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .imagefs import ImageTree, Node, Tomb
from .volume import VolumeTree

# 与 Linux path resolution 常见 ELOOP 阈值对齐。
MAX_SYMLINK_HOPS = 40


# ---------------------------------------------------------------------------
# 视图内部的单步查找结果
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeOutcome:
    node: Node
    source: dict  # 来源描述：层 or 卷


@dataclass(frozen=True)
class MissingOutcome:
    reason: str


# ---------------------------------------------------------------------------
# 对外的解析结果
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Hop:
    symlink_path: str
    target: str
    resolved_from: str


@dataclass
class ResolveResult:
    status: str  # found | missing | error
    query_path: str
    actual_path: str | None = None
    entry_type: str | None = None
    source: dict | None = None
    broken_segment: str | None = None
    broken_at: str | None = None
    reason: str | None = None
    error_code: str | None = None
    shadowed_by_mount: str | None = None
    symlink_hops: list[Hop] = field(default_factory=list)


def _join(parts: tuple[str, ...]) -> str:
    return "/" + "/".join(parts)


def _lexical_normalize(target: str, base: tuple[str, ...]) -> tuple[str, ...]:
    """把软链接目标按词法规则拼到 base 上（不访问文件系统）。"""

    stack: list[str] = list(base)
    for comp in target.split("/"):
        if comp in ("", "."):
            continue
        if comp == "..":
            if stack:
                stack.pop()
            # 越过根目录的 .. 按 POSIX 语义停在根。
            continue
        stack.append(comp)
    return tuple(stack)


class ContainerView:
    """合并后的容器视图：镜像层 + 挂载卷，统一按容器绝对路径查找。"""

    def __init__(
        self,
        tree: ImageTree,
        mounts: dict[tuple[str, ...], VolumeTree],
    ):
        self._tree = tree
        # 长路径在前，保证嵌套判断时最具体的挂载点先命中（挂载点互不嵌套，
        # 实际上不会有多个命中，仅做防御）。
        self._mounts = dict(sorted(mounts.items(), key=lambda kv: len(kv[0]), reverse=True))

    def _find_mount(self, parts: tuple[str, ...]) -> tuple[tuple[str, ...], VolumeTree] | None:
        for mount_parts, volume in self._mounts.items():
            if parts[: len(mount_parts)] == mount_parts:
                return mount_parts, volume
        return None

    def _mount_label(self, mount_parts: tuple[str, ...]) -> str:
        return _join(mount_parts)

    def get(self, parts: tuple[str, ...]):
        """返回 NodeOutcome / MissingOutcome。"""

        mount = self._find_mount(parts)
        if mount is not None:
            mount_parts, volume = mount
            rel = parts[len(mount_parts):]
            label = self._mount_label(mount_parts)
            if not rel:
                # 挂载点自身：卷根目录（镜像里该目录的内容已被盖住）。
                return NodeOutcome(
                    node=Node(entry_type="dir", layer_index=-1),
                    source={"origin": "volume", "mountpoint": label,
                            "detail": "卷根目录（挂载点自身）"},
                )
            return self._volume_lookup(volume, rel, label)
        return self._image_lookup(parts)

    def _volume_lookup(self, volume: VolumeTree, rel: tuple[str, ...], label: str):
        # 父路径必须是目录。
        for depth in range(1, len(rel)):
            ancestor = volume.lookup(rel[:depth])
            if ancestor is None:
                break
            if ancestor.entry_type != "dir":
                return MissingOutcome(
                    f"卷 {label} 内的父路径 {_join(rel[:depth])} 是普通文件，"
                    "无法继续向下查找"
                )
        hit = volume.lookup(rel)
        if hit is None:
            return MissingOutcome(f"卷 {label} 的条目清单中不存在 {_join(rel)}")
        return NodeOutcome(
            node=Node(entry_type=hit.entry_type, layer_index=-1),
            source={"origin": "volume", "mountpoint": label},
        )

    def _image_lookup(self, parts: tuple[str, ...]):
        # 父路径必须是目录（软链接在解析主循环里已被跟随，不会出现在此）。
        for depth in range(1, len(parts)):
            ancestor = self._tree.lookup(parts[:depth])
            if isinstance(ancestor, Node) and ancestor.entry_type != "dir":
                return MissingOutcome(
                    f"镜像路径 {_join(parts[:depth])} 是{_type_cn(ancestor.entry_type)}，"
                    "无法继续向下查找"
                )
        hit = self._tree.lookup(parts)
        if hit is None:
            return MissingOutcome("镜像的任何层中都未出现过该路径")
        if isinstance(hit, Tomb):
            if hit.reason == "whiteout":
                return MissingOutcome(
                    f"该路径被第 {hit.layer_index} 层的 whiteout 删除标记（.wh. 条目）"
                    "遮蔽；删除标记只影响更下层"
                )
            return MissingOutcome(
                f"所在目录被第 {hit.layer_index} 层的不透明目录标记（.wh..wh..opq）"
                "声明为不透明，其下层内容被遮蔽"
            )
        return NodeOutcome(
            node=hit,
            source={"origin": "layer", "layer_index": hit.layer_index,
                    "detail": f"镜像第 {hit.layer_index} 层（0 表示最底层）"},
        )

    def shadow_mount(self, parts: tuple[str, ...]) -> str | None:
        mount = self._find_mount(parts)
        return self._mount_label(mount[0]) if mount else None


def _type_cn(entry_type: str) -> str:
    return {"file": "普通文件", "dir": "目录", "symlink": "软链接"}.get(entry_type, entry_type)


def resolve(view: ContainerView, query_parts: tuple[str, ...]) -> ResolveResult:
    """在容器视图里解析一个容器绝对路径。"""

    query = _join(query_parts)

    # 根目录特判。
    if not query_parts:
        return ResolveResult(
            status="found",
            query_path=query,
            actual_path="/",
            entry_type="dir",
            source={"origin": "image", "detail": "镜像根目录"},
        )

    stack: tuple[str, ...] = ()
    remaining: list[str] = list(query_parts)
    hops: list[Hop] = []
    seen_links: set[tuple[str, ...]] = set()

    while True:
        if not remaining:
            outcome = view.get(stack)
            return _finalize(outcome, view, stack, query, hops)

        comp = remaining[0]
        target_parts = stack + (comp,)
        outcome = view.get(target_parts)

        if isinstance(outcome, MissingOutcome):
            return ResolveResult(
                status="missing",
                query_path=query,
                broken_segment=comp,
                broken_at=_join(target_parts),
                reason=outcome.reason,
                shadowed_by_mount=view.shadow_mount(target_parts),
                symlink_hops=hops,
            )

        node = outcome.node
        if not remaining[1:]:
            # 末分量命中即返回（即使是软链接也返回链接本身，与 stat(2) /
            # 词法路径解析语义一致）；只有链接后面还有分量时才需要跟随。
            return ResolveResult(
                status="found",
                query_path=query,
                actual_path=_join(target_parts),
                entry_type=node.entry_type,
                source=outcome.source,
                shadowed_by_mount=view.shadow_mount(target_parts),
                symlink_hops=hops,
            )
        if node.entry_type == "symlink":
            # 成环检测：同一个软链接绝对路径被二次跟随即构成环。
            if target_parts in seen_links:
                return ResolveResult(
                    status="error",
                    query_path=query,
                    error_code="symlink_cycle",
                    reason=f"软链接解析在 {_join(target_parts)} 处成环（ELOOP）",
                    shadowed_by_mount=view.shadow_mount(target_parts),
                    symlink_hops=hops,
                )
            if len(hops) >= MAX_SYMLINK_HOPS:
                return ResolveResult(
                    status="error",
                    query_path=query,
                    error_code="too_many_symlink_hops",
                    reason=f"软链接跳数超过上限 {MAX_SYMLINK_HOPS}，放弃解析",
                    shadowed_by_mount=view.shadow_mount(target_parts),
                    symlink_hops=hops,
                )

            seen_links.add(target_parts)
            raw_target = node.link_target or ""
            if raw_target.startswith("/"):
                base: tuple[str, ...] = ()
                resolved_from = "/"
            else:
                base = stack
                resolved_from = _join(stack) or "/"
            hops.append(
                Hop(symlink_path=_join(target_parts), target=raw_target, resolved_from=resolved_from)
            )
            new_parts = _lexical_normalize(raw_target, base)
            stack = ()
            remaining = list(new_parts) + remaining[1:]
            continue

        # 普通文件 / 目录：能走到这里说明 remaining[1:] 非空（末分量已在前面短路）。
        if node.entry_type == "file":
            # ENOTDIR 语义：中间分量是普通文件，无法继续向下解析。
            return ResolveResult(
                status="missing",
                query_path=query,
                broken_segment=comp,
                broken_at=_join(target_parts),
                reason=f"路径 {_join(target_parts)} 是普通文件，其下不能再解析子分量（ENOTDIR）",
                shadowed_by_mount=view.shadow_mount(target_parts),
                symlink_hops=hops,
            )
        stack = target_parts
        remaining = remaining[1:]


def _finalize(
    outcome: NodeOutcome | MissingOutcome,
    view: ContainerView,
    stack: tuple[str, ...],
    query: str,
    hops: list[Hop],
) -> ResolveResult:
    if isinstance(outcome, MissingOutcome):
        # 理论上剩余分量为空时才走到这里，broken 段即最终路径的最后一段。
        return ResolveResult(
            status="missing",
            query_path=query,
            broken_segment=stack[-1] if stack else None,
            broken_at=_join(stack) if stack else "/",
            reason=outcome.reason,
            shadowed_by_mount=view.shadow_mount(stack),
            symlink_hops=hops,
        )
    return ResolveResult(
        status="found",
        query_path=query,
        actual_path=_join(stack),
        entry_type=outcome.node.entry_type,
        source=outcome.source,
        shadowed_by_mount=view.shadow_mount(stack),
        symlink_hops=hops,
    )
