"""卷初始化接口测试：层覆盖、可见性、并发、中断重试、与排障接口联动。"""

from __future__ import annotations

import io
import json
import tarfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_volume_store
from app.volstore import MANIFEST_NAME, STAGING_DIRNAME, VolumeStore


# ---------------------------------------------------------------------------
# 造层工具（与 tests/test_troubleshoot.py 同款）
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
                payload = spec[2] if len(spec) > 2 else b"data"
                if isinstance(payload, str):
                    payload = payload.encode()
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.REGTYPE
                ti.size = len(payload)
                tf.addfile(ti, io.BytesIO(payload))
                continue
            elif kind == "symlink":
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.SYMTYPE
                ti.linkname = spec[2]
            elif kind in ("whiteout", "opaque"):
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.CHRTYPE
                ti.devmajor = 0
                ti.devminor = 0
            else:
                raise AssertionError(kind)
            tf.addfile(ti)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# fixture：每个用例一个独立存储目录，并覆盖 FastAPI 依赖
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    s = VolumeStore(tmp_path / "volumes")
    app.dependency_overrides[get_volume_store] = lambda: s
    yield s
    app.dependency_overrides.pop(get_volume_store, None)


@pytest.fixture()
def client(store):
    return TestClient(app)


def init(client, name, source_dir, layers):
    files = [("layers", (f"layer{i}.tar", data, "application/x-tar"))
             for i, data in enumerate(layers)]
    return client.post(
        "/api/volumes/init",
        files=files,
        data={"volume_name": name, "source_dir": source_dir},
    )


def entry_map(resp_json):
    return {item["path"]: item["type"] for item in resp_json["entries"]}


# ---------------------------------------------------------------------------
# 基本初始化 + 层覆盖语义
# ---------------------------------------------------------------------------

def test_init_writes_visible_content_and_listing(client, store):
    l0 = make_tar([
        ("etc", "dir"),
        ("etc/app", "dir"),
        ("etc/app/config.yaml", "file", "v0"),
        ("etc/app/empty.d", "dir"),
        ("etc/app/sub", "dir"),
        ("etc/app/sub/deep.txt", "file", "deep"),
    ])
    r = init(client, "cfg", "/etc/app", [l0])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "initialized"
    assert body["volume_name"] == "cfg"
    assert body["source_dir"] == "/etc/app"
    assert entry_map(body) == {
        "config.yaml": "file",
        "empty.d": "dir",          # 空目录保留
        "sub": "dir",
        "sub/deep.txt": "file",
    }
    assert body["file_count"] == 2
    assert body["dir_count"] == 2
    # 磁盘上的真实内容
    root = store._root / "cfg"
    assert (root / "config.yaml").read_bytes() == b"v0"
    assert (root / "sub" / "deep.txt").read_bytes() == b"deep"
    assert (root / "empty.d").is_dir()
    assert (root / MANIFEST_NAME).is_file()


def test_layer_override_takes_bytes_from_visible_layer(client, store):
    """同名文件被上层覆盖：卷里必须是上层（可见层）的原字节。"""

    l0 = make_tar([
        ("etc", "dir"),
        ("etc/app", "dir"),
        ("etc/app/config.yaml", "file", "v0-lower"),
        ("etc/app/keep.txt", "file", "keep-lower"),
    ])
    l1 = make_tar([
        ("etc/app/config.yaml", "file", "v1-upper"),
    ])
    r = init(client, "cfg", "/etc/app", [l0, l1])
    assert r.status_code == 201, r.text
    root = store._root / "cfg"
    assert (root / "config.yaml").read_bytes() == b"v1-upper"   # 可见层的字节
    assert (root / "keep.txt").read_bytes() == b"keep-lower"    # 未覆盖的仍取下层


def test_whiteout_and_opaque_entries_not_copied(client, store):
    l0 = make_tar([
        ("etc", "dir"),
        ("etc/app", "dir"),
        ("etc/app/old.yaml", "file", "old"),
        ("etc/app/keep.yaml", "file", "keep"),
        ("etc/app/cache", "dir"),
        ("etc/app/cache/a.bin", "file", "a"),
    ])
    l1 = make_tar([
        ("etc/app/.wh.old.yaml", "whiteout"),
        ("etc/app/cache/.wh..wh..opq", "opaque"),
        ("etc/app/cache/new.bin", "file", "new"),
    ])
    r = init(client, "cfg", "/etc/app", [l0, l1])
    assert r.status_code == 201, r.text
    assert entry_map(r.json()) == {
        "keep.yaml": "file",
        "cache": "dir",
        "cache/new.bin": "file",
    }
    root = store._root / "cfg"
    assert not (root / "old.yaml").exists()          # whiteout 删除的不复制
    assert not (root / "cache" / "a.bin").exists()   # 不透明目录遮蔽的下层内容不复制
    assert (root / "cache" / "new.bin").read_bytes() == b"new"


def test_hidden_symlink_is_fine_visible_symlink_fails(client, store):
    """可见软链接报错；被 whiteout 遮蔽的软链接不影响。"""

    l0 = make_tar([
        ("etc", "dir"),
        ("etc/app", "dir"),
        ("etc/app/link", "symlink", "/elsewhere"),
        ("etc/app/real.txt", "file", "real"),
    ])
    r = init(client, "cfg", "/etc/app", [l0])
    assert r.status_code == 422
    assert "软链接" in r.json()["error"]
    assert list((store._root / ".staging").iterdir()) == []  # 无残留

    # 上层 whiteout 删掉软链接后，源目录就不含可见软链接了
    l1 = make_tar([("etc/app/.wh.link", "whiteout")])
    r2 = init(client, "cfg2", "/etc/app", [l0, l1])
    assert r2.status_code == 201, r2.text
    assert entry_map(r2.json()) == {"real.txt": "file"}


def test_source_dir_must_be_visible_dir(client):
    only_file = make_tar([("etc", "dir"), ("etc/app", "file", "x")])
    assert init(client, "v1", "/etc/app", [only_file]).status_code == 422
    assert init(client, "v2", "/not/there", [only_file]).status_code == 422

    link = make_tar([("etc", "dir"), ("etc/app", "symlink", "/other")])
    r = init(client, "v3", "/etc/app", [link])
    assert r.status_code == 422 and "软链接" in r.json()["error"]

    # 源目录被 whiteout 遮蔽
    l0 = make_tar([("etc", "dir"), ("etc/app", "dir")])
    l1 = make_tar([("etc/.wh.app", "whiteout")])
    r = init(client, "v4", "/etc/app", [l0, l1])
    assert r.status_code == 422 and "遮蔽" in r.json()["error"]


def test_source_dir_can_be_image_root(client, store):
    l0 = make_tar([
        ("etc", "dir"),
        ("etc/app", "dir"),
        ("etc/app/cfg", "file", "c"),
        ("top.txt", "file", "t"),
    ])
    r = init(client, "whole", "/", [l0])
    assert r.status_code == 201, r.text
    assert entry_map(r.json()) == {
        "etc": "dir", "etc/app": "dir", "etc/app/cfg": "file", "top.txt": "file",
    }


def test_large_file_copied_in_chunks(tmp_path):
    """小块尺寸 + 较大文件：分块循环必须拼出完整原字节。"""

    store = VolumeStore(tmp_path / "volumes", chunk_size=64)
    app.dependency_overrides[get_volume_store] = lambda: store
    try:
        payload = bytes((i * 31 + 7) % 256 for i in range(100_000))
        l0 = make_tar([("data", "dir"), ("data/big.bin", "file", payload)])
        c = TestClient(app)
        r = init(c, "bigvol", "/data", [l0])
        assert r.status_code == 201, r.text
        assert r.json()["total_bytes"] == len(payload)
        assert (store._root / "bigvol" / "big.bin").read_bytes() == payload
    finally:
        app.dependency_overrides.pop(get_volume_store, None)


def test_invalid_volume_name_rejected(client):
    l0 = make_tar([("etc", "dir")])
    for bad in ("", "../x", "a/b", ".hidden", "-lead", "name with space"):
        r = init(client, bad, "/etc", [l0])
        assert r.status_code == 422, (bad, r.status_code)


def test_reserved_manifest_name_rejected(client):
    l0 = make_tar([
        ("etc", "dir"),
        ("etc/app", "dir"),
        (f"etc/app/{MANIFEST_NAME}", "file", "clash"),
    ])
    r = init(client, "cfg", "/etc/app", [l0])
    assert r.status_code == 422 and "保留" in r.json()["error"]


# ---------------------------------------------------------------------------
# 按名使用：写齐才可见
# ---------------------------------------------------------------------------

def test_volume_usable_by_name_only_after_commit(client, store):
    assert client.get("/api/volumes/cfg/entries").status_code == 404
    assert client.get("/api/volumes").json() == []

    l0 = make_tar([("etc", "dir"), ("etc/app", "dir"), ("etc/app/a", "file", "a")])
    assert init(client, "cfg", "/etc/app", [l0]).status_code == 201

    r = client.get("/api/volumes/cfg/entries")
    assert r.status_code == 200
    assert entry_map(r.json()) == {"a": "file"}
    listed = client.get("/api/volumes").json()
    assert [v["volume_name"] for v in listed] == ["cfg"]
    assert listed[0]["file_count"] == 1


def test_listing_feeds_troubleshoot_volume_entries(client):
    """初始化返回的清单可直接作为排障接口 volume_entries 的值。"""

    l0 = make_tar([
        ("etc", "dir"),
        ("etc/app", "dir"),
        ("etc/app/config.yaml", "file", "v0"),
        ("etc/app/sub", "dir"),
        ("etc/app/sub/x", "file", "x"),
    ])
    body = init(client, "cfg", "/etc/app", [l0]).json()

    files = [("layers", ("layer0.tar", l0, "application/x-tar"))]
    r = client.post(
        "/api/troubleshoot",
        files=files,
        data={
            "target_path": "/etc/app/sub/x",
            "mount_points": json.dumps(["/etc/app"]),
            "volume_entries": json.dumps({"/etc/app": body["entries"]}),
        },
    )
    assert r.status_code == 200, r.text
    after = r.json()["after_mount"]
    assert after["status"] == "found"
    assert after["source"]["origin"] == "volume"
    assert after["source"]["mountpoint"] == "/etc/app"


# ---------------------------------------------------------------------------
# 同名并发与重复请求
# ---------------------------------------------------------------------------

def test_duplicate_request_returns_effective_listing(client, store):
    l0 = make_tar([("etc", "dir"), ("etc/app", "dir"), ("etc/app/a", "file", "a")])
    r1 = init(client, "cfg", "/etc/app", [l0])
    assert r1.status_code == 201

    # 重复请求（哪怕源目录/层不同）只返回已生效清单，不重新生效
    other = make_tar([("etc", "dir"), ("etc/app", "dir"), ("etc/app/b", "file", "b")])
    r2 = init(client, "cfg", "/etc/app", [other])
    assert r2.status_code == 200
    assert r2.json()["status"] == "already_initialized"
    assert entry_map(r2.json()) == {"a": "file"}
    assert not (store._root / "cfg" / "b").exists()


def test_concurrent_same_name_only_one_takes_effect(store):
    l0 = make_tar([("etc", "dir"), ("etc/app", "dir"), ("etc/app/a", "file", "a")])

    def worker(_):
        # 每个线程独立 TestClient（独立事件循环），共享同一个 store
        with TestClient(app) as c:
            return init(c, "race", "/etc/app", [l0])

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(worker, range(8)))

    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(201) == 1, statuses          # 只有一份生效
    assert statuses.count(200) == 7, statuses          # 其余拿到已生效清单
    bodies = [r.json() for r in responses]
    assert sum(b["status"] == "initialized" for b in bodies) == 1
    assert {json.dumps(entry_map(b), sort_keys=True) for b in bodies} == {
        json.dumps({"a": "file"})
    }


def test_concurrent_different_names_all_succeed(store):
    l0 = make_tar([("etc", "dir"), ("etc/app", "dir"), ("etc/app/a", "file", "a")])

    def worker(i):
        with TestClient(app) as c:
            return init(c, f"vol-{i}", "/etc/app", [l0])

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(worker, range(4)))

    assert all(r.status_code == 201 for r in responses)
    assert sorted(v["volume_name"] for v in client_get_volumes()) == [
        "vol-0", "vol-1", "vol-2", "vol-3",
    ]


def client_get_volumes():
    with TestClient(app) as c:
        return c.get("/api/volumes").json()


# ---------------------------------------------------------------------------
# 中断 / 写盘失败后的重试
# ---------------------------------------------------------------------------

def test_failed_write_leaves_no_trace_and_retry_works(client, store, monkeypatch):
    l0 = make_tar([("etc", "dir"), ("etc/app", "dir"), ("etc/app/a", "file", "a")])

    def boom(self, staging, src_parts, plan, layer_blobs):
        # 模拟写了一半时磁盘故障：暂存目录里留点残骸再抛错
        (staging / "partial").write_text("half")
        raise OSError("disk full")

    monkeypatch.setattr(VolumeStore, "_write_plan", boom)
    r = init(client, "cfg", "/etc/app", [l0])
    assert r.status_code == 500
    assert "可重新初始化" in r.json()["error"]

    # 未生效：按名不可用，暂存残骸已清理
    assert client.get("/api/volumes/cfg/entries").status_code == 404
    assert not (store._root / "cfg").exists()
    assert list((store._root / STAGING_DIRNAME).iterdir()) == []

    monkeypatch.undo()
    r2 = init(client, "cfg", "/etc/app", [l0])
    assert r2.status_code == 201, r2.text
    assert (store._root / "cfg" / "a").read_bytes() == b"a"


def test_stale_staging_from_crash_does_not_block_reinit(tmp_path):
    """进程中断留下的暂存残骸：不影响重新初始化，且新 Store 启动时清理。"""

    root = tmp_path / "volumes"
    store = VolumeStore(root)
    stale = root / STAGING_DIRNAME / "cfg.deadbeef"
    stale.mkdir(parents=True)
    (stale / "half-file").write_text("partial")

    # 崩溃后重启进程 = 新建 VolumeStore：残留被清理
    store2 = VolumeStore(root)
    assert list((root / STAGING_DIRNAME).iterdir()) == []

    app.dependency_overrides[get_volume_store] = lambda: store2
    try:
        c = TestClient(app)
        l0 = make_tar([("etc", "dir"), ("etc/app", "dir"), ("etc/app/a", "file", "a")])
        r = init(c, "cfg", "/etc/app", [l0])
        assert r.status_code == 201, r.text
        assert (root / "cfg" / "a").read_bytes() == b"a"
    finally:
        app.dependency_overrides.pop(get_volume_store, None)


def test_initialized_volume_survives_store_restart(tmp_path):
    """已生效的卷在「进程重启」（新建 Store）后仍可按名使用。"""

    root = tmp_path / "volumes"
    store1 = VolumeStore(root)
    app.dependency_overrides[get_volume_store] = lambda: store1
    try:
        c = TestClient(app)
        l0 = make_tar([("etc", "dir"), ("etc/app", "dir"), ("etc/app/a", "file", "a")])
        assert init(c, "cfg", "/etc/app", [l0]).status_code == 201
    finally:
        app.dependency_overrides.pop(get_volume_store, None)

    store2 = VolumeStore(root)  # 模拟重启
    app.dependency_overrides[get_volume_store] = lambda: store2
    try:
        c = TestClient(app)
        r = c.get("/api/volumes/cfg/entries")
        assert r.status_code == 200
        assert entry_map(r.json()) == {"a": "file"}
        # 重启后重复初始化也只返回既有清单
        l0 = make_tar([("etc", "dir"), ("etc/app", "dir"), ("etc/app/a", "file", "a")])
        r2 = init(c, "cfg", "/etc/app", [l0])
        assert r2.status_code == 200
        assert r2.json()["status"] == "already_initialized"
    finally:
        app.dependency_overrides.pop(get_volume_store, None)


def test_concurrent_first_failure_lets_another_take_effect(store, monkeypatch):
    """同名并发时若先拿到锁的请求写盘失败，后面的请求仍可生效。"""

    calls = {"n": 0}
    real_write = VolumeStore._write_plan

    def fail_once(self, staging, src_parts, plan, layer_blobs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient disk error")
        return real_write(self, staging, src_parts, plan, layer_blobs)

    monkeypatch.setattr(VolumeStore, "_write_plan", fail_once)
    l0 = make_tar([("etc", "dir"), ("etc/app", "dir"), ("etc/app/a", "file", "a")])

    def worker(_):
        with TestClient(app) as c:
            return init(c, "flaky", "/etc/app", [l0])

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(worker, range(4)))

    codes = sorted(r.status_code for r in responses)
    assert codes.count(201) == 1, codes       # 恰好一份生效
    assert 500 in codes                        # 首个尝试失败
    assert codes.count(200) + codes.count(201) + codes.count(500) == 4
    assert (store._root / "flaky" / "a").read_bytes() == b"a"
    assert list((store._root / STAGING_DIRNAME).iterdir()) == []


def test_leftover_dir_without_manifest_conflicts(client, store):
    """卷名被无清单的残留目录占用：按名不可用（404），初始化报 409。"""

    (store._root / "junk").mkdir()
    (store._root / "junk" / "half").write_text("x")

    assert client.get("/api/volumes/junk/entries").status_code == 404
    l0 = make_tar([("etc", "dir"), ("etc/app", "dir")])
    r = init(client, "junk", "/etc/app", [l0])
    assert r.status_code == 409
    assert "残留" in r.json()["error"]
