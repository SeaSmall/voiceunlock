r"""session.py -- 判断当前是否处于锁屏状态（三条信号并用）

⚠ 这里踩过一个关键的反向坑，务必读完再改：
    我最初只用 `OpenInputDesktop` + 桌面名判断，结果**锁屏时它反而失败**：
    安全桌面 `Winlogon` 归 SYSTEM 所有，普通用户会话进程**打不开**（返回空句柄）。
    于是 is_locked() 在真正锁屏时返回 False —— 把真请求全拒了。
    正确解读是：
        OpenInputDesktop 成功且名为 "Default"  -> 未锁屏
        OpenInputDesktop 成功且名为 "Winlogon" -> 锁屏
        OpenInputDesktop **失败**              -> 安全桌面正激活（锁屏 或 UAC 提示）
    这也是 RustDesk 那个已合并 PR（#14335）为什么要改用 WTSSessionInfoEx 的原因。

所以本模块同时取三条信号：
  1. 输入桌面名（能拿到就最直观）
  2. OpenInputDesktop 是否成功（失败 = 安全桌面激活）
  3. WTS 的 WTSSessionInfoEx.SessionFlags（最权威，但 SessionFlags 的取值语义
     在 MSDN 与实测之间有"反向"的历史争议，所以只当参考、且原值照记进日志）

所有返回句柄的 Win32 函数都显式声明 restype —— 不声明会让 64 位句柄被 c_int 截断
（这个坑在 hooks.py 里真实踩过：GetModuleHandleW 未声明 restype -> err=126）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct

user32 = ctypes.WinDLL("user32", use_last_error=True)
wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2

user32.OpenInputDesktop.restype = wt.HANDLE
user32.OpenInputDesktop.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
user32.CloseDesktop.restype = wt.BOOL
user32.CloseDesktop.argtypes = (wt.HANDLE,)
user32.GetUserObjectInformationW.restype = wt.BOOL
user32.GetUserObjectInformationW.argtypes = (wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wt.DWORD, ctypes.POINTER(wt.DWORD))

WTS_CURRENT_SERVER_HANDLE = None
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTSSessionInfoEx = 26
WTSConnectState = 8

wtsapi32.WTSQuerySessionInformationW.restype = wt.BOOL
wtsapi32.WTSQuerySessionInformationW.argtypes = (
    wt.HANDLE, wt.DWORD, ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.DWORD))
wtsapi32.WTSFreeMemory.argtypes = (ctypes.c_void_p,)
kernel32.LocalFree.argtypes = (wt.HLOCAL,)


def input_desktop_name() -> str:
    """当前输入桌面名；打不开返回空串（锁屏时通常就是打不开）。"""
    h = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        need = wt.DWORD(0)
        ok = user32.GetUserObjectInformationW(h, UOI_NAME, buf,
                                              ctypes.sizeof(buf), ctypes.byref(need))
        return buf.value if ok else ""
    finally:
        user32.CloseDesktop(h)


def can_open_input_desktop() -> bool:
    """能不能打开输入桌面。锁屏/安全桌面激活时 -> False。"""
    h = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not h:
        return False
    user32.CloseDesktop(h)
    return True


def wts_connect_state():
    """WTSConnectState 的原始数值（1=Active 0=Connected ...）；拿不到返回 None。"""
    buf = ctypes.c_void_p()
    n = wt.DWORD(0)
    ok = wtsapi32.WTSQuerySessionInformationW(
        WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION, WTSConnectState,
        ctypes.byref(buf), ctypes.byref(n))
    if not ok or not buf:
        return None
    try:
        return int(struct.unpack_from("<i", ctypes.string_at(buf, n.value), 0)[0])
    finally:
        wtsapi32.WTSFreeMemory(buf)


def wts_session_flags():
    """WTSSessionInfoEx 里的 SessionFlags 原值；拿不到返回 None。

    WTSINFOEXW 的内存布局（x64，4 字节对齐）：
        offset 0  : DWORD Level
        offset 4  : (padding)
        offset 8  : ULONG SessionId
        offset 12 : int   SessionState
        offset 16 : LONG  SessionFlags   <- 我们要的
    直接按偏移解，避免为整个 WTSINFOEX_LEVEL1_W（含多个定长宽字符数组）写结构体。
    """
    buf = ctypes.c_void_p()
    n = wt.DWORD(0)
    ok = wtsapi32.WTSQuerySessionInformationW(
        WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION, WTSSessionInfoEx,
        ctypes.byref(buf), ctypes.byref(n))
    if not ok or not buf or n.value < 20:
        return None
    try:
        raw = ctypes.string_at(buf, n.value)
        return int(struct.unpack_from("<i", raw, 16)[0])
    finally:
        wtsapi32.WTSFreeMemory(buf)


def lock_signals() -> dict:
    """把三条信号一起取出来，便于记录与诊断。"""
    name = input_desktop_name()
    can_open = can_open_input_desktop()
    return {
        "desktop": name,
        "can_open_input_desktop": can_open,
        "wts_connect_state": wts_connect_state(),
        "session_flags": wts_session_flags(),
    }


def is_locked(sig: dict | None = None) -> bool:
    """锁屏判定。**以输入桌面名为准**，其余只做兜底。

    ⚠ 为什么不能靠 SessionFlags：实测在本机【未锁屏】时读到的是 0，
    而 MSDN 把 0 定义成 WTS_SESSIONSTATE_LOCK —— 与实测相反（这个"反向"
    在社区里也是长期存在的坑）。所以它只当最后的兜底信号，且按 sf==1 判锁屏。
    """
    s = sig or lock_signals()
    name = (s.get("desktop") or "").lower()
    if name == "winlogon":
        return True
    if name:
        # 能读到桌面名就最可信：读到 Default 就是没锁
        return False
    if s.get("can_open_input_desktop") is False:
        # 打不开输入桌面 = 安全桌面正激活（锁屏，或 UAC 同意提示）
        return True
    sf = s.get("session_flags")
    if sf is not None:
        return sf == 1          # 实测语义（与 MSDN 相反）
    return False


def describe() -> str:
    s = lock_signals()
    return ("锁屏=%s | 输入桌面=%r 可打开=%s WTS状态=%s SessionFlags=%s"
            % ("是" if is_locked(s) else "否", s["desktop"],
               s["can_open_input_desktop"], s["wts_connect_state"], s["session_flags"]))


if __name__ == "__main__":
    from . import cli
    cli.fix_console()
    print("当前信号 : %s" % describe())
    print()
    print("判读：")
    print("  未锁屏 -> 桌面='Default'，可打开=True，SessionFlags 大概率为 1(UNLOCK)")
    print("  已锁屏 -> 桌面=''（打不开安全桌面），可打开=False，SessionFlags 大概率为 0(LOCK)")
    print("  按 Win+L 再跑一次即可对照。")
