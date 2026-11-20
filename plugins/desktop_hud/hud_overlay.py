# -*- coding: utf-8 -*-
"""桌面监控插件 - 悬浮窗（纯 ctypes Win32，无第三方依赖，无 tkinter）。

为什么自己画窗口而不用 tkinter：守护进程是 console=False 的精简环境，
tkinter 不在其中；且 tkinter 主循环必须独占线程、与守护的单线程钩子冲突。
这里用 user32/gdi32 建一个 WS_EX_LAYERED 的分层窗口，跑在**独立线程**里，
自带消息循环 + WM_TIMER 采样，守护线程只通过 PostMessage 投递控制指令，
绝不阻塞在原生调用上。

线程模型（v1.6.0 起三段）：
  守护线程                          悬浮窗线程                     采样线程
  --------                          ----------                     ----------
  start()  ──spawn────────────────►  _run(): 建窗口/字体 → 消息循环
  set_prefs(p) ─lock+PostMessage──►  WM_APP_REFRESH: 应用偏好/重排/重绘
  set_visible(v) ─lock+Post───────►  WM_APP_REFRESH: ShowWindow
  stop()   ──PostMessage(WM_CLOSE)►  DestroyWindow → WM_DESTROY → PostQuit
                                     WM_APP_SAMPLE ◄──PostMessage── 采样线程
  on_tick(): 请求队列 ◄── 菜单/拖动把变更塞进队列（线程安全）

采样**不在**窗口线程里做：PDH / ADL 在驱动更新、睡眠恢复、GPU 计数器提供者
重启时都可能卡住几百毫秒甚至数秒，落在 WM_TIMER 里就是窗口一起冻结（重绘、
右键菜单、拖动全部卡死；本机 2026-11-19 17:56 的日志里就出现过一次
「悬浮窗创建失败（桌面会话不可用？）」，2 秒后才建好——首帧采样建 PDH 查询
的成本压在了建窗路径上）。v1.6.0 把采样搬到独立线程：窗口线程只读最近一次
快照并重绘，采样变慢时按 2 倍耗时退避（上限 5s）并把慢事件写进日志。

桌面常驻（desktop_only，v1.2.0）：窗口挂为桌面壁纸层 WorkerW 的子窗
（Rainmeter / Wallpaper Engine 同款做法）——高于壁纸、低于一切应用窗口，
Win+D 不会最小化它；层级本身保证「不遮挡应用」。关闭则回顶层 + 始终置顶。
"""
import ctypes
import os
import threading
import time
from ctypes import wintypes as wt

try:                       # 作为包模块被宿主 import
    from . import hud_metrics
except ImportError:        # 作为脚本独立自测
    import hud_metrics

# ---------------------------------------------------------------- 常量
LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, WPARAM, LPARAM)
ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, LPARAM)

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
LWA_ALPHA = 0x2

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_TIMER = 0x0113
WM_CONTEXTMENU = 0x007B
WM_RBUTTONUP = 0x0205
WM_NCRBUTTONUP = 0x00A5
WM_NCHITTEST = 0x0084
WM_EXITSIZEMOVE = 0x0232
WM_NULL = 0x0000
WM_APP = 0x8000
WM_APP_REFRESH = WM_APP + 1
WM_APP_SAMPLE = WM_APP + 2
HTCAPTION = 2

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = ctypes.c_void_p(-1)
HWND_NOTOPMOST = ctypes.c_void_p(-2)
GWL_EXSTYLE = -20

SPI_GETWORKAREA = 0x0030
MONITOR_DEFAULTTONEAREST = 0x00000002


class MONITORINFO(ctypes.Structure):
    """GetMonitorInfoW 的出参；rcWork 才是扣掉任务栏的工作区。"""
    _fields_ = [('cbSize', wt.DWORD), ('rcMonitor', wt.RECT),
                ('rcWork', wt.RECT), ('dwFlags', wt.DWORD)]
LOGPIXELSX = 88
DT_LEFT = 0x0
DT_TOP = 0x0
DT_SINGLELINE = 0x20
DT_NOPREFIX = 0x800
TRANSPARENT_BK = 1
FW_NORMAL = 400
DEFAULT_CHARSET = 1
CLEARTYPE_QUALITY = 5
FIXED_PITCH_FF_MODERN = 0x31

MF_STRING = 0x0
MF_CHECKED = 0x8
MF_UNCHECKED = 0x0
MF_SEPARATOR = 0x800
MF_POPUP = 0x10
TPM_RIGHTBUTTON = 0x2
TPM_RETURNCMD = 0x100

IDC_ARROW = 32512
DPI_PER_MONITOR_V2 = ctypes.c_void_p(-4)

# 菜单命令 id
M_CPU = 1001
M_GPU = 1002
M_VRAM = 1003
M_RAM = 1004
M_CPUFREQ = 1005
M_GPUFREQ = 1006
M_HIDEINGAME = 1010
M_CLICKTHROUGH = 1011
M_DESKTOPONLY = 1012
M_PAUSEINGAME = 1013
M_POS_TR = 1020
M_POS_TL = 1021
M_POS_BR = 1022
M_POS_BL = 1023
M_POS_TC = 1024
M_POS_BC = 1025
M_POS_LC = 1026
M_POS_RC = 1027
M_OPENCFG = 1030
M_RESET = 1031
M_CLOSE = 1032
M_DIAG = 1033
M_GPU_AUTO = 1040
M_GPU_LOAD = 1041
M_GPU_BASE = 1050          # + 适配器下标（最多 8 张）

DEFAULT_PREFS = {
    'show_cpu': True,
    'show_gpu': True,
    'show_vram': True,
    'show_ram': True,
    'show_cpu_freq': False,
    'show_gpu_freq': False,
    'hide_in_game': True,
    'pause_in_game': True,
    'desktop_only': True,
    'click_through': False,
    'position': 'top-right',   # 四角 / 四边居中（*-center）/ custom
    'x': 0,
    'y': 0,
    'opacity': 215,            # 0..255
    'refresh_ms': 1000,        # 采样/重绘节拍（200..10000）
    'font_size': 14,           # 96dpi 下的像素高度
    'margin': 16,
    # v1.6.0：阈值与精度可配（原来 70 / 90 与整数百分比是写死的）
    'warn_percent': 70,
    'high_percent': 90,
    'pct_decimals': 0,         # 0 / 1
    # v1.6.0：目标显卡选择（auto 显存最大 / load 占用最大 / luid 指定）
    'gpu_select': 'auto',
    'gpu_luid': '',
}


def _interval_ms(prefs):
    """刷新节拍（毫秒），钳到 200..10000。refresh_sec 由 plugin.py 迁移。"""
    try:
        ms = int(prefs.get('refresh_ms', 1000))
    except (TypeError, ValueError):
        ms = 1000
    return max(200, min(10000, ms))


def _pct_num(prefs, key, default):
    try:
        return max(1, min(100, int(prefs.get(key, default))))
    except (TypeError, ValueError):
        return default


def _pct_int(v):
    """宽松整数：设置面板存进来可能是字符串 / 布尔 / 浮点。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0

_BG = 0x00141418               # COLORREF = 0x00BBGGRR -> RGB(24,20,20)
_FG = 0x00E6E6EB               # RGB(235,230,230)
_DIM = 0x009A9AA0              # RGB(160,154,154)
_WARN = 0x0050A0F0             # RGB(240,160,80) 琥珀
_HIGH = 0x005050E6             # RGB(230,80,80) 红


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wt.UINT),
        ('style', wt.UINT),
        ('lpfnWndProc', WNDPROC),
        ('cbClsExtra', ctypes.c_int),
        ('cbWndExtra', ctypes.c_int),
        ('hInstance', wt.HINSTANCE),
        ('hIcon', wt.HICON),
        ('hCursor', wt.HANDLE),
        ('hbrBackground', wt.HBRUSH),
        ('lpszMenuName', wt.LPCWSTR),
        ('lpszClassName', wt.LPCWSTR),
        ('hIconSm', wt.HICON),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ('hdc', wt.HDC),
        ('fErase', wt.BOOL),
        ('rcPaint', wt.RECT),
        ('fRestore', wt.BOOL),
        ('fIncUpdate', wt.BOOL),
        ('rgbReserved', wt.BYTE * 32),
    ]


def _rgb(r, g, b):
    return (b << 16) | (g << 8) | r


def _lo(l):
    v = l & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _hi(l):
    v = (l >> 16) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


class HudOverlay(object):
    """分层悬浮窗。start() 后窗口跑在独立线程；所有控制方法线程安全。"""

    def __init__(self, request_cb=None):
        self._request_cb = request_cb
        self._lock = threading.Lock()
        self._prefs = dict(DEFAULT_PREFS)
        self._pending_prefs = None
        self._pending_visible = None
        self._pending_paused = None
        self._paused = False
        self._interval = 1000       # 采样节拍（毫秒），由 _apply_prefs 更新
        self._last_prefs_req = None  # 上次收到的偏好请求，用于去重（守护每 tick 都调）
        self._visible = True
        self._shown = False
        self._parent = None         # 桌面 WorkerW 句柄（desktop_only 时挂为其子窗）
        self._hwnd = None
        self._thread = None
        self._created = threading.Event()
        self._stopping = False
        self._wndproc_cb = None
        self._class_name = None
        self._font = None
        self._sample = {}
        self._lines = []
        self._w = 120
        self._h = 60
        self._dpi = 96
        self._font_px = 14
        self._label_w = 40
        self._pad = 8
        self._line_h = 20
        # ---- GDI 缓存（v1.5.0：原来每 tick 全量重做）----
        self._font_key = None       # (px, dpi)：未变不 DeleteObject+CreateFontW
        self._measure_key = None    # (文本元组, px, dpi, pad, line_h)：未变不量文本
        self._last_pos = None       # 上次 SetWindowPos 的 (x, y, w, h)
        self._bg_brush = None       # 背景刷缓存，_teardown 里释放
        self._u = None
        self._g = None
        self._k = None
        # ---- v1.6.0 采样线程（窗口线程只读快照，绝不自己采样）----
        self._sample_lock = threading.Lock()
        self._sample_ms = 0.0       # 最近一次采样耗时（诊断 / 退避判据）
        self._slow_events = 0
        self._last_slow_ms = None
        self._last_slow_log = 0.0
        self._worker = None
        self._worker_stop = None
        self._pending_sampler_cfg = None
        self._menu_adapters = []
        # v1.6.1 采样器所有权：Sampler 由**采样线程自己**创建与关闭，窗口线程
        # 与后续新建的线程都不再共享任何 PDH/ADL 句柄。这样即使旧线程卡在
        # 驱动调用里没能退出，新线程拿着的是另一套句柄，不会出现两条线程
        # 同时读同一个 PDH 查询的未定义行为。
        self._worker_gen = 0            # 采样线程代号（每起一条 +1）
        self._sampler_state = 'idle'    # idle / running / stopped / orphan
        self._restart_pending = False   # 想恢复采样，但旧线程还没退出
        self._sample_at = 0.0           # 最近一次成功采样的墙钟
        self._sample_meta = {}          # GPU 缺失原因等（采样线程发布，只读）
        self._backend_lines = []        # 采样线程发布的分项诊断（窗口线程只读）

    # ------------------------------------------------------ 对外控制（守护线程）
    def start(self, prefs=None, visible=True, timeout=1.5):
        """建窗口线程。返回 True 表示窗口已就绪。不抛异常。"""
        if self._thread is not None and self._thread.is_alive():
            if prefs is not None:
                self.set_prefs(prefs)
            self.set_visible(visible)
            return True
        with self._lock:
            if prefs:
                self._prefs.update(prefs)
            self._visible = bool(visible)
            self._stopping = False
        self._created.clear()
        self._thread = threading.Thread(target=self._run, name='hud-overlay')
        self._thread.daemon = True
        self._thread.start()
        ok = self._created.wait(timeout)
        if not ok:
            # 超时（如启动瞬间 PDH 初始化慢）：线程可能还在跑并稍后建窗。
            # 必须请求停止，否则孤儿线程占住窗口类，后续重试永远注册失败。
            self.stop(timeout=0.2)
        return bool(ok and self._hwnd)

    def stop(self, timeout=1.5):
        """请求关闭并等待线程退出（on_exit 用，必须 < 2s）。"""
        with self._lock:
            self._stopping = True
            hwnd = self._hwnd
        if hwnd:
            try:
                self._u.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        t = self._thread
        if t is not None:
            t.join(timeout)
            if t.is_alive():
                # 首次 WM_CLOSE 可能发在窗口创建之前（start 超时路径）：
                # 窗口若已建好，补发一次让它走 WM_DESTROY -> 退出消息循环。
                with self._lock:
                    hwnd2 = self._hwnd
                if hwnd2:
                    try:
                        self._u.PostMessageW(hwnd2, WM_CLOSE, 0, 0)
                    except Exception:
                        pass
                t.join(0.3)
        self._stop_worker(timeout=0.5)     # 窗口线程可能已随 WM_DESTROY 退出
        with self._lock:
            self._hwnd = None
        self._thread = None

    def is_running(self):
        t = self._thread
        return bool(t is not None and t.is_alive() and self._hwnd)

    def set_prefs(self, prefs):
        with self._lock:
            new = dict(prefs or {})
            hwnd = self._hwnd
            # 守护每个 tick 都会推一次偏好；内容没变就不投递，否则 _apply_prefs
            # 会反复整窗重排（连带 SetWindowPos / 字体重建），纯属浪费。
            if hwnd and new == self._last_prefs_req:
                return
            self._pending_prefs = new
            if hwnd:
                self._last_prefs_req = new
        if hwnd:
            try:
                self._u.PostMessageW(hwnd, WM_APP_REFRESH, 0, 0)
            except Exception:
                pass

    def set_visible(self, visible):
        with self._lock:
            self._pending_visible = bool(visible)
            hwnd = self._hwnd
        if hwnd:
            try:
                self._u.PostMessageW(hwnd, WM_APP_REFRESH, 0, 0)
            except Exception:
                pass

    def set_paused(self, paused):
        """游戏会话停采样（交给别的监控软件）。停 = 停采样线程，窗口另由
        set_visible 管；恢复 = 重启采样线程，第一个读数异步补上。"""
        with self._lock:
            self._pending_paused = bool(paused)
            hwnd = self._hwnd
        if hwnd:
            try:
                self._u.PostMessageW(hwnd, WM_APP_REFRESH, 0, 0)
            except Exception:
                pass

    def _emit(self, action, **kw):
        if self._request_cb is None:
            return
        try:
            self._request_cb(dict(action=action, **kw))
        except Exception:
            pass

    # ------------------------------------------------------ 线程主体
    def _run(self):
        try:
            self._setup_api()
        except Exception as e:
            self._emit('log', msg='悬浮窗 API 初始化失败: %r' % (e,))
            self._created.set()
            return
        try:
            self._set_dpi_awareness()
            with self._lock:
                prefs = dict(self._prefs)
                visible = self._visible
            # 采样器构造是纯 Python 对象、不加载 DLL；第一帧采样（PDH 开查询
            # + ADL 初始化，本机实测 384ms）交给采样线程，建窗路径不再等它。
            # v1.6.1：Sampler 不再在这里创建，改由采样线程自己持有（见
            # _sample_loop）——窗口线程一旦持有它，恢复监控时新线程就会和
            # 卡在驱动调用里的旧线程共用同一组句柄。
            if not self._create_window():
                self._created.set()
                return
            if self._stopping:
                # start() 已超时放弃：窗口刚建好就自行销毁退出（finally 清理）
                self._created.set()
                return
            self._apply_prefs(prefs, initial=True)
            self._relayout()          # 先画一帧占位，数值等采样线程补上
            self._apply_visible(visible)
            self._start_worker()
            self._created.set()
            self._message_loop()
        except Exception as e:
            self._emit('log', msg='悬浮窗线程异常: %r' % (e,))
            self._created.set()
        finally:
            self._teardown()

    # ------------------------------------------------------ 采样线程（v1.6.0）
    def _take_sampler_cfg(self):
        with self._lock:
            cfg = self._pending_sampler_cfg
            self._pending_sampler_cfg = None
        return cfg

    def _sampler_cfg(self):
        """当前偏好的采样器参数（采样线程首次启动时用）。"""
        with self._lock:
            prefs = dict(self._prefs)
        return {'gpu_clock': bool(prefs.get('show_gpu_freq')),
                'gpu_select': prefs.get('gpu_select') or 'auto',
                'gpu_luid': prefs.get('gpu_luid') or ''}

    def _start_worker(self, cfg=None):
        """起采样线程（幂等）。返回是否真的起了新线程。

        铁律：**旧线程还活着就绝不起第二条**。旧线程可能正卡在 PDH/ADL 的
        原生调用里，它手里的采样器仍是活的；此时再起一条，两条线程会同时
        读同一组句柄（v1.6.0 的重入风险）。这种情况下只登记「待恢复」，
        由 _on_refresh（守护每轮都会推一次偏好）重试。
        """
        if cfg is None:
            cfg = self._sampler_cfg()   # 先取，别在持锁时再进 _lock（会自锁）
        with self._lock:
            old = self._worker
            if old is not None and old.is_alive():
                self._restart_pending = True
                self._pending_sampler_cfg = cfg
                return False
            stop_ev = threading.Event()
            self._worker_stop = stop_ev
            self._worker_gen += 1
            gen = self._worker_gen
            self._sampler_state = 'running'
            self._restart_pending = False
            t = threading.Thread(target=self._sample_loop,
                                 args=(stop_ev, gen, cfg),
                                 name='hud-sampler-%d' % gen)
            t.daemon = True
            self._worker = t
        t.start()
        return True

    def _stop_worker(self, timeout=0.8):
        """请求采样线程退出，返回是否真的退出了。

        原生调用卡住时 join 会超时：此时把状态标成 orphan 并**保留线程引用**，
        采样器归那条线程所有，随进程回收；绝不在窗口线程里 close 它。
        """
        with self._lock:
            ev = self._worker_stop
            self._worker_stop = None
            t = self._worker
        if ev is not None:
            ev.set()
        if t is not None:
            t.join(timeout)
            if t.is_alive():
                with self._lock:
                    self._sampler_state = 'orphan'
                return False
        with self._lock:
            if not (self._worker is not None and self._worker.is_alive()):
                self._sampler_state = 'stopped'
        return True

    def _make_sampler(self, cfg):
        """采样器工厂：**只允许采样线程调用**（测试可替换它注入替身）。"""
        return hud_metrics.Sampler(
            enable_gpu_clock=bool((cfg or {}).get('gpu_clock')))

    def _sample_loop(self, stop_ev, gen, init_cfg):
        """采样线程主体：Sampler 由本线程创建、使用、关闭，不与其他线程共享。"""
        sampler = None
        try:
            sampler = self._make_sampler(init_cfg)
        except Exception as e:
            self._emit('log', msg='采样器创建失败: %r' % (e,))
            with self._lock:
                if self._worker_gen == gen:
                    self._sampler_state = 'failed'
            return
        applied = init_cfg
        tick = 0
        try:
            while not stop_ev.is_set():
                cfg = self._take_sampler_cfg()
                if cfg is None:
                    cfg = applied if applied is not None else {}
                applied = None
                if cfg:
                    # 偏好里的采样器参数只在采样线程里应用：无锁竞争，也不会
                    # 让窗口线程等一次原生调用
                    try:
                        sampler.set_gpu_clock_enabled(cfg['gpu_clock'])
                        sampler.set_gpu_target(cfg['gpu_select'],
                                               cfg['gpu_luid'])
                    except Exception as e:
                        self._emit('log', msg='应用采样配置失败: %r' % (e,))
                t0 = time.perf_counter()
                try:
                    snap = sampler.sample()
                except Exception as e:
                    snap = {}
                    self._emit('log', msg='采样异常: %r' % (e,))
                dt_ms = (time.perf_counter() - t0) * 1000.0
                meta = self._gpu_meta(sampler, snap)
                with self._sample_lock:
                    self._sample = snap or {}
                    self._sample_ms = dt_ms
                    self._sample_at = time.time()
                    self._sample_meta = meta
                # 分项诊断每 15 拍发布一次（也给窗口线程的菜单/诊断用），
                # 避免每拍都去拼字符串、读 PDH 状态。
                tick += 1
                if tick % 15 == 1:
                    try:
                        with self._sample_lock:
                            self._backend_lines = list(sampler.diagnostics())
                            self._menu_adapters = list(sampler.adapters(ttl=30.0)[:8])
                    except Exception:
                        pass
                backoff = self._note_sample_cost(dt_ms)
                hwnd = self._hwnd
                if hwnd:
                    try:
                        self._u.PostMessageW(hwnd, WM_APP_SAMPLE, 0, 0)
                    except Exception:
                        pass
                # 等下一拍：200ms 粒度检查停止请求与节拍改动
                deadline = time.monotonic() + self._interval / 1000.0 + backoff
                while not stop_ev.is_set():
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    stop_ev.wait(min(left, 0.2))
        finally:
            # 只有拥有它的线程能关闭它：这条路一定在原生调用返回之后才走到。
            try:
                sampler.close()
            except Exception:
                pass
            with self._lock:
                if self._worker_gen == gen:
                    if self._sampler_state != 'orphan':
                        self._sampler_state = 'stopped'

    def _gpu_meta(self, sampler, snap):
        """GPU 没数据时给出人话原因，区分「没目标卡 / 计数器不可用 / 还没数据」。"""
        meta = {'reason': '', 'target': '', 'target_name': ''}
        try:
            st = sampler.pdh.status()
            meta['target'] = st.get('target') or ''
            meta['target_name'] = st.get('target_name') or ''
            meta['error'] = st.get('error') or ''
            if snap.get('gpu') is None:
                if not st.get('ok'):
                    meta['reason'] = 'no-ctr'     # 驱动计数器不可用
                elif not st.get('target'):
                    meta['reason'] = 'no-target'  # 目标卡缺失
                else:
                    meta['reason'] = 'wait'       # 有目标但这一拍没采到
        except Exception:
            pass
        return meta

    def _note_sample_cost(self, dt_ms):
        """慢采样退避，返回本轮额外等待的秒数（上限 5s）。

        判据：单次耗时超过「节拍的一半且 ≥40ms」。日志 60s 去重——驱动一直
        慢的时候不能把 guard.log 刷满。
        """
        interval_ms = max(200, int(self._interval))
        slow = dt_ms > max(40.0, interval_ms * 0.5)
        now = time.time()
        if slow:
            self._slow_events += 1
            self._last_slow_ms = max(self._last_slow_ms or 0.0, dt_ms)
            if now - self._last_slow_log > 60.0:
                self._last_slow_log = now
                self._emit('log', msg='桌面监控采样偏慢：单次 %.0fms（第 %d 次），'
                                      '本轮退避 %.1fs'
                           % (dt_ms, self._slow_events,
                              min(dt_ms * 2.0 / 1000.0, 5.0)))
            return min(dt_ms * 2.0 / 1000.0, 5.0)
        if self._last_slow_ms is not None:
            self._emit('log', msg='桌面监控采样恢复正常（最慢一次 %.0fms，'
                                  '慢事件累计 %d 次）'
                       % (self._last_slow_ms, self._slow_events))
            self._last_slow_ms = None
        return 0.0

    def _setup_api(self):
        # use_last_error=True：否则 ctypes.get_last_error() 恒 0，注册失败
        # 日志只能写 err=0，没法定位（实测踩过）。
        u = ctypes.WinDLL('user32', use_last_error=True)
        g = ctypes.WinDLL('gdi32', use_last_error=True)
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        self._u, self._g, self._k = u, g, k

        k.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        k.GetModuleHandleW.restype = wt.HINSTANCE

        u.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        u.RegisterClassExW.restype = wt.ATOM
        u.CreateWindowExW.argtypes = [
            wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
        u.CreateWindowExW.restype = wt.HWND
        u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, WPARAM, LPARAM]
        u.DefWindowProcW.restype = LRESULT
        u.DestroyWindow.argtypes = [wt.HWND]
        u.DestroyWindow.restype = wt.BOOL
        u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        u.ShowWindow.restype = wt.BOOL
        u.UpdateWindow.argtypes = [wt.HWND]
        u.UpdateWindow.restype = wt.BOOL
        u.SetLayeredWindowAttributes.argtypes = [
            wt.HWND, wt.DWORD, wt.BYTE, wt.DWORD]
        u.SetLayeredWindowAttributes.restype = wt.BOOL
        u.SetWindowPos.argtypes = [
            wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wt.UINT]
        u.SetWindowPos.restype = wt.BOOL
        u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        u.GetWindowRect.restype = wt.BOOL
        u.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        u.GetClientRect.restype = wt.BOOL
        u.GetMessageW.argtypes = [
            ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
        u.GetMessageW.restype = wt.BOOL
        u.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
        u.TranslateMessage.restype = wt.BOOL
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
        u.DispatchMessageW.restype = LRESULT
        u.PostMessageW.argtypes = [wt.HWND, wt.UINT, WPARAM, LPARAM]
        u.PostMessageW.restype = wt.BOOL
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        u.PostQuitMessage.restype = None
        u.BeginPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]
        u.BeginPaint.restype = wt.HDC
        u.EndPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]
        u.EndPaint.restype = wt.BOOL
        u.InvalidateRect.argtypes = [
            wt.HWND, ctypes.POINTER(wt.RECT), wt.BOOL]
        u.InvalidateRect.restype = wt.BOOL
        u.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, wt.LPVOID]
        u.SetTimer.restype = ctypes.c_size_t
        u.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
        u.KillTimer.restype = wt.BOOL
        u.GetDC.argtypes = [wt.HWND]
        u.GetDC.restype = wt.HDC
        u.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
        u.ReleaseDC.restype = ctypes.c_int
        u.SetForegroundWindow.argtypes = [wt.HWND]
        u.SetForegroundWindow.restype = wt.BOOL
        u.CreatePopupMenu.argtypes = []
        u.CreatePopupMenu.restype = wt.HMENU
        u.AppendMenuW.argtypes = [
            wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
        u.AppendMenuW.restype = wt.BOOL
        u.TrackPopupMenuEx.argtypes = [
            wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID]
        u.TrackPopupMenuEx.restype = ctypes.c_int
        u.DestroyMenu.argtypes = [wt.HMENU]
        u.DestroyMenu.restype = wt.BOOL
        u.SystemParametersInfoW.argtypes = [
            wt.UINT, wt.UINT, wt.LPVOID, wt.UINT]
        u.SystemParametersInfoW.restype = wt.BOOL
        u.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
        u.MonitorFromWindow.restype = wt.HANDLE
        u.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(MONITORINFO)]
        u.GetMonitorInfoW.restype = wt.BOOL
        u.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
        u.GetCursorPos.restype = wt.BOOL
        u.LoadCursorW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
        u.LoadCursorW.restype = wt.HANDLE
        u.IsWindow.argtypes = [wt.HWND]
        u.IsWindow.restype = wt.BOOL
        u.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
        u.FindWindowW.restype = wt.HWND
        u.SetParent.argtypes = [wt.HWND, wt.HWND]
        u.SetParent.restype = wt.HWND
        u.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
        u.FindWindowExW.restype = wt.HWND
        u.EnumWindows.argtypes = [ENUMPROC, wt.LPARAM]
        u.EnumWindows.restype = wt.BOOL
        u.SendMessageTimeoutW.argtypes = [
            wt.HWND, wt.UINT, WPARAM, LPARAM, wt.UINT, wt.UINT,
            ctypes.POINTER(wt.DWORD)]
        u.SendMessageTimeoutW.restype = LRESULT
        u.DrawTextW.argtypes = [
            wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(wt.RECT), wt.UINT]
        u.DrawTextW.restype = ctypes.c_int
        u.FillRect.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH]
        u.FillRect.restype = ctypes.c_int
        for nm in ('GetWindowLongPtrW', 'SetWindowLongPtrW'):
            if hasattr(u, nm):
                fn = getattr(u, nm)
                fn.argtypes = [wt.HWND, ctypes.c_int, LRESULT]
                fn.restype = LRESULT
        if hasattr(u, 'GetDpiForWindow'):
            u.GetDpiForWindow.argtypes = [wt.HWND]
            u.GetDpiForWindow.restype = wt.UINT
        if hasattr(u, 'SetThreadDpiAwarenessContext'):
            u.SetThreadDpiAwarenessContext.argtypes = [wt.LPVOID]
            u.SetThreadDpiAwarenessContext.restype = wt.LPVOID

        g.CreateFontW.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
            wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR]
        g.CreateFontW.restype = wt.HFONT
        g.DeleteObject.argtypes = [wt.HANDLE]
        g.DeleteObject.restype = wt.BOOL
        g.CreateSolidBrush.argtypes = [wt.DWORD]
        g.CreateSolidBrush.restype = wt.HBRUSH
        g.SetTextColor.argtypes = [wt.HDC, wt.DWORD]
        g.SetTextColor.restype = wt.DWORD
        g.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
        g.SetBkMode.restype = ctypes.c_int
        g.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
        g.SelectObject.restype = wt.HANDLE
        g.GetTextExtentPoint32W.argtypes = [
            wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(wt.SIZE)]
        g.GetTextExtentPoint32W.restype = wt.BOOL
        g.GetDeviceCaps.argtypes = [wt.HDC, ctypes.c_int]
        g.GetDeviceCaps.restype = ctypes.c_int

    def _set_dpi_awareness(self):
        try:
            self._u.SetThreadDpiAwarenessContext(DPI_PER_MONITOR_V2)
        except Exception:
            pass

    def _create_window(self):
        u, k = self._u, self._k
        self._wndproc_cb = WNDPROC(self._wndproc)
        # 类名带实例 id：同进程内旧实例（如 start 超时遗留的孤儿线程）
        # 还占着旧类名时，新实例仍能注册成功，不被 ERROR_CLASS_ALREADY_EXISTS 卡死。
        self._class_name = 'WingmanDesktopHud_%d_%x' % (os.getpid(), id(self))
        hinst = k.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = self._wndproc_cb
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = None
        wc.hCursor = u.LoadCursorW(None, ctypes.c_wchar_p(IDC_ARROW))
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = self._class_name
        wc.hIconSm = None
        atom = u.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            self._emit('log', msg='RegisterClassExW 失败 err=%d'
                       % ctypes.get_last_error())
            return False
        ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
        hwnd = u.CreateWindowExW(
            ex, self._class_name, 'Wingman HUD', WS_POPUP,
            0, 0, self._w, self._h, None, None, hinst, None)
        if not hwnd:
            self._emit('log', msg='CreateWindowExW 失败 err=%d'
                       % ctypes.get_last_error())
            return False
        with self._lock:
            self._hwnd = hwnd
        try:
            self._dpi = int(u.GetDpiForWindow(hwnd)) or 96
        except Exception:
            self._dpi = 96
        if self._dpi <= 0:
            hdc = u.GetDC(hwnd)
            try:
                self._dpi = g_getdpi(self._g, hdc)
            finally:
                u.ReleaseDC(hwnd, hdc)
        return True

    def _message_loop(self):
        u = self._u
        msg = wt.MSG()
        while True:
            r = u.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:
                break
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    def _teardown(self):
        u, g = self._u, self._g
        joined = self._stop_worker(timeout=1.0)
        if not joined:
            # 采样线程还卡在原生调用里：它手里的采样器/句柄不能碰，线程是
            # daemon，随进程回收。这里只留一条线索，方便事后对照日志。
            self._emit('log', msg='采样线程未在 1s 内退出（随进程回收，采样器归该线程所有）')
        try:
            if self._font:
                g.DeleteObject(self._font)
                self._font = None
            if self._bg_brush:
                g.DeleteObject(self._bg_brush)
                self._bg_brush = None
        except Exception:
            pass
        # 缓存一并作废：下次建窗必须重新建字体、重量文本、重设位置
        self._font_key = None
        self._measure_key = None
        self._last_pos = None
        try:
            if self._class_name:
                u.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
                u.UnregisterClassW.restype = wt.BOOL
                u.UnregisterClassW(self._class_name,
                                   self._k.GetModuleHandleW(None))
        except Exception:
            pass
        with self._lock:
            self._hwnd = None

    # ------------------------------------------------------ 窗口过程
    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_NCHITTEST:
                # 整窗当标题栏 -> 左键可拖动；右键仍走 WM_CONTEXTMENU
                if not self._prefs.get('click_through'):
                    return HTCAPTION
                return self._u.DefWindowProcW(hwnd, msg, wp, lp)
            if msg == WM_PAINT:
                self._paint(hwnd)
                return 0
            if msg == WM_APP_SAMPLE:
                self._on_sample()
                return 0
            if msg == WM_APP_REFRESH:
                self._on_refresh()
                return 0
            if msg in (WM_RBUTTONUP, WM_NCRBUTTONUP):
                # 整窗 HTCAPTION 时右键落在非客户区，DefWindowProc 不保证
                # 转发 WM_CONTEXTMENU，这里显式接住两种右键抬起消息。
                pt = wt.POINT()
                self._u.GetCursorPos(ctypes.byref(pt))
                self._show_menu(pt.x, pt.y)
                return 0
            if msg == WM_CONTEXTMENU:
                self._show_menu(_lo(lp), _hi(lp))
                return 0
            if msg == WM_EXITSIZEMOVE:
                self._on_drag_end()
                return 0
            if msg == WM_CLOSE:
                self._u.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self._u.PostQuitMessage(0)
                return 0
        except Exception as e:
            self._emit('log', msg='wndproc msg=0x%X 异常: %r' % (msg, e))
        return self._u.DefWindowProcW(hwnd, msg, wp, lp)

    def _on_refresh(self):
        with self._lock:
            p = self._pending_prefs
            self._pending_prefs = None
            v = self._pending_visible
            self._pending_visible = None
            pa = self._pending_paused
            self._pending_paused = None
        if p is not None:
            self._apply_prefs(p)
        if pa is not None:
            self._apply_paused(pa)
        if v is not None:
            self._apply_visible(v)
        # 待恢复的采样：旧线程退出后才能起新的。每轮 WM_APP_REFRESH（守护每个
        # tick 都会推偏好）重试一次，所以卡顿一结束就自动恢复，不用重启插件。
        if self._restart_pending and not self._paused and not self._stopping:
            if self._start_worker(self._sampler_cfg()):
                self._emit('log', msg='旧采样线程已退出，采样已恢复')

    def _apply_paused(self, paused):
        if paused == self._paused:
            return
        self._paused = paused
        try:
            if paused:
                self._stop_worker()       # 游戏里停采样：只停采样线程，窗口由 hide 管
            else:
                # 恢复：立即起采样线程，第一个读数由它异步补上（窗口线程不等）。
                # 旧线程若还卡在驱动调用里，_start_worker 只登记待恢复，绝不
                # 起第二条线程去共用它的 PDH/ADL 句柄。
                if not self._start_worker(self._sampler_cfg()):
                    self._emit('log', msg='旧采样线程仍在驱动调用中，暂缓恢复采样'
                                          '（退出后自动重试）')
                self._relayout()
                if self._hwnd:
                    self._u.InvalidateRect(self._hwnd, None, True)
        except Exception:
            pass

    def _apply_prefs(self, p, initial=False):
        with self._lock:
            self._prefs.update(p)
            prefs = dict(self._prefs)
        self._interval = _interval_ms(prefs)
        # 采样器参数不在窗口线程执行：投给采样线程，下一拍生效
        with self._lock:
            self._pending_sampler_cfg = {
                'gpu_clock': bool(prefs.get('show_gpu_freq')),
                'gpu_select': prefs.get('gpu_select') or 'auto',
                'gpu_luid': prefs.get('gpu_luid') or '',
            }
        self._apply_style(prefs)
        self._sync_parent(bool(prefs.get('desktop_only')))
        self._make_font(prefs)
        try:
            self._u.SetLayeredWindowAttributes(
                self._hwnd, 0, int(max(40, min(255, prefs.get('opacity', 215)))),
                LWA_ALPHA)
        except Exception:
            pass
        self._relayout()
        self._update_visibility()

    def _apply_style(self, prefs):
        u = self._u
        hwnd = self._hwnd
        try:
            fn = getattr(u, 'SetWindowLongPtrW', None) or \
                getattr(u, 'SetWindowLongW', None)
            get = getattr(u, 'GetWindowLongPtrW', None) or \
                getattr(u, 'GetWindowLongW', None)
            if fn is None or get is None:
                return
            cur = get(hwnd, GWL_EXSTYLE)
            # TOPMOST 位交给 _update_visibility 的 z 序状态机管（SetWindowPos
            # 换 band 时系统会自动增删该位）；这里强写会造成 band/样式拉锯。
            ex = WS_EX_LAYERED | WS_EX_TOOLWINDOW | (cur & WS_EX_TOPMOST)
            if prefs.get('click_through'):
                ex |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
            get.argtypes = [wt.HWND, ctypes.c_int]
            get.restype = LRESULT
            fn.argtypes = [wt.HWND, ctypes.c_int, LRESULT]
            fn.restype = LRESULT
            if cur != ex:
                fn(hwnd, GWL_EXSTYLE, ex)
                u.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                               SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER |
                               SWP_NOACTIVATE | SWP_FRAMECHANGED)
        except Exception:
            pass

    def _apply_visible(self, visible):
        with self._lock:
            self._visible = bool(visible)
        self._update_visibility()

    def _find_desktop_workerw(self):
        """壁纸层 WorkerW：Progman 收到 0x052C 后会在图标层之后生成一个
        空 WorkerW（Wallpaper Engine / Rainmeter 同款做法）。挂为其子窗 =
        高于壁纸、低于图标与一切应用窗口，且 Win+D 不会最小化它。
        找不到时回退 Progman 本身，仍是桌面层。"""
        u = self._u
        progman = u.FindWindowW('Progman', None)
        if not progman:
            return None
        done = wt.DWORD()
        u.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0, 1000,
                              ctypes.byref(done))
        found = []

        def cb(h, lp):
            if u.FindWindowExW(h, None, 'SHELLDLL_DefView', None):
                w = u.FindWindowExW(None, h, 'WorkerW', None)
                if w:
                    found.append(w)
            return True

        ref = ENUMPROC(cb)
        u.EnumWindows(ref, 0)
        return found[0] if found else progman

    def _sync_parent(self, desktop_only):
        """desktop_only 开 = 挂到桌面 WorkerW 下（桌面常驻）；关 = 回到
        顶层 + 始终置顶（旧行为）。层级关系本身就保证「不遮挡应用窗口」，
        不需要跟 z 序 band / Win+D 最小化搏斗。"""
        hwnd = self._hwnd
        if not hwnd:
            return
        try:
            if desktop_only:
                if self._parent is None:
                    w = self._find_desktop_workerw()
                    if not w:
                        return
                    self._u.SetParent(hwnd, w)
                    self._parent = w
                    self._u.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                                         SWP_NOMOVE | SWP_NOSIZE |
                                         SWP_NOACTIVATE)
                    self._relayout()
            elif self._parent is not None:
                self._u.SetParent(hwnd, None)
                self._parent = None
                self._u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                self._relayout()
        except Exception:
            pass

    def _update_visibility(self):
        """显隐 = 守护请求（游戏隐藏由插件 hide_in_game 走 set_visible）。
        桌面常驻的层级由 _sync_parent 负责，这里只管显示 / 隐藏。"""
        with self._lock:
            want = self._visible
        try:
            if not want:
                if self._shown:
                    self._u.ShowWindow(self._hwnd, SW_HIDE)
                    self._shown = False
                return
            if not self._shown:
                self._u.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
                self._shown = True
                self._u.InvalidateRect(self._hwnd, None, True)
        except Exception:
            pass

    def _make_font(self, prefs):
        g = self._g
        try:
            px = int(round(float(prefs.get('font_size', 14)) * self._dpi / 96.0))
            px = max(9, px)
            key = (px, self._dpi)
            if self._font and key == self._font_key:
                return          # 字号/DPI 没变就别重建字体（原来每次偏好都重建）
            if self._font:
                g.DeleteObject(self._font)
                self._font = None
            self._font = g.CreateFontW(
                -px, 0, 0, 0, FW_NORMAL, 0, 0, 0, DEFAULT_CHARSET,
                0, 0, CLEARTYPE_QUALITY, FIXED_PITCH_FF_MODERN, 'Consolas')
            self._font_px = px
            self._font_key = key
            self._measure_key = None    # 字体换了，文本宽度必须重量
        except Exception:
            self._font = None
            self._font_px = 14
            self._font_key = None
            self._measure_key = None

    # ------------------------------------------------------ 采样 / 布局 / 绘制
    def _on_sample(self):
        """采样线程送来的新读数：只做布局 + 重绘，窗口线程绝不再采样。"""
        if self._paused:
            return
        with self._sample_lock:
            snapshot = self._sample
        self._sample = snapshot
        self._relayout()
        try:
            if self._shown:
                self._u.InvalidateRect(self._hwnd, None, True)
        except Exception:
            pass

    def _build_lines(self):
        p = self._prefs
        s = self._sample or {}
        lines = []

        # v1.6.0：精度可配（0/1 位小数）。关掉小数时保持原来的定宽写法，
        # 文本长度只取决于位数，_relayout 的等宽缓存才有意义。
        dec = 1 if _pct_int(p.get('pct_decimals', 0)) else 0

        def pct(v):
            if v is None:
                return '  -'
            if dec:
                return '%5.1f%%' % v
            return '%3d%%' % int(round(v))

        def ghz(mhz):
            return '%.1fG' % (mhz / 1000.0) if mhz else '-'

        def gb(b):
            return b / 1073741824.0 if b is not None else None

        if p.get('show_cpu'):
            extra = ('  ' + ghz(s.get('cpu_mhz'))) if p.get('show_cpu_freq') else ''
            lines.append(('CPU', pct(s.get('cpu')) + extra, s.get('cpu')))
        if p.get('show_gpu'):
            extra = ('  ' + ghz(s.get('gpu_mhz'))) if p.get('show_gpu_freq') else ''
            val = pct(s.get('gpu'))
            if s.get('gpu') is None:
                # 没读数时别只留一个「-」：区分目标卡缺失 / 计数器不可用 /
                # 尚未采到，用户一眼能判断是硬件环境还是时序问题。
                tag = (self._sample_meta or {}).get('reason') or ''
                if tag:
                    val = val + ' ' + tag
            lines.append(('GPU', val + extra, s.get('gpu')))
        if p.get('show_vram'):
            u_, t_ = gb(s.get('vram_used')), gb(s.get('vram_total'))
            detail = '%s/%sG' % ('%.1f' % u_ if u_ is not None else '-',
                                 '%.0f' % t_ if t_ else '-')
            lines.append(('VRAM', pct(s.get('vram_percent')) + '  ' + detail,
                          s.get('vram_percent')))
        if p.get('show_ram'):
            u_, t_ = gb(s.get('ram_used')), gb(s.get('ram_total'))
            detail = '%s/%sG' % ('%.1f' % u_ if u_ is not None else '-',
                                 '%.0f' % t_ if t_ else '-')
            lines.append(('RAM', pct(s.get('ram_percent')) + '  ' + detail,
                          s.get('ram_percent')))
        if not lines:
            lines.append(('HUD', '(no items)', None))
        return lines

    def _relayout(self):
        self._lines = self._build_lines()
        u, g = self._u, self._g
        hwnd = self._hwnd
        if not hwnd:
            return
        pad = max(6, int(round(8 * self._dpi / 96.0)))
        line_h = int(round(self._font_px * 1.45))
        texts = tuple('%-4s %s' % (label, val) for label, val, _p in self._lines)
        # Consolas 等宽且全是 ASCII：宽度只取决于字符数，不取决于具体数字。
        # 用「各行长度」而不是文本内容做 key，否则数值每秒变一次等于没缓存。
        key = (tuple(len(t) for t in texts), self._font_px, self._dpi, pad, line_h)
        if key == self._measure_key:
            # 位数没变 -> 宽度没变，省掉 GetDC + 6 次 GetTextExtentPoint32W
            self._reposition()
            return
        hdc = u.GetDC(hwnd)
        maxw = 0
        old = None
        measured = True
        try:
            if self._font:
                old = g.SelectObject(hdc, self._font)
            sz = wt.SIZE()
            if g.GetTextExtentPoint32W(hdc, 'VRAM ', 5, ctypes.byref(sz)):
                self._label_w = sz.cx
            for text in texts:
                if g.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(sz)):
                    maxw = max(maxw, sz.cx)
        except Exception:
            maxw = 160
            measured = False
        finally:
            if old:
                g.SelectObject(hdc, old)
            u.ReleaseDC(hwnd, hdc)
        self._pad = pad
        self._line_h = line_h
        self._w = maxw + pad * 2
        self._h = line_h * len(self._lines) + pad * 2
        # 量失败时不缓存，下次老老实实重量
        self._measure_key = key if measured else None
        self._reposition()

    def _work_area(self):
        """窗口**所在显示器**的工作区。

        v1.5.0：原来只用 SPI_GETWORKAREA，它只返回主屏工作区，
        副屏上选「右上/右下」会落到主屏坐标。改成 MonitorFromWindow +
        GetMonitorInfoW 取当前屏；拿不到时回退旧行为。
        """
        u = self._u
        try:
            if self._hwnd and getattr(u, 'MonitorFromWindow', None) is not None:
                hmon = u.MonitorFromWindow(self._hwnd, MONITOR_DEFAULTTONEAREST)
                if hmon:
                    mi = MONITORINFO()
                    mi.cbSize = ctypes.sizeof(MONITORINFO)
                    if u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                        rc = mi.rcWork
                        return rc.left, rc.top, rc.right, rc.bottom
        except Exception:
            pass
        rc = wt.RECT()
        try:
            if u.SystemParametersInfoW(
                    SPI_GETWORKAREA, 0, ctypes.byref(rc), 0):
                return rc.left, rc.top, rc.right, rc.bottom
        except Exception:
            pass
        return 0, 0, 1920, 1080

    def _virtual_screen(self):
        """四个角围出的虚拟屏（全部显示器）。拿不到就返回 None（不启用回收）。"""
        try:
            u = self._u
            return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),   # X / Y
                    u.GetSystemMetrics(78), u.GetSystemMetrics(79))   # CX / CY
        except Exception:
            return None

    def _reposition(self):
        p = self._prefs
        hwnd = self._hwnd
        if not hwnd:
            return
        left, top, right, bottom = self._work_area()
        m = int(p.get('margin', 16))
        pos = p.get('position', 'top-right')
        if pos == 'custom':
            x, y = int(p.get('x', 0)), int(p.get('y', 0))
            # v1.6.0：显示器布局变过（拔副屏 / 改分辨率）之后，存下来的自定义坐标
            # 可能整块落在屏外，用户看到的就是「悬浮窗不见了」。这里回收到右上角。
            vs = self._virtual_screen()
            if vs:
                vx, vy, vw, vh = vs
                if (x + self._w <= vx or x >= vx + vw
                        or y + self._h <= vy or y >= vy + vh):
                    if not getattr(self, '_pos_clamp_logged', False):
                        self._pos_clamp_logged = True
                        self._emit('log', msg=(
                            '自定义坐标 (%s,%s) 在可见屏幕之外，已回收到右上角'
                            '（显示器布局变化后会出现这种情况）'
                            % (p.get('x'), p.get('y'))))
                    x, y = right - self._w - m, top + m
                    # 顺手把内存里的坐标改成回收后的值，免得每拍都判一次屏外
                    p['x'], p['y'] = x, y
        elif pos == 'top-left':
            x, y = left + m, top + m
        elif pos == 'bottom-right':
            x, y = right - self._w - m, bottom - self._h - m
        elif pos == 'bottom-left':
            x, y = left + m, bottom - self._h - m
        elif pos == 'top-center':
            x, y = left + (right - left - self._w) // 2, top + m
        elif pos == 'bottom-center':
            x, y = left + (right - left - self._w) // 2, bottom - self._h - m
        elif pos == 'left-center':
            x, y = left + m, top + (bottom - top - self._h) // 2
        elif pos == 'right-center':
            x, y = right - self._w - m, top + (bottom - top - self._h) // 2
        else:                                   # top-right
            x, y = right - self._w - m, top + m
        if self._parent:
            # 子窗坐标相对父窗原点；WorkerW 跨虚拟屏幕，原点未必 (0,0)
            pr = wt.RECT()
            if self._u.GetWindowRect(self._parent, ctypes.byref(pr)):
                x -= pr.left
                y -= pr.top
        target = (x, y, self._w, self._h)
        if target == self._last_pos:
            return          # 位置尺寸都没变，别每秒 SetWindowPos 一次
        try:
            # NOZORDER：布局/拖拽只动位置尺寸，不碰父子层级决定的 z 序
            self._u.SetWindowPos(hwnd, None, x, y, self._w, self._h,
                                 SWP_NOACTIVATE | SWP_NOZORDER |
                                 (SWP_SHOWWINDOW if self._shown else 0))
            self._last_pos = target
        except Exception:
            pass

    def _paint(self, hwnd):
        u, g = self._u, self._g
        ps = PAINTSTRUCT()
        hdc = u.BeginPaint(hwnd, ctypes.byref(ps))
        if not hdc:
            return
        brush = None
        old = None
        try:
            rc = wt.RECT()
            u.GetClientRect(hwnd, ctypes.byref(rc))
            if self._bg_brush is None:
                self._bg_brush = g.CreateSolidBrush(_BG)   # 缓存，_teardown 释放
            u.FillRect(hdc, ctypes.byref(rc), self._bg_brush)
            g.SetBkMode(hdc, TRANSPARENT_BK)
            if self._font:
                old = g.SelectObject(hdc, self._font)
            pad = getattr(self, '_pad', 8)
            line_h = getattr(self, '_line_h', 20)
            label_w = getattr(self, '_label_w', 40)
            # v1.6.0：阈值可配（默认 70/90），保证 high 不小于 warn
            warn = _pct_num(self._prefs, 'warn_percent', 70)
            high = max(warn, _pct_num(self._prefs, 'high_percent', 90))
            for i, (label, val, pv) in enumerate(self._lines):
                y = pad + i * line_h
                # 标签暗色
                g.SetTextColor(hdc, _DIM)
                lr = wt.RECT(pad, y, pad + label_w, y + line_h)
                u.DrawTextW(hdc, '%-4s' % label, -1, ctypes.byref(lr),
                            DT_LEFT | DT_TOP | DT_SINGLELINE | DT_NOPREFIX)
                # 数值按占用染色
                col = _FG
                if pv is not None:
                    if pv >= high:
                        col = _HIGH
                    elif pv >= warn:
                        col = _WARN
                g.SetTextColor(hdc, col)
                vr = wt.RECT(pad + label_w, y, self._w - pad, y + line_h)
                u.DrawTextW(hdc, val, -1, ctypes.byref(vr),
                            DT_LEFT | DT_TOP | DT_SINGLELINE | DT_NOPREFIX)
        except Exception as e:
            self._emit('log', msg='paint 异常: %r' % (e,))
        finally:
            if old:
                g.SelectObject(hdc, old)
            u.EndPaint(hwnd, ctypes.byref(ps))

    # ------------------------------------------------------ 拖动 / 菜单
    def _on_drag_end(self):
        try:
            rc = wt.RECT()
            if not self._u.GetWindowRect(self._hwnd, ctypes.byref(rc)):
                return
            with self._lock:
                self._prefs['position'] = 'custom'
                self._prefs['x'] = int(rc.left)
                self._prefs['y'] = int(rc.top)
                prefs = dict(self._prefs)
            self._emit('prefs', prefs=prefs)
        except Exception:
            pass

    def _show_menu(self, x, y):
        u = self._u
        p = self._prefs
        if x == -1 and y == -1:
            pt = wt.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            x, y = pt.x, pt.y
        menu = u.CreatePopupMenu()
        if not menu:
            return

        def item(mid, text, checked):
            u.AppendMenuW(menu, MF_STRING | (MF_CHECKED if checked else MF_UNCHECKED),
                          mid, text)

        def sep():
            u.AppendMenuW(menu, MF_SEPARATOR, 0, None)

        try:
            item(M_CPU, 'CPU 占用', p.get('show_cpu'))
            item(M_GPU, 'GPU 占用', p.get('show_gpu'))
            item(M_VRAM, '显存占用', p.get('show_vram'))
            item(M_RAM, '内存占用', p.get('show_ram'))
            item(M_CPUFREQ, 'CPU 频率', p.get('show_cpu_freq'))
            item(M_GPUFREQ, 'GPU 频率', p.get('show_gpu_freq'))
            sep()
            item(M_HIDEINGAME, '游戏中自动隐藏', p.get('hide_in_game'))
            item(M_PAUSEINGAME, '游戏中停止监控', p.get('pause_in_game'))
            item(M_DESKTOPONLY, '桌面常驻（不遮挡窗口）', p.get('desktop_only'))
            item(M_CLICKTHROUGH, '鼠标穿透', p.get('click_through'))
            sep()
            # v1.6.0：目标显卡子菜单（枚举结果按显存降序，最多 8 张）
            mode = p.get('gpu_select') or 'auto'
            luid = p.get('gpu_luid') or ''
            # 适配器清单由采样线程发布（DxgiAdapters 归它所有）；窗口线程只读
            # 缓存，避免在 UI 线程里再碰一次 DXGI/PDH 的原生调用。
            with self._sample_lock:
                ads = list(self._menu_adapters)
            self._menu_adapters = list(ads[:8])
            sub = u.CreatePopupMenu()
            if sub:
                u.AppendMenuW(sub, MF_STRING |
                              (MF_CHECKED if mode == 'auto' else MF_UNCHECKED),
                              M_GPU_AUTO, '自动（显存总量最大）')
                u.AppendMenuW(sub, MF_STRING |
                              (MF_CHECKED if mode == 'load' else MF_UNCHECKED),
                              M_GPU_LOAD, '自动（当前占用最大）')
                if self._menu_adapters:
                    u.AppendMenuW(sub, MF_SEPARATOR, 0, None)
                    for idx, a in enumerate(self._menu_adapters):
                        label = '%s  %.1fG' % (a['name'][:26],
                                               a['vram'] / 1073741824.0)
                        checked = (mode == 'luid' and luid == a['luid'])
                        u.AppendMenuW(sub, MF_STRING |
                                      (MF_CHECKED if checked else MF_UNCHECKED),
                                      M_GPU_BASE + idx, label)
                u.AppendMenuW(menu, MF_POPUP | MF_STRING, sub, '目标显卡')
            sep()
            pos = p.get('position', 'top-right')
            item(M_POS_TR, '位置：右上角', pos == 'top-right')
            item(M_POS_TL, '位置：左上角', pos == 'top-left')
            item(M_POS_BR, '位置：右下角', pos == 'bottom-right')
            item(M_POS_BL, '位置：左下角', pos == 'bottom-left')
            item(M_POS_TC, '位置：顶部居中', pos == 'top-center')
            item(M_POS_BC, '位置：底部居中', pos == 'bottom-center')
            item(M_POS_LC, '位置：左侧居中', pos == 'left-center')
            item(M_POS_RC, '位置：右侧居中', pos == 'right-center')
            sep()
            u.AppendMenuW(menu, MF_STRING, M_DIAG, '诊断信息写入日志')
            u.AppendMenuW(menu, MF_STRING, M_OPENCFG, '打开配置文件')
            u.AppendMenuW(menu, MF_STRING, M_RESET, '恢复默认')
            u.AppendMenuW(menu, MF_STRING, M_CLOSE, '关闭悬浮窗')
            u.SetForegroundWindow(self._hwnd)
            cmd = u.TrackPopupMenuEx(
                menu, TPM_RETURNCMD | TPM_RIGHTBUTTON, x, y, self._hwnd, None)
            u.PostMessageW(self._hwnd, WM_NULL, 0, 0)
            if cmd:
                self._apply_menu(cmd)
        except Exception as e:
            self._emit('log', msg='menu 异常: %r' % (e,))
        finally:
            u.DestroyMenu(menu)

    def _apply_menu(self, cmd):
        special = None
        with self._lock:
            p = self._prefs
            if cmd == M_CPU:
                p['show_cpu'] = not p.get('show_cpu')
            elif cmd == M_GPU:
                p['show_gpu'] = not p.get('show_gpu')
            elif cmd == M_VRAM:
                p['show_vram'] = not p.get('show_vram')
            elif cmd == M_RAM:
                p['show_ram'] = not p.get('show_ram')
            elif cmd == M_CPUFREQ:
                p['show_cpu_freq'] = not p.get('show_cpu_freq')
            elif cmd == M_GPUFREQ:
                p['show_gpu_freq'] = not p.get('show_gpu_freq')
            elif cmd == M_HIDEINGAME:
                p['hide_in_game'] = not p.get('hide_in_game')
            elif cmd == M_PAUSEINGAME:
                p['pause_in_game'] = not p.get('pause_in_game')
            elif cmd == M_DESKTOPONLY:
                p['desktop_only'] = not p.get('desktop_only')
            elif cmd == M_CLICKTHROUGH:
                p['click_through'] = not p.get('click_through')
            elif cmd == M_GPU_AUTO:
                p['gpu_select'] = 'auto'
                p['gpu_luid'] = ''
            elif cmd == M_GPU_LOAD:
                p['gpu_select'] = 'load'
                p['gpu_luid'] = ''
            elif M_GPU_BASE <= cmd < M_GPU_BASE + 8:
                idx = cmd - M_GPU_BASE
                ads = self._menu_adapters
                if 0 <= idx < len(ads):
                    p['gpu_select'] = 'luid'
                    p['gpu_luid'] = ads[idx]['luid']
                else:
                    return          # 菜单快照过期（换过卡），什么都不改
            elif cmd in (M_POS_TR, M_POS_TL, M_POS_BR, M_POS_BL,
                         M_POS_TC, M_POS_BC, M_POS_LC, M_POS_RC):
                p['position'] = {M_POS_TR: 'top-right', M_POS_TL: 'top-left',
                                 M_POS_BR: 'bottom-right',
                                 M_POS_BL: 'bottom-left',
                                 M_POS_TC: 'top-center',
                                 M_POS_BC: 'bottom-center',
                                 M_POS_LC: 'left-center',
                                 M_POS_RC: 'right-center'}[cmd]
            elif cmd == M_RESET:
                p.clear()
                p.update(DEFAULT_PREFS)
            elif cmd == M_DIAG:
                special = 'diag'
            elif cmd == M_OPENCFG:
                special = 'open_config'
            elif cmd == M_CLOSE:
                special = 'close'
            prefs = dict(p)
        # 锁外再做 Win32 / 回传：_apply_visible 等会再次取锁，锁内调用会死锁
        if special == 'diag':
            # 诊断只写日志，不改偏好：直接把分项状态交给插件落 guard.log
            self._emit('log', msg=' | '.join(self._diagnostics_lines()))
            return
        if special == 'open_config':
            self._emit('open_config', prefs=prefs)
            return
        if special == 'close':
            self._emit('close')
            self._apply_visible(False)
            return
        self._apply_prefs(prefs)
        self._emit('prefs', prefs=prefs)

    # ------------------------------------------------------ 诊断（v1.6.0）
    def _diagnostics_lines(self):
        """分项状态：右键「诊断信息写入日志」与 --self-test 共用一份口径。

        只读采样线程发布的结果，不在窗口线程里再调一次 PDH/ADL —— 那会变成
        「第二条线程碰同一组句柄」，正是 v1.6.1 要消除的重入。
        """
        with self._sample_lock:
            sample_ms = self._sample_ms
            meta = dict(self._sample_meta or {})
            backend = list(self._backend_lines)
            sample_at = self._sample_at
            gen = self._worker_gen
            state = self._sampler_state
        age = (time.time() - sample_at) if sample_at else -1.0
        lines = [
            'hud running=%s paused=%s worker=%s gen=%d state=%s interval=%dms '
            'sample_ms=%.1f slow=%d stale=%s restart_pending=%s'
            % (self.is_running(), self._paused,
               bool(self._worker is not None and self._worker.is_alive()),
               gen, state, self._interval, sample_ms, self._slow_events,
               ('%.1fs' % age) if age >= 0 else 'n/a', self._restart_pending),
        ]
        if meta.get('reason'):
            lines.append('hud gpu_reason=%s target=%s(%s) err=%s'
                         % (meta.get('reason'), meta.get('target') or '-',
                            meta.get('target_name') or '-',
                            meta.get('error') or 'none'))
        if backend:
            lines.extend(backend)
        elif state != 'running':
            lines.append('sampler=%s（采样线程未运行）' % state)
        return lines

    def status(self):
        """给插件自检用的结构化快照（不建窗口也能读）。"""
        with self._sample_lock:
            sample = dict(self._sample or {})
            sample_ms = self._sample_ms
            sample_at = self._sample_at
            meta = dict(self._sample_meta or {})
            state = self._sampler_state
            gen = self._worker_gen
        age_ms = int((time.time() - sample_at) * 1000) if sample_at else -1
        stale_after = max(3000, int(self._interval) * 3)
        return {
            'running': self.is_running(),
            'paused': self._paused,
            'interval_ms': self._interval,
            'sample_ms': round(sample_ms, 2) if sample_ms else 0.0,
            'slow_events': self._slow_events,
            'worker_alive': bool(self._worker is not None
                                 and self._worker.is_alive()),
            'worker_gen': gen,
            'sampler_state': state,
            'restart_pending': self._restart_pending,
            'sample_age_ms': age_ms,
            'stale': bool(age_ms < 0 or age_ms > stale_after),
            'gpu_reason': meta.get('reason') or '',
            'gpu_target': meta.get('target_name') or meta.get('target') or '',
            'show_paused': bool(self._paused),
            'sample': sample,
            'backend': self._diagnostics_lines(),
        }


def g_getdpi(g, hdc):
    try:
        v = g.GetDeviceCaps(hdc, LOGPIXELSX)
        return v if v > 0 else 96
    except Exception:
        return 96


# ---------------------------------------------------------------- 独立自测
if __name__ == '__main__':
    import sys
    import time
    secs = 20.0
    pos = 'top-right'
    for a in sys.argv[1:]:
        if a.startswith('--seconds='):
            secs = float(a.split('=', 1)[1])
        elif a.startswith('--position='):
            pos = a.split('=', 1)[1]
        elif a == '--freq':
            pass
    prefs = dict(DEFAULT_PREFS)
    prefs['position'] = pos
    if '--freq' in sys.argv:
        prefs['show_cpu_freq'] = True
        prefs['show_gpu_freq'] = True
    if '--click-through' in sys.argv:
        prefs['click_through'] = True

    ov = HudOverlay(request_cb=lambda r: print('request:', r.get('action'),
                                               flush=True))
    ok = ov.start(prefs=prefs, visible=True)
    print('overlay started=%s running=%s' % (ok, ov.is_running()))
    t0 = time.time()
    try:
        while time.time() - t0 < secs:
            time.sleep(0.3)
    finally:
        ov.stop()
    print('overlay stopped, running=%s' % ov.is_running())