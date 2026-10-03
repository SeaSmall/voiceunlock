#!/usr/bin/env python3
"""export_onnx.py -- 把 CAM++ 从 funasr/torch 导成 ONNX（一次性工具，产物随安装包分发）

为什么要导：实测 `import funasr` 单独就要 100+ 秒（torch 只要 1.7 秒），运行时绝不能带它。
导完之后运行时只需要 onnxruntime，纯 CPU 推理。

必须与 funasr 推理路径逐项一致（来源 funasr/models/campplus/）：
    model.py:135  load_audio_text_image_video(fs=16000) -> float32 ∈ [-1,1]，【没有】1<<15 放大
    utils.py:103  Kaldi.fbank(au.unsqueeze(0), num_mel_bins=80)
                  torchaudio 2.0.0 默认值：window='povey', dither=0.0, energy_floor=1.0,
                  low_freq=20, high_freq=0, preemph=0.97, snip_edges=True, use_power=True,
                  use_log_fbank=True, htk_compat=False   ← 注意：不是 hamming / dither=1.0
    utils.py:104  feature -= feature.mean(dim=0)          # 逐句均值归一化（必须复刻）
    model.py:145  CAMPPlus.forward(x), x=(B,T,80) -> (B,192)

★ 时间维契约见 vu_common.py 顶部：T 必须满足 ceil(T/2) % 100 == 0。

用法（必须用带 torch+funasr 的解释器，例如 GPT-SoVITS runtime python）：
    <runtime>\\python.exe tools\\export_onnx.py
"""
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 可选：额外一套 site-packages（带 torch+funasr 的解释器环境），例如 <带 torch 的解释器的 site-packages>
SV_ENV = os.environ.get("QQCALL_SV_ENV", "")
if SV_ENV and os.path.isdir(SV_ENV) and SV_ENV not in sys.path:
    sys.path.insert(0, SV_ENV)

import numpy as np
import torch

import vu_common as V

# CAM++ 上游权重目录，例如 <上游模型目录>\campplus_sv（用 VOICEUNLOCK_SV_DIR 指定）
SV_DIR = os.environ.get("VOICEUNLOCK_SV_DIR") or os.path.join(ROOT, "models", "campplus_sv")
# ONNX 产物目录，默认 <项目根>\models（用 VOICEUNLOCK_MODELS 指定）
OUT_DIR = os.environ.get("VOICEUNLOCK_MODELS") or os.path.join(ROOT, "models")
OUT = os.path.join(OUT_DIR, "campplus.onnx")
SEG_LEN = V.SEG_LEN


def log(msg):
    print("[export] %s" % msg, flush=True)


def fbank_like_funasr(a):
    """与 campplus/utils.py:extract_feature 完全同一个调用（torchaudio 默认参数）。"""
    import torchaudio.compliance.kaldi as Kaldi
    t = torch.from_numpy(np.asarray(a, dtype=np.float32)).unsqueeze(0)
    fb = Kaldi.fbank(t, num_mel_bins=80)
    return fb - fb.mean(dim=0, keepdim=True)      # 逐句均值归一化


t0 = time.time()
from funasr import AutoModel
log("import funasr 完成 %.1fs" % (time.time() - t0))

# ---- 关键补丁：seg_pooling 去掉 ceil_mode，从而保住动态时间维 ----
import torch.nn.functional as F
import funasr.models.campplus.components as CP


def seg_pooling_no_ceil(self, x, seg_len=SEG_LEN, stype="avg"):
    """与 components.py:172 数值等价（仅当输入长度是 seg_len 整数倍），但导出友好。"""
    if stype == "avg":
        seg = F.avg_pool1d(x, kernel_size=seg_len, stride=seg_len, ceil_mode=False)
    elif stype == "max":
        seg = F.max_pool1d(x, kernel_size=seg_len, stride=seg_len, ceil_mode=False)
    else:
        raise ValueError("Wrong segment pooling type.")
    shape = seg.shape
    seg = seg.unsqueeze(-1).expand(*shape, seg_len).reshape(*shape[:-1], -1)
    return seg[..., : x.shape[-1]]


CP.CAMLayer.seg_pooling = seg_pooling_no_ceil
log("已打补丁: CAMLayer.seg_pooling -> ceil_mode=False")
log("时间维契约: T 必须满足 ceil(T/2) %% %d == 0, 即 T ∈ %s ..." %
    (SEG_LEN, V.ALIGNED_T[:8]))

t0 = time.time()
am = AutoModel(model=SV_DIR, device="cpu", disable_update=True,
               disable_pbar=True, disable_log=True)
log("加载模型完成 %.1fs" % (time.time() - t0))

model = am.model
assert type(model).__name__ == "CAMPPlus", type(model).__name__
model.eval()
log("内层模型 = %s   output_level = %s" % (type(model).__name__, model.output_level))

wav = os.path.join(SV_DIR, "examples", "speaker1_a_cn_16k.wav")
a, sr = V.read_wav(wav)
fb_full = fbank_like_funasr(a)
T = V.largest_aligned_T(fb_full.shape[0])
assert T > 0, "示例音频太短，找不到对齐的 T"
fb = fb_full[:T].contiguous()
dummy = fb.unsqueeze(0).contiguous()
log("原始 fbank=%d 帧 -> 对齐到 %d 帧 (L=%d)  min=%.2f max=%.2f" %
    (fb_full.shape[0], T, V.tdnn_len(T), float(fb.min()), float(fb.max())))

with torch.no_grad():
    ref = model(dummy)
log("torch(打补丁) 输出 %s" % (tuple(ref.shape),))

os.makedirs(OUT_DIR, exist_ok=True)
torch.onnx.export(
    model, dummy, OUT,
    input_names=["fbank"], output_names=["embedding"],
    dynamic_axes={"fbank": {1: "T"}, "embedding": {0: "B"}},
    opset_version=13, do_constant_folding=True,
)
log("已写出 %s (%.1f MB)" % (OUT, os.path.getsize(OUT) / 1e6))

# ---- onnxruntime 校验：只喂对齐 T ----
import onnxruntime as ort
sess = ort.InferenceSession(OUT, providers=["CPUExecutionProvider"])
for T2 in (200, 400, 600):
    assert V.is_aligned(T2), T2
    x = np.random.RandomState(T2).uniform(-12.0, 22.0, size=(1, T2, 80)).astype(np.float32)
    with torch.no_grad():
        r1 = model(torch.from_numpy(x)).numpy()
    r2 = sess.run(["embedding"], {"fbank": x})[0]
    cos = float(np.dot(r1.ravel(), r2.ravel()) /
                (np.linalg.norm(r1) * np.linalg.norm(r2)))
    log("T=%-4d (L=%d) torch vs onnx  max|delta|=%.3e  cosine=%.8f" %
        (T2, V.tdnn_len(T2), float(np.abs(r1 - r2).max()), cos))

# ---- 反证：非对齐 T 必须当场报错 ----
try:
    bad = np.random.RandomState(1).uniform(-12, 22, size=(1, 300, 80)).astype(np.float32)
    with torch.no_grad():
        model(torch.from_numpy(bad))
    log("!! 警告: T=300 竟然没报错，契约可能不成立")
except Exception as e:
    log("契约验证 OK: T=300 当场报错 (%s)" % type(e).__name__)
log("EXPORT OK")
