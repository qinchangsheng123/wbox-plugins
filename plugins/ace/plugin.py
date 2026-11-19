# -*- coding: utf-8 -*-
"""插件：ACE 防护（游戏期间限速 + 退出后清理）。

从 v1 `guard_core.Guard` 搬来的逻辑，逐段对应（行号为 v1.3.1 的 guard_core.py）：
  migrate: ace_guard -> ace_cleanup            605-615
  AceCleaner 构造 / _snapshot_impl             639-642
  queue_ace() 927 / tick_ace() 943 / ace_watch_names() 981
  throttle_cfg() 990 / throttle_targets() 998 / tick_throttle() 1005
  会话分支 1162-1174 / 1196-1197 / 1206-1207 / 1227-1232

状态归属：`ace_pending` / `ace_exit_at` / `ace_next_check` / `ace_verdict` / `cleaner`
/ `ace_seen` / `throttled` / `throttle_tried` / `throttle_retry` / `ace_touched`
/ `restore_noted` / `update_block_noted` / `priv_noted` 全部搬进本插件。

四道闸门的实现仍在 `ace_native.AceCleaner`（随 exe 冻结，本插件只调用不改写），
默认秒数仍来自 `core_defaults`——插件目录可被替换，闸门参数不能跟着变。
"""
import os
import time

from core_defaults import ACE_CLEANUP_DEFAULTS, ACE_THROTTLE_DEFAULTS
from plugin_api import WingmanPlugin


class Plugin(WingmanPlugin):
    """ACE 清理（四道闸门）+ 运行时限速（只改优先级，不碰反作弊内部）。"""

    DEFAULT_CONFIG = {
        'ace_cleanup': dict(ACE_CLEANUP_DEFAULTS),
        'ace_throttle': dict(ACE_THROTTLE_DEFAULTS),
    }

    def __init__(self, ctx):
        WingmanPlugin.__init__(self, ctx)
        # ---- 清理状态（v1 的 Guard 字段）----
        self.cleaner = None
        self.ace_pending = False
        self.ace_exit_at = 0.0
        self.ace_next_check = 0.0
        self.ace_verdict = ''
        self.ace_touched = False       # 本局是否真的出现过 ACE（没出现就别排清理任务）
        self.restore_noted = False     # 本局"ACE 自行退出、无需还原"已记录过日志
        # ---- 限速状态 ----
        self.ace_seen = {}             # pid -> 首次见到 ACE 的时刻（延迟限制用）
        self.throttled = set()         # 已被限速的 pid（退出/关开关时按它还原）
        self.throttled_names = {}      # pid -> 进程名（还原日志要写清楚是哪几个）
        self.throttle_tried = set()    # 已成功限速的 pid
        self.throttle_retry = {}       # pid -> 下次允许重试的时刻
        self.update_block_noted = False
        self.priv_noted = False
        # ---- v2.2.0 决策解释（审查项 4）----
        # 每一轮判定都留一条：为什么清 / 为什么跳过、闸门当时的值、目标身份。
        # 只保留最近 20 条并落盘，自检与诊断包直接读。
        self.decisions = []
        self.last_action = ''
        self._decision_stamp = None      # (code, reason) 用于日志去重
        self._decision_log_at = 0.0      # 上次写日志的时刻（周期性刷一条）
        # ---- 配置缓存 ----
        self.cfg = {}
        self.ac_cfg = {}
        self._stamp = None
        self._core_mod = None
        # ---- 配置派生集合的缓存（v2.1.0：原来每个 tick 重建 3 个 set + 1 个 dict）----
        self._th_cfg = None
        self._watch = frozenset()
        self._targets = frozenset()
        self._upd = frozenset()

    # ---------------------------------------------------------------- 内部
    def _core(self):
        """懒加载内核里的 Win32 优先级辅助函数（面板不调用，故不会拉起 Win32）。"""
        if self._core_mod is None:
            self._core_mod = self.ctx.import_core('guard_core')
        return self._core_mod

    # ---------------------------------------------------------------- 配置
    def migrate(self, raw):
        """v1 的 `ace_guard` 段平滑迁移（guard_core.py:605-615 逐字保留）。"""
        if not isinstance(raw, dict):
            return
        if isinstance(raw.get('ace_cleanup'), dict):
            return
        legacy = raw.get('ace_guard')
        if not isinstance(legacy, dict):
            return
        ac = dict(ACE_CLEANUP_DEFAULTS)
        ac['enabled'] = bool(legacy.get('enabled', True))
        if legacy.get('ace_processes'):
            ac['ace_processes'] = legacy['ace_processes']
        try:
            ac['max_idle_sec'] = int(legacy.get('max_idle_sec', ac['max_idle_sec']))
        except (TypeError, ValueError):
            pass
        raw['ace_cleanup'] = ac
        self.ctx.log('旧配置段 ace_guard 已迁移为 ace_cleanup')

    def on_config(self, cfg):
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.ac_cfg = self.cfg.get('ace_cleanup') or {}
        self._rebuild_caches()
        ace_native = self.ctx.import_core('ace_native')
        # 状态文件必须留在安装根（ace_state.json）：搬到插件目录会让升级后
        # 冷却时间戳丢失，可能立刻触发一次清理（文档 §1 约定 4）。
        self.cleaner = ace_native.AceCleaner(
            self.ac_cfg, self.ctx.log,
            os.path.join(self.ctx.base, 'ace_state.json'),
            games_present=self.ctx.games_present)
        self.cleaner._snapshot_impl = self.ctx.snapshot
        self._log_summary()

    # ---------------------------------------------------------------- 决策记录
    def _session_key(self):
        try:
            return int(self.ctx.session().started_at or 0)
        except Exception:
            return 0

    def _gate_snapshot(self):
        """本轮四道闸门的实际取值：排查"为什么没清"首先看这几个数。"""
        ac = self.ac_cfg or {}
        return {
            'grace_sec': ac.get('grace_sec'),
            'min_ace_age_sec': ac.get('min_ace_age_sec'),
            'quiet_after_update_sec': ac.get('quiet_after_update_sec'),
            'cooldown_sec': ac.get('cooldown_sec'),
            'dry_run': bool(ac.get('dry_run')),
        }

    def _target_snapshot(self, procs=None):
        """目标身份：名字 + pid + 已运行秒数 + 当前 CPU/IO（限速目标再带状态）。"""
        if self.cleaner is None:
            return []
        try:
            procs = procs if procs is not None else (self.ctx.procs() or {})
            core = self._core()
            out = []
            for pid, name in self.cleaner.ace_processes(procs or {}):
                age = None
                try:
                    age = self.ctx.import_core('ace_native').process_age_sec(pid)
                except Exception:
                    age = None
                item = {'pid': int(pid), 'name': name,
                        'age_sec': int(age) if age is not None else None,
                        'throttled': int(pid) in self.throttled}
                try:
                    item['cpu'] = core.priority_class(pid)[1]
                    io_now = core.io_priority(pid)
                    item['io'] = core.IO_HINTS.get(io_now, io_now)
                except Exception:
                    pass
                out.append(item)
            return out
        except Exception:
            return []

    def _record_decision(self, code, reason, action='', extra=None):
        """记一条决策并（有变化时）落盘 + 写日志。

        日志策略：判定码变了、真的动手了、或距上次已过 300 秒才写一行——
        否则 COOLDOWN/GRACE 阶段每分钟一条会把 guard.log 刷满。
        """
        now = time.time()
        rec = {'ts': round(now, 3), 'kind': 'cleanup', 'code': code,
               'reason': reason, 'action': action,
               'session': self._session_key(),
               'gates': self._gate_snapshot(),
               'targets': self._target_snapshot()}
        if extra:
            rec.update(extra)
        self.decisions.append(rec)
        if len(self.decisions) > 20:
            del self.decisions[:-20]
        stamp = (code, reason, action)
        changed = stamp != self._decision_stamp
        if changed:
            self._decision_stamp = stamp
        if changed or action or now - self._decision_log_at >= 300:
            self._decision_log_at = now
            self.last_action = action
            self._log_decision(rec)
            self._save_decisions()
            if action or code == 'CLEAN':
                try:
                    self.ctx.import_core('events').emit(
                        'ace', code=code, msg=reason, action=action,
                        dur_ms=None)
                except Exception:
                    pass
        return rec

    def _log_decision(self, rec):
        tg = rec.get('targets') or []
        targets = ', '.join('%s#%d%s' % (
            t.get('name'), t.get('pid'),
            ('/%.0fs' % t['age_sec']) if t.get('age_sec') is not None else '')
            for t in tg) or '无'
        g = rec.get('gates') or {}
        self.ctx.log('ACE 决策 %s：%s｜目标=%s｜闸门 grace=%s min_age=%s quiet=%s '
                     'cooldown=%s%s%s'
                     % (rec['code'], rec['reason'], targets,
                        g.get('grace_sec'), g.get('min_ace_age_sec'),
                        g.get('quiet_after_update_sec'), g.get('cooldown_sec'),
                        '｜DryRun' if g.get('dry_run') else '',
                        ('｜动作=%s' % rec['action']) if rec.get('action') else ''))

    def _save_decisions(self):
        try:
            self.ctx.write_state('ace_decisions',
                                 {'version': 1, 'items': self.decisions[-20:]})
        except Exception as e:
            self.ctx.log('写 ACE 决策记录失败: %r' % (e,))

    def _log_summary(self):
        """功能开关摘要只在真的变化时打一行（v1 571-582 的 stamp 语义）。"""
        ac = self.ac_cfg
        stamp = '%s|%s' % (ac.get('enabled'), ac.get('dry_run'))
        if stamp == self._stamp:
            return
        self._stamp = stamp
        self.ctx.log('ACE 清理：%s%s' %
                     ('开启' if ac.get('enabled') else '关闭',
                      '（DryRun 只判定不动手）' if ac.get('dry_run') else ''))

    def on_load(self):
        pass

    # ---------------------------------------------------------------- 会话
    def on_session_start(self, tick):
        """新会话：清掉上一局的"出现过 ACE / 已结算"标记（v1 1173-1174）。"""
        self.ace_touched = False
        self.restore_noted = False
        self.update_block_noted = False

    def on_session_resume(self, tick):
        """闪断恢复：取消排队中的清理（v1 1162-1164）。"""
        if self.ace_pending:
            self.ace_pending = False
            self.ctx.log('游戏会话继续，取消 ACE 清理调度')

    def on_session_grace(self, tick):
        """游戏全部退出：按闸门排队（v1 1206 的 queue_ace(now)）。"""
        self.queue_ace(tick.now)

    def on_exit(self, reason):
        """守护退出前还原被限速的 ACE（v1 1227-1232），别留一个"没人管"的低优先级进程。"""
        if not self.throttled:
            return
        core = self._core()
        done = len(self.throttled)
        for pid in list(self.throttled):
            try:
                core.restore_process(pid)
            except Exception as e:
                self.ctx.log('还原 pid %d 失败: %r' % (pid, e))
        self.throttled.clear()
        self.ctx.log('守护退出，已还原 %d 个 ACE 进程的优先级' % done)

    def on_tick(self, tick):
        """每轮：①observe ②限速 ③清理判定（v1 1140-1143 / 1220-1221）。"""
        procs = tick.procs
        if procs is None:
            # 内核在快照失败时不会发 on_tick；真收到 None 就当"未知"，什么都不判定
            return
        now = tick.now
        # ① 闸门 4 的时间基准：必须在游戏期间也持续观察更新组件
        if self.cleaner is not None:
            try:
                self.cleaner.observe(procs, now)
            except Exception as e:
                self.ctx.log('ACE 观察失败: %r' % (e,))
        # ② 本局是否出现过 ACE（决定退出后要不要排清理）
        if tick.names & self.ace_watch_names():
            self.ace_touched = True
        # ③ 限速（含"会话彻底结束后还原"）
        self.tick_throttle(now, procs, tick.names)
        # ④ 清理排队推进
        self.tick_ace(now, tick.names)

    # ---------------------------------------------------------------- 清理
    def queue_ace(self, now):
        """排队：宽限期从"游戏全部退出"那一刻起算（v1 927-941）。"""
        ac = self.ac_cfg
        if not ac.get('enabled', False):     # 功能开关：退出后清理 ACE
            return
        if not self.ace_touched:
            self.ctx.log('本局没有出现 ACE 进程，跳过清理调度')
            self._record_decision('NOACE', '本局未出现 ACE 进程',
                                  extra={'phase': 'queue'})
            return
        self.ace_pending = True
        self.ace_exit_at = now
        self.ace_next_check = now
        self.ace_verdict = ''
        self.ctx.log('ACE 清理已排队：宽限 %d 秒、ACE 保护期 %d 秒、更新组件静默 %d 秒，每 %d 秒复核一次' %
                     (int(ac.get('grace_sec', 180)), int(ac.get('min_ace_age_sec', 300)),
                      int(ac.get('quiet_after_update_sec', 30)),
                      int(ac.get('eval_interval_sec', 60))))

    def tick_ace(self, now, names):
        """推进排队中的清理（v1 943-979 原文搬迁）。"""
        if not self.ace_pending or self.cleaner is None:
            return
        ac = self.ac_cfg
        if not ac.get('enabled', False):
            self.ace_pending = False
            return
        max_idle = float(ac.get('max_idle_sec', 1200))
        if now - self.ace_exit_at > max_idle:
            self.ctx.log('ACE 清理排队超过 %d 秒上限，本轮结束' % max_idle)
            self.ace_pending = False
            return
        if now < self.ace_next_check:
            return
        procs = self.ctx.snapshot()
        if procs is None:
            # 没读到进程表时不能判 NOACE/CLEAN：保持排队状态，下一轮复核
            return
        code, reason = self.cleaner.verdict(procs, self.ace_exit_at)
        if code != self.ace_verdict:
            self.ctx.log('ACE 判定 %s：%s' % (code, reason))
            self.ace_verdict = code
        if code == 'CLEAN':
            before = self._target_snapshot(procs)
            ok = False
            try:
                ok = bool(self.cleaner.run_cleanup(procs))
            except Exception as e:
                self.ctx.log('ACE 清理异常: %r' % (e,))
            self._record_decision('CLEAN', reason,
                                  action='清理成功' if ok else '清理未完成',
                                  extra={'ok': ok, 'before': before})
            self.ace_pending = False
            self.ace_verdict = ''
            return
        if code == 'NOACE':
            self._record_decision('NOACE', reason, action='结束排队')
            self.ace_pending = False
            self.ace_verdict = ''
            return
        self._record_decision(code, reason)
        self.ace_next_check = now + float(ac.get('eval_interval_sec', 60))

    # ---------------------------------------------------------------- 限速
    def ace_watch_names(self):
        """本程序关心的 ACE 进程名合集（清理名单 + 限速名单 + 更新组件）。

        v2.1.0：改为返回 on_config 里算好的 frozenset。会话 0.4s 节拍时
        原来每秒要重建 2.5 次三个集合，纯属白做。
        """
        if self._th_cfg is None:
            self._rebuild_caches()
        return self._watch

    def _rebuild_caches(self):
        """配置派生集合只在 on_config 时算一次（唯一正文，别在 tick 里重算）。"""
        th = dict(ACE_THROTTLE_DEFAULTS)
        got = self.cfg.get('ace_throttle')
        if isinstance(got, dict):
            th.update(got)
        self._th_cfg = th
        ac = self.ac_cfg or {}
        ace_names = frozenset(n.lower() for n in (ac.get('ace_processes') or []))
        proc_names = frozenset(n.lower() for n in (th.get('processes') or []))
        self._upd = frozenset(n.lower() for n in (th.get('update_processes') or []))
        self._watch = ace_names | proc_names | self._upd
        self._targets = proc_names or ace_names

    def throttle_cfg(self):
        """始终返回完整限速配置；config 缺段/缺键时用默认值补齐（v1 990-997）。

        v2.1.0：返回 on_config 缓存的那份字典。**调用方只读不改**——
        全文件所有用法都是 th.get(...)，没有任何写入。
        """
        if self._th_cfg is None:
            self._rebuild_caches()
        return self._th_cfg

    def throttle_targets(self):
        if self._th_cfg is None:
            self._rebuild_caches()
        return self._targets

    def _restore_all(self, core):
        """还原全部被限速的 ACE，逐个记结果（谁成功、谁失败、错误码）。"""
        done = []
        failed = []
        for pid in list(self.throttled):
            name = self.throttled_names.get(pid) or '?'
            try:
                ok = core.restore_process(pid)
            except Exception as e:
                failed.append('%s#%d(%r)' % (name, pid, e))
                continue
            if ok is False:
                failed.append('%s#%d(读回不符)' % (name, pid))
            else:
                done.append('%s#%d' % (name, pid))
        self.throttled.clear()
        self.throttled_names.clear()
        if done:
            self.ctx.log('还原明细：%s' % ', '.join(done))
        if failed:
            self.ctx.log('还原失败 %d 个：%s（进程可能已退出或权限不足）'
                         % (len(failed), '，'.join(failed)))
        return len(done)

    def tick_throttle(self, now, procs, names=None):
        """游戏期间把 ACE 的 I/O 与 CPU 优先级压到最低（v1 1005-1089 原文搬迁）。

        只在 ACE 进程已存活 delay_sec 之后动手（避开启动自检），
        更新期（sguardupdate64 等在跑）一律不动手 —— 与 ACE 清理的闸门 2 同源。
        """
        core = self._core()
        th = self.throttle_cfg()
        if not th.get('enabled'):
            if self.throttled:
                done = self._restore_all(core)
                self.restore_noted = True
                self.ctx.log('ACE 限速已关闭，已还原 %d 个进程的优先级' % done)
            self.ace_seen.clear()
            self.throttle_tried.clear()
            self.throttle_retry.clear()
            return

        # 限速只属于"游戏期间"。会话彻底结束（idle）后把降下去的东西还原，
        # 否则常驻的 ACE 服务会被一直压在 IDLE，影响下一局或其它腾讯游戏。
        # v1 的条件是 `not session and pending_stop is None`，在 v2 就是 state == 'idle'。
        if self.ctx.session().state == 'idle':
            if self.throttled:
                done = self._restore_all(core)
                self.restore_noted = True   # 本局已结算，下一轮别再补"无需还原"
                self.ctx.log('游戏已退出，已还原 %d 个 ACE 进程的优先级' % done)
            elif self.ace_touched and not self.restore_noted:
                # 本局 ACE 出现过，但退出时已经自己退干净了 —— 没有 pid 可还原。
                # 补一条日志，否则日志里只有"ACE 限速"却找不到还原，看起来像漏做。
                self.ctx.log('本局 ACE 已自行退出，无需还原优先级')
                self.restore_noted = True
            self.ace_seen.clear()
            self.throttle_tried.clear()
            self.throttle_retry.clear()
            return

        # v2.1.0：内核本轮已算好 names 就直接用，别再从 143 项进程表重建集合；
        # 更新组件名单也改用 on_config 缓存的 frozenset。
        names_now = names if names is not None else set(procs.values())
        upd = self._upd
        busy = names_now & upd
        if busy:
            if not self.update_block_noted:
                self.ctx.log('ACE 组件正在更新（%s），本轮不做限制' % ', '.join(sorted(busy)))
                self.update_block_noted = True
            return
        self.update_block_noted = False

        if not core.enable_debug_privilege() and not self.priv_noted:
            self.ctx.log('未能启用 SeDebugPrivilege，ACE 限速大概率被拒（错误码 5，需以管理员运行）')
            self.priv_noted = True

        # 进程退出后清掉状态，避免 pid 复用造成误判
        for pid in [p for p in self.ace_seen if p not in procs]:
            self.ace_seen.pop(pid, None)
        for pid in [p for p in self.throttle_tried if p not in procs]:
            self.throttle_tried.discard(pid)
        for pid in [p for p in self.throttle_retry if p not in procs]:
            self.throttle_retry.pop(pid, None)
        self.throttled &= set(procs)
        self.throttled_names = dict((p, n) for p, n in self.throttled_names.items()
                                    if p in self.throttled)

        targets = self.throttle_targets()
        delay = float(th.get('delay_sec', 20))
        bind_last = bool(th.get('affinity_last_core', False))
        hint = th.get('io_priority', 0)
        if isinstance(hint, str):
            hint = {'very-low': 0, 'low': 1, 'normal': 2}.get(hint.lower(), 0)
        hint = int(hint)

        for pid, name in procs.items():
            if name not in targets or pid in self.throttle_tried:
                continue
            retry_at = self.throttle_retry.get(pid)
            if retry_at is not None and now < retry_at:
                continue
            first = self.ace_seen.get(pid)
            if first is None:
                self.ace_seen[pid] = now
                continue
            if now - first < delay:
                continue
            ok, info = core.throttle_process(pid, hint, bind_last)
            if ok:
                self.throttle_tried.add(pid)
                self.throttle_retry.pop(pid, None)
                self.throttled.add(pid)
                self.throttled_names[pid] = name
                _, shown = core.priority_class(pid)
                io_now = core.io_priority(pid)
                self.ctx.log('ACE 限速: %s (pid %d) → %s（读回 CPU=%s，磁盘IO=%s）' %
                             (name, pid, info, shown, core.IO_HINTS.get(io_now, io_now)))
            else:
                # 权限/时序导致失败时不要在整局里放弃，隔 60 秒再试一次
                self.throttle_retry[pid] = now + 60
                self.ctx.log('ACE 限速失败: %s (pid %d) %s，60 秒后重试' % (name, pid, info))

    # ---------------------------------------------------------------- 自检
    def on_self_test(self, out):
        """--self-test 的 ACE 段落（v1 self_test() 1246-1272）。"""
        core = self._core()
        ac = self.ac_cfg
        th = self.throttle_cfg()
        out.append('ACE 防护       : %s | 宽限 %s s | ACE 保护期 %s s | DryRun %s' %
                   (bool(ac.get('enabled', False)), ac.get('grace_sec'),
                    ac.get('min_ace_age_sec'), bool(ac.get('dry_run'))))
        out.append('  ACE 限速    : %s | delay %s s | targets %d | 更新后静默 %s s' %
                   (bool(th.get('enabled')), th.get('delay_sec'),
                    len(self.throttle_targets()), ac.get('quiet_after_update_sec', 30)))
        procs = core.snapshot()
        if procs is None:
            out.append('ACE procs now : snapshot 失败（取不到进程表）')
            out.append('ACE services  : n/a')
            return
        ace_names = set(n.lower() for n in ac.get('ace_processes', []))
        out.append('ACE procs now : %s' % sorted(set(procs.values()) & ace_names))
        out.append('ACE services  : %s' %
                   (self.cleaner.services_running() if self.cleaner else 'n/a'))
        targets = self.throttle_targets()
        preview = self._target_snapshot(procs)
        if preview:
            out.append('ACE 目标预览   : %s' % '; '.join(
                '%s#%d age=%ss %s/%s%s'
                % (t['name'], t['pid'],
                   t.get('age_sec') if t.get('age_sec') is not None else '?',
                   t.get('cpu') or '?', t.get('io') or '?',
                   ' 已限速' if t.get('throttled') else '')
                for t in preview))
        else:
            out.append('ACE 目标预览   : 当前没有匹配的 ACE 进程')
        for pid, name in sorted(procs.items(), key=lambda kv: kv[1]):
            if name in targets:
                io_now = core.io_priority(pid)
                out.append('   throttled   : %s (pid %d) CPU=%s IO=%s'
                           % (name, pid, core.priority_class(pid)[1],
                              core.IO_HINTS.get(io_now, io_now)))
        if self.decisions:
            last = self.decisions[-1]
            out.append('ACE 最近决策   : %s｜%s｜动作=%s｜%s'
                       % (last.get('code'), last.get('reason'),
                          last.get('action') or '无',
                          time.strftime('%H:%M:%S',
                                        time.localtime(last.get('ts') or 0))))
