# -*- coding: utf-8 -*-
"""WBOX 插件：本地 AI 按需启停（local_ai）v1.0.4。

请求方（例如 D:\\.workbuddy\\scripts\\local_ai_prep.py）写请求信号文件：
    <signal_path>  ->  {"action":"start","client":"...","pid":123,"ts":...}

插件行为：
    信号出现 -> 拉起 llama-server -> 轮询 /health 就绪 -> 写 ready.json
    信号消失 + /slots 全部 is_processing=false 持续 idle_timeout_sec -> 关闭

安全边界（不可改）：
    1. 只终止本插件 Popen 出来的 PID；用户手动启动的服务 state=external，永不关闭。
    2. on_tick 只做轻量 stat / 内存判断；Popen、HTTP、文件写一律 ctx.spawn 到后台线程。
    3. 模块顶层无副作用（面板进程也会 import 本文件）。
    4. 不 import uuid / platform / wmi（PyInstaller 会顺着拖包）。
       也不用 urllib / http.client / socket：冻结运行时缺 selectors，三者
       导入即 ModuleNotFoundError。HTTP 探测改调系统自带 curl.exe。
"""
import json
import os
import subprocess
import threading
import time

from plugin_api import WingmanPlugin

PLUGIN_VERSION = '1.0.4'
DEFAULT_SIGNAL = 'D:/.workbuddy/scripts/local_ai_request.json'
READY_NAME = 'local_ai_ready.json'

STATE_IDLE = 'idle'
STATE_STARTING = 'starting'
STATE_READY = 'ready'
STATE_EXTERNAL = 'external'
STATE_STOPPING = 'stopping'
STATE_ERROR = 'error'

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

# external 状态连续多少次探测失败就判定"外部服务已消失"。
# 2 秒一次探测 → 连续 3 次约 6 秒，足够识别服务被关/崩溃，又不怕单次超时抖动。
EXTERNAL_LOST_STREAK = 3


def _norm(path):
    """把配置里的 D:/xxx 转成 Windows 原生路径；空值原样返回。"""
    if not path:
        return path
    try:
        return os.path.normpath(str(path))
    except Exception:
        return path


class Plugin(WingmanPlugin):
    """按需拉起 / 关闭本地 llama.cpp 服务，空闲判定以 /slots 为准。"""

    API = 1
    DEFAULT_CONFIG = {
        'local_ai_enabled': True,
        'local_ai': {
            'exe_path': 'D:/01-Tools/llama.cpp/bin/llama-server.exe',
            'model_path': 'D:/01-Tools/llama.cpp/models/qwen3.5-9b/Qwen3.5-9B-Q5_K_M.gguf',
            'mmproj_path': 'D:/01-Tools/llama.cpp/models/qwen3.5-9b/mmproj-F16.gguf',
            'port': 8080,
            'ctx_size': 8192,
            'n_gpu_layers': 999,
            'idle_timeout_sec': 120,
            'startup_timeout_sec': 120,
            'probe_interval_sec': 2.0,
            'signal_path': DEFAULT_SIGNAL,
        },
    }

    def __init__(self, ctx):
        WingmanPlugin.__init__(self, ctx)
        self._lock = threading.Lock()
        self._state = STATE_IDLE
        self._proc = None
        self._pid = 0
        self._busy = False
        self._healthy = False
        self._probe_at = 0.0
        self._probe_fail = 0
        self._idle_since = 0.0
        self._stamp = ''
        self._args = []
        self._base_url = 'http://127.0.0.1:8080'

    # ---------------------------------------------------------------- 配置
    def _cfg(self, key, default=None):
        return self.ctx.cfg('local_ai.' + key, default)

    def _enabled(self):
        return bool(self.ctx.cfg('local_ai_enabled', True))

    def on_config(self, cfg):
        # 轻量：只重建命令行，不碰文件 / 进程。
        port = int(self._cfg('port', 8080) or 8080)
        ctx_size = int(self._cfg('ctx_size', 8192) or 8192)
        ngl = int(self._cfg('n_gpu_layers', 999) or 999)
        exe = _norm(self._cfg('exe_path', ''))
        model = _norm(self._cfg('model_path', ''))
        mmproj = _norm(self._cfg('mmproj_path', ''))
        self._base_url = 'http://127.0.0.1:%d' % port
        self._args = [
            exe, '-m', model,
            '--mmproj', mmproj,
            '--reasoning', 'off',
            '--alias', 'qwen3.5-9b,qwen35-9b-heretic',
            '-ngl', str(ngl), '-c', str(ctx_size), '-np', '1', '--jinja',
            '--host', '127.0.0.1', '--port', str(port),
            '--temp', '0.7', '--top-p', '0.8', '--top-k', '20',
            '--min-p', '0.0', '--presence-penalty', '1.5',
        ]
        self._log_summary()

    def _log_summary(self):
        stamp = '%s|%s|%s|%s' % (
            self._cfg('exe_path', ''), self._cfg('model_path', ''),
            self._cfg('port', 8080), self._cfg('idle_timeout_sec', 120))
        if stamp == self._stamp:
            return
        self._stamp = stamp
        missing = [k for k, v in (
            ('exe_path', self._cfg('exe_path', '')),
            ('model_path', self._cfg('model_path', '')),
            ('mmproj_path', self._cfg('mmproj_path', '')),
        ) if not v or not os.path.isfile(_norm(v))]
        self.ctx.log('本地 AI 按需启停 v%s：%s%s；端口 %s，空闲 %ss 后关闭；信号 %s' % (
            PLUGIN_VERSION,
            '开启' if self._enabled() else '关闭',
            '（缺文件：%s）' % ','.join(missing) if missing else '',
            self._cfg('port', 8080), self._cfg('idle_timeout_sec', 120),
            self._cfg('signal_path', DEFAULT_SIGNAL)))

    # ---------------------------------------------------------------- 信号文件
    def _signal_path(self):
        return _norm(self._cfg('signal_path', DEFAULT_SIGNAL))

    def _ready_path(self):
        return os.path.join(os.path.dirname(self._signal_path()), READY_NAME)

    def _read_request(self):
        """有请求返回 dict，无请求返回 None。坏文件当无请求（不拉起服务）。"""
        path = self._signal_path()
        try:
            if not os.path.isfile(path):
                return None
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                obj = json.load(f)
            if not isinstance(obj, dict) or obj.get('action') != 'start':
                return None
            return obj
        except (OSError, ValueError):
            return None

    def _write_ready(self, status, **extra):
        """原子写 ready.json；请求方据此判断服务是否可用。"""
        path = self._ready_path()
        try:
            d = os.path.dirname(path)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            obj = {'status': status, 'at': time.time(), 'plugin': 'local_ai'}
            obj.update(extra)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                json.dump(obj, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as exc:
            self.ctx.log('写本地 AI 就绪文件失败：%r' % (exc,))

    # ---------------------------------------------------------------- HTTP（仅后台线程调用）
    def _curl(self):
        """系统自带 curl.exe（Win10 1803+）；冻结 exe 里没有 HTTP Python 模块。"""
        windir = os.environ.get('WINDIR', 'C:\\Windows')
        path = os.path.join(windir, 'System32', 'curl.exe')
        return path if os.path.isfile(path) else 'curl.exe'

    def _http_json(self, path, timeout):
        """用 curl 取本地 HTTP JSON，避免冻结运行时缺 selectors。

        v1.0.3：必须带 CREATE_NO_WINDOW。WBOX.exe 是窗口程序（没有控制台），
        curl.exe 是控制台程序；不指定该标志时 Windows 会给子进程新分配一个
        控制台窗口，桌面上就会每 2 秒闪一个标题为 curl.exe 路径的黑框。
        """
        port = int(self._cfg('port', 8080) or 8080)
        url = 'http://127.0.0.1:%d%s' % (port, path)
        out = subprocess.check_output(
            [self._curl(), '-s', '--noproxy', '*', '--max-time',
             str(max(1, int(timeout))), url],
            stderr=subprocess.DEVNULL, timeout=timeout + 2,
            creationflags=CREATE_NO_WINDOW)
        return json.loads(out.decode('utf-8', 'replace'))

    def _probe(self):
        """取 /slots 判断是否有任务在处理；失败一律当不健康、不繁忙。"""
        try:
            slots = self._http_json('/slots', 2.0)
            busy = any(bool(s.get('is_processing')) for s in slots) if isinstance(slots, list) else False
            self._busy = busy
            self._healthy = True
            self._probe_fail = 0
        except Exception:
            self._healthy = False
            self._busy = False
            self._probe_fail += 1

    def _health_ok(self):
        try:
            return self._http_json('/health', 2.0).get('status') == 'ok'
        except Exception:
            return False

    # ---------------------------------------------------------------- 状态回收（仅后台线程调用）
    def _on_external_lost(self):
        """外部服务没了：退回 idle，把"下次请求自己拉起"的能力还回来。

        v1.0.4 前 external 是个死胡同：谁也不会把它改回 idle，一旦探到过外部
        服务，后面真请求就再也不触发拉起了，只能重启守护。这里只清状态、写
        ready，不碰任何进程（外部服务不是我们拉起的）。
        """
        with self._lock:
            if self._state != STATE_EXTERNAL:
                return
            self._state = STATE_IDLE
            self._busy = False
            self._probe_fail = 0
            self._idle_since = 0.0
        self._write_ready('stopped', external=True)
        self.ctx.log('本地 AI 外部服务已消失，回到待命（下次请求会自己拉起）')

    def _do_reap(self):
        """我们自己拉起的服务自己退了：清状态 + 写 stopped，不杀任何 PID。"""
        with self._lock:
            if self._state != STATE_READY or self._proc is None:
                return
            proc = self._proc
            self._proc = None
            self._pid = 0
            self._state = STATE_IDLE
            self._busy = False
            self._probe_fail = 0
            self._idle_since = 0.0
        code = proc.poll()
        self._write_ready('stopped', exit_code=code)
        self.ctx.log('本地 AI 服务已自行退出（exit=%s），回到待命' % (code,))

    # ---------------------------------------------------------------- 进程管理（仅后台线程调用）
    @staticmethod
    def _terminate(proc, quick=False):
        try:
            proc.terminate()
            if quick:
                return
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
                proc.wait(timeout=3)
        except Exception:
            pass

    def _do_start(self):
        """后台启动；先探测是否已有外部服务，有则不接管、不关闭。"""
        with self._lock:
            if self._state != STATE_STARTING:
                return
        if self._health_ok():
            self._write_ready('ready', pid=0, external=True)
            with self._lock:
                self._state = STATE_EXTERNAL
            self.ctx.log('本地 AI 服务已由外部启动，插件只等待请求、不接管关闭')
            return

        self._write_ready('starting')
        started = time.time()
        try:
            proc = subprocess.Popen(
                self._args,
                creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
                close_fds=True,
            )
        except Exception as exc:
            self.ctx.log('拉起本地 AI 失败：%r' % (exc,))
            self._write_ready('error', error=str(exc))
            with self._lock:
                self._state = STATE_ERROR
            return

        with self._lock:
            self._proc = proc
            self._pid = proc.pid

        deadline = time.time() + float(self._cfg('startup_timeout_sec', 120) or 120)
        ok = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if self._health_ok():
                ok = True
                break
            time.sleep(1.0)

        if ok:
            with self._lock:
                self._state = STATE_READY
            self._write_ready('ready', pid=proc.pid, port=int(self._cfg('port', 8080) or 8080))
            self.ctx.log('本地 AI 已就绪：pid=%d，冷启动 %.1fs（上下文 %s）' % (
                proc.pid, time.time() - started, self._cfg('ctx_size', 8192)))
        else:
            self._terminate(proc)
            with self._lock:
                self._state = STATE_ERROR
                self._proc = None
                self._pid = 0
            self._write_ready('error', error='startup timeout or process exited')
            self.ctx.log('本地 AI 启动超时/退出，已终止本次拉起的进程')

    def _do_stop(self, quick=False):
        """只关闭自己拉起的进程；external 状态直接回到 idle。"""
        with self._lock:
            if self._state in (STATE_IDLE, STATE_STOPPING):
                return
            state = self._state
            proc = self._proc
            self._state = STATE_STOPPING
            self._proc = None
            self._pid = 0
        if state == STATE_EXTERNAL or proc is None:
            self._write_ready('stopped', external=True)
        else:
            self._terminate(proc, quick=quick)
            self._write_ready('stopped')
            self.ctx.log('本地 AI 已关闭：pid=%d（空闲超时或插件退出）' % proc.pid)
        with self._lock:
            self._state = STATE_IDLE
            self._busy = False
            self._idle_since = 0.0

    # ---------------------------------------------------------------- 钩子
    def on_load(self):
        self._log_summary()

    def on_tick(self, tick):
        now = float(tick.now or time.time())
        enabled = self._enabled()
        if not enabled:
            if self._state not in (STATE_IDLE,):
                self.ctx.spawn(self._do_stop)
            self.ctx.set_status('本地 AI 已禁用')
            return

        req = self._read_request()
        want = req is not None

        with self._lock:
            state = self._state

        # 探测节流：2 秒一次，放在后台线程，主线程只读缓存。
        # v1.0.3：只在"可能正在服务"的状态探测 —— idle 时压根没有服务可问，
        # 每 2 秒拉一个 curl 纯属空转（43200 次/天），还会在桌面上闪黑窗。
        interval = float(self._cfg('probe_interval_sec', 2.0) or 2.0)
        if state in (STATE_READY, STATE_EXTERNAL) and now - self._probe_at >= interval:
            self._probe_at = now
            self.ctx.spawn(self._probe)

        # v1.0.4：两种"状态卡死"的回收。
        # ① external 探不到服务 → 退回 idle，否则真请求再也不会拉起（旧版 bug）。
        # ② 自己拉起的服务退了 → 立刻清状态写 stopped，别让 ready.json 悬着"ready"
        #    最久 120 秒，调用方会去连一个死端口。
        if state == STATE_EXTERNAL and self._probe_fail >= EXTERNAL_LOST_STREAK:
            self.ctx.spawn(self._on_external_lost)
        elif state == STATE_READY and self._proc is not None and self._proc.poll() is not None:
            self.ctx.spawn(self._do_reap)

        if want and state == STATE_IDLE:
            with self._lock:
                should_start = self._state == STATE_IDLE
                if should_start:
                    self._state = STATE_STARTING
            if should_start:
                self.ctx.spawn(self._do_start)

        if want:
            self._idle_since = 0.0
        elif state in (STATE_READY, STATE_EXTERNAL) and not self._busy:
            if not self._idle_since:
                self._idle_since = now
            elif state == STATE_READY and now - self._idle_since >= float(
                    self._cfg('idle_timeout_sec', 120) or 120):
                self.ctx.spawn(self._do_stop)

        if state == STATE_IDLE:
            text = '本地 AI 待命（未启动）'
        elif state == STATE_STARTING:
            text = '本地 AI 启动中…'
        elif state == STATE_EXTERNAL:
            text = '本地 AI 外部服务在用'
        elif state == STATE_STOPPING:
            text = '本地 AI 关闭中…'
        elif state == STATE_ERROR:
            text = '本地 AI 启动失败（见日志）'
        elif want or self._busy:
            text = '本地 AI 服务中'
        else:
            left = int(float(self._cfg('idle_timeout_sec', 120) or 120)
                       - (now - self._idle_since))
            text = '本地 AI 空闲 %ds 后关闭' % max(0, left)
        self.ctx.set_status(text)

    def on_exit(self, reason):
        # 契约：不得阻塞超过 2 秒 -> 只 terminate，不 wait。
        if self._state not in (STATE_IDLE,):
            self._do_stop(quick=True)

    def on_self_test(self, out):
        paths = [('exe', self._cfg('exe_path', '')),
                 ('model', self._cfg('model_path', '')),
                 ('mmproj', self._cfg('mmproj_path', ''))]
        detail = ','.join('%s=%s' % (k, 'ok' if v and os.path.isfile(_norm(v)) else 'missing')
                          for k, v in paths)
        out.append('local_ai v%s: %s; port=%s; signal=%s; state=%s' % (
            PLUGIN_VERSION, detail, self._cfg('port', 8080),
            self._cfg('signal_path', DEFAULT_SIGNAL), self._state))
