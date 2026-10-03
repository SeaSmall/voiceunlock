"""hooks.py -- 【已弃用 / 未验证】低级键鼠钩子：拦物理输入，放行远程工具的注入输入

⚠️ 状态：**用户明确说"不需要吞输入"，本模块已退役，不接线、未验证。**
   实测装钩子失败：`SetWindowsHookExW` 返回 err=126 (ERROR_MOD_NOT_FOUND)，
   根因是本文件里 `GetModuleHandleW` 没有声明 restype，64 位 HMODULE 被默认的
   `c_int` 截断成垃圾句柄。**未修复，因为功能已不需要。**
   若将来要用，先加：
       kernel32.GetModuleHandleW.restype = wt.HMODULE
       kernel32.GetModuleHandleW.argtypes = (wt.LPCWSTR,)
   然后按本文件顶部说的流程（先 dry_run 演练、再真拦）验证。

保留它的唯一价值：记录"拦物理/放行注入"这条判据的完整实现与安全底线，
以及为什么最终没有采用（见方案 §12.10）。

--- 原设计说明（保留）---

守夜模式的核心机制，判据只有一条：

    物理输入（LLKHF_INJECTED / LLMHF_INJECTED 未置位） -> 拦截
    注入输入（这两个标志置位）                          -> 放行

不需要识别是哪个远程工具，也不需要判断"是否真有人在远程操作" ——
**看到注入事件，就说明有人在远程动它。**

★★ 安全底线（这个东西出错会把人锁在键盘外面，必须读）★★
  1. **只拦物理输入**：注入输入一律放行，所以远程工具永远能操作。
  2. **回调里任何异常都当"放行"处理** —— 绝不能因为异常而吞掉事件。
  3. **失败即放行（fail-open）**：`fail_open()` 一调，立刻停止拦截、全部放行。
     看门狗、救援热键、标记文件，走的都是它。
  4. **进程退出即自动卸载钩子**（Windows 行为），所以进程崩了输入会自己恢复。
  5. 默认带 `dry_run`：只统计不拦截，用来验证钩子确实生效。

为什么不用 BlockInput()：那个 API 一旦进程崩溃会把整机输入锁死，只能重启。
低级钩子随进程消失，天然安全得多。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01
WM_QUIT = 0x0012
PM_REMOVE = 0x0001
VK_CONTROL, VK_SHIFT, VK_MENU, VK_Q = 0x11, 0x10, 0x12, 0x51

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.CallNextHookEx.argtypes = (wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.UnhookWindowsHookEx.argtypes = (wt.HHOOK,)
user32.PeekMessageW.argtypes = (ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
user32.PostThreadMessageW.argtypes = (wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
kernel32.GetCurrentThreadId.restype = wt.DWORD


class InputBlocker:
    """拦物理键鼠、放行注入键鼠。线程安全，可随时 fail_open。"""

    def __init__(self, dry_run: bool = True, pass_injected: bool = True,
                 rescue_vks=(VK_CONTROL, VK_SHIFT, VK_MENU, VK_Q),
                 rescue_hold_s: float = 3.0, on_rescue=None, logger=print,
                 on_event=None):
        self.dry_run = bool(dry_run)
        self.pass_injected = bool(pass_injected)
        self.rescue_vks = tuple(rescue_vks)
        self.rescue_hold_s = float(rescue_hold_s)
        self.on_rescue = on_rescue
        self.log = logger
        self.on_event = on_event      # on_event(kind, injected, info) 用于排查/日志

        self.blocking = False          # True 时真的拦物理输入
        self._down = set()             # 当前按下的救援键
        self._rescue_since = 0.0
        self._rescue_fired = False

        self.stats = {"kb_phys": 0, "kb_inj": 0, "ms_phys": 0, "ms_inj": 0,
                      "blocked": 0, "passed": 0, "errors": 0}
        self.heartbeat = time.time()   # 消息循环心跳（给看门狗看）
        self.last_event = time.time()

        self._thread = None
        self._tid = 0
        self._hkb = None
        self._hms = None
        # 必须保住回调对象的引用，否则会被 GC 掉 -> 崩溃
        self._kb_proc = HOOKPROC(self._keyboard_cb)
        self._ms_proc = HOOKPROC(self._mouse_cb)

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> bool:
        """在专用线程里装钩子并跑消息循环（低级钩子要求安装线程泵消息）。"""
        if self._thread and self._thread.is_alive():
            return True
        self._thread = threading.Thread(target=self._run, name="vu-hooks", daemon=True)
        self._thread.start()
        for _ in range(100):                       # 等钩子装好（最多 2 秒）
            if self._hkb or self._hms:
                return True
            time.sleep(0.02)
        return bool(self._hkb or self._hms)

    def stop(self) -> None:
        self.blocking = False
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=3.0)
        self._unhook()

    def fail_open(self, why: str = "") -> None:
        """立刻停止拦截、全部放行。看门狗/救援/标记文件都调它。"""
        if self.blocking:
            self.blocking = False
            self.log("[hooks] 已转为全部放行（fail-open）%s" % (("： " + why) if why else ""))

    def _unhook(self) -> None:
        for h in (self._hkb, self._hms):
            if h:
                try:
                    user32.UnhookWindowsHookEx(h)
                except Exception:
                    pass
        self._hkb = self._hms = None

    def _run(self) -> None:
        self._tid = int(kernel32.GetCurrentThreadId())
        hmod = kernel32.GetModuleHandleW(None)
        try:
            self._hkb = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, hmod, 0)
            self._hms = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, hmod, 0)
        except Exception as e:
            self.log("[hooks] 安装钩子失败: %s" % e)
            return
        if not self._hkb or not self._hms:
            self.log("[hooks] 安装钩子失败: err=%d" % ctypes.get_last_error())
            return
        self.log("[hooks] 已安装 键盘=%s 鼠标=%s  模式=%s"
                 % (bool(self._hkb), bool(self._hms),
                    "演练(只统计)" if self.dry_run else "拦截物理输入"))

        msg = wt.MSG()
        while True:
            self.heartbeat = time.time()
            got = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE)
            if got:
                if msg.message == WM_QUIT:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.01)
        self._unhook()
        self.log("[hooks] 已卸载钩子")

    # ---------------------------------------------------------------- 判定

    def _decide(self, kind: str, injected: bool, info: dict | None = None) -> bool:
        """返回 True = 拦截（吞掉），False = 放行。"""
        self.heartbeat = time.time()
        self.last_event = time.time()
        self.stats["kb_phys" if kind == "kb" else "ms_phys"] += (0 if injected else 1)
        self.stats["kb_inj" if kind == "kb" else "ms_inj"] += (1 if injected else 0)
        if self.on_event:
            try:
                self.on_event(kind, injected, info or {})
            except Exception:
                self.stats["errors"] += 1

        if injected and self.pass_injected:
            self.stats["passed"] += 1
            return False                      # 注入输入一律放行
        if not self.blocking or self.dry_run:
            self.stats["passed"] += 1
            return False
        self.stats["blocked"] += 1
        return True

    def _rescue_tick(self, vk: int, down: bool) -> bool:
        """救援热键：连按住的救援键达到设定时长 -> 放行并触发回调。"""
        if vk not in self.rescue_vks:
            return False
        if down:
            self._down.add(vk)
            if len(self._down) >= len(self.rescue_vks):
                if not self._rescue_since:
                    self._rescue_since = time.time()
                elif (time.time() - self._rescue_since) >= self.rescue_hold_s:
                    if not self._rescue_fired:
                        self._rescue_fired = True
                        self.log("[hooks] 救援热键触发（按住 %.1fs）" % self.rescue_hold_s)
                        self.fail_open("救援热键")
                        if self.on_rescue:
                            try:
                                self.on_rescue()
                            except Exception:
                                pass
        else:
            self._down.discard(vk)
            self._rescue_since = 0.0
            self._rescue_fired = False
        return False

    # ---------------------------------------------------------------- 回调

    def _keyboard_cb(self, ncode, wparam, lparam):
        try:
            if ncode == HC_ACTION:
                kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                injected = bool(kb.flags & LLKHF_INJECTED)
                down = bool(wparam in (0x0100, 0x0104))       # WM_KEYDOWN / WM_SYSKEYDOWN
                if self._rescue_tick(int(kb.vkCode), down):
                    return 1
                if self._decide("kb", injected, {"vk": int(kb.vkCode), "down": down}):
                    return 1
        except Exception:
            # 回调里出错绝不能吞事件：放行
            self.stats["errors"] += 1
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def _mouse_cb(self, ncode, wparam, lparam):
        try:
            if ncode == HC_ACTION:
                ms = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                injected = bool(ms.flags & LLMHF_INJECTED)
                if self._decide("ms", injected, {"flags": int(ms.flags)}):
                    return 1
        except Exception:
            self.stats["errors"] += 1
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def summary(self) -> str:
        s = self.stats
        return ("物理键盘 %d / 注入键盘 %d | 物理鼠标 %d / 注入鼠标 %d | "
                "已拦 %d / 已放行 %d | 异常 %d"
                % (s["kb_phys"], s["kb_inj"], s["ms_phys"], s["ms_inj"],
                   s["blocked"], s["passed"], s["errors"]))
