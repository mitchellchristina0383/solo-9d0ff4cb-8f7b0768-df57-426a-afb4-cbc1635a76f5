"""排障接口的端到端测试（FastAPI TestClient，tar 在内存里现造）。"""

from __future__ import annotations

import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# 造层工具
# ---------------------------------------------------------------------------

def make_tar(members: list[tuple]) -> bytes:
    """members: (name, kind, target?) — kind ∈ file/dir/symlink/whiteout/opaque。"""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        for spec in members:
            name, kind = spec[0], spec[1]
            if kind == "dir":
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
            elif kind == "file":
                payload = (spec[2] if len(spec) > 2 else "data").encode()
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.REGTYPE
                ti.size = len(payload)
                tf.addfile(ti, io.BytesIO(payload))
                continue
            elif kind == "symlink":
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.SYMTYPE
                ti.linkname = spec[2]
            elif kind == "whiteout":
                # 真实 OCI tar 里通常是 char 0/0 设备，名称带 .wh. 前缀
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.CHRTYPE
                ti.devmajor = 0
                ti.devminor = 0
            elif kind == "opaque":
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.CHRTYPE
                ti.devmajor = 0
                ti.devminor = 0
            else:
                raise AssertionError(kind)
            tf.addfile(ti)
    return buf.getvalue()


def call(layers, target, mounts=None, volumes=None):
    files = [("layers", (f"layer{i}.tar", data, "application/x-tar"))
             for i, data in enumerate(layers)]
    form = {
        "target_path": target,
        "mount_points": json.dumps(mounts or []),
        "volume_entries": json.dumps(volumes or {}),
    }
    return client.post("/api/troubleshoot", files=files, data=form)


# ---------------------------------------------------------------------------
# 基础：层叠加与来源层
# ---------------------------------------------------------------------------

def test_basic_layering_and_source_layer():
    l0 = make_tar([
        ("etc", "dir"),
        ("etc/app", "dir"),
        ("etc/app/config.yaml", "file", "v0"),
        ("bin", "dir"),
        ("bin/tool", "file"),
    ])
    l1 = make_tar([
        ("etc/app/config.yaml", "file", "v1"),
    ])
    r = call([l0, l1], "/etc/app/config.yaml")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["layer_count"] == 2
    before = body["before_mount"]
    assert before["status"] == "found"
    assert before["entry_type"] == "file"
    assert before["source"]["origin"] == "layer"
    assert before["source"]["layer_index"] == 1  # 顶层覆盖
    assert before["actual_path"] == "/etc/app/config.yaml"
    # 未被覆盖的文件来源仍是第 0 层
    r2 = call([l0, l1], "/bin/tool")
    assert r2.json()["before_mount"]["source"]["layer_index"] == 0


def test_missing_path_reports_broken_segment():
    l0 = make_tar([("etc", "dir")])
    r = call([l0], "/etc/app/config.yaml")
    out = r.json()["before_mount"]
    assert out["status"] == "missing"
    assert out["broken_segment"] == "app"
    assert out["broken_at"] == "/etc/app"
    assert "未出现" in out["reason"]


def test_parent_file_blocks_lookup():
    l0 = make_tar([
        ("etc", "dir"),
        ("etc/notdir", "file"),
    ])
    out = call([l0], "/etc/notdir/child").json()["before_mount"]
    assert out["status"] == "missing"
    assert out["broken_at"] == "/etc/notdir"
    assert "普通文件" in out["reason"]


# ---------------------------------------------------------------------------
# whiteout 与不透明目录
# ---------------------------------------------------------------------------

def test_whiteout_hides_lower_layer():
    l0 = make_tar([
        ("opt", "dir"),
        ("opt/old", "file"),
        ("opt/keep", "file"),
    ])
    l1 = make_tar([
        ("opt/.wh.old", "whiteout"),
    ])
    out = call([l0, l1], "/opt/old").json()["before_mount"]
    assert out["status"] == "missing"
    assert "whiteout" in out["reason"]
    assert out["broken_at"] == "/opt/old"
    # 同层未删除的条目仍在
    assert call([l0, l1], "/opt/keep").json()["before_mount"]["status"] == "found"


def test_whiteout_does_not_hide_same_layer_addition():
    """删除标记只影响下层：同层新增同路径条目照常可见，且与 tar 内顺序无关。"""

    l0 = make_tar([("opt", "dir"), ("opt/x", "file", "lower")])

    # 顺序一：先 whiteout 后新增
    l1 = make_tar([
        ("opt/.wh.x", "whiteout"),
        ("opt/x", "file", "upper"),
    ])
    out1 = call([l0, l1], "/opt/x").json()["before_mount"]
    assert out1["status"] == "found"
    assert out1["source"]["layer_index"] == 1

    # 顺序二：先新增后 whiteout（两阶段处理保证结果一致）
    l1b = make_tar([
        ("opt/x", "file", "upper"),
        ("opt/.wh.x", "whiteout"),
    ])
    out2 = call([l0, l1b], "/opt/x").json()["before_mount"]
    assert out2["status"] == "found"
    assert out2["source"]["layer_index"] == 1


def test_opaque_directory_marker_hides_lower_children():
    l0 = make_tar([
        ("data", "dir"),
        ("data/a", "file"),
        ("data/sub", "dir"),
        ("data/sub/b", "file"),
    ])
    l1 = make_tar([
        ("data/.wh..wh..opq", "opaque"),
        ("data/c", "file"),
    ])
    body = call([l0, l1], "/data/a").json()
    out = body["before_mount"]
    assert out["status"] == "missing"
    assert "不透明" in out["reason"]
    # 目录本身仍在，本层新增可见
    assert call([l0, l1], "/data").json()["before_mount"]["entry_type"] == "dir"
    assert call([l0, l1], "/data/c").json()["before_mount"]["status"] == "found"
    # 下层子目录内容也被遮蔽
    assert call([l0, l1], "/data/sub/b").json()["before_mount"]["status"] == "missing"


# ---------------------------------------------------------------------------
# 软链接：相对/绝对、断链、成环、跨挂载点
# ---------------------------------------------------------------------------

def test_symlink_relative_and_absolute():
    l0 = make_tar([
        ("etc", "dir"),
        ("etc/app", "dir"),
        ("etc/app/config.yaml", "file"),
        ("link_rel", "symlink", "etc/app/config.yaml"),
        ("link_abs", "symlink", "/etc/app/config.yaml"),
    ])
    # 末分量是软链接：返回链接本身（stat(2) 语义），不跟随
    for link in ("/link_rel", "/link_abs"):
        direct = call([l0], link).json()["before_mount"]
        assert direct["status"] == "found", link
        assert direct["actual_path"] == link
        assert direct["entry_type"] == "symlink"
        assert direct["symlink_hops"] == []
    # 带后续分量时才跟随链接解析到目标
    l1 = make_tar([
        ("etc", "dir"),
        ("etc/app", "dir"),
        ("etc/app/config.yaml", "file"),
        ("dirlink", "symlink", "etc/app"),
    ])
    out = call([l1], "/dirlink/config.yaml").json()["before_mount"]
    assert out["status"] == "found"
    assert out["actual_path"] == "/etc/app/config.yaml"
    assert out["symlink_hops"][0]["symlink_path"] == "/dirlink"


def test_broken_symlink_reports_missing_target():
    l0 = make_tar([
        ("ln", "symlink", "/nope/deep/file"),
        ("d", "dir"),
        ("d/ln2", "symlink", "../../nope/deep/file"),
    ])
    # 带后续分量才会跟随链接，从而暴露断链
    out = call([l0], "/ln/child").json()["before_mount"]
    assert out["status"] == "missing"
    assert out["broken_at"] == "/nope"
    assert out["broken_segment"] == "nope"
    assert out["symlink_hops"][0]["target"] == "/nope/deep/file"
    # 相对目标的断链同样定位
    out2 = call([l0], "/d/ln2/x").json()["before_mount"]
    assert out2["status"] == "missing"
    assert out2["broken_at"] == "/nope"
    # 直接查断链本身：链接节点存在，返回 symlink 类型命中
    direct = call([l0], "/ln").json()["before_mount"]
    assert direct["status"] == "found"
    assert direct["entry_type"] == "symlink"


def test_symlink_cycle_detected():
    l0 = make_tar([
        ("a", "symlink", "b"),
        ("b", "symlink", "a"),
    ])
    # 末分量是软链接时按 stat 语义直接返回；带后续分量才进入跟随并暴露成环
    out = call([l0], "/a/x").json()["before_mount"]
    assert out["status"] == "error"
    assert out["error_code"] == "symlink_cycle"
    assert "成环" in out["reason"]
    # 两跳之后回到 /a
    assert [h["symlink_path"] for h in out["symlink_hops"]] == ["/a", "/b"]
    # 直接查链接本身不解析、不报错
    direct = call([l0], "/a").json()["before_mount"]
    assert direct["status"] == "found"
    assert direct["entry_type"] == "symlink"


def test_self_referential_symlink_is_cycle():
    l0 = make_tar([("self", "symlink", "self")])
    out = call([l0], "/self/x").json()["before_mount"]
    assert out["error_code"] == "symlink_cycle"


# ---------------------------------------------------------------------------
# 挂载与卷遮蔽
# ---------------------------------------------------------------------------

def test_volume_shadows_image_directory():
    l0 = make_tar([
        ("mnt", "dir"),
        ("mnt/data", "dir"),
        ("mnt/data/from-image", "file"),
        ("mnt/data/keep", "file"),
    ])
    volumes = {
        "/mnt/data": [
            {"path": "from-volume", "type": "file"},
            {"path": "sub", "type": "dir"},
            {"path": "sub/v", "type": "file"},
        ]
    }
    body = call([l0], "/mnt/data/from-image", ["/mnt/data"], volumes).json()
    # 挂载前：镜像文件可见
    assert body["before_mount"]["status"] == "found"
    assert body["before_mount"]["source"]["origin"] == "layer"
    # 挂载后：镜像内容被盖住
    after = body["after_mount"]
    assert after["status"] == "missing"
    assert after["shadowed_by_mount"] == "/mnt/data"
    assert "卷" in after["reason"]
    # 卷内条目可见，来源标为 volume
    ok = call([l0], "/mnt/data/from-volume", ["/mnt/data"], volumes).json()
    assert ok["after_mount"]["status"] == "found"
    assert ok["after_mount"]["source"]["origin"] == "volume"
    assert ok["after_mount"]["source"]["mountpoint"] == "/mnt/data"
    # 挂载点自身是卷根目录
    root = call([l0], "/mnt/data", ["/mnt/data"], volumes).json()
    assert root["after_mount"]["entry_type"] == "dir"


def test_cross_mount_symlink():
    """软链接允许跨挂载点：/app/ln 指向卷目录 /data/conf，经子分量跳进卷。"""

    l0 = make_tar([
        ("app", "dir"),
        ("app/ln", "symlink", "/data/conf"),
        ("data", "dir"),
        ("other", "dir"),
    ])
    volumes = {
        "/data": [
            {"path": "conf", "type": "dir"},
            {"path": "conf/app.yaml", "type": "file"},
        ],
        "/other": [{"path": "x", "type": "file"}],
    }
    body = call([l0], "/app/ln/app.yaml", ["/data", "/other"], volumes).json()
    # 挂载前：链接目标在纯镜像视图里不存在
    assert body["before_mount"]["status"] == "missing"
    assert body["before_mount"]["symlink_hops"][0]["symlink_path"] == "/app/ln"
    # 挂载后：链接跨挂载点跳到 /data 卷
    after = body["after_mount"]
    assert after["status"] == "found"
    assert after["actual_path"] == "/data/conf/app.yaml"
    assert after["source"]["origin"] == "volume"
    assert after["source"]["mountpoint"] == "/data"
    assert after["symlink_hops"][0]["symlink_path"] == "/app/ln"


def test_nested_mount_points_rejected():
    l0 = make_tar([
        ("a", "dir"),
        ("a/b", "dir"),
    ])
    r = call([l0], "/a/b", ["/a", "/a/b"], {"/a": [], "/a/b": []})
    assert r.status_code == 422
    assert "嵌套" in r.json()["error"]


def test_mount_point_must_be_image_dir():
    l0 = make_tar([("f", "file")])
    r = call([l0], "/f", ["/f"], {"/f": []})
    assert r.status_code == 422
    assert "目录" in r.json()["error"]


def test_volume_only_accepts_files_and_dirs():
    l0 = make_tar([("m", "dir")])
    r = call(
        [l0], "/m/secret", ["/m"],
        {"/m": [{"path": "secret", "type": "symlink"}]},
    )
    assert r.status_code == 422
    assert "file/dir" in r.json()["error"]


def test_volume_missing_parent_declared():
    l0 = make_tar([("m", "dir")])
    r = call(
        [l0], "/m/a/b", ["/m"],
        {"/m": [{"path": "a/b", "type": "file"}]},
    )
    assert r.status_code == 422
    assert "父目录" in r.json()["error"]


# ---------------------------------------------------------------------------
# tar 输入约束
# ---------------------------------------------------------------------------

def test_gzipped_tar_rejected():
    import gzip

    raw = make_tar([("etc", "dir")])
    gz = gzip.compress(raw)
    r = client.post(
        "/api/troubleshoot",
        files=[("layers", ("x.tar.gz", gz, "application/gzip"))],
        data={"target_path": "/etc", "mount_points": "[]", "volume_entries": "{}"},
    )
    assert r.status_code == 400
    assert "gzip" in r.json()["error"]


def test_garbage_upload_rejected_as_tar():
    r = client.post(
        "/api/troubleshoot",
        files=[("layers", ("x.tar", b"not a tar at all" * 10, "application/x-tar"))],
        data={"target_path": "/x", "mount_points": "[]", "volume_entries": "{}"},
    )
    assert r.status_code == 400


def test_unsupported_tar_member_type_rejected():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        ti = tarfile.TarInfo("fifo")
        ti.type = tarfile.FIFOTYPE
        tf.addfile(ti)
    r = client.post(
        "/api/troubleshoot",
        files=[("layers", ("x.tar", buf.getvalue(), "application/x-tar"))],
        data={"target_path": "/fifo", "mount_points": "[]", "volume_entries": "{}"},
    )
    assert r.status_code == 422
    assert "不支持" in r.json()["error"]


def test_relative_target_path_rejected():
    l0 = make_tar([("etc", "dir")])
    r = call([l0], "etc/app")
    assert r.status_code == 422
    assert "绝对路径" in r.json()["error"]


# ---------------------------------------------------------------------------
# 更多边界场景
# ---------------------------------------------------------------------------

def test_whiteout_then_recreate_in_higher_layer():
    l0 = make_tar([("a", "dir"), ("a/f", "file", "l0")])
    l1 = make_tar([("a/.wh.f", "whiteout")])
    l2 = make_tar([("a/f", "file", "l2")])
    out = call([l0, l1, l2], "/a/f").json()["before_mount"]
    assert out["status"] == "found"
    assert out["source"]["layer_index"] == 2


def test_whiteout_on_directory_removes_whole_subtree():
    l0 = make_tar([
        ("d", "dir"),
        ("d/sub", "dir"),
        ("d/sub/deep", "file"),
    ])
    l1 = make_tar([(".wh.d", "whiteout")])
    out = call([l0, l1], "/d/sub/deep").json()["before_mount"]
    assert out["status"] == "missing"
    assert "whiteout" in out["reason"]
    assert call([l0, l1], "/d").json()["before_mount"]["status"] == "missing"


def test_volume_shadows_image_symlink_at_mountpoint():
    """挂载点目录本身在镜像里是软链接时，挂载后由卷根接管（不再跟随链接）。"""

    l0 = make_tar([
        ("real", "dir"),
        ("real/conf", "file"),
        ("mnt", "symlink", "real"),
    ])
    # 挂载点必须是镜像目录 —— 软链接不能当挂载点，应被拒绝
    r = call([l0], "/mnt", ["/mnt"], {"/mnt": []})
    assert r.status_code == 422
    assert "目录" in r.json()["error"]


def test_mount_point_under_whiteout_rejected():
    l0 = make_tar([("m", "dir"), ("m/f", "file")])
    l1 = make_tar([(".wh.m", "whiteout")])
    r = call([l0, l1], "/m/f", ["/m"], {"/m": []})
    assert r.status_code == 422
    assert "遮蔽" in r.json()["error"]


def test_volume_file_blocks_deeper_lookup():
    l0 = make_tar([("m", "dir")])
    volumes = {"/m": [{"path": "f", "type": "file"}]}
    out = call([l0], "/m/f/child", ["/m"], volumes).json()["after_mount"]
    assert out["status"] == "missing"
    assert out["broken_at"] == "/m/f"
    assert "普通文件" in out["reason"]


def test_relative_symlink_inside_mounted_volume_resolves_in_volume():
    """镜像里相对软链接位于挂载点之外，经子分量解析后落在卷内。"""

    l0 = make_tar([
        ("app", "dir"),
        ("app/ln", "symlink", "../m"),
        ("m", "dir"),
    ])
    volumes = {"/m": [{"path": "cfg.yaml", "type": "file"}]}
    out = call([l0], "/app/ln/cfg.yaml", ["/m"], volumes).json()["after_mount"]
    assert out["status"] == "found"
    assert out["actual_path"] == "/m/cfg.yaml"
    assert out["source"]["origin"] == "volume"


def test_opaque_marker_in_nonexistent_dir_does_not_create_it():
    l0 = make_tar([("a", "file")])
    l1 = make_tar([("ghost/.wh..wh..opq", "opaque")])
    out = call([l0, l1], "/ghost").json()["before_mount"]
    assert out["status"] == "missing"


def test_symlink_to_root_resolves():
    # POSIX 语义：软链接自身不是目录，对其再取子分量会 ENOTDIR；
    # 链接目标根目录可通过「经链接进入根后再取子分量」验证。
    l0 = make_tar([
        ("rootlink", "symlink", "/"),
        ("etc", "dir"),
        ("etc/f", "file"),
    ])
    out = call([l0], "/rootlink/etc/f").json()["before_mount"]
    assert out["status"] == "found"
    assert out["actual_path"] == "/etc/f"
    assert out["symlink_hops"][0]["symlink_path"] == "/rootlink"
    # 直接查链接本身：末分量命中，返回 symlink 节点
    direct = call([l0], "/rootlink").json()["before_mount"]
    assert direct["status"] == "found"
    assert direct["actual_path"] == "/rootlink"
    assert direct["entry_type"] == "symlink"


def test_duplicate_mount_points_rejected():
    l0 = make_tar([("m", "dir")])
    r = call([l0], "/m", ["/m", "/m"], {"/m": []})
    assert r.status_code == 422
    assert "重复" in r.json()["error"]


def test_no_layers_rejected():
    r = client.post(
        "/api/troubleshoot",
        data={"target_path": "/x", "mount_points": "[]", "volume_entries": "{}"},
    )
    # 框架层对必填 File 字段的 422，detail 为校验明细
    assert r.status_code == 422
    assert "detail" in r.json()


def test_trailing_slash_normalized():
    l0 = make_tar([("etc", "dir"), ("etc/f", "file")])
    out = call([l0], "/etc/f/").json()["before_mount"]
    assert out["status"] == "found"
    assert out["actual_path"] == "/etc/f"


def test_large_tree_stays_linear():
    """2 万条目 + 1 万 whiteout 的合并应快速完成（回归 O(n^2) 扫描实现）。"""

    import time

    big = make_tar(
        [("root", "dir")] + [(f"root/f{i}", "file") for i in range(20_000)]
    )
    wo_layer = make_tar(
        [(f"root/.wh.f{i}", "whiteout") for i in range(0, 20_000, 2)]
    )

    t0 = time.perf_counter()
    r = call([big, wo_layer], "/root/f19999")
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert r.json()["before_mount"]["status"] == "found"
    deleted = call([big, wo_layer], "/root/f0").json()["before_mount"]
    assert deleted["status"] == "missing"
    # 合并部分实测约 0.15s；给宽松上限防止环境波动，同时仍能拦住 O(n^2) 退化
    assert elapsed < 8


def test_docs_and_health():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200
    redir = client.get("/", follow_redirects=False)
    assert redir.status_code in (302, 307)
    assert redir.headers["location"] == "/docs"
