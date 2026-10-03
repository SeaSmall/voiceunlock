# -*- coding: utf-8 -*-
"""决定性问题：本机当前账户到底是不是"空密码"？

check_credential.py 用 LOGON32_LOGON_NETWORK 测空密码得到 1327
(ERROR_ACCOUNT_RESTRICTION) —— 那是 LimitBlankPasswordUse=1 挡住网络登录，
恰恰说明"空密码被当成正确密码"。本脚本改用【交互式登录】(LOGON32_LOGON_INTERACTIVE)
再测同一件事：交互式登录不受 LimitBlankPasswordUse 约束。

  * 空密码 + 交互式 => 成功   ==> 账户确实是空密码（锁屏按回车即进）
  * 空密码 + 交互式 => 1326   ==> 账户有密码，先前 1327 另有原因

只做 LogonUserW，不落盘、不打印任何口令、立即 CloseHandle。
"""
import ctypes
from ctypes import wintypes
import getpass
import sys

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

advapi32.LogonUserW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
    wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
]
advapi32.LogonUserW.restype = wintypes.BOOL

LOGON32_LOGON_INTERACTIVE = 2
LOGON32_LOGON_NETWORK = 3
LOGON32_PROVIDER_DEFAULT = 0

ERR_BAD_PASSWORD = 1326          # ERROR_LOGON_FAILURE
ERR_ACCOUNT_RESTRICTION = 1327   # ERROR_ACCOUNT_RESTRICTION


def try_logon(user, domain, password, logon_type):
    tok = wintypes.HANDLE()
    ok = advapi32.LogonUserW(user, domain, password, logon_type,
                             LOGON32_PROVIDER_DEFAULT, ctypes.byref(tok))
    err = 0 if ok else ctypes.get_last_error()
    if ok and tok:
        kernel32.CloseHandle(tok)
    return bool(ok), err


def main():
    user = getpass.getuser()
    domain = "."
    print("== 交互式登录 (LOGON32_LOGON_INTERACTIVE=2) ==")
    print("   不受 LimitBlankPasswordUse 限制")
    print()

    ok, err = try_logon(user, domain, "", LOGON32_LOGON_INTERACTIVE)
    print("  空密码        : %s  err=%d" % ("成功 <<<" if ok else "失败", err))

    ok2, err2 = try_logon(user, domain, "", LOGON32_LOGON_NETWORK)
    print("  空密码(网络)  : %s  err=%d" % ("成功" if ok2 else "失败", err2))

    print()
    if ok:
        print("  判定: 账户【是空密码】—— 交互式登录用空密码就成功；")
        print("        锁屏只需按回车即可进入，声纹 CP 没有可用凭据可交付。")
        if not ok2:
            print("        (网络登录被 LimitBlankPasswordUse 挡下 -> 1327，与之前观察一致)")
    elif err == ERR_BAD_PASSWORD:
        print("  判定: 账户【有密码】(空密码被判为密码错 1326)；")
        print("        那 1327 另有原因，需要进一步查。")
    else:
        print("  判定: 不确定 (err=%d，既非成功也非 1326)" % err)
        if err == ERR_ACCOUNT_RESTRICTION:
            print("        1327 在此路径下也可能是其它账户限制，需另查。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
