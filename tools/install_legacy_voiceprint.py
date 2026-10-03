"""install_legacy_voiceprint.py -- 把你给的 .voiceprint.npy 直接装成档案

为什么可以直接用（证据链）：
  1. `enroll_voice.py` 的注释写着："实测你本人只拿到 0.15"。
     那次测量用的就是这份 `.voiceprint.npy`（文件时间戳 2026-10-02 16:08，
     早于 enroll_voice.py 的最后修改时间 16:21，且之后再没重新生成过）。
     → 这份向量与本人声音**确实相关**（0.15 而不是 0）。
  2. 它当时被判"不通过"，是因为阈值设了 0.35 —— 0.15 < 0.35。
     **那是阈值问题，不是向量坏。**
  3. 我先前用"它与本机所有录音 cos≈0"推断它坏掉，是推理错误：
     本机那些录音都是别人的声音，而好声纹与别人本来就应该≈0。

所以：直接装。装成 `legacy:voiceprint`（不绑定设备），
这样它会走【跨设备兜底】对所有设备生效（跨设备兜底是对所有档案取最大）。

阈值先给一个【仅用于标定】的低值，第一次试完把实测分数报出来，再定真正的阈值。
"""
from __future__ import annotations

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import cli                      # noqa: E402
from src import config as C              # noqa: E402
from src.profiles import ProfileStore    # noqa: E402

LEGACY_ID = "legacy:voiceprint"


def main() -> int:
    cli.fix_console()
    src_path = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
        else os.environ.get("VU_VOICEPRINT", "").strip()
    if not src_path:
        print("用法错误：请给出声纹 .npy 路径（位置参数或环境变量 VU_VOICEPRINT）。例如："
              r" python tools/install_legacy_voiceprint.py <旧声纹目录>\.voiceprint.npy",
              file=sys.stderr)
        return 2
    thr = 0.10
    for i, a in enumerate(sys.argv):
        if a == "--threshold" and i + 1 < len(sys.argv):
            thr = float(sys.argv[i + 1])

    if not os.path.isfile(src_path):
        print("找不到声纹文件: %s" % src_path)
        return 1
    v = np.load(src_path)
    v = np.asarray(v, dtype=np.float64).ravel()
    if v.size == 0:
        print("空的向量")
        return 1
    v = v / np.linalg.norm(v)

    st = ProfileStore()
    st.put(LEGACY_ID,
           "legacy-voiceprint（旧声纹导入）",
           v, thr,
           samples=0,
           source="legacy",
           provisional=True,
           extra={
               "note": "来自 %s；阈值 %.2f 仅为标定用，第一次试完立即按实测分调整"
                       % (src_path, thr),
               "origin": src_path,
           })
    st.save()

    print("已装入档案")
    print("  文件    : %s" % src_path)
    print("  维度    : %d   范数 %.6f" % (v.size, float(np.linalg.norm(v))))
    print("  id      : %s（不绑定设备 -> 对所有设备走跨设备兜底）" % LEGACY_ID)
    print("  阈值    : %.2f  <-- 仅用于标定，测出真实分数后要改" % thr)
    print("  档案库  : %s" % st.path)
    print()
    print("下一步：")
    print("  .\\venv\\Scripts\\python.exe -m src.enroll --test")
    print("  会要求说满 3 段（约 6 秒）—— 因为这是跨设备兜底路径。")
    print("  测完把分数报出来，我把阈值改成按实测定的值。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
