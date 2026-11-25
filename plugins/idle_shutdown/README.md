# idle_shutdown · 任务后自动关机 v1.0.0

把作者本机的「零点后任务静默再关机」脚本 v1.3.0 移植成 WBOX 插件。
原来的独立计划任务 `ShutdownAfterMidnight` 不再需要，功能由本插件常驻完成。

## 判据（与旧脚本一致）

三条同时满足并持续 `idle_min` 分钟，且当前处于时间窗内，才允许关机：

1. **会话静默**：`~\.workbuddy\projects\*\*.jsonl` 与 `~\.qoder\projects\*\*.jsonl`
   最近写入距今超过阈值。只认会话 jsonl，`logs\*.log` 是心跳文件、刻意不看。
2. **进程组 CPU 低**：`workbuddy.exe` / `qoder.exe` 各自进程组 CPU < `cpu_max`（默认 8%）；
   没开的应用算空闲。采样 1.5 秒，在后台线程做。
3. **键鼠空闲**：`GetLastInputInfo` 距上次输入超过阈值。
4. **时间窗**：默认 00:00–08:00；窗口外只待命，绝不关机。
5. **见过任务**：默认要求本次窗口内至少观察到一次「忙」，防止整晚挂机被误关。
6. **抖动容忍**：单轮忙不算忙，连续两轮忙才把静默计时清零；探测异常按「忙」处理。

## 关机与取消

- 命中后先执行飞牛 NAS 关机（若开启），再调用 `InitiateSystemShutdownExW`，
  失败回退 `shutdown /s /t`。倒计时用 Windows 自带窗口，不另弹 tkinter 窗口。
- 取消方式：
  1. Windows 倒计时窗口里取消（会同时调用 `AbortSystemShutdownW` / `shutdown /a`）；
  2. 在本插件数据目录或旧脚本目录写 `stop.flag` / `.cancelled`；
  3. 在面板里关掉插件开关。
- 取消后代次号 +1，已排队的关机线程会自行停手；删除标记文件后下一个时间窗恢复。

## 飞牛 NAS

`nas_shutdown` 开启时调用旧脚本目录的 `nas_poweroff.py`，由它在运行时从自己的凭证文件
读取 SSH 口令。**插件包内没有任何口令、主机地址或凭证路径**。
NAS 关失败只记 `guard.log`，不拦本机；顺序始终是 NAS → 等 5 秒 → 本机。

## 设置项

| 设置 | 默认 | 说明 |
|---|---|---|
| 启用 | 开 | 总开关 |
| 时间窗 | 夜间 00:00–08:00 | 可选晚间 20:00–23:59、全天 |
| 静默多久后关机 | 10 分钟 | 三项判据同时满足后计时 |
| 关机倒计时 | 60 秒 | Windows 自带倒计时窗口 |
| 必须先见到任务在跑 | 开 | 安全闸门，不建议关 |
| 关机前先关飞牛 NAS | 关 | 需本机装了 paramiko 的 Python |

运行期参数（不在面板展示，可写进 `config.json` 的 `idle_shutdown` 段）：
`poll_sec`=30、`cpu_max`=8.0、`require_idle_input`=true、
`watch_workbuddy`=true、`watch_qoder`=true、`dry_run`=false。

## 变更记录

- v1.0.0（2026-11-25）：首版，移植 v1.3.0 旧脚本全部判据 + 飞牛联动 + 倒计时取消。
