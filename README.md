# OCI 层叠加与卷挂载排障接口

容器启动时报「找不到配置文件」时，值班员需要判断问题出在**镜像构建**（层叠加、
whiteout、软链接）还是**卷挂载**（挂载点把镜像内容盖住）。本服务用 FastAPI
模拟这两套机制，对同一个待查路径分别给出**挂载前**与**挂载后**的解析结果。

另外提供**卷初始化**能力：新容器挂卷前，把镜像里某个源目录的默认配置
（最终可见的普通文件与目录）预先落盘到命名卷，卷写齐后按名取用清单，
清单格式与排障接口的 `volume_entries` 直接兼容。

## 能力范围

### 排障（POST /api/troubleshoot）

- 按「底到顶」顺序上传多个**未压缩** OCI 层 tar，只读取 tar 条目元数据，
  不落盘、不提取文件内容；
- 镜像层条目支持：普通文件、目录、软链接、whiteout（`.wh.<name>`）、
  不透明目录标记（`.wh..wh..opq`）；
- 叠加语义：whiteout 只遮蔽更下层，同层新增同路径条目照常可见（两阶段处理，
  与 tar 内顺序无关）；不透明标记遮蔽目录的下层内容，目录本身与上层新增保留；
- 挂载点必须是镜像里存在的目录，且多个挂载点**互不嵌套**；
- 卷只含文件和目录；挂载后挂载点目录内的镜像内容被整体盖住；
- 容器内逐分量解析软链接，支持相对/绝对目标、**跨挂载点**跳转；
  与 Linux `stat(2)` 语义一致：软链接作为路径末分量时返回链接本身
  （`entry_type=symlink`），只有链接后还有子分量时才跟随；
  跟随过程成环返回 `symlink_cycle`，跳数超 40 返回 `too_many_symlink_hops`；
- 查找命中时返回实际路径、类型、来源层（layer_index，0 为最底层）或来源卷；
  中断时返回断在哪一分量、绝对位置与具体原因（含遮蔽来源挂载点）。

### 卷初始化（POST /api/volumes/init）

- 填写 `volume_name` 与镜像内 `source_dir`，底到顶上传未压缩层 tar；
- 把源目录**最终可见**的普通文件和目录写入持久卷根下的同名子目录
  （`<VOLUME_ROOT>/<volume_name>/`），**空目录保留**；
- 文件字节取自**可见条目所在的层**，大文件按 1 MiB 分块复制；
  whiteout / 不透明标记遮蔽的内容不复制；
- 源目录内出现**可见软链接**即报错（422）；被遮蔽的软链接不影响；
- 提交协议：先写暂存目录 `<VOLUME_ROOT>/.staging/<name>.<uuid>` 并 fsync，
  再 `rename` 原子生效；**卷写齐才可按名使用**（GET 接口只看得到已生效卷）；
- 生效前写盘失败或进程中断：只留下暂存残骸（启动时自动清理），
  卷名不被半成品占用，**可用同名重新初始化**；
- 同名并发只生效一份；重复/并发请求返回**已生效**的卷清单
  （HTTP 200 + `status=already_initialized`）；不同卷名可同时初始化；
- 响应与 `GET /api/volumes/{name}/entries` 返回的条目清单
  （`[{"path": ..., "type": "file|dir"}]`，相对卷根）可直接作为
  排障接口 `volume_entries` 的值。

## 目录结构

```
app/
  errors.py      # 统一业务异常（400 tar 不可读 / 422 输入不合法 / 404 / 409 / 500）
  tarloader.py   # 只读解析未压缩层 tar -> 有序事件
  imagefs.py     # 底到顶层合并（whiteout / opaque / 覆盖）+ 挂载点校验
  volume.py      # 卷清单解析（仅 file/dir，父目录必须显式声明）
  resolver.py    # 容器视图、软链接跟随、跨挂载、成环检测
  volstore.py    # 命名卷持久化：可见性收集、分块落盘、暂存+rename 提交、按卷名互斥
  main.py        # FastAPI 路由与响应模型
tests/           # pytest 用例（排障 33 + 卷初始化 17）
scripts/
  e2e_smoke.py   # 端到端冒烟脚本（造层 -> 打接口 -> 断言，含卷初始化联动）
Dockerfile
docker-compose.yml
```

## 用 Docker Compose 启动

```bash
docker compose up --build
# 打开交互文档
open http://localhost:8000/docs      # Linux: xdg-open http://localhost:8000/docs
```

`/docs` 里可直接试用 `POST /api/troubleshoot`，响应中并排展示
`before_mount` 与 `after_mount` 两组结果；也可试用 `POST /api/volumes/init`
初始化命名卷，再用 `GET /api/volumes/{name}/entries` 取清单喂回排障接口。

初始化好的卷持久化在 compose 命名卷 `volume-data`（容器内 `/srv/data/volumes`，
可用环境变量 `VOLUME_ROOT` 覆盖），容器重建后仍可按名使用。

## 卷初始化请求格式（POST /api/volumes/init，multipart/form-data）

| 字段 | 形式 | 说明 |
| --- | --- | --- |
| `layers` | 文件，可重复 | 未压缩 tar，重复字段的顺序 = 最底层 → 最顶层 |
| `volume_name` | 字符串 | 目标卷名：字母数字开头，可含 `.` `_` `-`，最长 128 字符 |
| `source_dir` | 字符串 | 镜像内源目录绝对路径，如 `/etc/app`；`/` 表示整个镜像根 |

响应（201 本次生效 / 200 返回已生效清单）：

```json
{
  "volume_name": "app-config",
  "source_dir": "/etc/app",
  "status": "initialized",
  "entries": [
    {"path": "config.yaml", "type": "file"},
    {"path": "sub", "type": "dir"},
    {"path": "sub/x", "type": "file"}
  ],
  "file_count": 2,
  "dir_count": 1,
  "total_bytes": 1234
}
```

`entries` 可直接作为排障接口 `volume_entries["/挂载点"]` 的值。
配套查询接口：`GET /api/volumes`（全部已生效卷）、
`GET /api/volumes/{name}/entries`（按名取清单，未生效返回 404）。

## 本地开发（无 Docker 时）

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
.venv/bin/python -I -m pytest tests/ -q          # 单元/接口测试
.venv/bin/python -I scripts/e2e_smoke.py         # 起服务后的端到端冒烟
```

## 请求格式（multipart/form-data）

| 字段 | 形式 | 说明 |
| --- | --- | --- |
| `layers` | 文件，可重复 | 未压缩 tar，重复字段的顺序 = 最底层 → 最顶层 |
| `target_path` | 字符串 | 待查容器内绝对路径，如 `/etc/app/config.yaml` |
| `mount_points` | JSON 字符串 | 挂载点数组，如 `["/etc/app","/var/lib/app"]` |
| `volume_entries` | JSON 字符串 | 挂载点 → 卷条目清单，路径相对卷根，仅 `file`/`dir` |

`volume_entries` 示例：

```json
{
  "/etc/app": [
    {"path": "mounted.yaml", "type": "file"},
    {"path": "sub", "type": "dir"},
    {"path": "sub/v", "type": "file"}
  ],
  "/var/lib/app": []
}
```

## 响应解读

命中：

```json
{
  "status": "found",
  "actual_path": "/etc/app/mounted.yaml",
  "entry_type": "file",
  "source": {"origin": "volume", "mountpoint": "/etc/app"},
  "shadowed_by_mount": "/etc/app",
  "symlink_hops": [
    {"symlink_path": "/appcfg", "target": "/etc/app/mounted.yaml", "resolved_from": "/"}
  ]
}
```

中断（注意 `broken_segment` / `broken_at` / `reason` 三段式定位，
以及 `shadowed_by_mount` 提示镜像内容被卷盖住）：

```json
{
  "status": "missing",
  "broken_segment": "config.yaml",
  "broken_at": "/etc/app/config.yaml",
  "reason": "卷 /etc/app 的条目清单中不存在 /config.yaml",
  "shadowed_by_mount": "/etc/app"
}
```

成环：

```json
{"status": "error", "error_code": "symlink_cycle",
 "reason": "软链接解析在 /cycle1 处成环（ELOOP）"}
```

## 排障判断指引

- 挂载前 missing、挂载后也 missing：问题在镜像构建（路径不存在 / 被 whiteout /
  不透明目录遮蔽 / 软链接断链），`reason` 会指出具体机制与层号；
- 挂载前 found、挂载后 missing 且带 `shadowed_by_mount`：问题在卷挂载，
  镜像里的文件被卷目录盖住；
- `status=error` + `symlink_cycle`：镜像里的软链接本身成环，与卷无关。

## 约束与限制

- 仅接受未压缩 tar；gzip/xz/zstd/bzip2 流会被魔数识别并明确拒绝；
- 不支持 hardlink、设备节点、FIFO 等条目（返回 422）；whiteout / 不透明
  标记按 OCI 惯例接受任意条目类型，仅以 `.wh.` 名称识别；
- 卷内不允许软链接；卷清单的每个父目录必须显式声明；
- 单层上限 128 MiB、总计 384 MiB、单层 5 万条目（防御性阈值）；
- 同层 tar 内出现重复路径条目会被拒绝（层间覆盖才是合法叠加方式）。
- 卷初始化只保留条目类型与文件字节：tar 里的权限/属主/时间戳不落盘；
- 层 tar 整体读入内存后再解析（受上述上限约束），未做流式上传解析；
- 同名互斥是进程内实现：按单进程（uvicorn 单 worker）部署，
  多 worker/多副本部署需要外置锁（如文件锁）才能保证同名只生效一份；
- 卷内 `.volume-manifest.json` 是服务的清单文件（保留名），
  源目录里出现同名条目会被拒绝。
