"""cli.py -- 命令行入口的小工具

fix_console()：两件事，都在任何输出发生之前做完。
    1) 把 stdout/stderr 的编码错误策略改成 replace（历史原因，见下）。
    2) 按系统语言装好输出层（见 src/i18n.py）：非中文 Windows 上把 print /
       input / argparse 的打印口换成会翻译的实现。中文系统上是**纯 no-op**。

为什么必须做（本项目踩过的坑，也是隔壁 asr_server.py 注释里写过的）：
    Windows 控制台是 GBK。一旦 print 里出现非 GBK 字符（emoji、部分符号），
    就会抛 UnicodeEncodeError 把整个脚本打崩 —— 而且在"最后打印结论"时崩，
    前面的工作全白做。
    【只改 errors，不要改 encoding】：改了 encoding 会让中文输出变成另一种编码，
    管道/重定向之后又是一堆乱码。
    英文系统上则是另一个症状：代码页 437，中文全变 `?` —— 靠 i18n 输出英文解决。
"""
from __future__ import annotations

import sys


class _NullStream:
    """给 windowed 打包用的空 stdout/stderr。

    ★ 为什么必须做：PyInstaller 的 --noconsole/--windowed 下，Python 3 里
    sys.stdout / sys.stderr 都是 **None**，任何 print() 都会抛
    AttributeError('NoneType' object has no attribute 'write')。
    常驻 agent 的第一行日志就会把整个进程打崩 —— 而且崩在"打印日志"上，
    看起来像功能坏了。这里把它们换成吃掉一切的对象。
    """

    encoding = "utf-8"
    errors = "replace"

    def write(self, s):                      # noqa: D102
        return len(s) if s else 0

    def writelines(self, lines):             # noqa: D102
        for _ in lines:
            pass

    def flush(self):                         # noqa: D102
        pass

    def isatty(self):                        # noqa: D102
        return False

    def reconfigure(self, **kw):             # noqa: D102
        for k, v in kw.items():
            if k in ("encoding", "errors") and v:
                setattr(self, k, v)


def fix_console() -> None:
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None:
            setattr(sys, name, _NullStream())
            continue
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass
    # 输出语言（非中文系统输出英文）。放在最后：即使上面某个 stream 是 None，
    # 翻译层也已经装好了，后面所有 print 都吃得到。
    try:
        from . import i18n
        i18n.install()
    except Exception:
        pass
