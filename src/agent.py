r"""agent.py -- 解锁 agent（用户会话里的常驻进程）

链路里它是最中间的一环：

    LogonUI.exe (SYSTEM, 安全桌面)
      └─ VoiceUnlockProvider.dll (CP)
           └─ 命名管道 \\.\pipe\VoiceUnlock
                └─ 【本 agent】(用户会话)  ── 判定内核（声纹）
                     └─ DPAPI 用户作用域解密密码

为什么密码必须在这里解密
    CP 跑在 LogonUI 里、身份是 SYSTEM，**解不开用户作用域的 DPAPI**；
    而且它也不该解 —— 这样密文只需要在用户会话里短暂变成明文。

安全措施
    * 管道 DACL 显式授权（SYSTEM + Administrators + 交互用户），不用 NULL DACL
    * 校验对端映像（默认只认 LogonUI.exe）
    * 只在【确认锁屏】(session.is_locked) 时才应答
    * 尝试限速（避免被反复触发刷麦克风）
    * 只返回凭据，不返回任何其它信息；失败一律给 {ok:false}

命令
    run             常驻（默认；给 --once 只服务一次，便于测试）
    set-password    加密保存 Windows 登录密码（不回显）
    clear-password  删除已保存凭据
    status          凭据 / 档案 / 锁屏状态一览
    test            不走管道，直接跑一次判定
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import threading
import time

from . import cli
from . import config as C
from .i18n import tr          # getpass 的提示绕开 print 直接写 stderr，得自己翻
from . import devices as D
from . import audio as A
from . import session
from .dpapi import CredentialStore
from .profiles import ProfileStore
from .verify import Verifier
from .win32pipe import PipeServer

DEFAULT_PIPE = r"\\.\pipe\VoiceUnlock"
LOG_PATH = os.path.join(C.HOME, "logs", "agent.log")


def _log(msg: str) -> None:
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


class Agent:
    def __init__(self, pipe_name: str = DEFAULT_PIPE, allow_any_peer: bool = False,
                 require_locked: bool = True, min_interval_s: float = 3.0):
        self.pipe_name = pipe_name
        self.require_locked = require_locked
        self.min_interval_s = float(min_interval_s)
        self.allow_images = () if allow_any_peer else ("logonui.exe",)
        self.store = CredentialStore()
        self.profiles = ProfileStore()
        self.verifier = Verifier(store=self.profiles, logger=_log)
        cfg = C.load_config()
        self.cfg = cfg
        # 常驻监听电平的时长：锁屏挂着时一直听，有人开口才开始录音
        self.wait_voice_s = float((cfg.get("audio") or {}).get("wait_voice_s", 45.0))
        self._last_attempt = 0.0
        self._lock = threading.Lock()
        self.stats = {"asked": 0, "accepted": 0, "rejected": 0, "refused": 0}

        # ---- 常驻监听 + 解锁票据 ----
        # 为什么要"票据"而不是在 handle 里等：CP 是 SYSTEM，它调用 GetSerialization
        # 期间 LogonUI 的界面是被占住的。如果我们在那里干等 25~60 秒，用户就被卡在
        # 那个界面上（实测把密码框都挤没了）。所以改成：
        #   常驻线程一直听电平 -> 听到就录+比对 -> 通过则备好一张短时票据
        #   CP 来问时：有票据就立刻交；没有就最多等 unlock_wait_s 秒，然后干净地失败
        self.ticket = None                       # (expire_ts, {username,password,domain})
        # 凭据"现在还能不能登录"的缓存（见 cred_live_ok）
        self._cred_live = None                   # (ok, why)
        self._cred_live_ts = 0.0
        self._cred_stale_warn_ts = 0.0
        # 票据存活时间：LogonUI 自己的重试周期实测 7~16 秒（我们控制不了它），
        # 20 秒曾经过紧 —— 票据刚备好就过期，用户就得再等一轮。给到 40 秒。
        self.ticket_ttl_s = 40.0
        self.unlock_wait_s = float((cfg.get("audio") or {}).get("unlock_wait_s", 8.0))
        self.listen_poll_s = float((cfg.get("audio") or {}).get("listen_poll_s", 5.0))
        self._stop = threading.Event()
        self._listen_thread = None

    # ---------------------------------------------------------------- 常驻监听

    def start_resident_listener(self) -> None:
        if self._listen_thread and self._listen_thread.is_alive():
            return
        self._listen_thread = threading.Thread(target=self._resident_loop,
                                               name="vu-listen", daemon=True)
        self._listen_thread.start()
        _log("[agent] 常驻监听已启动（每秒轮询 %.0f 秒窗口，只在锁屏时听）"
             % self.listen_poll_s)

    def stop_resident_listener(self) -> None:
        self._stop.set()

    def _resident_loop(self) -> None:
        """后台常驻：锁屏时持续监听电平；听到就录、比对，通过则备票据。"""
        while not self._stop.is_set():
            try:
                if not self.store.exists():
                    time.sleep(5.0)
                    continue
                if not session.is_locked():
                    # 没锁屏就不该开着麦听（省资源，也避免"一直在录"的观感）
                    time.sleep(2.0)
                    continue
                devs = D.trusted_only(D.list_capture_devices())
                if not devs:
                    time.sleep(3.0)
                    continue
                aud = self.cfg.get("audio") or {}
                trig = A.wait_for_speech(
                    devs, timeout_s=self.listen_poll_s, logger=_log,
                    abs_floor=float(aud.get("level_floor", 0.003)),
                    rel_factor=float(aud.get("level_rel", 6.0)))
                if trig is None:
                    continue
                _log("[agent] 常驻监听：电平触发 %s -> 开始录音比对" % trig["name"][:26])
                # 常驻路径不计失败额度：后台一直在听，不该被环境噪声的误触发锁死
                #
                # ★ 两级递进 —— 为什么（实测）：
                #   pick_best_window 的倍数直接等于"用户说完之后还要干等多久"。
                #   3 倍 = 录 6.0 秒，整条解锁链路实测 14 秒，而锁屏上【没有任何反馈】，
                #   用户只会认为"不行"（这正是被判定为失败的那次）。
                #   所以先用 1.5 倍抢一枪（3.0 秒）：本设备已有档案时通常就中，
                #   中了立刻备票；不中再退回 3 倍那套（短句 / 嘈杂场景更稳）。
                #   ★ 为什么是 1.5 而不是 1.0：1.0 只有 2.015 秒，正好一个窗口、
                #     "挑窗"失去意义，等于从起音点硬取 2 秒。实测这样分数会掉到
                #     0.165 —— 而阈值是 0.1603，只高出 0.005，太悬。
                #     3.0 秒能提供约 4 个候选窗口，既挑得动、又只多等 1 秒。
                r = None
                for mul, label in ((1.5, "快判 3.0s"), (3.0, "退回 6.0s")):
                    t0 = time.time()
                    r = self.verifier.attempt(device_ids=[trig["id"]],
                                              count_failures=False,
                                              pick_best_window=True,
                                              best_window_multiplier=mul)
                    dt = time.time() - t0
                    _log("[agent] 常驻判定(%s 用时%.1fs)：%s 模式=%s 分数=%s 阈值=%s  %s%s"
                         % (label, dt,
                            "通过" if r.get("accepted") else "拒绝", r.get("mode"),
                            r.get("scores"), r.get("threshold"), r.get("reason"),
                            ("  采集错误=%s" % r["errors"]) if r.get("errors") else ""))
                    if r.get("accepted"):
                        break
                if not r.get("accepted"):
                    # 只有"疑似冒名"才计失败：分数接近阈值说明确实像本人（可能是
                    # 攻击者在模仿），而噪声分数远低于阈值，不该算账。
                    sc = [x for x in (r.get("scores") or []) if x is not None]
                    thr = r.get("threshold") or 0.0
                    if sc and thr and max(sc) >= thr * 0.6:
                        self.verifier.state.on_fail(self.cfg)
                        _log("[agent] 分数接近阈值(%.4f>=%.4f)，计入一次失败"
                             % (max(sc), thr * 0.6))
                if r.get("accepted"):
                    # ★ 这一步可能是失败点，而且原因很容易被误读：
                    #   文件在、但本会话解不开（DPAPI 用户作用域的前提被破坏，
                    #   典型是"会话是用旧密码建立的、之后密码被改过"）。
                    #   踩过：这里抛异常被外层吞掉，症状只剩一句"票据未就绪"，
                    #   看起来像声纹不灵，其实是凭据读不出来。必须单独报清楚。
                    try:
                        cred = self.store.load(with_password=True)
                    except Exception as e:
                        self._warn_cred_unreadable(e)
                        continue
                    # ★ 提交之前先确认真能登录（防账户锁定，见 cred_live_ok）
                    #   无密码账户例外：不提交凭据，改用"注入回车"。
                    live_ok, live_why = self.cred_live_ok()
                    if not live_ok:
                        self._warn_cred_stale(live_why)
                        continue
                    pw = cred.get("password") or ""
                    action = "inject_enter" if not pw else "credential"
                    self.ticket = (time.time() + self.ticket_ttl_s,
                                   {"username": cred.get("username", ""),
                                    "password": pw,
                                    "domain": cred.get("domain", "."),
                                    "action": action})
                    _log("[agent] ★ 已备好解锁票据（%.0f 秒内有效，方式=%s），"
                         "等 LogonUI 来取"
                         % (self.ticket_ttl_s,
                            "注入回车（本账户无密码，不提交凭据）"
                            if action == "inject_enter"
                            else "提交凭据（已用 LogonUser 验证可登录）"))
            except Exception as e:
                _log("[agent] 常驻监听异常: %r" % (e,))
                time.sleep(2.0)

    # ---------------------------------------------------------------- 凭据自检

    def cred_check(self):
        """凭据能不能在【本会话】里解开 —— 这是 DPAPI 用户作用域的硬前提。

        为什么值得单独一个方法（踩过）：文件存在 != 读得出来。会话若是在
        "账户还没密码 / 还是旧密码"时建立的，之后密码被改过，DPAPI 主密钥会被
        用新密码重新保护，老会话就拿不到它了 —— 此时 CryptUnprotectData 返回
        0x8009000B (NTE_BAD_KEY_STATE)。症状极具误导性：声纹全对、票据却不出现。
        返回 (ok, 说明)。
        """
        if not self.store.exists():
            return False, "还没有保存凭据（先跑 tools\\setup_password.py）"
        try:
            cred = self.store.load(with_password=True)
        except Exception as e:
            return False, ("本会话解不开凭据：%s  "
                           "→ 请在【当前登录会话】里重跑 setup_password.cmd "
                           "(--keep-password) 重新保存一次" % (e,))
        return True, ("OK（用户=%s，密码长度=%d）"
                      % (cred.get("username"), len(cred.get("password") or "")))

    def _warn_cred_unreadable(self, exc) -> None:
        """限速地把"凭据读不出来"这个真因喊出来，别让它被淹掉。"""
        now = time.time()
        if now - getattr(self, "_cred_warn_ts", 0.0) < 60.0:
            return
        self._cred_warn_ts = now
        _log("[agent] ✗ 凭据存在但【本会话解不开】：%s" % (exc,))
        _log("[agent]   这不是声纹的问题。DPAPI 用户作用域要求"
             "『保存凭据的会话』与『使用凭据的会话』是同一个登录会话。")
        _log("[agent]   改过密码之后老会话就失效了 —— 请双击 setup_password.cmd "
             "用 --keep-password 重新保存一次，然后重启 agent。")

    def cred_live_ok(self, max_age_s: float = 30.0):
        """确认"库里存的密码【现在】还能登录" —— 这是防止把账户锁死的关键一步。

        ★ 为什么必须有它（真实风险，不是假想）：
          我们的 CP 是【自动提交密码】。如果用户改过密码、而库里存的还是旧的，
          那么每通过一次声纹就会自动提交一次【错误密码】。本机账户策略是
          "10 次密码错 -> 锁定 10 分钟"，也就是说：重复说话 10 次就有可能把账户
          锁死，之后连正确密码都进不去（要等 10 分钟）。
          所以提交之前先真的 LogonUser 验证一次：不对就【一次都不提交】，
          并把原因喊清楚。代价是每 30 秒最多一次 LogonUser（几毫秒）。
        """
        now = time.time()
        if self._cred_live is not None and (now - self._cred_live_ts) < max_age_s:
            return self._cred_live
        ok, why = self.cred_check()
        if not ok:
            self._cred_live, self._cred_live_ts = (False, why), now
            return self._cred_live
        try:
            cred = self.store.load(with_password=True)
        except Exception as e:
            self._cred_live, self._cred_live_ts = (False, "解密失败：%r" % (e,)), now
            return self._cred_live
        if not (cred.get("password") or ""):
            # 无密码账户：**不提交任何凭据**，让 CP 在安全桌面上注入一个回车
            # （Windows 自己的"按回车即登录"就是合法解锁路径）。
            # 所以这里直接判为可用 —— LogonUser 对空密码会返回 1327，
            # 那不代表密码错，而且我们根本不走提交凭据那条路。
            self._cred_live = (True, "无密码账户（解锁方式=注入回车）")
            self._cred_live_ts = now
            return self._cred_live
        try:
            from .setup_password import (LOGON32_LOGON_INTERACTIVE, LOGON_ERRORS,
                                         try_logon)
            good, err = try_logon(cred.get("username", ""), cred.get("domain", "."),
                                  cred.get("password", ""), LOGON32_LOGON_INTERACTIVE)
        except Exception as e:
            self._cred_live, self._cred_live_ts = (False, "验证异常：%r" % (e,)), now
            return self._cred_live
        if good:
            self._cred_live = (True, "OK")
        else:
            self._cred_live = (False, "LogonUser 失败 err=%s（%s）"
                               % (err, LOGON_ERRORS.get(err, "未知")))
        self._cred_live_ts = now
        return self._cred_live

    def _warn_cred_stale(self, why: str) -> None:
        """限速地把"密码已失效，我拒绝提交"喊出来。"""
        now = time.time()
        if now - self._cred_stale_warn_ts < 60.0:
            return
        self._cred_stale_warn_ts = now
        _log("[agent] ✗ 拒绝提交凭据：%s" % why)
        _log("[agent]   原因：库里保存的登录密码【现在已经不能登录了】"
             "（最常见：你改过 Windows 密码）")
        _log("[agent]   为什么宁可不提交：我们的 CP 会自动替你交密码，"
             "而本机策略是 10 次密码错就锁账户 10 分钟 ——")
        _log("[agent]   继续自动提交会把你锁在门外，连正确密码都用不了。")
        _log("[agent]   → 请在【当前登录会话】里重跑「设置登录凭据」更新密码。")

    def _take_ticket(self):
        """取走票据（单次使用）。没有或已过期返回 None。"""
        t = self.ticket
        if not t:
            return None
        if t[0] <= time.time():
            self.ticket = None
            return None
        self.ticket = None
        return t[1]

    # ---------------------------------------------------------------- 处理一次请求

    def _bypass_voice(self) -> bool:
        """仅测试用的旁路：跳过声纹，直接用已保存凭据（无麦克风环境下的链路验证）。"""
        return bool((self.cfg.get("decision") or {}).get("test_bypass_voice", False))

    def handle(self, req: dict, ctx: dict) -> dict:
        cmd = (req or {}).get("cmd", "")
        self.stats["asked"] += 1
        _log("[agent] 收到请求 cmd=%r 对端=%s" % (cmd, ctx.get("client_image", "?")))

        if cmd == "ping":
            # CP 会先快速问这一句：agent 不在时立刻失败，不让用户在锁屏上干等
            # cred_readable：文件在但本会话解不开时，CP 可以据此给出更准确的提示
            # ★ ticket：CP 的看门线程靠它决定"要不要让凭据出现"。
            #   为 false 时 CP 枚举 0 个凭据 —— 锁屏界面上完全没有我们的东西。
            t = self.ticket
            return {"ok": True, "ready": self.store.exists(),
                    "cred_readable": self.cred_check()[0],
                    "ticket": bool((self._bypass_voice() and self.store.exists())
                                   or (t and t[0] > time.time())),
                    "locked": session.is_locked(), "signals": session.lock_signals()}

        if cmd != "unlock":
            self.stats["refused"] += 1
            return {"ok": False, "reason": "unknown-cmd"}

        if not self.store.exists():
            self.stats["refused"] += 1
            _log("[agent] 拒绝：还没有保存登录密码（先跑 set-password）")
            return {"ok": False, "reason": "no-credential-stored"}

        # ★ 第二道闸：交出去之前确认密码现在能登录（无密码账户走"注入回车"，
        #   根本不提交凭据，所以不受这道闸限制 —— 见下面 cred_live_ok 的分支）。
        live_ok, live_why = self.cred_live_ok()
        if not live_ok:
            self.stats["refused"] += 1
            self._warn_cred_stale(live_why)
            self.ticket = None
            return {"ok": False, "reason": "credential-not-valid-now"}

        sig = session.lock_signals()
        if self.require_locked and not session.is_locked(sig):
            self.stats["refused"] += 1
            _log("[agent] 拒绝：当前不是锁屏状态  %s" % session.describe())
            return {"ok": False, "reason": "not-locked"}
        _log("[agent] 锁屏信号: %s" % session.describe())

        # ★ 测试开关（生产恒为 false）：不看票据、直接让 CP"注入回车"。
        #   用途：在没有麦克风的环境（如虚拟机）里验证"注入回车能否解锁无密码账户"。
        if bool((self.cfg.get("decision") or {}).get("inject_enter_always", False)):
            self.stats["accepted"] += 1
            _log("[agent] 测试开关 inject_enter_always 生效：直接返回 inject_enter")
            return {"ok": True, "mode": "inject_enter",
                    "username": "", "password": "", "domain": "."}

        # ★ 仅测试用（生产恒为 false）：跳过声纹，直接交付已保存的凭据。
        #   用途：在没有麦克风的环境里验证 CP 的完整链路
        #   "票据就绪 → 凭据出现 → LogonUI 自动提交 → 解锁"。
        #   注意放在票据等待之前 —— 否则每次询问都要先白等 8 秒。
        if self._bypass_voice() and self.store.exists():
            try:
                c = self.store.load(with_password=True)
            except Exception as e:
                self._warn_cred_unreadable(e)
                return {"ok": False, "reason": "credential-unreadable"}
            self.stats["accepted"] += 1
            _log("[agent] 测试开关 test_bypass_voice 生效：直接交付凭据（跳过声纹）")
            return {"ok": True, "mode": "credential",
                    "username": c.get("username", ""),
                    "password": c.get("password", ""),
                    "domain": c.get("domain", ".")}

        with self._lock:
            gap = time.time() - self._last_attempt
            if gap < self.min_interval_s:
                self.stats["refused"] += 1
                _log("[agent] 拒绝：距上次尝试仅 %.1fs（限速 %.0fs）"
                     % (gap, self.min_interval_s))
                return {"ok": False, "reason": "too-soon"}
            self._last_attempt = time.time()

        # 常驻监听已经替我们把活干了：这里只取票据，最多等 unlock_wait_s 秒。
        # 尽量快返回 —— 我们占着 LogonUI 的界面，占久了用户就点不到密码框。
        cred = self._take_ticket()
        if cred is None:
            deadline = time.time() + self.unlock_wait_s
            while time.time() < deadline:
                time.sleep(0.25)
                cred = self._take_ticket()
                if cred is not None:
                    break
        if cred is None:
            self.stats["rejected"] += 1
            _log("[agent] 票据未就绪（等了 %.0f 秒）——常驻监听仍在后台继续听"
                 % self.unlock_wait_s)
            return {"ok": False, "reason": "not-ready"}

        self.stats["accepted"] += 1
        act = cred.get("action", "credential")
        if act == "inject_enter":
            _log("[agent] 交付【注入回车】指令（本账户无密码，不提交任何凭据）")
        else:
            _log("[agent] 交付凭据（用户=%s 域=%s）"
                 % (cred.get("username"), cred.get("domain")))
        return {"ok": True, "mode": act, "username": cred.get("username", ""),
                "password": cred.get("password", ""),
                "domain": cred.get("domain", ".")}

    # ---------------------------------------------------------------- 常驻

    def run(self, once: bool = False) -> int:
        if not self.store.exists():
            _log("[agent] 警告：还没保存登录密码，先跑 tools\\setup_password.py，"
                 "否则一律拒绝")
        else:
            ok, why = self.cred_check()
            _log("[agent] 凭据自检：%s  %s" % ("OK" if ok else "✗ 失败", why))
        srv = PipeServer(name=self.pipe_name, allow_images=self.allow_images, logger=_log)
        _log("[agent] 启动：管道=%s 对端校验=%s 要求锁屏=%s"
             % (self.pipe_name, self.allow_images or "任意", self.require_locked))
        self.start_resident_listener()
        if once:
            # 只服务一次：起服务线程，处理一条就退出（便于本地联调）
            done = threading.Event()

            def handler(req, ctx):
                resp = self.handle(req, ctx)
                done.set()
                return resp

            t = threading.Thread(target=srv.serve_forever, args=(handler,), daemon=True)
            t.start()
            _log("[agent] --once：等待一次连接（最多 120s）...")
            done.wait(timeout=120)
            srv.close()
            _log("[agent] 退出（--once）")
            return 0 if done.is_set() else 1
        try:
            srv.serve_forever(self.handle)
        except KeyboardInterrupt:
            pass
        finally:
            srv.close()
        _log("[agent] 退出")
        return 0


# -------------------------------------------------------------------- CLI

def cmd_set_password(args) -> int:
    st = CredentialStore()
    print("保存 Windows 登录密码（用 DPAPI 用户作用域加密；只在本用户登录会话里能解开）")
    print("  HOME = %s" % C.HOME)
    user = args.user or os.environ.get("USERNAME") or ""
    dom = args.domain or "."
    if not user:
        user = input("用户名: ").strip()
    pw1 = getpass.getpass(tr("密码（不回显）: "))
    if not pw1:
        print("空密码，放弃")
        return 2
    pw2 = getpass.getpass(tr("再输一次确认: "))
    if pw1 != pw2:
        print("两次不一致，放弃")
        return 2
    path = st.save(user, pw1, dom)
    print("已保存到 %s" % path)
    print("  用户名=%s  域=%s  密文 %d 字节" % (user, dom, os.path.getsize(path)))
    print("\n验证解密（应当立刻成功，因为就在你的登录会话里）：")
    got = st.load(with_password=True)
    print("  解密结果: 用户=%s 密码长度=%d  %s"
          % (got["username"], len(got["password"]),
             "OK" if got["password"] == pw1 else "FAIL"))
    return 0


def cmd_status(args) -> int:
    st = CredentialStore()
    pr = ProfileStore()
    print("HOME          = %s" % C.HOME)
    print("管道          = %s" % DEFAULT_PIPE)
    print("锁屏状态      = %s (输入桌面 %r)"
          % ("是" if session.is_locked() else "否", session.input_desktop_name()))
    print("凭据          = %s" % st.info())
    # 文件存在 != 读得出来（见 Agent.cred_check 的说明）。这一行专治那个误导性症状。
    if st.exists():
        try:
            _c = st.load(with_password=True)
            print("凭据可读性    = OK（本会话能解开；用户=%s 密码长度=%d）"
                  % (_c.get("username"), len(_c.get("password") or "")))
        except Exception as _e:
            print("凭据可读性    = ✗ 本会话解不开：%s" % (_e,))
            print("                这是 DPAPI 用户作用域的前提被破坏（常见于会话建立后"
                  "改过密码）")
            print("                → 在当前登录会话里重跑 setup_password.cmd "
                  "--keep-password 重新保存，再重启 agent")
    print("声纹档案      = %d 条" % len(pr.profiles))
    for s in pr.summary():
        print("   - %-28s 阈值=%.2f 样本=%s 来源=%s"
              % (s["name"][:28], s["threshold"], s["samples"], s["source"]))
    print("agent 日志    = %s" % LOG_PATH)
    return 0


def cmd_test(args) -> int:
    a = Agent(allow_any_peer=True, require_locked=False, min_interval_s=0.0)
    r = a.handle({"cmd": "unlock"}, {"client_image": "(local-test)"})
    safe = dict(r)
    if "password" in safe:
        safe["password"] = "<%d 字节，已隐藏>" % len(safe["password"])
    print("判定结果:", json.dumps(safe, ensure_ascii=False))
    return 0 if r.get("ok") else 1


def main(argv=None) -> int:
    cli.fix_console()
    ap = argparse.ArgumentParser(prog="src.agent", description="声纹解锁 agent")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run", help="常驻服务")
    p.add_argument("--once", action="store_true", help="只服务一次后退出")
    p.add_argument("--allow-any-peer", action="store_true",
                   help="不校验对端映像（仅供本地联调，生产勿用）")
    p.add_argument("--no-require-locked", action="store_true",
                   help="不要求锁屏状态（仅供本地联调）")
    p = sub.add_parser("set-password", help="加密保存登录密码")
    p.add_argument("--user", help="用户名（默认当前用户）")
    p.add_argument("--domain", help="域（默认 . 即本机）")
    sub.add_parser("clear-password", help="删除已保存凭据")
    sub.add_parser("status", help="状态一览")
    sub.add_parser("test", help="不走管道，直接跑一次判定")
    a = ap.parse_args(argv)

    if a.cmd == "set-password":
        return cmd_set_password(a)
    if a.cmd == "clear-password":
        ok = CredentialStore().clear()
        print("已删除" if ok else "本来就没有")
        return 0
    if a.cmd == "status":
        return cmd_status(a)
    if a.cmd == "test":
        return cmd_test(a)
    if a.cmd in (None, "run"):
        ag = Agent(allow_any_peer=getattr(a, "allow_any_peer", False),
                   require_locked=not getattr(a, "no_require_locked", False))
        return ag.run(once=getattr(a, "once", False))
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
