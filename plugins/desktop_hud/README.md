# 桌面监控（desktop_hud）

在桌面显示一块半透明小面板，实时显示 **CPU / GPU / 显存 / 内存** 占用，
可选显示 **CPU / GPU 频率**。每一项都有独立显示开关。桌面常驻：其他窗口
未最大化时在空隙里照常可见、但绝不遮挡窗口（v1.2.0 起挂为桌面壁纸层子窗）。
游戏会话中自动隐藏 / 停止监控（v1.3.0 起可停采样，交给游戏内其他监控软件；
与 RTSS 游戏内 OSD 互补，回到桌面自动恢复）。

纯 ctypes + 标准库实现，无第三方依赖、不携带任何二进制；悬浮窗跑在独立线程，
不阻塞守护主循环。

> 开发注意：live 的 Wingman.exe 是裁剪过的冻结运行时，标准库只打包了一部分
> （实测 PYZ 含 `threading`/`ctypes`/`json`，**不含 `queue`**）。插件只能依赖
> 已打包模块；跨线程队列用 `threading.Lock` + list 代替 `queue.Queue`，
> 否则面板会报「未能加载：ModuleNotFoundError: No module named 'queue'」。
>
> 热重载注意：宿主只重新 exec entry（plugin.py），`from . import hud_overlay`
> 默认命中 sys.modules 里上一轮的旧兄弟模块。plugin.py 顶部主动 pop 掉
> `hud_overlay`/`hud_metrics` 缓存，保证改动能真正生效（否则右键修复等改动
> 在 live 进程里不生效，独立新进程测试却正常）。

## 默认行为

- 位置：桌面右上角（工作区内，避开任务栏）。
- 刷新：1 秒；外观：深色半透明、等宽字体、占用 ≥70% 琥珀 / ≥90% 红。
- 显示项：CPU / GPU / 显存 / 内存 占用开；CPU / GPU 频率**默认关**。
- 游戏中自动隐藏：开。鼠标穿透：关。
- 游戏中停止监控：开（会话中停采样并隐藏，退出立即补采样恢复；关闭则仅按
  「自动隐藏」处理，采样继续）。
- 桌面常驻：开（挂为桌面壁纸层 WorkerW 的子窗：高于壁纸、低于一切应用
  窗口，Win+D 不最小化）。关闭则回顶层 + 始终置顶。

## 右键菜单

在面板上右键：

- 逐项勾选：CPU 占用 / GPU 占用 / 显存占用 / 内存占用 / CPU 频率 / GPU 频率
- 游戏中自动隐藏、游戏中停止监控、桌面常驻（不遮挡窗口）、鼠标穿透
- 位置：右上 / 左上 / 右下 / 左下（也可直接左键拖拽到任意位置）
- 打开配置文件、恢复默认、关闭悬浮窗

菜单与拖拽的改动**立即生效**并持久化；「恢复默认」回到 config.json 的基础值。

> 鼠标穿透开启后右键不再响应（鼠标事件穿透到桌面）。如需改回，请在面板
> 关闭本插件开关再打开，或编辑 config.json 的 `desktop_hud.click_through`。

## 配置（config.json）

```jsonc
{
  "desktop_hud_enabled": true,        // 总开关（面板拨动）
  "desktop_hud": {
    "show_cpu": true, "show_gpu": true, "show_vram": true, "show_ram": true,
    "show_cpu_freq": false, "show_gpu_freq": false,
    "hide_in_game": true, "pause_in_game": true,
    "desktop_only": true, "click_through": false,
    "position": "top-right",          // top-right/top-left/bottom-right/bottom-left/custom
    "x": 0, "y": 0,                   // position=custom 时的坐标
    "opacity": 215,                   // 0..255
    "refresh_sec": 1.0, "font_size": 14, "margin": 16
  }
}
```

运行期菜单 / 拖拽覆盖保存在 `plugins\desktop_hud\data\hud.json`
（`{"prefs":…, "hidden":…}`），优先级高于 config.json；删除该文件即回到配置基础值。

## 取数口径与限制

- CPU 占用：`GetSystemTimes` 差分；CPU 频率：注册表标称 `~MHz` ×
  PDH `% Processor Performance`（与任务管理器「速度」口径一致）。
- 内存：`GlobalMemoryStatusEx`。
- GPU 占用 / 显存占用：PDH `GPU Engine` / `GPU Adapter Memory`（取独显 LUID）。
- 显存总量：注册表 `HardwareInformation.qwMemorySize`（WMI AdapterRAM 4GB 封顶，弃用）。
- **GPU 频率仅 AMD 且 ADL 可用时有效**，默认关闭。v1.4.0 起优先走
  `ADL2_New_QueryPMLogData_Get`（PMLog，Navi/RDNA 起），传感器 `CLK_GFXCLK`
  单位已是 MHz；老驱动无 PMLog 时回退 `Overdrive5_CurrentActivity_Get`
  （`iEngineClock` 单位 10kHz，需 /100）。非 AMD 显卡显示「-」。
  本机 RX 9070 GRE（RDNA4，驱动 32.0.31019.2002）实测：Overdrive5 恒返回 -1，
  PMLog 正常，空闲约 110–140 MHz。
- ADL 初始化失败不再永久放弃：60s 退避重试（驱动更新/睡眠恢复可自愈），
  失败原因记入 `last_error`，`--self-test` 的 `gpu_clock_error=` 可直接看到。
- 多显示器时角落位置以主显示器工作区为准；拖拽可放到任意显示器。

## v1.4.0 变更（修复）

- `_cdll()` 误用未定义的 `_cdecl`（实际定义为 `_CDECL`）→ NameError 被宽
  except 吞掉，ADL 从未初始化成功，GPU 频率在任何 AMD 卡上都显示「-」。已修正。
- GPU 频率改走 PMLog（RDNA4 上 Overdrive5 不支持），量纲 MHz 不再除 100。
- 刷新节拍不再被守护 tick 劫持：`set_prefs` 内容未变不投递，`_apply_prefs`
  只在 `refresh_sec` 真变化时 `SetTimer`（同 id 重装会重置计时，0.4s 节拍
  下 1s 定时器实测 12s 内 0 次触发）。
- `on_exit` 先排空请求队列再停窗：菜单/拖拽改动不再因 30s 空闲节拍而丢失。
- `--self-test` 新增 `gpu_clock_error=`，区分「后端正常」与「初始化失败」。

## v1.5.0 变更（性能 / 多屏）

- GDI 不再每 tick 全量重做：
  - `_make_font` 缓存 `(字号px, dpi)`，未变不 `DeleteObject`+`CreateFontW`；
  - `_relayout` 缓存**各行字符数**（Consolas 等宽且全 ASCII，宽度只取决于
    字符数，不取决于具体数字），位数没变就跳过 `GetDC` + 6 次
    `GetTextExtentPoint32W`；量文本失败时不写缓存，下次重量；
  - `_reposition` 记住上次 `(x, y, w, h)`，未变不 `SetWindowPos`；
  - `_paint` 的背景刷改为成员缓存，`_teardown` 里统一释放（原来每帧建了再删）。
- 角落定位改用 `MonitorFromWindow` + `GetMonitorInfoW` 取**窗口所在显示器**的
  工作区。原来只用 `SPI_GETWORKAREA`，它只返回主屏工作区，副屏上选
  「右上/右下」会落到主屏坐标。拿不到显示器信息时回退旧行为。
- 热重载摘模块缓存只摘**本包内**的名字（`__package__ + '.hud_overlay'`）。
  原来无条件 pop 顶层 `hud_overlay`/`hud_metrics`，会踢掉其它插件的同名模块。
  离线独立导入（无包上下文）时仍回退顶层名。

## 安装 / 卸载

- 安装：面板「＋」选择 `desktop_hud-v1.5.0.zip`，或解压到 `plugins\desktop_hud\`。
- 卸载：面板垃圾桶，或删除目录。卸载不影响其它插件。
- 自检：`WBOX.exe --self-test` 会输出本插件的取数后端与当前显示项。