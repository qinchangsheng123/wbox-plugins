# -*- coding: utf-8 -*-
"""插件：弹窗关闭（腾讯系直播 / 赛事 / 推荐弹窗）。

从 v1 `guard_core.Guard` 里搬来的调用点，逐段对应（行号为 v1.3.1 的 guard_core.py）：
  PopupGuard 构造                     643
  会话中提前关窗口 watch_early()      1213-1218
  退出后 arm()                        1207-1208
  闪断恢复取消监视                    1165-1167
  开局 begin_session()                1176-1177
  每轮 tick()                         1222
  启动清理 sweep('启动清理')          1266-1272

低层实现仍在 `popup_guard.PopupGuard`（随 exe 冻结）；保护名单、窗口判定、
"游戏期间只发 WM_CLOSE 不动进程树"这些安全语义一行没改。
"""
from plugin_api import WingmanPlugin
import plugin_api


def merge_protect_names(user, defaults):
    """保护名单取并集（实现见 `plugin_api.merge_name_list`，内核合并时用的是同一个）。

    为什么还要在插件里再做一次：内核 `plugin_host.merge_defaults` 已经对
    `popup_guard.protect_names` 做过安全并集，但插件可能被喂一份"只含自己声明
    路径"的配置子集（plugin_cfg），也可能是老版本内核。保护名单是安全值，
    宁可多做一次并集（幂等），也不能让 qq.exe 漏出保护名单 —— 游戏退出后
    清宿主进程树时会把常驻 QQ/微信一起结束，用户直接掉线。

    只作用于传给 PopupGuard 的副本：不回写 config.json，不改入参。
    """
    return plugin_api.merge_name_list(user, defaults)


class Plugin(WingmanPlugin):
    """把弹窗宿主当"宿主进程树"清理，常驻主干（wegame/QQ/微信）绝不碰。"""

    DEFAULT_CONFIG = {}      # 默认值全部沿用 core_defaults.DEFAULTS['popup_guard']

    def __init__(self, ctx):
        WingmanPlugin.__init__(self, ctx)
        self.guard = None
        self.cfg = {}
        self.protect = []
        self._record_seq = 0

    # ---------------------------------------------------------------- 配置
    def on_config(self, cfg):
        self.cfg = cfg if isinstance(cfg, dict) else {}
        pg_cfg = self._popup_cfg(self.cfg)
        popup_guard = self.ctx.import_core('popup_guard')
        self.guard = popup_guard.PopupGuard(pg_cfg, self.ctx.log,
                                            game_alive=self.ctx.games_present)
        self._log_summary(pg_cfg)

    def _default_protect(self):
        """内核默认保护名单（随 exe 冻结，不来自可替换的 plugins\\ 目录）。"""
        try:
            core_defaults = self.ctx.import_core('core_defaults')
            return (core_defaults.DEFAULTS.get('popup_guard') or {}).get(
                'protect_names') or []
        except Exception as e:
            # 读不到默认值就别硬合并：宁可保留用户配置里的项，也不能编一份名单出来。
            self.ctx.log('读取内核默认保护名单失败，保护名单退化为配置原值: %r' % (e,))
            return []

    def _popup_cfg(self, cfg):
        """拷一份 popup_guard 段给 PopupGuard，并对 protect_names 单键做并集。

        **必须拷贝**：cfg 是内核持有的合并结果，就地改会影响面板/自检/后续插件的
        读数（并集只对"这一次构造 PopupGuard"生效）。
        """
        raw = cfg.get('popup_guard')
        pg_cfg = dict(raw) if isinstance(raw, dict) else {}
        merged = merge_protect_names(pg_cfg.get('protect_names'),
                                     self._default_protect())
        if merged != list(pg_cfg.get('protect_names') or []):
            self.ctx.log('保护名单已补齐：配置 %d 项 + 内核默认 -> 共 %d 项（%s）' %
                         (len(pg_cfg.get('protect_names') or []), len(merged),
                          ', '.join(merged)))
        pg_cfg['protect_names'] = merged
        self.protect = merged
        return pg_cfg

    def _poll_fast(self):
        """会话轮询节拍属于内核配置，插件只读（ctx.cfg 可以读全部键，写才受限）。"""
        return self.ctx.cfg('poll_fast_sec', 0.4)

    def _log_summary(self, pg_cfg):
        """功能开关摘要只在真的变化时打一行（v1 571-582 的 stamp 语义）。"""
        stamp = '%s|%s|%s|%s' % (
            pg_cfg.get('enabled'), pg_cfg.get('watch_sec'),
            ','.join(pg_cfg.get('process_names') or []),
            pg_cfg.get('early_close_enabled'))
        if stamp == getattr(self, '_stamp', None):
            return
        self._stamp = stamp
        self.ctx.log('弹窗拦截：%s%s（会话中窗口提前拦截 %s；退出即清宿主进程树，'
                     '之后 %d 秒内新窗口也拦；宿主 %s；会话轮询 %ss）' %
                     ('开启' if pg_cfg.get('enabled') else '关闭',
                      '（观察模式：只记录不关闭）'
                      if pg_cfg.get('observe_only') else '',
                      '开' if pg_cfg.get('early_close_enabled') else '关',
                      int(pg_cfg.get('watch_sec', 90)),
                      ','.join(pg_cfg.get('process_names') or []) or '未配置',
                      self._poll_fast()))

    def on_load(self):
        pass

    # ---------------------------------------------------------------- 生命周期
    def on_start(self):
        """守护启动先清一轮：上次守护没跑的时候游戏退出了，宿主会一直挂着。

        sweep 内部自己判游戏在不在，游戏在跑不会动手（v1 1266-1272）。
        """
        if self.guard is None:
            return
        try:
            self.guard.sweep('启动清理')
        except Exception as e:
            self.ctx.log('启动清理弹窗宿主失败: %r' % (e,))

    # ---------------------------------------------------------------- 会话
    def on_session_start(self, tick):
        """新会话：清本局提前拦截计数（v1 1176-1177）。"""
        if self.guard is None:
            return
        self.guard.begin_session()

    def on_session_resume(self, tick):
        """闪断恢复：取消监视，别把还在玩的人的窗口当弹窗（v1 1165-1167）。"""
        if self.guard is None:
            return
        if self.guard.armed:
            self.guard.armed = False
            self.ctx.log('游戏会话继续，取消弹窗监视')

    def on_session_grace(self, tick):
        """游戏全部退出：立刻清一轮 + 拍窗口基线，并登记快节拍（v1 1207-1208）。"""
        if self.guard is None:
            return
        try:
            self.guard.arm(tick.now, tick.procs)
        except Exception as e:
            self.ctx.log('弹窗监视启动失败: %r' % (e,))
            return
        # v1 的双速节拍靠 popup.armed；v2 由插件登记"我要快节拍到监视结束"
        try:
            watch = float((self.cfg.get('popup_guard') or {}).get('watch_sec', 90))
        except (TypeError, ValueError):
            watch = 90.0
        self.ctx.request_fast_poll(tick.now + watch)

    def on_tick(self, tick):
        """①会话中提前关窗口 ②推进监视（v1 1213-1218 / 1222）。"""
        if self.guard is None:
            return
        procs = tick.procs
        # 游戏会话期间：弹窗窗口一冒头就先关掉，不必等游戏进程全部消失。
        # 只发 WM_CLOSE，不动进程树（游戏内嵌页面还靠它承载）。
        if tick.session.active and procs is not None:
            try:
                self.guard.watch_early(procs)
            except Exception as e:
                self.ctx.log('提前拦截弹窗失败: %r' % (e,))
        try:
            self.guard.tick(tick.now, procs)
        except Exception as e:
            self.ctx.log('弹窗监视异常: %r' % (e,))
        # v2.1.0：把最近动作落盘（观察模式 / 关闭记录），供面板与诊断包读取。
        # 只在真的新增记录时写，避免每 tick 一次 IO。
        try:
            if getattr(self.guard, 'record_seq', 0) != self._record_seq:
                self._record_seq = self.guard.record_seq
                self.ctx.write_state('popup_recent', {
                    'version': 1,
                    'observe_only': bool(self.guard.observe_only),
                    'items': list(self.guard.records)[-20:],
                })
        except Exception as e:
            self.ctx.log('写弹窗动作记录失败: %r' % (e,))

    # ---------------------------------------------------------------- 自检
    def on_self_test(self, out):
        """--self-test 的弹窗段落（v1 self_test() 1254-1261）。"""
        pg_cfg = self.cfg.get('popup_guard') or {}
        out.append('弹窗关闭       : %s | 提前拦截 %s | 延迟 %s s | 监视 %s s | '
                   '会话轮询 %s s | 结束进程树 %s | 宿主 %s | 路径 %s' %
                   (bool(pg_cfg.get('enabled', False)),
                    bool(pg_cfg.get('early_close_enabled', True)),
                    pg_cfg.get('delay_sec'), pg_cfg.get('watch_sec'),
                    self._poll_fast(),
                    bool(pg_cfg.get('kill_tree', True)),
                    ','.join(pg_cfg.get('process_names') or []) or '无',
                    ','.join(pg_cfg.get('path_hints') or []) or '不限'))
        names = [n.lower() for n in (pg_cfg.get('protect_names') or [])]
        out.append('弹窗观察模式   : %s（只记录不关闭）'
                   % bool(pg_cfg.get('observe_only', False)))
        out.append('弹窗保护名单   : %d 项%s'
                   % (len(names), '（含 qq/微信/TIM 常驻）'
                      if any(n in names for n in ('qq.exe', 'weixin.exe',
                                                  'wechat.exe')) else '（缺少常驻项，请检查）'))
        if getattr(self.guard, 'records', None):
            out.append('弹窗最近动作   : %s' % '; '.join(
                '%s %s#%s %r' % (r.get('action'), r.get('exe'), r.get('pid'),
                                 r.get('title') or '')
                for r in self.guard.records[-3:]))
