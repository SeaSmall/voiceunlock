"""check_old_with_funasr.py -- 用【旧的那套 funasr 前端】去比旧声纹

为什么必须做这个对照：
    在独立运行时（venv）里，你所有真实录音对旧声纹 .voiceprint.npy 的 cos
    都约等于 0（-0.03 ~ +0.03）。这有两种解释：
      A. 前端换了（96k np.interp -> 引擎 16k）导致掉分 —— 但那只该掉到 0.2~0.5
      B. 那份声纹压根不是这些录音里的声音（比如登记时设备没拾到声音 = 录到了静音）
    这个脚本用**与当初登记完全相同的前端**（funasr AutoModel.generate）再比一次：
      * 如果原路径下也≈0  -> 是 B，旧声纹不可能沿用，必须重新登记
      * 如果原路径下很高  -> 是 A，只是前端差异，可以想办法迁移

顺便还测：
    * 纯静音 / 低电平噪声 的 embedding 与旧声纹的相似度（验证"录到静音"这个猜想）
    * 官方示例里两位说话人 与旧声纹的相似度（看它是不是像某个陌生人）

用法（必须用带 torch+funasr 的解释器；所有路径都从环境变量读）：
    set VOICEUNLOCK_SV_DIR=<上游模型目录>\\campplus_sv
    set VOICEUNLOCK_OLD_VP=<旧声纹目录>\\.voiceprint.npy
    set VU_REAL_WAVS=<音频目录>\\a.wav;<音频目录>\\b.wav
    set QQCALL_SV_ENV=<带 torch 的解释器的 site-packages>          （可选：额外一套 site-packages）
    <runtime>\\python.exe tools\\check_old_with_funasr.py
"""
from __future__ import annotations

import glob
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

SV_ENV = os.environ.get("QQCALL_SV_ENV", "")
if SV_ENV and os.path.isdir(SV_ENV) and SV_ENV not in sys.path:
    sys.path.insert(0, SV_ENV)

import numpy as np
import torch

from src import cli

def _env_paths(var, example, required=True):
    """读环境变量里的路径清单（多个用 ';' 分隔，支持 * 通配）。

    没设时：required=True 报用法错误退出；否则返回空列表。
    """
    raw = os.environ.get(var, "").strip()
    if not raw:
        if not required:
            return []
        print("用法错误：请设置环境变量 %s，多个路径用 ';' 分隔。例如：%s" % (var, example),
              file=sys.stderr)
        raise SystemExit(2)
    out = []
    for item in raw.split(";"):
        item = item.strip()
        if not item:
            continue
        hits = sorted(glob.glob(item)) if any(c in item for c in "*?[") else []
        out.extend(hits or [item])
    return out


# CAM++ 上游权重目录，例如 <上游模型目录>\campplus_sv（用 VOICEUNLOCK_SV_DIR 指定）
SV_DIR = os.environ.get("VOICEUNLOCK_SV_DIR") or os.path.join(ROOT, "models", "campplus_sv")
# 旧声纹文件（例如 <旧声纹目录>\.voiceprint.npy）
OLD_VP = os.environ.get("VOICEUNLOCK_OLD_VP", "").strip()
if not OLD_VP:
    print("用法错误：请设置环境变量 VOICEUNLOCK_OLD_VP（旧声纹 .npy 路径）。例如：%s"
          % r"set VOICEUNLOCK_OLD_VP=<旧声纹目录>\.voiceprint.npy", file=sys.stderr)
    raise SystemExit(2)
REAL = _env_paths("VU_REAL_WAVS", r"set VU_REAL_WAVS=<音频目录>\a.wav;<音频目录>\b.wav")


def read_wav(path):
    import wave
    with wave.open(path, "rb") as w:
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a / 32768.0, sr


def resample(a, sr_in, sr_out):
    import math
    if sr_in == sr_out:
        return a
    g = math.gcd(int(sr_in), int(sr_out))
    cutoff = 0.45 * sr_out / sr_in
    N = 64
    n = np.arange(-N // 2 + 1, N // 2 + 1)
    h = np.sinc(2 * cutoff * n) * np.hamming(len(n))
    h /= h.sum()
    a = np.convolve(a, h, mode="same")
    n_out = int(round(len(a) * sr_out / sr_in))
    return np.interp(np.arange(n_out) * (sr_in / sr_out), np.arange(len(a)), a).astype(np.float32)


def cos(x, y):
    x = np.asarray(x, dtype=np.float64).ravel()
    y = np.asarray(y, dtype=np.float64).ravel()
    return float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y)))


def main() -> int:
    cli.fix_console()
    old = np.load(OLD_VP)
    old = np.asarray(old, dtype=np.float64).ravel()
    old = old / np.linalg.norm(old)

    t0 = time.time()
    from funasr import AutoModel
    print("[i] import funasr 完成 %.1fs" % (time.time() - t0), flush=True)
    am = AutoModel(model=SV_DIR, device="cpu", disable_update=True,
                   disable_pbar=True, disable_log=True)

    def embed(a):
        r = am.generate(input=np.asarray(a, dtype=np.float32), cache={})
        e = r[0]["spk_embedding"]
        e = e.detach().cpu().numpy() if hasattr(e, "detach") else np.asarray(e)
        return np.asarray(e, dtype=np.float64).ravel()

    print("\n" + "=" * 84)
    print("1) 用【与登记完全相同的前端】比你的真实录音")
    print("=" * 84)
    print("%-42s %10s  %s" % ("文件", "cos(旧)", "说明"))
    scores = []
    for p in REAL:
        if not os.path.isfile(p):
            print("%-42s %10s  %s" % (os.path.basename(p)[:40], "-", "不存在"))
            continue
        a, sr = read_wav(p)
        a = resample(a, sr, 16000)
        c = cos(embed(a), old)
        scores.append(c)
        print("%-42s %10.4f  %.2f 秒, 原 %dHz" %
              (os.path.basename(p)[:40], c, len(a) / 16000, sr))

    print("\n" + "=" * 84)
    print("2) 旧声纹 vs 纯静音 / 低电平噪声  —— 验证『登记时录到了静音』这个猜想")
    print("=" * 84)
    rng = np.random.RandomState(0)
    cases = {
        "纯静音 2.0s": np.zeros(32000, dtype=np.float32),
        "纯静音 6.0s": np.zeros(96000, dtype=np.float32),
        "噪声 1e-4": (rng.randn(32000) * 1e-4).astype(np.float32),
        "噪声 1e-3": (rng.randn(32000) * 1e-3).astype(np.float32),
    }
    for k, v in cases.items():
        print("%-42s %10.4f" % (k, cos(embed(v), old)))

    print("\n" + "=" * 84)
    print("3) 旧声纹 vs 官方示例的两位说话人  —— 看它是不是像某个陌生人")
    print("=" * 84)
    for nm in ["speaker1_a_cn_16k.wav", "speaker1_b_cn_16k.wav", "speaker2_a_cn_16k.wav"]:
        p = os.path.join(SV_DIR, "examples", nm)
        if os.path.isfile(p):
            a, sr = read_wav(p)
            print("%-42s %10.4f" % (nm, cos(embed(a), old)))

    print("\n" + "=" * 84)
    print("结论")
    print("=" * 84)
    if scores:
        hi = max(scores)
        print("原前端下 你的录音 vs 旧声纹：最高 %.4f" % hi)
        if hi < 0.15:
            print("  => 即使是与登记完全相同的前端也对不上。")
            print("     『前端不同』解释不了这个结果（那通常还剩 0.2~0.5）。")
            print("     最可能：登记时设备没拾到你的声音（录到了静音/噪声），")
            print("     所以那份 .voiceprint.npy 从来就不是你的声纹。")
            print("     -> 必须重新登记一次。")
        elif hi >= 0.31:
            print("  => 原前端下能对上，说明旧声纹本身是有效的，")
            print("     单纯是新前端（16k 引擎重采样 vs 96k np.interp）的差异。")
            print("     -> 可以用原前端给已有录音重算一份向量迁移过来。")
        else:
            print("  => 介于两者之间：旧声纹有效但条件差异大，建议重新登记。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
