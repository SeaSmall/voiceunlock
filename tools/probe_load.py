#!/usr/bin/env python3
"""probe_load.py -- 探路：确认能用哪个解释器加载 CAM++，并把内部结构摸清楚。

一次性调查脚本，不属于运行时。目的是回答三件事：
  1. 哪个 python 能同时拿到 torch + funasr（导出 ONNX 需要）；
  2. funasr AutoModel 载入 campplus_sv 之后，内层 torch 模块是什么、forward 收什么；
  3. 前端参数实际取值（决定独立版 kaldi_native_fbank 怎么配）。
"""
import os
import sys
import warnings

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 可选：额外一套 site-packages（带 torch+funasr 的解释器环境），例如 <带 torch 的解释器的 site-packages>
SV_ENV = os.environ.get("QQCALL_SV_ENV", "")
if SV_ENV and os.path.isdir(SV_ENV) and SV_ENV not in sys.path:
    sys.path.insert(0, SV_ENV)

print("python     :", sys.executable)
print("version    :", sys.version.split()[0])

import numpy as np
import torch

print("torch      :", torch.__version__, "cuda_avail:", torch.cuda.is_available())

import funasr

print("funasr     :", funasr.__version__)

from funasr import AutoModel

# CAM++ 上游权重目录，例如 <上游模型目录>\campplus_sv（用 VOICEUNLOCK_SV_DIR 指定）
SV_DIR = os.environ.get("VOICEUNLOCK_SV_DIR") or os.path.join(ROOT, "models", "campplus_sv")
m = AutoModel(model=SV_DIR, device="cpu", disable_update=True,
              disable_pbar=True, disable_log=True)
print("AutoModel  :", type(m).__name__)

inner = getattr(m, "model", None)
print("inner model:", type(inner).__name__ if inner is not None else None)
if inner is not None:
    print("  output_level :", getattr(inner, "output_level", None))
    print("  modules      :", [n for n, _ in list(inner.named_children())])

fe = getattr(m, "frontend", None)
print("frontend   :", type(fe).__name__ if fe is not None else None)
if fe is not None:
    for a in ("fs", "window", "n_mels", "frame_length", "frame_shift",
              "dither", "snip_edges", "lfr_m", "lfr_n", "upsacle_samples"):
        print("   %-16s = %s" % (a, getattr(fe, a, None)))
    print("   %-16s = %s" % ("cmvn", getattr(fe, "cmvn", None)))


def read_wav(path):
    import wave
    with wave.open(path, "rb") as w:
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a / 32768.0, sr


ex = os.path.join(SV_DIR, "examples")
wavs = ["speaker1_a_cn_16k.wav", "speaker1_b_cn_16k.wav", "speaker2_a_cn_16k.wav"]
embs = {}
for name in wavs:
    p = os.path.join(ex, name)
    if not os.path.isfile(p):
        print("缺文件:", p)
        continue
    a, sr = read_wav(p)
    r = m.generate(input=a, cache={})
    e = r[0]["spk_embedding"]
    e = e.detach().cpu().numpy() if hasattr(e, "detach") else np.asarray(e)
    e = np.asarray(e, dtype=np.float64).reshape(-1)
    embs[name] = e / np.linalg.norm(e)
    print("%-26s sr=%d len=%.2fs  emb=%s norm=%.4f" %
          (name, sr, len(a) / sr, e.shape, float(np.linalg.norm(e))))

def cos(x, y):
    return float(np.dot(x, y))

print()
if len(embs) == 3:
    a1, b1, a2 = embs[wavs[0]], embs[wavs[1]], embs[wavs[2]]
    print("同一人 speaker1_a vs speaker1_b = %.4f   (期望 ~0.69)" % cos(a1, b1))
    print("不同人 speaker1_a vs speaker2_a = %.4f   (期望 ~0.00)" % cos(a1, a2))
    print("不同人 speaker1_b vs speaker2_a = %.4f" % cos(b1, a2))
print()
print("PROBE OK")
