"""FastAPI 排障接口。

POST /api/troubleshoot：
  * 底到顶上传若干未压缩 OCI 层 tar（multipart 字段 ``layers``，
    顺序即从最底层到最顶层）；
  * 提交待查容器内绝对路径 ``target_path``；
  * 提交 ``mount_points``（JSON 字符串数组）与 ``volume_entries``
    （JSON 对象：挂载点 -> 卷内条目清单）；
  * 分别返回「挂载前（纯镜像叠加视图）」与「挂载后（卷遮蔽挂载点）」的查找结果。

POST /api/volumes/init：
  * 挂卷前把镜像里某个源目录的默认配置初始化进命名卷（持久化落盘）；
  * 卷写齐才生效，可按名查询清单（GET /api/volumes/{name}/entries），
    清单格式与排障接口的 ``volume_entries`` 直接兼容。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .errors import InvalidInputError, TroubleshootError
from .imagefs import build_image_tree, normalize_absolute, validate_mount_points
from .resolver import ContainerView, ResolveResult, resolve
from .tarloader import load_layer
from .volume import VolumeTree, build_volume_tree
from .volstore import VolumeStore

MAX_LAYER_BYTES = 128 * 1024 * 1024  # 单层 128 MiB
MAX_TOTAL_BYTES = 384 * 1024 * 1024  # 全部层合计 384 MiB

# 持久卷根目录（可用环境变量覆盖；docker-compose 里挂命名卷持久化）。
VOLUME_ROOT_ENV = "VOLUME_ROOT"
DEFAULT_VOLUME_ROOT = "/srv/data/volumes"

app = FastAPI(
    title="OCI 层叠加与卷挂载排障接口",
    description=(
        "按底到顶上传未压缩 OCI 层 tar，模拟 whiteout / 不透明目录叠加、"
        "卷挂载遮蔽、软链接（可跨挂载点，成环报错）解析，"
        "对比挂载前后的路径查找结果。"
    ),
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# 响应模型（仅用于 OpenAPI /docs 展示与输出规整）
# ---------------------------------------------------------------------------

class SourceInfo(BaseModel):
    origin: str = Field(description="来源类别：layer=镜像层，volume=挂载卷，image=镜像根")
    layer_index: Optional[int] = Field(default=None, description="镜像层索引，0 为最底层")
    mountpoint: Optional[str] = Field(default=None, description="卷来源时的挂载点")
    detail: Optional[str] = None


class SymlinkHopModel(BaseModel):
    symlink_path: str = Field(description="被跟随的软链接容器绝对路径")
    target: str = Field(description="软链接原始目标（链接里写的内容）")
    resolved_from: str = Field(description="跟随该链接时所在的目录")


class LookupOutcome(BaseModel):
    status: str = Field(description="found=找到；missing=路径中断；error=解析错误（如成环）")
    query_path: str
    actual_path: Optional[str] = Field(default=None, description="解析后的实际容器绝对路径")
    entry_type: Optional[str] = Field(default=None, description="file/dir/symlink")
    source: Optional[SourceInfo] = Field(default=None, description="来源层或卷")
    broken_segment: Optional[str] = Field(default=None, description="中断在哪个路径分量上")
    broken_at: Optional[str] = Field(default=None, description="中断处的绝对路径")
    reason: Optional[str] = Field(default=None, description="中断或报错的原因说明")
    error_code: Optional[str] = Field(default=None, description="symlink_cycle / too_many_symlink_hops")
    shadowed_by_mount: Optional[str] = Field(
        default=None, description="中断/命中位置所在的挂载点（即镜像内容被卷遮蔽处）"
    )
    symlink_hops: list[SymlinkHopModel] = Field(default_factory=list)


class TroubleshootResponse(BaseModel):
    target_path: str
    layer_count: int
    mount_points: list[str]
    before_mount: LookupOutcome = Field(description="挂载前：仅镜像层叠加的查找结果")
    after_mount: LookupOutcome = Field(description="挂载后：挂载点被卷遮蔽后的查找结果")


# ---------------------------------------------------------------------------
# 卷初始化：响应模型与存储依赖
# ---------------------------------------------------------------------------

class VolumeEntryModel(BaseModel):
    path: str = Field(description="相对卷根的路径，可直接用于排障接口 volume_entries")
    type: str = Field(description="file | dir")


class VolumeInitResponse(BaseModel):
    volume_name: str
    source_dir: str = Field(description="镜像内的源目录绝对路径")
    status: str = Field(
        description="initialized=本次请求生效；already_initialized=卷此前已生效，返回既有清单"
    )
    entries: list[VolumeEntryModel] = Field(
        description="卷内条目清单（相对卷根），可直接作为排障接口 volume_entries 的值"
    )
    file_count: int
    dir_count: int
    total_bytes: int


class VolumeSummary(BaseModel):
    volume_name: str
    source_dir: str
    initialized_at: str
    file_count: int
    dir_count: int
    total_bytes: int


class VolumeEntriesResponse(BaseModel):
    volume_name: str
    source_dir: str
    entries: list[VolumeEntryModel]


_store_singleton: Optional[VolumeStore] = None
_store_singleton_lock = threading.Lock()


def get_volume_store() -> VolumeStore:
    """进程级单例：卷名互斥锁必须跨请求共享，测试可用依赖覆盖替换。"""

    global _store_singleton
    if _store_singleton is None:
        with _store_singleton_lock:
            if _store_singleton is None:
                _store_singleton = VolumeStore(
                    os.environ.get(VOLUME_ROOT_ENV, DEFAULT_VOLUME_ROOT)
                )
    return _store_singleton


@app.exception_handler(TroubleshootError)
async def _troubleshoot_error_handler(_: Request, exc: TroubleshootError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message})


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"])
async def health() -> dict:
    return {"status": "ok"}


def _result_to_dict(result: ResolveResult) -> dict:
    return {
        "status": result.status,
        "query_path": result.query_path,
        "actual_path": result.actual_path,
        "entry_type": result.entry_type,
        "source": result.source,
        "broken_segment": result.broken_segment,
        "broken_at": result.broken_at,
        "reason": result.reason,
        "error_code": result.error_code,
        "shadowed_by_mount": result.shadowed_by_mount,
        "symlink_hops": [
            {
                "symlink_path": hop.symlink_path,
                "target": hop.target,
                "resolved_from": hop.resolved_from,
            }
            for hop in result.symlink_hops
        ],
    }


@app.post(
    "/api/troubleshoot",
    response_model=TroubleshootResponse,
    tags=["排障"],
    summary="上传 OCI 层与挂载清单，对比挂载前后的路径查找结果",
)
async def troubleshoot(
    layers: list[UploadFile] = File(
        description=(
            "未压缩 OCI 层 tar 文件，multipart 顺序必须是从最底层到最顶层；"
            "接口只读取 tar 条目元数据，不提取任何文件内容。"
        )
    ),
    target_path: str = Form(description="待查的容器内绝对路径，如 /etc/app/config.yaml"),
    mount_points: str = Form(
        default="[]",
        description="JSON 字符串数组：挂载点绝对路径，必须是镜像里的目录且互不嵌套",
    ),
    volume_entries: str = Form(
        default="{}",
        description=(
            "JSON 对象：键为挂载点绝对路径，值为卷内条目清单 "
            '[{"path": "相对卷根的路径", "type": "file|dir"}]；卷只含文件和目录'
        ),
    ),
) -> TroubleshootResponse:
    # 1) 读入并逐层解析 tar（保留上传顺序：底 -> 顶）
    blobs = await _read_layer_blobs(layers)
    layer_events = []
    for index, (upload, data) in enumerate(zip(layers, blobs)):
        name = upload.filename or f"layer[{index}]"
        layer_events.append(load_layer(data, index, name))

    # 2) 合并镜像层
    tree = build_image_tree(layer_events)

    # 3) 校验目标路径
    target_parts = normalize_absolute(target_path, "target_path")

    # 4) 解析挂载点 JSON 并校验（镜像里存在、是目录、互不嵌套）
    try:
        raw_mounts = json.loads(mount_points)
    except json.JSONDecodeError as exc:
        raise InvalidInputError(f"mount_points 不是合法 JSON: {exc}") from exc
    if not isinstance(raw_mounts, list) or not all(isinstance(x, str) for x in raw_mounts):
        raise InvalidInputError("mount_points 必须是字符串数组（绝对路径）")
    mount_parts_list = validate_mount_points(raw_mounts, tree)

    # 5) 解析卷清单并逐挂载点构建卷视图
    try:
        raw_volumes = json.loads(volume_entries)
    except json.JSONDecodeError as exc:
        raise InvalidInputError(f"volume_entries 不是合法 JSON: {exc}") from exc
    if not isinstance(raw_volumes, dict):
        raise InvalidInputError("volume_entries 必须是 JSON 对象：挂载点 -> 条目清单")

    # 把用户给的卷清单键统一规范化为分量形式，避免 /a、/a/ 写法差异。
    normalized_volumes: dict[tuple[str, ...], list] = {}
    for raw_key, entries in raw_volumes.items():
        key_parts = normalize_absolute(raw_key, "volume_entries 的键")
        if key_parts in normalized_volumes:
            raise InvalidInputError(f"volume_entries 中挂载点 {raw_key} 重复")
        if not isinstance(entries, list):
            raise InvalidInputError(f"挂载点 {raw_key} 的卷清单必须是数组")
        normalized_volumes[key_parts] = entries

    mounts: dict[tuple[str, ...], VolumeTree] = {}
    for mount_parts in mount_parts_list:
        if mount_parts not in normalized_volumes:
            raise InvalidInputError(
                f"挂载点 {_join_parts(mount_parts)} 缺少对应的 volume_entries 清单（可为空数组）"
            )
        mounts[mount_parts] = build_volume_tree(
            normalized_volumes[mount_parts], f"挂载点 {_join_parts(mount_parts)} 的卷"
        )

    extra = [k for k in normalized_volumes if k not in mount_parts_list]
    if extra:
        raise InvalidInputError(
            "volume_entries 出现了未在 mount_points 中声明的挂载点: "
            + ", ".join(_join_parts(p) for p in extra)
        )

    # 6) 挂载前 / 挂载后分别解析
    before_view = ContainerView(tree, mounts={})
    after_view = ContainerView(tree, mounts=mounts)

    before = resolve(before_view, target_parts)
    after = resolve(after_view, target_parts)

    return TroubleshootResponse(
        target_path=_join_parts(target_parts),
        layer_count=len(layers),
        mount_points=[_join_parts(p) for p in mount_parts_list],
        before_mount=LookupOutcome(**_result_to_dict(before)),
        after_mount=LookupOutcome(**_result_to_dict(after)),
    )


def _join_parts(parts: tuple[str, ...]) -> str:
    return "/" + "/".join(parts) if parts else "/"


async def _read_layer_blobs(layers: list[UploadFile]) -> list[bytes]:
    """读入上传的层 tar 字节（保持上传顺序：底 -> 顶），并套用防御性上限。"""

    if not layers:
        raise InvalidInputError("至少需要上传一个 OCI 层 tar（layers 字段）")
    total_bytes = 0
    blobs: list[bytes] = []
    for index, upload in enumerate(layers):
        data = await upload.read()
        total_bytes += len(data)
        if len(data) > MAX_LAYER_BYTES:
            raise InvalidInputError(f"第 {index} 层超过 128MiB 单层上限")
        if total_bytes > MAX_TOTAL_BYTES:
            raise InvalidInputError("全部层合计超过 384MiB 上限")
        blobs.append(data)
    return blobs


# ---------------------------------------------------------------------------
# 卷初始化：挂卷前把镜像源目录的默认配置放进命名卷
# ---------------------------------------------------------------------------

@app.post(
    "/api/volumes/init",
    response_model=VolumeInitResponse,
    status_code=201,
    tags=["卷初始化"],
    summary="上传 OCI 层，把镜像源目录最终可见的 file/dir 初始化进命名卷",
)
async def init_volume(
    response: Response,
    volume_name: str = Form(
        description="目标卷名（字母数字开头，可含 . _ -）；同名重复请求返回已生效清单"
    ),
    source_dir: str = Form(
        description="镜像内的源目录绝对路径，如 /etc/app；其最终可见内容写入卷根"
    ),
    layers: list[UploadFile] = File(
        description=(
            "未压缩 OCI 层 tar 文件，multipart 顺序必须是从最底层到最顶层；"
            "文件字节取自可见条目所在的层，分块复制落盘"
        )
    ),
    store: VolumeStore = Depends(get_volume_store),
) -> VolumeInitResponse:
    blobs = await _read_layer_blobs(layers)
    src_parts = normalize_absolute(source_dir, "source_dir")
    # 落盘与 tar 解析是阻塞操作，放到线程里执行；不同卷名互不阻塞。
    outcome = await asyncio.to_thread(
        store.initialize, volume_name, src_parts, blobs
    )
    info = outcome.info
    if not outcome.created:
        response.status_code = 200  # 重复请求：返回已生效的卷清单
    return VolumeInitResponse(
        volume_name=info.volume_name,
        source_dir=info.source_dir,
        status="initialized" if outcome.created else "already_initialized",
        entries=[VolumeEntryModel(**item) for item in info.entries],
        file_count=info.file_count,
        dir_count=info.dir_count,
        total_bytes=info.total_bytes,
    )


@app.get(
    "/api/volumes",
    response_model=list[VolumeSummary],
    tags=["卷初始化"],
    summary="列出全部已写齐生效的命名卷",
)
async def list_volumes(store: VolumeStore = Depends(get_volume_store)) -> list[VolumeSummary]:
    ready = await asyncio.to_thread(store.list_ready)
    return [
        VolumeSummary(
            volume_name=info.volume_name,
            source_dir=info.source_dir,
            initialized_at=info.initialized_at,
            file_count=info.file_count,
            dir_count=info.dir_count,
            total_bytes=info.total_bytes,
        )
        for info in ready
    ]


@app.get(
    "/api/volumes/{volume_name}/entries",
    response_model=VolumeEntriesResponse,
    tags=["卷初始化"],
    summary="按名读取已生效卷的条目清单（供排障接口 volume_entries 使用）",
)
async def get_volume_entries(
    volume_name: str, store: VolumeStore = Depends(get_volume_store)
) -> VolumeEntriesResponse:
    info = await asyncio.to_thread(store.get_info, volume_name)
    return VolumeEntriesResponse(
        volume_name=info.volume_name,
        source_dir=info.source_dir,
        entries=[VolumeEntryModel(**item) for item in info.entries],
    )
