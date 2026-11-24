"""镜像层「底到顶」合并。

核心规则（与 overlayfs lowerdir 叠加语义对齐）：

* whiteout（``.wh.foo``）只影响更底层：删除下层 ``foo`` 整个子树，
  同一层里在 whiteout 之后新增的 ``foo`` 仍然可见；
* 不透明目录标记（``.wh..wh..opq``）让该目录对下层内容不可见，
  目录本身保留，本层及更上层在该目录里的新增内容照常可见；
* 同路径的普通条目按 tar 顺序覆盖旧条目（同层后者覆盖前者，上层覆盖下层）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import InvalidInputError
from .tarloader import LayerEvent, NodeEvent, OpaqueEvent, WhiteoutEvent


@dataclass(frozen=True)
class Node:
    """合并后某路径上的最终条目。"""

    entry_type: str  # "file" | "dir" | "symlink"
    layer_index: int  # 来源层：0 为最底层
    link_target: str | None = None


@dataclass(frozen=True)
class Tomb:
    """路径墓碑：最终视图里该路径不存在，并记录中断原因。"""

    layer_index: int  # 哪一层的 whiteout/opaque 造成的
    reason: str  # "whiteout" | "opaque"


@dataclass
class ImageTree:
    """镜像合并视图：绝对路径分量 -> Node / Tomb。"""

    entries: dict[tuple[str, ...], Node | Tomb]

    def lookup(self, parts: tuple[str, ...]) -> Node | Tomb | None:
        """None 表示从未出现过（也没有墓碑）。"""

        return self.entries.get(parts)


def build_image_tree(layers: list[list[LayerEvent]]) -> ImageTree:
    """按层列表（索引 0 为最底层）合并出镜像视图。"""

    entries: dict[tuple[str, ...], Node | Tomb] = {}
    # 父路径分量 -> 直接子名集合，让子树操作按子树实际大小完成，避免全表扫描。
    children: dict[tuple[str, ...], set[str]] = {}

    def _detach_subtree(root_parts: tuple[str, ...]) -> None:
        """DFS 删除 root_parts 及其全部后代（含子节点索引）。"""

        stack = [root_parts]
        while stack:
            cur = stack.pop()
            for child_name in children.pop(cur, ()):
                stack.append(cur + (child_name,))
            entries.pop(cur, None)
            if cur:
                siblings = children.get(cur[:-1])
                if siblings is not None:
                    siblings.discard(cur[-1])

    def _delete_subtree(root_parts: tuple[str, ...], tomb: Tomb) -> None:
        """删除整棵子树，并把 root_parts 自身立墓碑。"""

        _detach_subtree(root_parts)
        entries[root_parts] = tomb
        if root_parts:
            children.setdefault(root_parts[:-1], set()).add(root_parts[-1])

    def _opaque_hide_descendants(dir_parts: tuple[str, ...], tomb: Tomb) -> None:
        """把 dir_parts 的所有现存后代立 opaque 墓碑（目录自身不动）。"""

        stack = [p for p in (dir_parts + (n,) for n in children.get(dir_parts, ()))]
        while stack:
            cur = stack.pop()
            stack.extend(cur + (n,) for n in children.get(cur, ()))
            entries[cur] = tomb

    for layer_index, events in enumerate(layers):
        # 两阶段处理：先应用本层所有删除标记，再应用本层新增条目。
        # 这样无论 whiteout 与同层新增在 tar 中的先后顺序如何，
        # 「删除标记只影响下层，同层新增照常可见」的规则都成立。
        for event in events:
            if isinstance(event, OpaqueEvent):
                tomb = Tomb(layer_index=layer_index, reason="opaque")
                _opaque_hide_descendants(event.dir_parts, tomb)
                # 目录自身若是下层墓碑，不透明标记本身不把它“复活”，
                # 交给后续新增事件处理。
            elif isinstance(event, WhiteoutEvent):
                target = event.parent_parts + (event.name,)
                _delete_subtree(target, Tomb(layer_index=layer_index, reason="whiteout"))

        for event in events:
            if not isinstance(event, NodeEvent):
                continue
            node = Node(
                entry_type=event.entry_type,
                layer_index=layer_index,
                link_target=event.link_target,
            )
            old = entries.get(event.parts)
            if old is not None and event.entry_type != "dir":
                # 新条目不是目录：先清掉旧路径下挂着的后代，保证树一致。
                _detach_subtree(event.parts)
            entries[event.parts] = node
            if event.parts:
                children.setdefault(event.parts[:-1], set()).add(event.parts[-1])

    return ImageTree(entries=entries)


# ---------------------------------------------------------------------------
# 挂载点与目标路径的公共校验
# ---------------------------------------------------------------------------

def normalize_absolute(path: str, field: str) -> tuple[str, ...]:
    """把用户提交的绝对路径拆成纯分量。"""

    if not isinstance(path, str) or not path:
        raise InvalidInputError(f"{field} 不能为空")
    if "\x00" in path:
        raise InvalidInputError(f"{field} 含 NUL 字节")
    if not path.startswith("/"):
        raise InvalidInputError(f"{field} 必须是绝对路径（以 / 开头）: {path}")
    parts = tuple(p for p in path.split("/") if p not in ("", "."))
    if any(p == ".." for p in parts):
        raise InvalidInputError(f"{field} 不允许 '..' 分量，请提交规范化绝对路径: {path}")
    return parts


def validate_mount_points(
    raw_mounts: list[str], tree: ImageTree
) -> list[tuple[str, ...]]:
    """校验挂载点：绝对路径、镜像里存在且为目录、彼此互不嵌套。"""

    mounts: list[tuple[str, ...]] = []
    for raw in raw_mounts:
        parts = normalize_absolute(raw, "挂载点")
        if parts in mounts:
            raise InvalidInputError(f"挂载点重复提交: {raw}")
        found = tree.lookup(parts)
        if found is None:
            raise InvalidInputError(f"挂载点 {raw} 在镜像中不存在")
        if isinstance(found, Tomb):
            raise InvalidInputError(
                f"挂载点 {raw} 在镜像中已被第 {found.layer_index} 层的"
                f"{'不透明标记' if found.reason == 'opaque' else 'whiteout 删除标记'}遮蔽，不是可用目录"
            )
        if found.entry_type != "dir":
            raise InvalidInputError(f"挂载点 {raw} 在镜像中不是目录（实际为 {found.entry_type}）")
        mounts.append(parts)

    # 互不嵌套：任意两个挂载点都不能存在前缀关系。
    for i, a in enumerate(mounts):
        for j, b in enumerate(mounts):
            if i != j and len(a) < len(b) and b[: len(a)] == a:
                raise InvalidInputError(
                    f"挂载点不允许嵌套: /{'/'.join(a)} 包含 /{'/'.join(b)}"
                )
    return mounts
