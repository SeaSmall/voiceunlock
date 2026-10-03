"""vnc_ctl.py -- VNC 控制器（修正 Shift 映射 + 走 vncdo 的 CLI）

两个坑，都是实测踩出来的：

1) **Shift 没有被正确发送**。vncdotool 的 `type` 对需要 Shift 的字符直接发字符
   本身的 keysym，而 VMware 的 VNC 服务端按 PC 键表映射 —— `:` 需要 Shift+;，
   映射不到就退化成 `;`。实测把 `C:\\g.cmd` 打成了 `c;\\g.cmd`、把
   `http://...` 打成了 `http;//...`，命令全废。
   解决：需要 Shift 的字符显式发 `shift-<基础键>`。

2) **不要用 vncdotool 的 Python API**。那条路的动作全是 Twisted Deferred，
   要 reactor 在跑才会执行；用 time.sleep 等是不会让它们跑起来的
   （看起来"调用成功"，实际可能一个键都没发出去）。
   解决：只负责把文本翻译成 vncdo 的参数，然后交给 vncdo 的 CLI 去跑。

用法（动作按顺序执行；口令不写死在脚本里，见下）：
    set VU_VNC_PASS=<你的 VNC 口令>          # 或用 --pass 传；两者都不给就报用法错误
    python tools/vnc_ctl.py --server 127.0.0.1::5905 \
        key ctrl-esc pause 2 type "cmd" key enter pause 4 \
        type "C:\\g.cmd" key enter pause 10 capture out.png

动作：type TEXT / key KEY / pause SEC / capture FILE

环境变量：
    VU_VNC_PASS    VNC 口令（等价于 --pass）
    VU_VNC_SERVER  VNC 服务端，格式 <主机>::<端口>（等价于 --server；默认 127.0.0.1::5905）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

# 需要 Shift 的符号 -> 基础键
SHIFT_MAP = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]",
    "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
}


def expand_type(text: str) -> list:
    """把一个字符串翻译成 vncdo 的动作序列（需要 Shift 的显式带 shift）。"""
    out = []
    for ch in text:
        if ch == " ":
            out += ["key", "space"]
        elif ch.isalpha() and ch.isupper():
            out += ["key", "shift-" + ch.lower()]
        elif ch in SHIFT_MAP:
            out += ["key", "shift-" + SHIFT_MAP[ch]]
        else:
            out += ["key", ch] if not ch.isalnum() else ["type", ch]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server",
                    default=os.environ.get("VU_VNC_SERVER", "127.0.0.1::5905"),
                    help="VNC 服务端 <主机>::<端口>（默认 127.0.0.1::5905，或环境变量 VU_VNC_SERVER）")
    ap.add_argument("--pass", dest="password", default=None,
                    help="VNC 口令；不给就读环境变量 VU_VNC_PASS（没有默认口令）")
    ap.add_argument("--per-cmd-delay", type=float, default=0.03,
                    help="vncdo 每条动作之间的间隔（打字太快的客户机会丢键）")
    ap.add_argument("actions", nargs="+")
    a = ap.parse_args()

    # 口令只从 --pass / VU_VNC_PASS 来；不设默认值，缺了就明确报错退出。
    if not a.password:
        a.password = os.environ.get("VU_VNC_PASS", "")
    if not a.password:
        print("用法错误：缺少 VNC 口令。请用 --pass <口令> 或设置环境变量 VU_VNC_PASS。",
              file=sys.stderr)
        return 2

    # 先把自己的动作列表翻译成 vncdo 的参数列表
    argv = []
    acts = a.actions
    i = 0
    while i < len(acts):
        act = acts[i].lower()
        if act == "type":
            argv += expand_type(acts[i + 1])
            i += 2
        elif act in ("key", "pause", "capture"):
            argv += [act, acts[i + 1]]
            i += 2
        else:
            argv += [acts[i]]
            i += 1

    cmd = [sys.executable, "-m", "vncdotool.command",
           "-s", a.server, "-p", a.password,
           "--delay", str(int(a.per_cmd_delay * 1000))] + argv
    # 顺便把翻译结果打出来，便于排查"到底发了哪些键"
    print("vncdo args: %s" % " ".join(argv[:40]) + (" ..." if len(argv) > 40 else ""))
    r = subprocess.run(cmd, capture_output=True, text=True)
    warn = [l for l in (r.stderr or "").splitlines() if "Deprecation" not in l]
    if warn:
        print("stderr: %s" % " | ".join(warn[:3]))
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
