# local_ai · 本地 AI 按需启停 v1.0.4

WBOX 插件。本地 llama.cpp 冷启动约 40 秒、Q5+64K 占 10 GB 显存；本插件让它**只在有请求时运行**，请求结束且空闲一段时间后自动关闭。用户手动启动的服务不会被插件关闭。

## 触发协议（与调用方约定）

请求方（例如 `D:\.workbuddy\scripts\local_ai_prep.py`）在调用本地模型前写：

```json
// D:/.workbuddy/scripts/local_ai_request.json
{"action":"start","client":"local_ai_prep.py","pid":12345,"ts":1764000000}
```

插件检测到文件存在 → 拉起 `llama-server.exe` → 轮询 `/health` 就绪 → 写同目录的 `local_ai_ready.json`：

```json
{"status":"ready","pid":12345,"port":8080,"plugin":"local_ai","at":1764000000.0}
```

`status` 取值：`starting` / `ready` / `error` / `stopped`。`pid=0` 且 `external=true` 表示服务是用户自己启动的，插件只等待、不接管关闭。

调用方用完删除 `local_ai_request.json`。插件在**请求文件消失**且 `/slots` 全部 `is_processing=false` 持续 `idle_timeout_sec`（默认 120 秒）后终止自己拉起的进程，并把 `ready.json` 写成 `stopped`。

## 状态机

```
idle --请求--> starting --/health ok--> ready --请求消失+空闲超时--> idle
              |                  |  \
              |                  |   +--自己拉起的进程退出--> idle（写 stopped）
              +--已有外部服务----+--> external --连续 3 次探测不到（约 6s）--> idle
              |
              +--超时/进程退出--> error（保留请求文件时不会自动重试）
```

两个回收口是 v1.0.4 补的：`external` 以前是死胡同（没人能把它改回 `idle`，一旦探到过外部服务，后续真请求就再也不触发拉起，只能重启守护）；`ready` 下服务自己退了，`ready.json` 会悬着最多 `idle_timeout_sec` 秒的 `ready`，调用方会去连一个死端口。

## 关键实现约束

- `on_tick` 只做 `stat` 和内存判断；`Popen`、HTTP 探测、原子写文件全部 `ctx.spawn` 到后台线程，不拖慢 600 ms 守护节拍。
- HTTP 探测调系统 `curl.exe`，必须带 `creationflags=CREATE_NO_WINDOW`：WBOX.exe 是窗口程序（无控制台），不隐藏的话 Windows 每次都会给 curl 新开一个控制台黑窗。
- `/slots` 只在 `ready` / `external` 状态探测；`idle` 时没有服务可问，探测只会在桌面上闪黑窗。
- 只终止本插件 `Popen` 出来的 PID；`external` 状态下 `_proc=None`，回收 / 关闭逻辑只清状态、写 `ready.json`，绝不杀用户自己的服务。
- 启动参数固定带 `--reasoning off`、`--jinja`、`-np 1`、`--host 127.0.0.1`；端口和上下文可配，但不会改成 `0.0.0.0`。
- `--ctx_size` 默认 8192（摘要类任务够用，冷启动 40 秒）；设成 65536 会让加载显著变长。
- 不 import `uuid` / `platform` / `wmi`，避免 PyInstaller 拖包。
- 模块顶层无副作用，面板进程 import 本文件不会启动任何东西。

## 配置项（`config.json`）

| 键 | 默认 | 说明 |
|---|---|---|
| `local_ai_enabled` | true | 总开关 |
| `local_ai.exe_path` | `D:/01-Tools/llama.cpp/bin/llama-server.exe` | 可执行文件 |
| `local_ai.model_path` | `.../Qwen3.5-9B-Q5_K_M.gguf` | 权重 |
| `local_ai.mmproj_path` | `.../mmproj-F16.gguf` | 视觉投影；不需要视觉可留空 |
| `local_ai.port` | 8080 | 只绑 127.0.0.1 |
| `local_ai.ctx_size` | 8192 | 上下文长度 |
| `local_ai.idle_timeout_sec` | 120 | 空闲关闭等待秒数 |
| `local_ai.startup_timeout_sec` | 120 | 启动就绪上限 |
| `local_ai.signal_path` | `D:/.workbuddy/scripts/local_ai_request.json` | 请求信号路径 |

## 安装与验证

1. 用 `tools/pack.py` 打包：`python tools/pack.py plugins/local_ai`，产物在 `dist/local_ai-v1.0.4.zip` + `.sha256`。
2. 在 WBOX 面板「插件」分组安装该包，打开开关。
3. 造一个请求信号后观察 `guard.log`：应依次出现「已就绪」「空闲超时已关闭」。
4. 自检：`WBOX.exe --self-check` 里 `local_ai:1.0.4:api1:ok`。

## 边界

- 同一时刻只支持一个请求方；多个调用方并发写同一个 `request.json` 时，先结束的一方删文件，但后一方若仍在处理任务，`/slots` 会阻止关闭。
- 插件不负责启动失败后的重试；请求方删掉信号再重建即可重试。
- 信号文件放在 `D:\.workbuddy\scripts\`，该目录必须存在且可写。

## 变更记录

- **v1.0.4**：修状态卡死。`external` 连续 3 次探测失败（约 6s）退回 `idle`，真请求又能自己拉起；`ready` 下自己拉起的进程退出立即清状态并把 `ready.json` 改成 `stopped`（原来要等空闲超时，最长悬 120 秒）。
- **v1.0.3**：修桌面黑窗。`_http_json` 补 `CREATE_NO_WINDOW`（窗口程序拉控制台子进程会新开黑框）；`/slots` 探测限制在 `ready` / `external`，`idle` 不再每 2 秒空拉一次 curl。
- **v1.0.2**：空闲判定改看 `/slots` 的 `is_processing`，避免把长推理当成空闲误关。
- **v1.0.1**：`external` 状态不接管、不关闭用户自己启动的服务。
- **v1.0.0**：首版，信号文件驱动的按需启停。
