#!/usr/bin/env python3
"""dump_reference.py -- 用 funasr 原版算出「基准答案」并存盘，供独立版比对

为什么单独存盘：基准必须由**原版 funasr 环境**产生，而独立版跑在另一个 venv
（只有 onnxruntime + kaldi_native_fbank，没有 torch）。两个解释器靠 .npy 交接，
避免"用同一个环境自证"。

管道契约（两边必须一致，见 vu_common.py）：
    fbank(全长) -> 逐句均值归一化(全长) -> 裁到对齐 T -> 送模型
    对齐 T 由 V.largest_aligned_T 决定（ceil(T/2) 必须是 100 的倍数）

产物（默认 <项目根>\\logs\\ref\\，可用环境变量 VOICEUNLOCK_REF 覆盖）:
    <name>_fbank.npy          全长的 fbank（已做逐句均值归一化）
    <name>_fbank_aligned.npy  裁到对齐 T 后的 fbank
    <name>_emb_funasr.npy     AutoModel.generate() 全长 -> 192 维（线上原样）
    <name>_emb_aligned.npy    【原版模型】对 aligned fbank 的 forward  ← ONNX 的硬比对目标
    <name>_emb_ceil.npy       【打补丁模型】对同一 aligned fbank 的 forward
                              —— 用于证明 ceil_mode 等价性

用法:
    <runtime>\\python.exe tools\\dump_reference.py
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
# 基准产物目录，默认 <项目根>\logs\ref（用 VOICEUNLOCK_REF 指定）
OUT_DIR = os.environ.get("VOICEUNLOCK_REF") or os.path.join(ROOT, "logs", "ref")
NAMES = ["speaker1_a_cn_16k.wav", "speaker1_b_cn_16k.wav", "speaker2_a_cn_16k.wav"]
SEG_LEN = V.SEG_LEN


def log(msg):
    print("[ref] %s" % msg, flush=True)


t0 = time.time()
from funasr import AutoModel
log("import funasr 完成 %.1fs" % (time.time() - t0))

import torchaudio
import torchaudio.compliance.kaldi as Kaldi
log("torchaudio %s" % torchaudio.__version__)

t0 = time.time()
am = AutoModel(model=SV_DIR, device="cpu", disable_update=True,
               disable_pbar=True, disable_log=True)
model = am.model.eval()
log("加载模型完成 %.1fs" % (time.time() - t0))


class _NoCeil:
    """临时把 seg_pooling 换成 ceil_mode=False，用完还原。"""

    def __enter__(self):
        import torch.nn.functional as F
        import funasr.models.campplus.components as CP
        self.CP = CP                      # 存到 self —— __exit__ 看不到函数内的局部导入
        self.orig = CP.CAMLayer.seg_pooling

        def patched(s, x, seg_len=SEG_LEN, stype="avg"):
            fn = F.avg_pool1d if stype == "avg" else F.max_pool1d
            seg = fn(x, kernel_size=seg_len, stride=seg_len, ceil_mode=False)
            shape = seg.shape
            seg = seg.unsqueeze(-1).expand(*shape, seg_len).reshape(*shape[:-1], -1)
            return seg[..., : x.shape[-1]]

        CP.CAMLayer.seg_pooling = patched
        return self

    def __exit__(self, *a):
        self.CP.CAMLayer.seg_pooling = self.orig
        return False


os.makedirs(OUT_DIR, exist_ok=True)
e_full, e_align = {}, {}
for name in NAMES:
    stem = name.replace(".wav", "")
    a, sr = V.read_wav(os.path.join(SV_DIR, "examples", name))

    # 前端：与 campplus/utils.py:extract_feature 同一个调用
    t = torch.from_numpy(a).unsqueeze(0)
    fb_raw = Kaldi.fbank(t, num_mel_bins=80)
    np.save(os.path.join(OUT_DIR, stem + "_fbank_raw.npy"), fb_raw.numpy().astype(np.float32))

    fb = fb_raw - fb_raw.mean(dim=0, keepdim=True)   # 逐句均值归一化（全长）
    np.save(os.path.join(OUT_DIR, stem + "_fbank.npy"), fb.numpy().astype(np.float32))

    T = V.largest_aligned_T(fb.shape[0])
    fb_al = fb[:T].contiguous()
    np.save(os.path.join(OUT_DIR, stem + "_fbank_aligned.npy"), fb_al.numpy().astype(np.float32))

    # 线上原样基准（全长）
    r = am.generate(input=a, cache={})
    e = r[0]["spk_embedding"]
    e = e.detach().cpu().numpy() if hasattr(e, "detach") else np.asarray(e)
    e = np.asarray(e, dtype=np.float64).reshape(-1)
    np.save(os.path.join(OUT_DIR, stem + "_emb_funasr.npy"), e)
    e_full[name] = e / np.linalg.norm(e)

    # ONNX 的硬比对目标（原版模型 + aligned 输入）
    with torch.no_grad():
        e_al = model(fb_al.unsqueeze(0)).numpy().reshape(-1).astype(np.float64)
    np.save(os.path.join(OUT_DIR, stem + "_emb_aligned.npy"), e_al)
    e_align[name] = e_al / np.linalg.norm(e_al)

    log("%-24s 全帧=%d -> 对齐 T=%d (L=%d)  全长基准 vs 对齐基准 cos=%.6f" %
        (name, fb.shape[0], T, V.tdnn_len(T),
         V.cos(np.load(os.path.join(OUT_DIR, stem + "_emb_funasr.npy")), e_al)))

log("")
log("--- ceil_mode 等价性证明（同一 aligned 输入，原版 vs 打补丁）---")
with _NoCeil():
    for name in NAMES:
        stem = name.replace(".wav", "")
        fb_al = np.load(os.path.join(OUT_DIR, stem + "_fbank_aligned.npy"))
        with torch.no_grad():
            ep = model(torch.from_numpy(fb_al).unsqueeze(0)).numpy().reshape(-1).astype(np.float64)
        np.save(os.path.join(OUT_DIR, stem + "_emb_ceil.npy"), ep)
        c = V.cos(ep, np.load(os.path.join(OUT_DIR, stem + "_emb_aligned.npy")))
        log("  %-24s cos(ceil=True, ceil=False) = %.8f  %s" %
            (name, c, "OK" if c > 0.99999 else "FAIL"))

log("")
log("--- 全长基准的区分度（应与线上一致）---")
log("同一人 a vs b   = %+.4f  (期望 0.6936)" % V.cos(e_full[NAMES[0]], e_full[NAMES[1]]))
log("不同人 a vs 2a  = %+.4f  (期望 -0.0842)" % V.cos(e_full[NAMES[0]], e_full[NAMES[2]]))
log("不同人 b vs 2a  = %+.4f  (期望 0.0072)" % V.cos(e_full[NAMES[1]], e_full[NAMES[2]]))
log("")
log("--- 对齐基准的区分度（ONNX 将与此对齐）---")
log("同一人 a vs b   = %+.4f" % V.cos(e_align[NAMES[0]], e_align[NAMES[1]]))
log("不同人 a vs 2a  = %+.4f" % V.cos(e_align[NAMES[0]], e_align[NAMES[2]]))
log("不同人 b vs 2a  = %+.4f" % V.cos(e_align[NAMES[1]], e_align[NAMES[2]]))
log("基准已写入 %s" % OUT_DIR)
log("DUMP OK")
