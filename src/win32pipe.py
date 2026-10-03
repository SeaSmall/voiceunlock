"""win32pipe.py -- 命名管道服务端：给 Credential Provider 递凭据

角色分工（为什么必须这样分）：
    CP 跑在 LogonUI 里，身份是 **SYSTEM**，它解不开用户作用域的 DPAPI；
    所以密码只能由**用户会话里的 agent** 解密，经命名管道交给 CP。
    CP 拿到后打包成 KERB_INTERACTIVE_UNLOCK_LOGON 还给 LogonUI。

安全设计（这是本项目唯一会传明文密码的通道，必须做对）：
  1. **不用 NULL DACL**：参考项目 `windows-caochitam` 用的是 NULL DACL，
     作者自己也提醒了。这里改用 SDDL 显式授权：SYSTEM + Administrators + 交互用户。
  2. **校验对端身份**：连上之后取客户端 PID、查它的映像路径，
     必须是我们期望的那个（默认 LogonUI.exe）；不符就断开。
  3. **只在锁屏时应答**：见 session.is_locked()。
  4. 管道名本地命名空间（`\\\\.\\pipe\\`），不做远程。

协议（单条消息，UTF-8 JSON）：
    请求  {"cmd":"unlock"}
    响应  {"ok":true,"username":"...","password":"...","domain":"..."}
     或   {"ok":false,"reason":"..."}
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import threading
import time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_MESSAGE = 0x00000004
PIPE_READMODE_MESSAGE = 0x00000002
PIPE_WAIT = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255
INVALID_HANDLE_VALUE = wt.HANDLE(-1).value
ERROR_PIPE_CONNECTED = 535
SDDL_REVISION_1 = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 授权：SY = LocalSystem（LogonUI），BA = Administrators，IU = 交互用户（我们的 agent）
DEFAULT_SDDL = "D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;IU)"

# 所有返回句柄的都要声明 restype（否则 64 位句柄被 c_int 截断 —— 踩过）
kernel32.CreateNamedPipeW.restype = wt.HANDLE
kernel32.CreateNamedPipeW.argtypes = (wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD,
                                      wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p)
kernel32.ConnectNamedPipe.restype = wt.BOOL
kernel32.ConnectNamedPipe.argtypes = (wt.HANDLE, ctypes.c_void_p)
kernel32.DisconnectNamedPipe.restype = wt.BOOL
kernel32.DisconnectNamedPipe.argtypes = (wt.HANDLE,)
kernel32.ReadFile.restype = wt.BOOL
kernel32.ReadFile.argtypes = (wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                              ctypes.POINTER(wt.DWORD), ctypes.c_void_p)
kernel32.WriteFile.restype = wt.BOOL
kernel32.WriteFile.argtypes = (wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                               ctypes.POINTER(wt.DWORD), ctypes.c_void_p)
kernel32.FlushFileBuffers.restype = wt.BOOL
kernel32.FlushFileBuffers.argtypes = (wt.HANDLE,)
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CloseHandle.argtypes = (wt.HANDLE,)
kernel32.GetNamedPipeClientProcessId.restype = wt.BOOL
kernel32.GetNamedPipeClientProcessId.argtypes = (wt.HANDLE, ctypes.POINTER(wt.ULONG))
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = (wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                                ctypes.POINTER(wt.DWORD))
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wt.BOOL
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
    wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.ULONG))


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wt.BOOL)]


# ---- 客户端侧（测试用；也是 CP 那侧协议的 Python 镜像）----
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
ERROR_PIPE_BUSY = 231
ERROR_FILE_NOT_FOUND = 2
kernel32.CreateFileW.restype = wt.HANDLE
kernel32.CreateFileW.argtypes = (wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, wt.HANDLE)
kernel32.WaitNamedPipeW.restype = wt.BOOL
kernel32.WaitNamedPipeW.argtypes = (wt.LPCWSTR, wt.DWORD)
kernel32.SetNamedPipeHandleState.restype = wt.BOOL
kernel32.SetNamedPipeHandleState.argtypes = (wt.HANDLE, ctypes.POINTER(wt.DWORD),
                                             ctypes.c_void_p, ctypes.c_void_p)


def pipe_call(name: str, request: dict, timeout_ms: int = 15000) -> dict:
    """连管道 -> 发一条 JSON -> 读一条 JSON。失败也返回 dict（带 reason）。"""
    import time
    deadline = time.time() + timeout_ms / 1000.0
    h = None
    while True:
        h = kernel32.CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, None,
                                 OPEN_EXISTING, 0, None)
        if h and h != INVALID_HANDLE_VALUE:
            break
        err = ctypes.get_last_error()
        if time.time() >= deadline:
            return {"ok": False, "reason": "pipe-timeout(err=%d)" % err}
        if err == ERROR_PIPE_BUSY:
            kernel32.WaitNamedPipeW(name, 500)
        elif err == ERROR_FILE_NOT_FOUND:
            time.sleep(0.2)
        else:
            return {"ok": False, "reason": "pipe-error-%d" % err}
    try:
        mode = wt.DWORD(PIPE_READMODE_MESSAGE)
        kernel32.SetNamedPipeHandleState(h, ctypes.byref(mode), None, None)
        body = json.dumps(request, ensure_ascii=False).encode("utf-8")
        wbuf = ctypes.create_string_buffer(body, len(body))
        wrote = wt.DWORD(0)
        if not kernel32.WriteFile(h, wbuf, len(body), ctypes.byref(wrote), None):
            return {"ok": False, "reason": "write-failed(err=%d)" % ctypes.get_last_error()}
        rbuf = ctypes.create_string_buffer(65536)
        got = wt.DWORD(0)
        if not kernel32.ReadFile(h, rbuf, 65536, ctypes.byref(got), None):
            return {"ok": False, "reason": "read-failed(err=%d)" % ctypes.get_last_error()}
        return json.loads(rbuf.raw[:got.value].decode("utf-8"))
    except Exception as e:
        return {"ok": False, "reason": "client-exception: %r" % (e,)}
    finally:
        kernel32.CloseHandle(h)


def _make_sa(sddl: str):
    psd = ctypes.c_void_p()
    ok = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, SDDL_REVISION_1, ctypes.byref(psd), None)
    if not ok:
        raise OSError("解析 SDDL 失败: err=%d" % ctypes.get_last_error())
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(sa)
    sa.lpSecurityDescriptor = psd
    sa.bInheritHandle = False
    return sa, psd


def client_image_path(pipe_handle) -> str:
    """取对端进程的完整映像路径；拿不到返回空串。"""
    pid = wt.ULONG(0)
    if not kernel32.GetNamedPipeClientProcessId(pipe_handle, ctypes.byref(pid)):
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        kernel32.CloseHandle(h)


class PipeServer:
    """一次一个连接的管道服务端（够用：锁屏上同时只会有一个解锁流程）。

    handler(request: dict, ctx: dict) -> dict
        ctx 里有 client_image / peer 信息，便于 handler 记录与判定。
    """

    def __init__(self, name: str = r"\\.\pipe\VoiceUnlock", sddl: str = DEFAULT_SDDL,
                 allow_images=("logonui.exe",), logger=print):
        self.name = name
        self.sddl = sddl
        self.allow_images = tuple(x.lower() for x in (allow_images or ()))
        self.log = logger
        self._h = None
        self._stop = threading.Event()
        self.stats = {"conn": 0, "rejected_peer": 0, "handled": 0, "errors": 0}
        self._rej_logged = 0

    def _log_peer_reject(self, msg: str) -> None:
        """拒绝对端的日志要做**限流**。

        踩过：agent 权限不对时，锁屏每 30 秒能打出几百行"拒绝对端"，
        agent.log 被这一种错误刷满，真正的线索（分数/阈值/票据）全被淹掉。
        规则：前 3 次全记，之后每 50 次记一次并带上累计数。
        """
        n = self.stats["rejected_peer"]
        self._rej_logged += 1
        if n <= 3 or n % 50 == 0:
            self.log("%s（累计拒绝 %d 次%s）"
                     % (msg, n, "，后续每 50 次记一次" if n == 3 else ""))

    # ---------------------------------------------------------------- 生命周期

    def create(self) -> None:
        sa, self._psd = _make_sa(self.sddl)
        h = kernel32.CreateNamedPipeW(
            self.name, PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
            PIPE_UNLIMITED_INSTANCES, 65536, 65536, 0, ctypes.byref(sa))
        if not h or h == INVALID_HANDLE_VALUE:
            raise OSError("CreateNamedPipeW 失败: err=%d" % ctypes.get_last_error())
        self._h = h
        self.log("[pipe] 已创建 %s（DACL=%s）" % (self.name, self.sddl))

    def close(self) -> None:
        """关服务端。

        ⚠ 不能直接 CloseHandle —— 服务线程很可能正阻塞在 ConnectNamedPipe 上等着
        下一个连接，此时关掉句柄会让进程挂住（实测挂死过 75 秒以上）。
        先用一次"哑连接"把它唤醒，让循环看到 _stop 而干净退出。
        """
        self._stop.set()
        try:
            h = kernel32.CreateFileW(self.name, GENERIC_READ | GENERIC_WRITE, 0, None,
                                     OPEN_EXISTING, 0, None)
            if h and h != INVALID_HANDLE_VALUE:
                kernel32.CloseHandle(h)
        except Exception:
            pass
        time.sleep(0.25)
        if self._h:
            try:
                kernel32.CloseHandle(self._h)
            except Exception:
                pass
            self._h = None

    # ---------------------------------------------------------------- 收发

    def _read_message(self) -> bytes:
        buf = ctypes.create_string_buffer(65536)
        got = wt.DWORD(0)
        ok = kernel32.ReadFile(self._h, buf, 65536, ctypes.byref(got), None)
        self.log("[pipe] ReadFile -> %s err=%d 收到 %d 字节"
                 % (bool(ok), ctypes.get_last_error(), got.value))
        if not ok:
            return b""
        return buf.raw[:got.value]

    def _write_message(self, data: bytes) -> None:
        wrote = wt.DWORD(0)
        kernel32.WriteFile(self._h, data, len(data), ctypes.byref(wrote), None)
        kernel32.FlushFileBuffers(self._h)

    def serve_forever(self, handler) -> None:
        """阻塞循环：等连接 -> 校验对端 -> 交给 handler -> 回写 -> 断开。"""
        self.create()
        try:
            while not self._stop.is_set():
                okc = kernel32.ConnectNamedPipe(self._h, None)
                err = ctypes.get_last_error()
                self.log("[pipe] ConnectNamedPipe -> %s err=%d h=%r"
                         % (bool(okc), err, self._h))
                if not okc and err != ERROR_PIPE_CONNECTED:   # 已连上也算成功
                    self.log("[pipe] ConnectNamedPipe 失败 err=%d" % err)
                    break
                if self._stop.is_set():
                    break
                self.stats["conn"] += 1
                try:
                    img = client_image_path(self._h)
                    base = img.rsplit("\\", 1)[-1].lower() if img else ""
                    if self.allow_images and base not in self.allow_images:
                        self.stats["rejected_peer"] += 1
                        # ★ 取不到映像时把"最可能的原因"直接写进日志。
                        #   踩过：agent 以普通权限运行时 OpenProcess(SYSTEM 进程) 被拒
                        #   （err=5），映像名取不到，于是**所有**锁屏请求都被判
                        #   peer-not-allowed，日志里就是几百行"映像='?'" —— 光看这行
                        #   根本猜不到是权限问题。修法见 install.py 的自启计划任务
                        #   （RunLevel=HighestAvailable）。
                        if not img:
                            self._log_peer_reject(
                                "[pipe] 拒绝对端：**取不到对端映像**（OpenProcess 被拒）。"
                                "最可能的原因：agent 不是以管理员身份运行 —— "
                                "自启必须是【计划任务 + 最高权限】，"
                                "HKCU Run 那种普通权限启动做不了对端校验。")
                        else:
                            self._log_peer_reject(
                                "[pipe] 拒绝对端（映像=%r，期望 %s）"
                                % (img, self.allow_images))
                        self._write_message(json.dumps(
                            {"ok": False, "reason": "peer-not-allowed"},
                            ensure_ascii=False).encode("utf-8"))
                    else:
                        req_raw = self._read_message()
                        try:
                            req = json.loads(req_raw.decode("utf-8"))
                        except Exception:
                            req = {"cmd": ""}
                        ctx = {"client_image": img}
                        try:
                            resp = handler(req, ctx) or {"ok": False, "reason": "empty"}
                        except Exception as e:
                            self.stats["errors"] += 1
                            self.log("[pipe] handler 异常: %r" % (e,))
                            resp = {"ok": False, "reason": "handler-error"}
                        self.stats["handled"] += 1
                        self._write_message(json.dumps(resp, ensure_ascii=False)
                                            .encode("utf-8"))
                except Exception as e:
                    # 单次连接出错绝不能把服务线程打死 —— 日志记下继续等下一个
                    self.stats["errors"] += 1
                    self.log("[pipe] 本次连接处理异常: %r" % (e,))
                    import traceback
                    traceback.print_exc()
                finally:
                    kernel32.DisconnectNamedPipe(self._h)
        finally:
            self.close()
