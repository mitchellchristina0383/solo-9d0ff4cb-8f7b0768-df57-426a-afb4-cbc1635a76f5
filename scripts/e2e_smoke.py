"""端到端冒烟脚本：模拟值班排障单。

场景：
  层0（基础镜像）：/etc/app/config.yaml=v0、/var/lib/app、/logs/app.log
  层1（应用更新）：改写 config.yaml=v1、whiteout 删掉 /var/lib/app/old.cache、
                  /etc/app 打不透明标记；新增 /etc/app/new.conf
  层2（修复层）：新建软链接 /appcfg -> /etc/app/config.yaml、/cycle1<->/cycle2
  挂载：/etc/app（卷里只有 mounted.yaml）、/var/lib/app（空卷）
  待测路径覆盖：遮蔽、跨挂载软链接、断链、成环、whiteout 中断。

用法：python -I scripts/e2e_smoke.py [base_url]
层 tar 写到独立的临时目录，脚本目录与数据目录分离。
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
import tempfile
import urllib.request
import uuid

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def make_tar(members: list[tuple]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        for spec in members:
            name, kind = spec[0], spec[1]
            ti = tarfile.TarInfo(name)
            if kind == "dir":
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
            elif kind == "file":
                payload = (spec[2] if len(spec) > 2 else "data").encode()
                ti.type = tarfile.REGTYPE
                ti.size = len(payload)
                tf.addfile(ti, io.BytesIO(payload))
                continue
            elif kind == "symlink":
                ti.type = tarfile.SYMTYPE
                ti.linkname = spec[2]
            else:  # whiteout / opaque：char 0/0 设备
                ti.type = tarfile.CHRTYPE
                ti.devmajor = 0
                ti.devminor = 0
            tf.addfile(ti)
    return buf.getvalue()


def show(title: str, outcome: dict) -> None:
    print(f"\n== {title} ==")
    if outcome.get("status") == "found":
        src = outcome["source"]
        where = (
            f"卷 {src['mountpoint']}"
            if src["origin"] == "volume"
            else f"第 {src['layer_index']} 层"
        )
        print(f"  找到: {outcome['actual_path']} ({outcome['entry_type']}) <- {where}")
    elif outcome.get("status") == "error":
        print(f"  错误: [{outcome['error_code']}] {outcome['reason']}")
    else:
        print(
            f"  中断: 断在 {outcome['broken_at']} 的分量 "
            f"{outcome['broken_segment']!r}；{outcome['reason']}"
        )
        if outcome.get("shadowed_by_mount"):
            print(f"  注意: 该位置处于挂载点 {outcome['shadowed_by_mount']} 内，镜像内容已被卷遮蔽")
    for hop in outcome.get("symlink_hops", []):
        print(f"  跟随软链接: {hop['symlink_path']} -> {hop['target']}")


def post_multipart(path: str, fields: dict, files: list[tuple]) -> tuple[int, dict]:
    """files: [(field_name, filename, blob), ...]，同名字段按序重复。"""

    boundary = "----e2e" + uuid.uuid4().hex
    body = io.BytesIO()

    def w(line: bytes) -> None:
        body.write(line + b"\r\n")

    for name, value in fields.items():
        w(f"--{boundary}".encode())
        w(f'Content-Disposition: form-data; name="{name}"'.encode())
        w(b"")
        w(value.encode())
    for field, filename, blob in files:
        w(f"--{boundary}".encode())
        w(f'Content-Disposition: form-data; name="{field}"; filename="{filename}"'.encode())
        w(b"Content-Type: application/x-tar")
        w(b"")
        body.write(blob)
        w(b"")
    w(f"--{boundary}--".encode())
    w(b"")
    req = urllib.request.Request(
        BASE + path,
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def get_json(path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def check(failures: int, ok: bool, label: str, detail: str = "") -> int:
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return failures if ok else failures + 1


def run_volume_init_smoke(layer_blobs: tuple[bytes, bytes, bytes]) -> int:
    """卷初始化冒烟：初始化 -> 按名取清单 -> 清单喂给排障接口 -> 重复初始化。"""

    failures = 0
    vol = "smoke-" + uuid.uuid4().hex[:8]
    print("\n" + "#" * 72)
    print(f"# 卷初始化冒烟: 卷名 {vol}，源目录 /etc/app")

    # 1) 初始化：/etc/app 经层合并后可见的是 config.yaml(v1) 与 new.conf
    code, body = post_multipart(
        "/api/volumes/init",
        {"volume_name": vol, "source_dir": "/etc/app"},
        [("layers", f"layer{i}.tar", b) for i, b in enumerate(layer_blobs)],
    )
    entries = {e["path"]: e["type"] for e in body.get("entries", [])}
    failures = check(failures, code == 201 and body.get("status") == "initialized",
                     "初始化返回 201/initialized", f"HTTP {code}")
    failures = check(failures, entries == {"config.yaml": "file", "new.conf": "file"},
                     "清单只含最终可见条目（whiteout/opaque 已生效）", str(entries))

    # 2) 按名使用：清单可查询，且出现在卷列表里
    code, got = get_json(f"/api/volumes/{vol}/entries")
    failures = check(failures, code == 200 and {e["path"] for e in got.get("entries", [])}
                     == {"config.yaml", "new.conf"},
                     "GET /api/volumes/{name}/entries 返回已生效清单", f"HTTP {code}")
    code, listing = get_json("/api/volumes")
    failures = check(failures, code == 200
                     and any(v["volume_name"] == vol for v in listing),
                     "GET /api/volumes 列出已生效卷", f"HTTP {code}")

    # 3) 清单直接喂给排障接口：挂载后 config.yaml 应命中卷
    code, tro = post_multipart(
        "/api/troubleshoot",
        {
            "target_path": "/etc/app/config.yaml",
            "mount_points": json.dumps(["/etc/app"]),
            "volume_entries": json.dumps({"/etc/app": got["entries"]}),
        },
        [("layers", f"layer{i}.tar", b) for i, b in enumerate(layer_blobs)],
    )
    after = tro.get("after_mount", {})
    failures = check(
        failures,
        code == 200 and after.get("status") == "found"
        and after.get("source", {}).get("origin") == "volume",
        "初始化清单可直接用于排障接口 volume_entries",
        f"after_mount.status={after.get('status')}",
    )

    # 4) 重复初始化同名卷：返回已生效清单，不重新生效
    code, dup = post_multipart(
        "/api/volumes/init",
        {"volume_name": vol, "source_dir": "/etc/app"},
        [("layers", f"layer{i}.tar", b) for i, b in enumerate(layer_blobs)],
    )
    failures = check(failures, code == 200 and dup.get("status") == "already_initialized",
                     "重复初始化返回已生效清单", f"HTTP {code}")

    # 5) whiteout 语义：/var/lib/app 的 old.cache 被删除 -> 空卷
    vol2 = vol + "-empty"
    code, body2 = post_multipart(
        "/api/volumes/init",
        {"volume_name": vol2, "source_dir": "/var/lib/app"},
        [("layers", f"layer{i}.tar", b) for i, b in enumerate(layer_blobs)],
    )
    failures = check(failures, code == 201 and body2.get("entries") == [],
                     "被 whiteout 删空的源目录初始化为空卷", f"HTTP {code}")
    return failures


def main() -> int:
    layer0 = make_tar([
        ("etc", "dir"),
        ("etc/app", "dir"),
        ("etc/app/config.yaml", "file", "v0"),
        ("var", "dir"),
        ("var/lib", "dir"),
        ("var/lib/app", "dir"),
        ("var/lib/app/old.cache", "file"),
        ("logs", "dir"),
        ("logs/app.log", "file"),
    ])
    layer1 = make_tar([
        ("etc/app/.wh..wh..opq", "opaque"),
        ("etc/app/config.yaml", "file", "v1"),
        ("etc/app/new.conf", "file"),
        ("var/lib/app/.wh.old.cache", "whiteout"),
    ])
    layer2 = make_tar([
        ("appcfg", "symlink", "/etc/app"),
        ("oldcfg", "symlink", "/etc/app"),
        ("cycle1", "symlink", "cycle2"),
        ("cycle2", "symlink", "cycle1"),
    ])

    data_dir = tempfile.mkdtemp(prefix="e2e-layers-")
    for i, blob in enumerate((layer0, layer1, layer2)):
        with open(f"{data_dir}/layer{i}.tar", "wb") as fh:
            fh.write(blob)
    print(f"层 tar 已写入隔离目录: {data_dir}")

    mounts = ["/etc/app", "/var/lib/app"]
    volumes = {
        "/etc/app": [{"path": "mounted.yaml", "type": "file"}],
        "/var/lib/app": [],
    }
    fields = {
        "mount_points": json.dumps(mounts),
        "volume_entries": json.dumps(volumes),
    }
    failures = 0

    def run_case(target: str, expect: str) -> None:
        nonlocal failures
        f = dict(fields, target_path=target)
        # 重新组装多文件（同名字段 layers 出现三次）
        boundary = "----e2e" + uuid.uuid4().hex
        body = io.BytesIO()

        def w(line: bytes) -> None:
            body.write(line + b"\r\n")

        for name, value in f.items():
            w(f"--{boundary}".encode())
            w(f'Content-Disposition: form-data; name="{name}"'.encode())
            w(b"")
            w(value.encode())
        for i, blob in enumerate((layer0, layer1, layer2)):
            w(f"--{boundary}".encode())
            w(
                f'Content-Disposition: form-data; name="layers"; filename="layer{i}.tar"'.encode()
            )
            w(b"Content-Type: application/x-tar")
            w(b"")
            body.write(blob)
            w(b"")
        w(f"--{boundary}--".encode())
        w(b"")
        req = urllib.request.Request(
            BASE + "/api/troubleshoot",
            data=body.getvalue(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())

        print("#" * 72)
        print(f"# 待查路径: {target}    期望: {expect}")
        show("挂载前（纯镜像叠加）", result["before_mount"])
        show("挂载后（卷已遮蔽）", result["after_mount"])

        got = result["after_mount"]["status"]
        if got != expect:
            failures += 1
            print(f"  !! 断言失败: 期望 {expect}，实际 {got}")

    # 1) 遮蔽：挂载前能看到镜像文件，挂载后被卷盖住
    run_case("/etc/app/config.yaml", "missing")
    # 2) 卷内条目可见
    run_case("/etc/app/mounted.yaml", "found")
    # 3) 跨挂载点软链接：经 /appcfg 跳进 /etc/app 卷取文件
    run_case("/appcfg/mounted.yaml", "found")
    # 4) 同一条链接去取镜像里才有的文件：挂载前找到，挂载后被卷遮蔽而中断
    run_case("/oldcfg/config.yaml", "missing")
    # 5) whiteout 中断（挂载前后都不存在）
    run_case("/var/lib/app/old.cache", "missing")
    # 6) 未受影响的镜像文件
    run_case("/logs/app.log", "found")
    # 7) 成环报错（带后续分量才会跟随软链接并暴露环）
    run_case("/cycle1/x", "error")

    # 8) 卷初始化：初始化 -> 按名取清单 -> 清单喂给排障接口 -> 重复初始化
    failures += run_volume_init_smoke((layer0, layer1, layer2))

    print("\n" + "=" * 72)
    if failures:
        print(f"冒烟失败 {failures} 项")
        return 1
    print("全部冒烟断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
