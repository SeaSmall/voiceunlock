"""check_credential.py -- 用 Windows 自己验证保存的凭据，定位问题在哪一段

为什么要这个：
    锁屏登录失败（事件 4625，子状态 0xC000006A = 密码错），但声纹识别是通过的。
    问题只可能在两段：
      A. 存的时候就不对（DPAPI 里解出来的密码本身错）
      B. 传输时被搞坏（Python 发 JSON -> 管道 -> C++ 的朴素 JSON 解析器）
    区分方法：在本机用 LogonUser 拿【解密出来的密码】去登录。
      * LogonUser 失败 -> A：存的就是错的
      * LogonUser 成功 -> B：传输出问题（最可能是 C++ 的 ExtractString 不处理 JSON 转义：
        json.dumps 会把 " 转成 \" 、把 \\ 转成 \\\\，而朴素解析器不反转义 -> 密码被截断）
    全程不打印密码本身。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import cli                       # noqa: E402
from src.dpapi import CredentialStore     # noqa: E402

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LOGON32_LOGON_INTERACTIVE = 2       # 与锁屏那条路径最接近（CP 交的就是交互式解锁凭据）
LOGON32_LOGON_NETWORK = 3           # 传统"验证凭据"用法；空密码账户会被策略挡成 1327
LOGON32_PROVIDER_DEFAULT = 0
ERROR_LOGON_FAILURE = 1326          # 用户名或密码错误
ERROR_ACCOUNT_RESTRICTION = 1327    # 空密码账户的网络登录被 LimitBlankPasswordUse 挡下
advapi32.LogonUserW.restype = wt.BOOL
advapi32.LogonUserW.argtypes = (wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, wt.DWORD,
                                ctypes.POINTER(wt.HANDLE))
kernel32.CloseHandle.argtypes = (wt.HANDLE,)


def main() -> int:
    cli.fix_console()
    st = CredentialStore()
    if not st.exists():
        print("还没有保存凭据（先跑 python -m src.agent set-password）")
        return 2
    cred = st.load(with_password=True)
    user, dom, pw = cred["username"], cred["domain"], cred["password"]
    print("已保存凭据：用户=%s  域=%s  密码长度=%d" % (user, dom, len(pw)))
    # 只报"形状"，绝不打印内容 —— 用来判断有没有被截断/转义
    print("  密码首字符类别=%s  末字符类别=%s"
          % (_cls(pw[:1]), _cls(pw[-1:])))
    print("  含英文双引号=%s  含反斜杠=%s  含非ASCII=%s  含空格=%s"
          % ('"' in pw, "\\" in pw, any(ord(c) > 127 for c in pw), " " in pw))

    results = {}
    print()
    for label, lt in (("交互式（锁屏路径）", LOGON32_LOGON_INTERACTIVE),
                      ("网络式（传统验证）", LOGON32_LOGON_NETWORK)):
        tok = wt.HANDLE()
        ok = advapi32.LogonUserW(user, dom, pw, lt, LOGON32_PROVIDER_DEFAULT,
                                 ctypes.byref(tok))
        err = 0 if ok else ctypes.get_last_error()
        if ok and tok:
            kernel32.CloseHandle(tok)
        results[lt] = (bool(ok), err)
        print("  %-20s: %s%s" % (label, "成功" if ok else "失败 ",
                                 "" if ok else "err=%d (%s)" % (err, _errname(err))))

    ok_i, err_i = results[LOGON32_LOGON_INTERACTIVE]
    ok_n, err_n = results[LOGON32_LOGON_NETWORK]
    print()
    if ok_i:
        print("LogonUser: 成功  ->  保存的密码本身是对的，账户真能登上")
        if not ok_n:
            print("  （网络式失败 err=%d 不影响结论：空密码账户的网络登录本来就会被"
                  " LimitBlankPasswordUse 挡下）" % err_n)
        print("  结论：凭据这一段是好的。若锁屏仍失败，问题在【传输/解析】那一段")
        print("        （见方案：C++ ExtractString 不处理 JSON 转义）")
        return 0
    print("LogonUser: 失败  err=%d (%s)" % (err_i, _errname(err_i)))
    if err_i == ERROR_LOGON_FAILURE:
        print("  结论：保存的密码本身就错 —— 重新跑 set-password，注意大小写与输入法")
        print("        （全程不回显，请确认没有误触 CapsLock / 中文输入法）")
        print("        更稳的做法：用 tools\\setup_password.py，一次输入同时设系统密码+存库")
    elif err_i == ERROR_ACCOUNT_RESTRICTION:
        print("  结论：账户【空密码】。空密码账户在锁屏上不需要任何密码，")
        print("        声纹解锁没有凭据可交付 —— 先用 tools\\setup_password.py 设一个真密码。")
    else:
        print("  结论：不是密码错，而是别的失败（账户被禁/域格式等），看 err 含义")
    return 1


def _cls(s: str) -> str:
    if not s:
        return "空"
    c = s[0]
    if c.isdigit():
        return "数字"
    if c.isascii() and c.isalpha():
        return "字母"
    if c == " ":
        return "空格"
    if ord(c) > 127:
        return "非ASCII"
    return "符号"


def _errname(e: int) -> str:
    return {1326: "ERROR_LOGON_FAILURE 用户名或密码错误",
            1327: "ERROR_ACCOUNT_RESTRICTION",
            1331: "ERROR_ACCOUNT_DISABLED",
            1907: "密码必须修改"}.get(e, "未知")


if __name__ == "__main__":
    sys.exit(main())
