"""guest_bridge.py -- 主机与客户机之间的最小通道（不需要 VMware Tools，也不需要客户机凭据）

为什么需要它：
    虚拟机里没装 VMware Tools，所以 vmrun 的 runProgramInGuest / captureScreen
    全部被拒；而装 Tools 之后那些命令仍然需要客户机账户密码。
    我不想让你把密码交出来（密码不该进对话），所以改走这条路：

        主机侧（本脚本）:  HTTP 服务，SERVE_DIR 里的文件随便下；
                           POST/PUT 上来的内容落到 LOGS_DIR/guest/
        客户机侧        :  用内置的 curl.exe 下载脚本、回传结果
        屏幕与键盘      :  走 VNC（5905），不需要任何凭据

    于是"在客户机里跑命令 + 读回结果"这两件事都不依赖 Tools 和密码。

用法：
    python tools/guest_bridge.py --port 8899 --host 192.168.1.1
    # 客户机里（VNC 打字；<主机 IP> 换成 --host 的值，默认取环境变量 VU_BRIDGE_HOST）：
    #   curl -o C:\\g.cmd http://<主机 IP>:8899/g.cmd
    #   C:\\g.cmd
    # 脚本里回传结果：
    #   curl -X POST --data-binary @C:\\out.txt http://<主机 IP>:8899/upload/out.txt

环境变量：
    VU_BRIDGE_HOST  客户机该访问的主机地址（等价于 --host；默认 127.0.0.1）
"""
from __future__ import annotations

import argparse
import http.server
import os
import socketserver
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVE_DIR = os.path.join(ROOT, "staging")
RECV_DIR = os.path.join(ROOT, "logs", "guest")


def log(msg: str) -> None:
    line = "%s  %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
        with open(os.path.join(ROOT, "logs", "guest_bridge.log"), "a",
                  encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=SERVE_DIR, **kw)

    def log_message(self, fmt, *args):        # 走我们自己的日志
        log("HTTP %s" % (fmt % args))

    def do_POST(self):                        # noqa: N802
        return self._recv()

    def do_PUT(self):                         # noqa: N802
        return self._recv()

    def _recv(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        name = os.path.basename(self.path.replace("/upload/", "").strip("/")) or "body.bin"
        os.makedirs(RECV_DIR, exist_ok=True)
        dst = os.path.join(RECV_DIR, name)
        with open(dst, "wb") as f:
            f.write(body)
        log("收到客户机回传 %s（%d 字节）-> %s" % (name, len(body), dst))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default=os.environ.get("VU_BRIDGE_HOST", "127.0.0.1"),
                    help="客户机侧该访问的主机地址（只影响提示文本；服务仍监听 0.0.0.0）")
    a = ap.parse_args()
    os.makedirs(SERVE_DIR, exist_ok=True)
    os.makedirs(RECV_DIR, exist_ok=True)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", a.port), Handler) as srv:
        log("=== guest_bridge 监听 0.0.0.0:%d ===" % a.port)
        log("    客户机访问地址: http://%s:%d/" % (a.host, a.port))
        log("    下发目录: %s" % SERVE_DIR)
        log("    回传目录: %s" % RECV_DIR)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
