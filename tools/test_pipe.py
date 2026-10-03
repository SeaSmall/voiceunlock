"""test_pipe.py -- 实测命名管道：服务端 + 客户端 + DACL + 对端校验

要验四件事：
  1. 管道能建起来（含 SDDL 解析成功 —— 写错 SDDL 会直接失败）
  2. 消息模式下一来一回能通（消息边界、FlushFileBuffers）
  3. 对端映像校验：期望 LogonUI.exe 时，非 LogonUI 的连接会被拒
  4. 客户端侧 pipe_call 的失败路径（服务端不存在时能快速返回而不是挂死）

用法：
    venv\\Scripts\\python.exe tools\\test_pipe.py
"""
from __future__ import annotations

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import cli                       # noqa: E402
from src import session                    # noqa: E402
from src.win32pipe import PipeServer, pipe_call, client_image_path   # noqa: E402

FAILS = []
PIPE = r"\\.\pipe\VoiceUnlockTest"


def check(label, ok, detail=""):
    print("  [%s] %-46s %s" % ("OK" if ok else "FAIL", label, detail))
    if not ok:
        FAILS.append(label)


def main() -> int:
    cli.fix_console()
    print("=" * 80)
    print("0. 当前输入桌面（锁屏检测）")
    print("=" * 80)
    name = session.input_desktop_name()
    print("  输入桌面 = %r   锁屏 = %s" % (name, session.is_locked()))
    check("能读到输入桌面名", bool(name), name)

    print("\n" + "=" * 80)
    print("1+2. 管道一来一回（对端校验先关掉，因为测试客户端不是 LogonUI）")
    print("=" * 80)
    srv = PipeServer(name=PIPE, allow_images=(), logger=lambda m: print("   " + m))
    got = {}

    def handler(req, ctx):
        got["req"] = req
        got["img"] = ctx.get("client_image", "")
        return {"ok": True, "username": "Tester", "password": "pw-\u4e2d\u6587-123",
                "domain": ".", "echo": req.get("cmd")}

    t = threading.Thread(target=srv.serve_forever, args=(handler,), daemon=True)
    t.start()
    time.sleep(0.4)
    r = pipe_call(PIPE, {"cmd": "unlock"}, timeout_ms=8000)
    check("客户端收到响应", bool(r), str(r)[:70])
    check("ok=true", r.get("ok") is True)
    check("中文密码原样往返", r.get("password") == "pw-\u4e2d\u6587-123",
          repr(r.get("password")))
    check("服务端收到 cmd=unlock", got.get("req", {}).get("cmd") == "unlock")
    check("服务端能取到对端映像路径", bool(got.get("img")),
          os.path.basename(got.get("img", "")) or "(空)")

    print("\n" + "=" * 80)
    print("3. 对端校验：期望 LogonUI.exe，本测试客户端应当被拒")
    print("=" * 80)
    srv2 = PipeServer(name=PIPE + "2", allow_images=("logonui.exe",),
                      logger=lambda m: print("   " + m))
    t2 = threading.Thread(target=srv2.serve_forever,
                          args=(lambda req, ctx: {"ok": True},), daemon=True)
    t2.start()
    time.sleep(0.4)
    r2 = pipe_call(PIPE + "2", {"cmd": "unlock"}, timeout_ms=8000)
    check("被拒（reason=peer-not-allowed）", r2.get("reason") == "peer-not-allowed",
          str(r2)[:70])
    check("拒绝计数 = 1", srv2.stats["rejected_peer"] == 1,
          str(srv2.stats))

    print("\n" + "=" * 80)
    print("4. 服务端不存在时，客户端要快速失败而不是挂死")
    print("=" * 80)
    t0 = time.time()
    r3 = pipe_call(r"\\.\pipe\VoiceUnlockNoSuchPipe", {"cmd": "unlock"}, timeout_ms=1500)
    dt = time.time() - t0
    check("返回失败且耗时 < 3s", (not r3.get("ok")) and dt < 3.0,
          "%.2fs  %s" % (dt, str(r3)[:50]))

    srv.close()
    srv2.close()
    print("\n" + "=" * 80)
    if FAILS:
        print("PIPE TEST FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("PIPE TEST OK —— 管道、DACL、对端校验、失败路径全部可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
