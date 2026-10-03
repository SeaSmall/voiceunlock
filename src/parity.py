"""parity.py -- 打包一致性的确定性探针

为什么需要它：
    打包（PyInstaller）之后最容易【悄悄】坏掉的就是声纹推理那一段 ——
    ONNX 模型没被收进去、kaldi_native_fbank 是延迟导入所以静态分析没发现、
    onnxruntime 的原生 DLL 没随包走…… 这些在开发机上永远看不出来，
    到用户机器上才表现为"声纹永远不通过"。

    所以这里定义一段**完全确定性**的音频（不依赖外部 wav 文件、不依赖
    随机数实现细节），在源码环境算一个参考向量存下来，再到冻结版里重算，
    比较余弦。这就是"打包没把推理搞坏"的客观判据。

用法：
    开发侧（源码）: python tools/dump_parity_ref.py
    冻结侧       : VoiceUnlock.exe parity --ref logs\\parity_ref.npy
"""
from __future__ import annotations

import numpy as np

from . import sv as S


def probe_audio(frames: int = 200, seed: int = 1234) -> np.ndarray:
    """确定性测试音频：像语音的谐波堆 + 可复现的微小噪声。

    刻意不用随机音频：随机音频的 fbank 对浮点细节过于敏感，反而不好比对；
    也不读外部 wav：装到别的机器上不该依赖示例文件还在不在。
    """
    n = S.samples_for_frames(frames)
    t = np.arange(n, dtype=np.float64) / float(S.SR)
    x = (0.60 * np.sin(2 * np.pi * 120.0 * t)
         + 0.30 * np.sin(2 * np.pi * 240.0 * t)
         + 0.20 * np.sin(2 * np.pi * 360.0 * t)
         + 0.05 * np.sin(2 * np.pi * 1800.0 * t))
    x = x * (0.5 + 0.5 * np.sin(2 * np.pi * 3.1 * t))       # 3.1Hz 缓慢起伏
    x = x + 1e-4 * np.random.RandomState(seed).randn(n)
    peak = float(np.max(np.abs(x))) or 1.0
    return (x / peak * 0.7).astype(np.float32)


def probe_vector(onnx_path: str | None = None) -> np.ndarray:
    """用探针音频算一个 192 维向量（确定性）。"""
    emb = S.SpeakerEmbedder(onnx_path=onnx_path or S.DEFAULT_ONNX)
    vec, _info = emb.embed_audio(probe_audio())
    return np.asarray(vec, dtype=np.float64)


def cosine(a, b) -> float:
    x = np.asarray(a, dtype=np.float64).ravel()
    y = np.asarray(b, dtype=np.float64).ravel()
    nx, ny = float(np.linalg.norm(x)), float(np.linalg.norm(y))
    if nx <= 0 or ny <= 0:
        return 0.0
    return float(np.dot(x, y) / (nx * ny))
