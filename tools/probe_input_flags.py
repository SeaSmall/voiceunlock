"""probe_input_flags.py -- 验一个决定守夜模式生死的问题

问题：**远程工具（RustDesk / 向日葵）注入的键鼠，到底带不带
LLKHF_INJECTED / LLMHF_INJECTED 标志？**

为什么必须验：
    守夜模式的判据是"拦物理、放行注入"。如果远程工具的输入**不带**注入标志
    （例如走驱动级注入），那么一旦真的开始拦截，远程用户也会被一起拦掉 ——
    人被锁在机器外面。这是本方案风险最高的一步，所以先**只演练、不拦截**。

这个脚本做的事：
    1. 装上低级键鼠钩子，但 **dry_run=True（只统计，绝不吞事件）**
    2. 把每个事件记进 <项目根>\\logs\\input_flags.log
    3. 跑够指定秒数后，输出"物理 vs 注入"的统计

怎么用（需要你配合）：
    .\\venv\\Scripts\\python.exe tools\\probe_input_flags.py --seconds 90
    倒计时结束后：
      a) 先在本地按几个键、动一下鼠标          -> 应当记为 物理（injected=False）
      b) 再用 RustDesk / 向日葵 远程连过来操作  -> 应当记为 注入（injected=True）
    然后看结论：
      * 远程操作出现 injected=True  -> 判据成立，可以放心做拦截
      * 远程操作也是 injected=False -> 危险！拦截会把远程一起拦掉，必须换方案
      * 远程操作完全不出现          -> 钩子拿不到驱动级注入的事件，
                                        那"拦物理"就拦不住远程（安全），但也拦不住驱动级恶意注入
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import cli          # noqa: E402
from src.hooks import InputBlocker   # noqa: E402

LOG = os.path.join(ROOT, "logs", "input_flags.log")


def main() -> int:
    cli.fix_console()
    seconds = 60
    for i, a in enumerate(sys.argv):
        if a == "--seconds" and i + 1 < len(sys.argv):
            seconds = int(sys.argv[i + 1])

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    f = open(LOG, "w", encoding="utf-8", buffering=1)
    t0 = time.time()

    def on_event(kind, injected, info):
        f.write("%7.3f  %-6s injected=%-5s %s\n"
                % (time.time() - t0, kind, injected, info))

    blk = InputBlocker(dry_run=True, on_event=on_event)
    errors = []
    if not blk.start():
        print("装钩子失败（可能需要管理员权限？）")
        return 1
    print("钩子已装上（演练模式：只统计，不拦截 —— 你不会被锁住）")
    print("日志: %s\n" % LOG)
    print("倒计时 5 秒后开始记录 %d 秒：" % seconds)
    for i in (5, 4, 3, 2, 1):
        print("  %d ..." % i, flush=True)
        time.sleep(1)
    print("\n[1] 先在【本地】按几个键、动一下鼠标（例如按 a b c 各一下）", flush=True)
    print("[2] 再用【远程工具】连过来操作（RustDesk / 向日葵）", flush=True)
    print("    记录中 ...", flush=True)

    t_end = time.time() + seconds
    while time.time() < t_end:
        time.sleep(0.5)
    blk.stop()
    f.close()

    s = blk.stats
    print("\n" + "=" * 78)
    print("统计（%d 秒）" % seconds)
    print("=" * 78)
    print("  物理键盘事件 : %d" % s["kb_phys"])
    print("  注入键盘事件 : %d" % s["kb_inj"])
    print("  物理鼠标事件 : %d" % s["ms_phys"])
    print("  注入鼠标事件 : %d" % s["ms_inj"])
    print("  回调异常     : %d" % s["errors"])
    print()
    if s["errors"]:
        errors.append("回调出现异常 %d 次（应查日志）" % s["errors"])
    if s["kb_phys"] + s["ms_phys"] == 0:
        errors.append("完全没收到物理事件 —— 钩子可能没生效（或你没按键）")
    if s["kb_inj"] + s["ms_inj"] == 0:
        print("  [!] 没有捕获到任何【注入】事件。")
        print("      如果你刚才用远程工具操作过，说明它的输入不带注入标志、")
        print("      或者钩子拿不到它（驱动级注入）-> 拦截方案需要重新评估。")
        print("      如果你没测远程，那就先测一次再下结论。")
    else:
        print("  [OK] 捕获到注入事件 —— '拦物理、放行注入'这条判据成立。")
    for e in errors:
        print("  [!] %s" % e)
    print("\n逐事件日志: %s" % LOG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
