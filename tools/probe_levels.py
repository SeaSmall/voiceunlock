"""probe_levels.py -- 用运行时模块实测每一路输入设备的真实电平

用途：登记之前先知道"哪一路能拾到声音"。本机曾经出现过
      物理麦 NOTPRESENT、只有 WO Mic（手机当麦，peak 4e-4）的情况，
      盲目登记会得到一份没用的声纹。

用法：
    venv\\Scripts\\python.exe tools\\probe_levels.py [秒数]
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import audio as A      # noqa: E402
from src import devices as D    # noqa: E402
from src import sv as S         # noqa: E402


def main() -> int:
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    devs = D.list_capture_devices()
    frames = int(secs / 0.01)
    print("枚举到 %d 个输入设备，每路采 %.1f 秒" % (len(devs), secs))
    print("** 请对着麦克风连续说几句话，倒计时结束后开始采 **\n")
    for i in (3, 2, 1):
        print("  %d ..." % i, flush=True)
        time.sleep(1)
    print("  ● 说！", flush=True)
    samples, errs = A.capture_multi(devs, frames=frames, timeout_s=secs + 4)

    print("%-3s %-8s %-34s %10s %10s %8s %s" %
          ("#", "可信", "设备", "峰值", "RMS", "活跃比", "结论"))
    print("-" * 104)
    for i, d in enumerate(devs):
        a = samples.get(d["id"])
        name = d["name"][:32]
        if a is None:
            print("%-3d %-8s %-34s %10s %10s %8s %s" %
                  (i, "是" if d["trusted"] else "否", name, "-", "-", "-",
                   "打不开: %s" % errs.get(d["id"], "?")[:40]))
            continue
        st = A.speech_stats(a)
        verdict = "静音" if st["silent"] else ("信号偏弱" if st["peak"] < 1e-3 else "可用")
        print("%-3d %-8s %-34s %10.6f %10.6f %8.2f %s" %
              (i, "是" if d["trusted"] else "否", name,
               st["peak"], A.rms(a), st["active_ratio"], verdict))
    print()
    print("提示：登记要用『可信 + 可用』的那一路。不可信设备（混音总线）默认不参与判定。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
