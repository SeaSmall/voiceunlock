"""setup_password.py -- 设置登录凭据（把逻辑放进包内，安装包要用）

★ 默认行为是【只保存凭据，不改系统密码】。
  为什么改默认：原来这个流程会顺手把 Windows 登录密码也设掉。这个副作用太隐蔽 ——
  用户只想"让声纹能用"，结果账户密码被换了。**改别人的登录方式必须显式要求**。
  要设系统密码请显式加 --set-windows-password。

★ 为什么"保存"和"设置"必须在同一个程序里做完：
  以前是"在一处设系统密码、再到另一处用 set-password 存一遍"，两处只要有一次
  不一致，症状就是"声纹判定通过、票据也交了，但锁屏登录失败(4625 密码错)"，
  而人看到的只有"声纹不灵"—— 排查了很久。现在一次输入同时完成两件事。

★ 必须在【用户的登录会话】里运行（不是 SYSTEM、不是别的用户）：
  凭据用 DPAPI 用户作用域加密，只有同一个用户的登录会话能解开；
  实测过会话建立后再改密码会让老会话解不开（CryptUnprotectData 0x8009000B）。

安全约定：
    * 密码只在内存里：不回显、不进命令行、不写日志、不落盘明文
    * 凭据库里是 DPAPI 密文，可选熵绑定
    * 只打印长度与字符类别（便于发现输入法/CapsLock 事故）
    * 最后用 LogonUserW 真登一次，把"能不能登录"这个客观事实打出来
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import getpass
import os
import sys

from .dpapi import CredentialStore
from .i18n import tr          # getpass 的提示绕开 print 直接写 stderr，得自己翻

netapi32 = ctypes.WinDLL("netapi32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class USER_INFO_1003(ctypes.Structure):
    _fields_ = [("usri1003_password", wt.LPWSTR)]


netapi32.NetUserSetInfo.argtypes = (wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                    ctypes.c_void_p, ctypes.POINTER(wt.DWORD))
netapi32.NetUserSetInfo.restype = wt.DWORD

LOGON32_LOGON_INTERACTIVE = 2
LOGON32_LOGON_NETWORK = 3
LOGON32_PROVIDER_DEFAULT = 0
advapi32.LogonUserW.argtypes = (wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, wt.DWORD,
                                ctypes.POINTER(wt.HANDLE))
advapi32.LogonUserW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = (wt.HANDLE,)

NET_API_ERRORS = {
    0: "成功", 5: "拒绝访问（需要管理员权限）", 87: "参数错误",
    1355: "找不到该用户", 2221: "找不到该用户",
    2245: "密码不满足策略要求", 2246: "密码太短",
    2247: "密码不能与上一次相同", 2248: "密码不满足复杂度要求",
}
LOGON_ERRORS = {
    1326: "ERROR_LOGON_FAILURE 用户名或密码错误",
    1327: "ERROR_ACCOUNT_RESTRICTION 账户限制（空密码只许控制台登录等）",
    1331: "ERROR_ACCOUNT_DISABLED 账户已禁用",
    1907: "密码必须修改", 1909: "账户已锁定",
}


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


def set_account_password(user: str, pw: str) -> int:
    """NetUserSetInfo(level=1003)。密码走结构体，不出现在任何命令行里。"""
    info = USER_INFO_1003(pw)
    parm_err = wt.DWORD(0)
    st = netapi32.NetUserSetInfo(None, user, 1003, ctypes.byref(info),
                                 ctypes.byref(parm_err))
    if st != 0:
        print("  NetUserSetInfo 失败：status=%d (%s)  参数下标=%d"
              % (st, NET_API_ERRORS.get(st, "未知"), parm_err.value))
    return int(st)


def try_logon(user: str, dom: str, pw: str, logon_type: int):
    tok = wt.HANDLE()
    ok = advapi32.LogonUserW(user, dom, pw, logon_type, LOGON32_PROVIDER_DEFAULT,
                             ctypes.byref(tok))
    err = 0 if ok else ctypes.get_last_error()
    if ok and tok:
        kernel32.CloseHandle(tok)
    return bool(ok), err


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="setup-password",
                                 description="保存声纹解锁要用的登录凭据")
    ap.add_argument("--user", default=os.environ.get("USERNAME") or "",
                    help="账户名（默认当前用户）")
    ap.add_argument("--domain", default=".", help="域（默认 . 即本机）")
    ap.add_argument("--set-windows-password", action="store_true",
                    help="【会改系统】同时把输入的密码设为本机登录密码；"
                         "默认只保存凭据、不动系统")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认（供安装程序用）")
    a = ap.parse_args(argv)

    user = a.user
    if not user:
        print("拿不到用户名，请用 --user 指定")
        return 2

    print("=" * 72)
    print("声纹解锁 —— 登录凭据设置")
    print("=" * 72)
    print("账户      : %s\\%s" % (a.domain, user))
    print("将要改动  : %s" % ("【会改系统】设置本机登录密码 + 写入凭据库"
                              if a.set_windows_password else
                              "只写凭据库，【不改】系统密码"))
    print()
    print("[!] 请确认：关掉 CapsLock、输入法切英文 —— 大小写与中文标点是隐形杀手")
    print("    密码只在本窗口输入，不回显、不进日志、不落盘明文")
    print()

    pw1 = getpass.getpass(tr("登录密码（不回显）: "))
    if not pw1:
        print("空密码，放弃（空密码账户在锁屏上不需要凭据，声纹解锁没有意义）")
        return 2
    pw2 = getpass.getpass(tr("再输一次确认      : "))
    if pw1 != pw2:
        print("两次不一致，放弃。系统没被改动。")
        return 2

    print()
    print("  密码长度=%d  首字符=%s  末字符=%s  含空格=%s  含非ASCII=%s"
          % (len(pw1), _cls(pw1[:1]), _cls(pw1[-1:]),
             " " in pw1, any(ord(c) > 127 for c in pw1)))
    if not a.yes:
        if input("确认按上面内容执行？输入大写 YES 继续: ").strip() != "YES":
            print("已取消，系统没被改动。")
            return 2

    if a.set_windows_password:
        print("\n[1/3] 设置本机登录密码 ...")
        if set_account_password(user, pw1) != 0:
            print("      设置失败，凭据库未改动。")
            return 1
        print("      成功。")
    else:
        print("\n[1/3] 跳过设置系统密码（默认只保存凭据）")

    print("\n[2/3] 写入凭据库（DPAPI 用户作用域）...")
    store = CredentialStore()
    try:
        path = store.save(user, pw1, a.domain)
    except Exception as e:
        print("      写入失败：%r" % (e,))
        if a.set_windows_password:
            print("      注意：系统密码此时【已经改掉了】，请记好你刚输的密码。")
        return 1
    print("      已保存到 %s（%d 字节密文）" % (path, os.path.getsize(path)))
    got = store.load(with_password=True)
    if got.get("password") != pw1:
        print("      回读校验失败：解出来跟刚存的不是同一个东西，别继续了")
        return 1
    print("      回读校验 OK（用户=%s 域=%s）" % (got.get("username"), got.get("domain")))

    print("\n[3/3] 用 Windows 自己验证这个凭据能不能登录 ...")
    net_ok, net_err = try_logon(user, a.domain, pw1, LOGON32_LOGON_NETWORK)
    itr_ok, itr_err = try_logon(user, a.domain, pw1, LOGON32_LOGON_INTERACTIVE)
    print("      网络式登录 : %s%s" % ("成功" if net_ok else "失败 ",
                                      "" if net_ok else "err=%d (%s)"
                                      % (net_err, LOGON_ERRORS.get(net_err, "未知"))))
    print("      交互式登录 : %s%s" % ("成功" if itr_ok else "失败 ",
                                      "" if itr_ok else "err=%d (%s)"
                                      % (itr_err, LOGON_ERRORS.get(itr_err, "未知"))))
    print()
    print("=" * 72)
    if itr_ok:
        print("结论：凭据可用。锁屏上的声纹解锁有资格成功了。")
        print("若锁屏仍不解锁，跑 VoiceUnlock.exe paths 看 [凭据可读性] 那一行。")
        return 0
    print("结论：连 Windows 自己都登不上，凭据不可用。")
    if itr_err == 1326:
        print("      1326 = 密码错。刚设的密码立刻登不上，通常是输入法/CapsLock，")
        print("      或者这个密码根本不是该账户当前的密码。")
    elif itr_err == 1327:
        print("      1327 = 账户限制。该账户可能是空密码 —— 用 --set-windows-password 设一个。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
