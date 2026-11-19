# -*- coding: utf-8 -*-
"""插件：桌面监控（Desktop HUD）。

在桌面右上角（可拖拽 / 四角切换）显示一块半透明小面板：
CPU / GPU / 显存 / 内存 占用，可选 CPU / GPU 频率；每一项都有独立显示开关
（右键菜单勾选），游戏会话中自动隐藏 / 停止监控（与 RTSS 游戏内 OSD 互补）。

实现分工：
  hud_metrics.py  纯 ctypes 采样（GetSystemTimes / PDH / 注册表 / ADL）。
  hud_overlay.py  纯 ctypes 分层窗口 + 独立线程消息循环 + 右键菜单。
  plugin.py(本文件) 只做「胶水」：配置合并、生命周期、持久化、会话隐藏。

持久化模型（守护进程不会把 set_cfg 落盘，故运行期改动走私有状态文件）：
  config.json  desktop_hud_enabled  总开关（面板拨动，面板负责 save）。
  config.json  desktop_hud.{...}    基础偏好（用户可手改，深合并默认值）。
  data/hud.json {'prefs','hidden'}  右键菜单 / 拖拽产生的运行期覆盖，优先级最高；
                                    「恢复默认」清空 prefs 回到 config 基础值。

线程约定（v1.6.0）：悬浮窗跑在自己的线程；采样再拆出第三条线程（PDH/ADL
可能卡在原生调用里，不能压在建窗/重绘路径上）；菜单 / 拖拽变更通过线程安全
队列回传，本插件只在 on_tick（守护线程）里取队列并 write_state，绝不跨线程
写配置。
"""
import os
import sys
import threading

from plugin_api import WingmanPlugin, deep_merge

# 宿主热重载只重新 exec 本文件（entry），`from . import hud_overlay` 会命中
# sys.modules 里上一次加载缓存的旧兄弟模块（plugin_host._purge_module 只清
# entry）。这里主动摘掉自己的兄弟模块缓存，保证热重载拿到磁盘上的新代码。
# v1.5.0：只摘**本包内**的名字。原来无条件 pop 顶层 'hud_overlay'/'hud_metrics'，
# 将来别的插件用同名模块会被本插件踢掉缓存（跨插件污染）。
# 离线独立导入（无 __package__）时才回退到顶层名。
for _n in ('hud_overlay', 'hud_metrics'):
    if __package__:
        sys.modules.pop(__package__ + '.' + _n, None)
    else:
        sys.modules.pop(_n, None)

try:                       # 作为包模块被宿主加载
    from . import hud_overlay
except ImportError:        # 离线 / 独立导入
    import hud_overlay


class Plugin(WingmanPlugin):
    """桌面硬件占用悬浮窗。__init__ 无副作用（面板进程也会实例化）。"""

    DEFAULT_CONFIG = {
        'desktop_hud_enabled': True,
        'desktop_hud': dict(hud_overlay.DEFAULT_PREFS),
    }

    def __init__(self, ctx):
        WingmanPlugin.__init__(self, ctx)
        self.enabled = True
        self.prefs_base = dict(hud_overlay.DEFAULT_PREFS)
        self._state = {'prefs': {}, 'hidden': False}
        # 冻结运行时不含 queue 模块（实测 PYZ 缺），用 threading+list 代替
        self._req_q = []
        self._req_lock = threading.Lock()
        self._overlay = None
        self._last_visible = None
        self._last_paused = None

    # ---------------------------------------------------------------- 偏好
    def _effective(self):
        # 覆盖是「键级」而非整表替换：旧版本留下的 prefs 快照缺新键
        # （如 desktop_only）时回退基础值，否则新偏好对老用户永远不生效。
        merged = dict(self.prefs_base)
        merged.update(self._state.get('prefs') or {})
        return merged

    def _on_request(self, req):
        # 悬浮窗线程调用：只入队，不做任何 IO / 配置写入
        with self._req_lock:
            self._req_q.append(req)

    def _drain_requests(self):
        with self._req_lock:
            batch = self._req_q[:64]
            del self._req_q[:64]
        for req in batch:
            action = req.get('action')
            if action == 'prefs':
                self._state['prefs'] = dict(req.get('prefs') or {})
                self._save_state()
            elif action == 'close':
                self._state['hidden'] = True
                self._save_state()
                if self._overlay is not None:
                    self._overlay.set_visible(False)
                self._last_visible = False
            elif action == 'open_config':
                self._open_config()
            elif action == 'log':
                self.ctx.log(req.get('msg', ''))

    def _save_state(self):
        try:
            self.ctx.write_state('hud', self._state)
        except Exception as e:
            self.ctx.log('写悬浮窗状态失败: %r' % (e,))

    def _open_config(self):
        path = os.path.join(self.ctx.base, 'config.json')
        try:
            os.startfile(path)
        except Exception as e:
            self.ctx.log('打开配置文件失败: %r' % (e,))

    # ---------------------------------------------------------------- 配置
    @staticmethod
    def _migrate_prefs(d):
        """v1.6.0：refresh_sec（秒，浮点）→ refresh_ms（毫秒，整数）。

        面板只能存整数步进，旧键留着会让「设置里改了不生效」；这里只在旧键
        存在且没有新键时换算，用户显式配置的新键优先。
        """
        if isinstance(d, dict) and 'refresh_sec' in d:
            sec = d.pop('refresh_sec')
            if 'refresh_ms' not in d:
                try:
                    d['refresh_ms'] = int(float(sec) * 1000)
                except (TypeError, ValueError):
                    pass
        return d

    def on_config(self, cfg):
        self.enabled = bool(cfg.get('desktop_hud_enabled', True))
        seg = cfg.get('desktop_hud')
        base = dict(hud_overlay.DEFAULT_PREFS)
        if isinstance(seg, dict):
            # 迁移必须在合并默认值**之前**做：合并后 refresh_ms 已被默认 1000
            # 填上，旧键 refresh_sec 就再也搬不进来了。
            base = deep_merge(base, self._migrate_prefs(dict(seg)))
        self.prefs_base = base
        self._sync_overlay()

    def on_load(self):
        st = self.ctx.read_state('hud', None)
        if isinstance(st, dict):
            prefs = dict(st.get('prefs') or {})
            self._migrate_prefs(prefs)
            self._state = {'prefs': prefs,
                           'hidden': bool(st.get('hidden'))}

    # ---------------------------------------------------------------- 生命周期
    def on_start(self):
        self._sync_overlay()

    def _sync_overlay(self):
        """按总开关 / 用户隐藏决定悬浮窗存在与否，并推送当前偏好。"""
        want = self.enabled and not self._state.get('hidden')
        if want and self._overlay is None:
            self._overlay = hud_overlay.HudOverlay(request_cb=self._on_request)
            ok = self._overlay.start(prefs=self._effective(), visible=True,
                                     timeout=2.0)
            if not ok:
                self.ctx.log('悬浮窗创建失败（桌面会话不可用？），本插件暂不显示')
                self._overlay = None
                return
            self._last_visible = True
            self.ctx.log('桌面监控悬浮窗已启动')
        elif not want and self._overlay is not None:
            self._overlay.stop(timeout=1.5)
            self._overlay = None
            self._last_visible = None
            self._last_paused = None
        if self._overlay is not None:
            self._overlay.set_prefs(self._effective())

    def _sync_visible(self, session_state):
        if self._overlay is None:
            return
        prefs = self._effective()
        in_game = session_state != 'idle'
        # pause_in_game：停采样+隐藏（游戏里有别的监控软件，桌面监控重复）；
        # hide_in_game：只隐藏、采样继续。两者独立，pause 优先。
        paused = bool(prefs.get('pause_in_game')) and in_game
        want = not paused and not (prefs.get('hide_in_game') and in_game)
        prev = self._last_paused
        if paused != prev:
            self._overlay.set_paused(paused)
            self._last_paused = paused
            # 只在采样状态真的翻转时记一行。启动瞬间 paused=False 且 prev=None，
            # 原来也会记「恢复采样（游戏会话）」，日志里看着像刚退出游戏。
            if paused:
                self.ctx.log('桌面监控暂停采样（游戏会话）')
            elif prev:
                self.ctx.log('桌面监控恢复采样（游戏会话结束）')
        if want != self._last_visible:
            self._overlay.set_visible(want)
            self._last_visible = want

    def on_tick(self, tick):
        self._drain_requests()
        self._sync_overlay()
        self._sync_visible(tick.session.state)

    def on_exit(self, reason):
        # 菜单/拖拽的改动入队后只在 on_tick 落盘，空闲节拍可达 30s；
        # 退出前必须排空队列，否则刚改的偏好会丢。
        self._drain_requests()
        if self._overlay is not None:
            self._overlay.stop(timeout=1.5)
            self._overlay = None
        self._last_visible = None
        self._last_paused = None

    # ---------------------------------------------------------------- 自检
    def on_self_test(self, out):
        out.append('desktop_hud enabled : %s' % self.enabled)
        out.append('desktop_hud running : %s'
                   % bool(self._overlay and self._overlay.is_running()))
        p = self._effective()
        show = [k for k in ('show_cpu', 'show_gpu', 'show_vram', 'show_ram',
                            'show_cpu_freq', 'show_gpu_freq') if p.get(k)]
        out.append('desktop_hud show    : %s' % (','.join(show) or 'none'))
        out.append('desktop_hud pos     : %s opacity=%s refresh=%sms'
                   % (p.get('position'), p.get('opacity'),
                      hud_overlay._interval_ms(p)))
        out.append('desktop_hud color   : warn>=%s high>=%s decimals=%s'
                   % (p.get('warn_percent'), p.get('high_percent'),
                      p.get('pct_decimals')))
        out.append('desktop_hud gpu_sel : %s luid=%s'
                   % (p.get('gpu_select'), p.get('gpu_luid') or '-'))
        out.append('desktop_hud desktop_only=%s hide_in_game=%s pause_in_game=%s'
                   % (p.get('desktop_only'), p.get('hide_in_game'),
                      p.get('pause_in_game')))
        out.append('desktop_hud hidden  : %s (运行期关闭)' % self._state.get('hidden'))
        try:
            st = self._overlay.status() if self._overlay is not None else None
            if st is not None:
                out.append('desktop_hud worker  : running=%s paused=%s '
                           'interval=%sms sample=%.1fms slow=%s'
                           % (st['running'], st['paused'], st['interval_ms'],
                              st['sample_ms'], st['slow_events']))
                for ln in st.get('backend') or ():
                    out.append('desktop_hud ' + ln)
            # --self-test 是独立进程：它自己 reload_config 才把悬浮窗建出来，
            # 采样线程此刻还没跑完第一拍，直接读 status 只会全是空值/0.0ms，
            # 看起来像"线程活着但不采样"。这里补一次同口径的即时采样。
            if st is None or st['sample_ms'] <= 0.0:
                out.append('desktop_hud note    : 悬浮窗刚建/未运行，'
                           '改由一次性采样器读取（下面数值为真实读数）')
                s = hud_overlay.hud_metrics.Sampler(
                    enable_gpu_clock=bool(p.get('show_gpu_freq')))
                s.sample()
                for ln in s.diagnostics():
                    out.append('desktop_hud ' + ln)
                s.close()
        except Exception as e:
            out.append('desktop_hud sampler 自检失败: %r' % (e,))