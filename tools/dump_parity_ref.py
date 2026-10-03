"""dump_parity_ref.py -- 在源码环境生成打包一致性参考向量

先跑这个（源码环境），再打包，然后用冻结版的
    VoiceUnlock.exe parity --ref logs\\parity_ref.npy
比对。cos 必须 > 0.99999；否则说明打包把推理搞坏了
（模型没进去 / fbank 原生化没进去 / onnxruntime DLL 缺失）。
"""
from __future__ import annotations

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import sv as S                     # noqa: E402
from src.parity import probe_vector         # noqa: E402


def main() -> int:
    out = os.path.join(ROOT, "logs", "parity_ref.npy")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    print("ONNX      = %s" % S.DEFAULT_ONNX)
    vec = probe_vector()
    np.save(out, vec)
    print("参考向量   = %s  (%d 维, 范数 %.6f)" % (out, vec.size, float(np.linalg.norm(vec))))
    print("指纹(前8)  = %s" % np.round(vec[:8], 6).tolist())
    return 0


if __name__ == "__main__":
    sys.exit(main())
