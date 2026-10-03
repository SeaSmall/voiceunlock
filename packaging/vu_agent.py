"""vu_agent.py -- 常驻 agent 的打包入口（**无控制台窗口**）

为什么 agent 要单独一个 exe：
    它靠"HKCU\\...\\Run"或计划任务在每次登录时自动起，如果是有控制台的程序，
    用户每次登录都会看到一个黑窗口挂在那儿。而设置/登记界面又必须有控制台
    （getpass 要读键盘、结论要打印给人看）——一个 --noconsole 的 exe 兼顾不了。
    所以打包成两个 exe、共享同一份 _internal。
"""
from __future__ import annotations

import os
import sys
import time
import traceback


def _crash_log(exc_text: str) -> None:
    """无控制台时异常必须落盘 —— 否则就是"静默死亡"，用户只看到声纹不灵。"""
    try:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "VoiceUnlock", "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "agent_crash.log"), "a", encoding="utf-8") as f:
            f.write("\n=== %s ===\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), exc_text))
    except Exception:
        pass


def main() -> int:
    from src import cli
    cli.fix_console()          # windowed 下 sys.stdout 是 None，必须先兜住
    try:
        # ★ 必须把命令行参数交给 src.agent.main()：
        #   这里曾经直接 `Agent().run(once=False)`，等于**把 argv 丢掉** ——
        #   实际后果是 `VoiceUnlockAgent.exe run` 能跑（碰巧一样），但
        #   `--allow-any-peer` 之类的联调开关在发布的 exe 里**完全无效**
        #   （另一台机器上实测确认过：放在 run 前后都没用，启动日志里的
        #   对端校验始终是 ('logonui.exe',)）。文档写了却做不到，属于产品缺陷。
        from src.agent import main as agent_main
        return agent_main(sys.argv[1:])
    except Exception:
        _crash_log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
