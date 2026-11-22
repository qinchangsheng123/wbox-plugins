# -*- coding: utf-8 -*-
"""shutdown_bin —— 关机 / 重启前自动清空回收站（WBOX 插件，API 1）。

为什么不用 on_exit：WBOX 的 on_exit 只在守护自身停止（stop.flag / 热重载 /
fatal）时发；Windows 关机时守护进程是被系统直接结束的，钩子不会触发。
所以本插件在守护进程里起一条后台线程，创建一个隐藏顶层窗口，监听系统
广播的关机通知。

v1.1.0 两阶段清理（修复 v1.0.0 实测被系统提前杀掉的问题）：
  阶段一 WM_QUERYENDSESSION：立即启动后台清理，并同步等待至多 4 秒
  （低于系统 5 秒的挂起判定）。回收站不大时清理在这一阶段就完成，
  系统拿到 TRUE 时回收站已经空了，最可靠。
  阶段二 WM_ENDSESSION(TRUE)：若清理还没完成，创建一个**可见的**等待
  窗口并 ShutdownBlockReasonCreate——隐藏窗口的阻止理由不会被系统
  理会（v1.0.0 实测：通知后约 5 秒进程就被杀，清理没跑完）；可见窗口
  会让系统显示「此应用正在阻止关机」并一直等下去。清理完成或超过
  「最长等待」（默认 20 秒，可调 5–60 秒）后隐藏窗口、撤销阻止理由，
  放行关机。
  WM_ENDSESSION(FALSE)：关机被取消，立即隐藏窗口并放行。

v1.1.1 去重：阶段一已经清完时阶段二不再重起一轮（以前每次关机会多闪一次
可见等待窗口、并把「清理前 N 项」的记录覆盖成 0 项）。关机被取消后本轮记录
作废，下一次关机重新清。

v1.1.2 空回收站不再记失败：回收站本来就空时 SHEmptyRecycleBinW 返回
E_UNEXPECTED（0x8000FFFF），以前按 hr != 0 判失败，每次关机都记一条假失败
并发 CLEAN_FAIL 事件。现在按「查询 0 项 + 该 HRESULT」识别为无需清理。

注销（ENDSESSION_LOGOFF）默认不清理，可在设置里打开。
进程关机优先级设为 0x100（低于普通应用默认的 0x280），等大多数程序
退完再轮到本插件，尽量把它们在退出前删进回收站的东西一并清掉。

安全说明（规范 §0）：清空回收站是不可恢复的删除，这正是本插件的唯一
用途；除回收站外不读写任何其它文件。私有状态只写 data\\last_clean.json。

钩子契约（规范 §5）：
  - 面板进程也会 import 本模块并调用 on_load，所以监听线程只在 on_tick
    （面板绝不调用）里惰性拉起，冷启动与热重载都覆盖；
  - 建窗口 / 清回收站 / 等待都在后台线程或窗口线程里做，守护钩子不阻塞；
  - on_exit 里 PostMessage + join(1.0)，总耗时控制在 2 秒以内。
"""
import ctypes
import os
import threading
import time
from ctypes import wintypes

from plugin_api import WBOXPlugin

STATE_NAME = 'last_clean'

WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_APP = 0x8000
WM_APP_RELEASE = WM_APP + 1     # 清理完成 / 超时 / 取消 → 隐藏窗口并放行
ENDSESSION_LOGOFF = 0x80000000

SHERB_NOCONFIRMATION = 0x00000001
SHERB_NOPROGRESSUI = 0x00000002
SHERB_NOSOUND = 0x00000004

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
SS_CENTER = 0x00000001
COLOR_WINDOW = 5
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

# 阶段一同步等待预算：必须低于系统对 QUERYENDSESSION 的挂起判定（默认 5 秒）
QUERY_WAIT_SEC = 4.0
# 阶段二可见等待窗口尺寸
BLOCK_W, BLOCK_H = 420, 150

# 关机顺序：数值大的先被结束。普通应用默认 0x280，取 0x100 排在它们之后。
_SHUTDOWN_LEVEL = 0x100

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ('style', wintypes.UINT),
        ('lpfnWndProc', WNDPROC),
        ('cbClsExtra', ctypes.c_int),
        ('cbWndExtra', ctypes.c_int),
        ('hInstance', wintypes.HINSTANCE),
        ('hIcon', wintypes.HICON),
        ('hCursor', wintypes.HANDLE),   # wintypes 没有 HCURSOR，句柄同型
        ('hbrBackground', wintypes.HBRUSH),
        ('lpszMenuName', wintypes.LPCWSTR),
        ('lpszClassName', wintypes.LPCWSTR),
    ]


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('i64Size', ctypes.c_longlong),
        ('i64NumItems', ctypes.c_longlong),
    ]


def query_recycle_bin():
    """返回 (条目数, 字节数)；查询失败返回 (None, None)。只读，无副作用。"""
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    hr = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
    if hr != 0:
        return None, None
    return int(info.i64NumItems), int(info.i64Size)


def empty_recycle_bin():
    """清空当前用户所有盘的回收站，无确认框 / 进度条 / 声音。返回 HRESULT。"""
    fn = ctypes.windll.shell32.SHEmptyRecycleBinW
    fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
    fn.restype = ctypes.c_long
    return int(fn(None, None,
                  SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND))


class Plugin(WBOXPlugin):
    DEFAULT_CONFIG = {
        'shutdown_bin_enabled': True,
        'shutdown_bin': {
            'max_wait_sec': 20,
            'clean_on_logoff': False,
        },
    }
    API = 1

    def __init__(self, ctx):
        WBOXPlugin.__init__(self, ctx)   # 必须：否则 self.ctx 不存在
        self.enabled = True
        self.max_wait_sec = 20
        self.clean_on_logoff = False
        self.last_clean = None
        self._watcher = None        # 监听线程（ctx.spawn 返回的 Thread）
        self._hwnd = None           # 隐藏窗口句柄，监听线程自己维护
        self._wndproc_ref = None    # 持有回调引用，防止被 GC
        self._retry_at = 0.0        # 监听线程失败后的重试节流
        self._clean_evt = None      # 本轮关机的清理完成事件
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 配置
    def on_config(self, cfg):
        was_enabled = self.enabled
        self.enabled = bool(cfg.get('shutdown_bin_enabled', True))
        seg = cfg.get('shutdown_bin')
        if not isinstance(seg, dict):
            seg = {}
        try:
            wait = int(seg.get('max_wait_sec', 20))
        except (TypeError, ValueError):
            wait = 20
        self.max_wait_sec = max(5, min(60, wait))
        self.clean_on_logoff = bool(seg.get('clean_on_logoff', False))
        if self.enabled and not was_enabled:
            self._retry_at = 0.0    # 重新启用后下一轮 tick 立即拉起监听
        self.ctx.set_status(self._status_text())

    # ---------------------------------------------------------------- 生命周期
    def on_load(self):
        # 面板进程也会走到这里：只读状态、写摘要，不产生任何系统副作用。
        rec = self.ctx.read_state(STATE_NAME, None)
        if isinstance(rec, dict):
            self.last_clean = rec
        self.ctx.set_status(self._status_text())

    def on_tick(self, tick):
        # 监听线程只在守护里拉起（面板绝不调用 on_tick）；
        # 热重载不重发 on_start，所以这里惰性拉起才能覆盖冷启动 + 热重载。
        if self.enabled:
            t = self._watcher
            if t is None or not t.is_alive():
                now = time.time()
                if now >= self._retry_at:
                    self._retry_at = now + 30.0   # 建窗失败时 30 秒后再试
                    self._watcher = self.ctx.spawn(
                        self._watcher_main, name='bin-shutdown-watch')
        else:
            t = self._watcher
            if t is not None and t.is_alive():
                self._stop_watcher(join=False)

    def on_exit(self, reason):
        # 禁止阻塞超过 2 秒：PostMessage 不等待，join 最多 1 秒。
        self._stop_watcher(join=True)
        self.ctx.log('插件退出（%s）' % reason)

    def on_self_test(self, out):
        t = self._watcher
        alive = bool(t is not None and t.is_alive())
        out.append('shutdown_bin     : enabled=%s watcher_alive=%s hwnd=%s last=%r'
                   % (self.enabled, alive, self._hwnd, self.last_clean))

    # ---------------------------------------------------------------- 监听线程
    def _watcher_main(self):
        """后台线程：注册窗口类、建隐藏顶层窗口、跑消息循环。

        注意不能用 HWND_MESSAGE 消息窗口——它收不到 WM_ENDSESSION 这类
        系统广播；必须是（平时不显示的）顶层窗口。
        """
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM]
        u.DefWindowProcW.restype = LRESULT
        u.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        u.CreateWindowExW.restype = wintypes.HWND
        u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                  wintypes.UINT, wintypes.UINT]
        u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.DispatchMessageW.restype = LRESULT
        u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        u.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.UINT]
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetSystemMetrics.restype = ctypes.c_int
        u.ShutdownBlockReasonCreate.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        u.ShutdownBlockReasonCreate.restype = wintypes.BOOL
        u.ShutdownBlockReasonDestroy.argtypes = [wintypes.HWND]
        u.ShutdownBlockReasonDestroy.restype = wintypes.BOOL
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k.GetModuleHandleW.restype = wintypes.HINSTANCE
        k.SetProcessShutdownParameters.argtypes = [wintypes.DWORD, wintypes.DWORD]

        hinst = k.GetModuleHandleW(None)
        # 类名带实例标识：热重载在同一进程里重建实例时不与旧类冲突
        cls = 'WBOX_shutdown_bin_%d_%d' % (os.getpid(), id(self))
        self._wndproc_ref = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.hbrBackground = wintypes.HBRUSH(COLOR_WINDOW + 1)
        wc.lpszClassName = cls
        if not u.RegisterClassW(ctypes.byref(wc)):
            raise RuntimeError('RegisterClassW 失败（err=%d）'
                               % ctypes.GetLastError())
        k.SetProcessShutdownParameters(_SHUTDOWN_LEVEL, 0)
        hwnd = u.CreateWindowExW(0, cls, 'WBOX 正在清空回收站', 0,
                                 0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            u.UnregisterClassW(cls, hinst)
            raise RuntimeError('CreateWindowExW 失败（err=%d）'
                               % ctypes.GetLastError())
        # 等待窗口里的说明文字（子窗口随父窗口一起显示 / 隐藏）
        u.CreateWindowExW(0, 'STATIC', '正在清空回收站，完成后自动继续关机…',
                          WS_CHILD | WS_VISIBLE | SS_CENTER,
                          12, 24, BLOCK_W - 24, 60, hwnd, None, hinst, None)
        self._hwnd = hwnd
        self.ctx.log('关机监听已就绪（hwnd=%s，注销%s清理）'
                     % (hwnd, '也' if self.clean_on_logoff else '不'))
        msg = wintypes.MSG()
        try:
            while True:
                r = u.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r <= 0:      # 0 = WM_QUIT，-1 = 出错
                    break
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._hwnd = None
            u.UnregisterClassW(cls, hinst)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        u = ctypes.windll.user32
        try:
            if msg == WM_QUERYENDSESSION:
                # 阶段一：立即起清理，同步等至多 QUERY_WAIT_SEC 秒。
                # 小回收站在这里就清完，系统拿到 TRUE 时已经干净了。
                logoff = bool(lparam & ENDSESSION_LOGOFF)
                if self.enabled and (self.clean_on_logoff or not logoff):
                    evt = self._start_clean(logoff)
                    if evt is not None:
                        evt.wait(QUERY_WAIT_SEC)
                return 1        # 同意关机；等待预算低于系统挂起判定
            if msg == WM_ENDSESSION:
                if wparam:
                    logoff = bool(lparam & ENDSESSION_LOGOFF)
                    if self.enabled and (self.clean_on_logoff or not logoff):
                        evt = self._start_clean(logoff)   # 兜底：阶段一没起过
                        if evt is not None and not evt.is_set():
                            # 阶段二：还没清完 → 可见窗口 + 阻止理由，系统会等
                            self._begin_block(hwnd, evt)
                    else:
                        self.ctx.log('收到结束会话通知（logoff=%s），按设置跳过清理'
                                     % logoff)
                else:
                    # 关机被取消：立即放行；本轮记录作废，下次关机重新清
                    # （用户可能取消后又删了东西再关）。
                    self._release_block(hwnd)
                    with self._lock:
                        self._clean_evt = None
                    self.ctx.log('系统关机被取消，已放行')
                return 0
            if msg == WM_APP_RELEASE:
                self._release_block(hwnd)
                return 0
            if msg == WM_CLOSE:
                u.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                u.PostQuitMessage(0)
                return 0
        except BaseException as e:      # 回调里绝不把异常抛回系统
            try:
                self.ctx.log('处理关机消息异常：%r' % (e,))
            except BaseException:
                pass
            if msg == WM_QUERYENDSESSION:
                return 1
            if msg in (WM_ENDSESSION, WM_APP_RELEASE, WM_CLOSE, WM_DESTROY):
                return 0
        return u.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---------------------------------------------------------------- 清理
    def _start_clean(self, logoff):
        """起一轮清理（同一轮关机只清一次）。返回完成事件；已清完返回 None。

        v1.1.1：以前只挡「还在跑」的情况，阶段一 0.2 秒清完后阶段二会再起一轮，
        每次关机会多闪一次可见等待窗口、多写一条 items=0 的记录，把「清理前
        N 项」的实证覆盖掉。现在本轮已清完就直接返回 None。
        """
        with self._lock:
            if self._clean_evt is not None:
                if self._clean_evt.is_set():
                    return None     # 本轮已清完，阶段二不必再清
                return self._clean_evt
            evt = threading.Event()
            self._clean_evt = evt
        self.ctx.log('收到%s通知，开始清空回收站'
                     % ('注销' if logoff else '关机/重启'))
        self.ctx.spawn(self._clean_worker, evt, logoff, self._hwnd,
                       name='bin-empty')
        return evt

    def _begin_block(self, hwnd, evt):
        """阶段二：可见等待窗口 + 阻止理由 + 超时看门狗。仅在窗口线程调用。"""
        u = ctypes.windll.user32
        u.ShutdownBlockReasonCreate(hwnd, 'WBOX 正在清空回收站，马上就好…')
        cx = u.GetSystemMetrics(0)
        cy = u.GetSystemMetrics(1)
        x = max(0, (cx - BLOCK_W) // 2)
        y = max(0, (cy - BLOCK_H) // 2)
        u.SetWindowPos(hwnd, HWND_TOPMOST, x, y, BLOCK_W, BLOCK_H,
                       SWP_NOACTIVATE | SWP_SHOWWINDOW)
        u.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        self.ctx.log('清理仍在进行，显示等待窗口并挂起关机（最长 %d 秒）'
                     % self.max_wait_sec)
        self.ctx.spawn(self._release_worker, hwnd, evt, name='bin-release')

    def _clean_worker(self, evt, logoff, hwnd):
        t0 = time.time()
        n, size = query_recycle_bin()
        hr = empty_recycle_bin()
        dur_ms = int((time.time() - t0) * 1000)
        ok = (hr == 0)
        # 回收站本来就是空的时候 SHEmptyRecycleBinW 返回 E_UNEXPECTED
        # （0x8000FFFF）。2026-11-22 实测：空 → 0x8000FFFF；放 1 项进去 → 0；
        # 清空后再调 → 又回到 0x8000FFFF。这不是失败，否则每次关机都会记一条
        # 假失败、还发一条 CLEAN_FAIL 事件。
        empty_bin_hr = (hr & 0xFFFFFFFF) in (0x8000FFFF, 1)
        already_empty = (n == 0 and empty_bin_hr)
        if already_empty:
            ok = True
        rec = {
            'at': round(time.time(), 3),
            'ok': ok,
            'hr': hr & 0xFFFFFFFF,
            'items': n,
            'bytes': size,
            'dur_ms': dur_ms,
            'trigger': 'logoff' if logoff else 'shutdown',
        }
        if already_empty:
            rec['already_empty'] = True
        self.last_clean = rec
        try:
            self.ctx.write_state(STATE_NAME, rec)   # 动作完成立即落盘
        except OSError as e:
            self.ctx.log('写私有状态失败：%r' % (e,))
        if n is None:
            before = '清理前统计不可用'
        else:
            before = '清理前 %d 项 / %.1f MB' % (n, (size or 0) / 1048576.0)
        if already_empty:
            self.ctx.log('回收站本来就是空的，无需清理（%s，hr=0x%08X）'
                         % (before, rec['hr']))
        else:
            self.ctx.log('回收站清理%s：%s，耗时 %.1f 秒（hr=0x%08X）'
                         % ('成功' if ok else '失败', before, dur_ms / 1000.0,
                            rec['hr']))
        self.ctx.set_status(self._status_text())
        self._emit('CLEAN_OK' if ok else 'CLEAN_FAIL',
                   '%s items=%s bytes=%s' % (rec['trigger'], n, size), dur_ms)
        evt.set()
        if hwnd:
            try:
                ctypes.windll.user32.PostMessageW(hwnd, WM_APP_RELEASE, 0, 0)
            except BaseException:
                pass

    def _release_worker(self, hwnd, evt):
        evt.wait(self.max_wait_sec)
        if not evt.is_set():
            self.ctx.log('清理超过 %d 秒未完成，先放行关机（清理线程继续收尾）'
                         % self.max_wait_sec)
            self._emit('CLEAN_TIMEOUT', '超过最长等待，放行关机')
        try:
            ctypes.windll.user32.PostMessageW(hwnd, WM_APP_RELEASE, 0, 0)
        except BaseException:
            pass

    def _release_block(self, hwnd):
        """隐藏等待窗口并撤销阻止理由。仅在窗口线程调用。"""
        u = ctypes.windll.user32
        try:
            u.ShowWindow(hwnd, SW_HIDE)
        except BaseException:
            pass
        try:
            u.ShutdownBlockReasonDestroy(hwnd)
        except BaseException:
            pass

    # ---------------------------------------------------------------- 工具
    def _stop_watcher(self, join):
        hwnd = self._hwnd
        if hwnd:
            try:
                u = ctypes.windll.user32
                u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                           wintypes.WPARAM, wintypes.LPARAM]
                u.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except BaseException:
                pass
        t = self._watcher
        if join and t is not None and t.is_alive():
            t.join(1.0)

    def _status_text(self):
        if not self.enabled:
            return '已关闭'
        rec = self.last_clean
        if isinstance(rec, dict) and rec.get('at'):
            lt = time.localtime(rec['at'])
            items = rec.get('items')
            return '待命 · 上次 %02d-%02d %02d:%02d 清 %s 项' % (
                lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min,
                items if items is not None else '?')
        return '待命 · 关机前自动清理'

    def _emit(self, code, msg, dur_ms=0):
        try:
            ev = self.ctx.import_core('events')
            ev.emit('shutdown_bin', code=code, plugin=self.ctx.id,
                    dur_ms=dur_ms, msg=msg)
        except BaseException:
            pass        # 旧内核没有 events 模块时静默降级