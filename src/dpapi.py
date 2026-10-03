"""dpapi.py -- 用 DPAPI（用户作用域）加密保存 Windows 登录密码

为什么必须有它
    Credential Provider 的 `GetSerialization` 必须交出一个**可用的凭据** ——
    也就是密码本身。所以密码得存在某处，而它绝不能是明文。

为什么用【用户作用域】而不是机器作用域
    机器作用域（CRYPTPROTECT_LOCAL_MACHINE）意味着任何 SYSTEM 进程都能解开它；
    用户作用域只有该用户的登录会话能解 —— 而我们的 agent 恰好就跑在该用户会话里。
    这也解释了为什么 **CP（SYSTEM）不碰 DPAPI**：它解不了，也不该解。
    交付路径：agent 解密 -> 命名管道 -> CP 打包 -> LogonUI。

安全边界（要诚实写清楚）
    用户作用域 DPAPI 的强度 = 该用户登录会话主密钥的强度。任何**以该用户身份**
    运行的代码都能解开（这正是 agent 能解密的原因）。所以：
      * 它防的是"拿到磁盘文件的人"和"别的用户"
      * 它不防"已经能以你身份执行代码的人" —— 那种情况下对方本来就能读内存
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import tempfile

from . import config as C


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

crypt32.CryptProtectData.restype = wt.BOOL
crypt32.CryptProtectData.argtypes = (
    ctypes.POINTER(DATA_BLOB), wt.LPCWSTR, ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(DATA_BLOB))
crypt32.CryptUnprotectData.restype = wt.BOOL
crypt32.CryptUnprotectData.argtypes = (
    ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wt.LPWSTR), ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(DATA_BLOB))
kernel32.LocalFree.argtypes = (wt.HLOCAL,)

CRYPTPROTECT_UI_FORBIDDEN = 0x01
# 可选熵：让"仅复制 credential.bin 到别的账户"也不够用
ENTROPY = b"VoiceUnlock/v1/credential"


def _blob(data: bytes) -> DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _take(blob: DATA_BLOB) -> bytes:
    out = ctypes.string_at(blob.pbData, blob.cbData)
    kernel32.LocalFree(blob.pbData)
    return out


def protect(data: bytes, entropy: bytes = ENTROPY) -> bytes:
    """加密（DPAPI 用户作用域）。"""
    pin, pent = _blob(data), _blob(entropy)
    pout = DATA_BLOB()
    ok = crypt32.CryptProtectData(ctypes.byref(pin), None, ctypes.byref(pent),
                                  None, None, CRYPTPROTECT_UI_FORBIDDEN,
                                  ctypes.byref(pout))
    if not ok:
        raise OSError("CryptProtectData 失败: err=%d" % ctypes.get_last_error())
    return _take(pout)


def unprotect(blob: bytes, entropy: bytes = ENTROPY) -> bytes:
    """解密。**只能在该用户的登录会话里成功** —— 这正是设计意图。"""
    pin, pent = _blob(blob), _blob(entropy)
    pout = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(ctypes.byref(pin), None, ctypes.byref(pent),
                                    None, None, CRYPTPROTECT_UI_FORBIDDEN,
                                    ctypes.byref(pout))
    if not ok:
        raise OSError("CryptUnprotectData 失败: err=%d（换用户/换机器都解不开，这是正常的）"
                      % ctypes.get_last_error())
    return _take(pout)


class CredentialStore:
    """保存 {username, domain, password_enc} 到 HOME/credential.bin（原子写）。"""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(C.HOME, "credential.bin")

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def save(self, username: str, password: str, domain: str = ".") -> str:
        payload = {
            "version": 1,
            "username": username,
            "domain": domain or ".",
            "password_enc": protect(password.encode("utf-8")).hex(),
        }
        return self._write(payload)

    def save_passwordless(self, username: str, domain: str = ".") -> str:
        """记录"本机账户没有密码"这种状态 —— 不保存任何密码。

        为什么需要单独一种状态（而不是存一个空字符串）：
          账户无密码时，**不能**让 CP 去"提交空密码"：LSA 会以 1327
          （空密码限制）拒绝，而且反复提交可能触发账户锁定策略。
          正确做法是走 Windows 自己的"按回车即登录"路径（见 CP 的注入回车）。
          这里只存一个标记，`load(with_password=True)["password"]` 返回空串，
          上层据此选择"注入回车"而不是"提交凭据"。
        """
        payload = {
            "version": 1,
            "username": username,
            "domain": domain or ".",
            "passwordless": True,
        }
        return self._write(payload)

    def _write(self, payload: dict) -> str:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path),
                                   prefix=".cred-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return self.path

    def load(self, with_password: bool = False) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            d = json.load(f)
        out = {"username": d.get("username", ""), "domain": d.get("domain", ".")}
        if with_password:
            if d.get("passwordless"):
                out["password"] = ""
                out["passwordless"] = True
            else:
                out["password"] = unprotect(bytes.fromhex(d["password_enc"])).decode("utf-8")
        return out

    def clear(self) -> bool:
        try:
            os.remove(self.path)
            return True
        except OSError:
            return False

    def info(self) -> str:
        if not self.exists():
            return "未保存任何凭据"
        d = self.load(with_password=False)
        return ("已保存凭据: 用户=%s 域=%s（密码为 DPAPI 用户作用域密文，%d 字节）"
                % (d["username"], d["domain"], os.path.getsize(self.path)))


if __name__ == "__main__":
    from . import cli
    cli.fix_console()
    print("凭据文件:", os.path.join(C.HOME, "credential.bin"))
    st = CredentialStore()
    print(st.info())
    # 自测：加解密往返（不落盘）
    probe = "测试密码-abc123!@#"
    enc = protect(probe.encode("utf-8"))
    dec = unprotect(enc).decode("utf-8")
    print("DPAPI 往返:", "OK" if dec == probe else "FAIL", "（密文 %d 字节）" % len(enc))
    # 负例：换一个熵应当解不开
    try:
        unprotect(enc, entropy=b"wrong-entropy")
        print("错误熵竟然解开了: FAIL")
    except OSError:
        print("错误熵解不开: OK")
