"""check_existing_voiceprint.py -- 你已有的 .voiceprint.npy 还能不能用？

背景
    旧的那份 .voiceprint.npy 是 enroll_voice.py 产出的 192 维 CAM++ 向量，
    模型与我们完全相同，但**前端不同**：
        旧：soundcard 96000Hz/2ch -> np.interp 线性降采样到 16k（无抗混叠）
        新：直接向设备请求 16000Hz，由 WASAPI 音频引擎重采样
        （旧代码注释里记录过：两边插值方式不同，本人相似度会掉到 0.15）
    所以"能不能沿用"必须**实测**，不能推理。

这个脚本做两件事（都不需要重新录你的声音）
    1. 用新运行时给你已有的真实录音算向量，与旧 .voiceprint.npy 比 —— 判断能否沿用
    2. 顺便拿你自己的 GPT-SoVITS 克隆音也来比 —— 量化"克隆音能不能骗过声纹"

用法（录音清单从环境变量读，不用改脚本）
    set VOICEUNLOCK_OLD_VP=<旧声纹目录>\\.voiceprint.npy
    set VU_REAL_WAVS=<音频目录>\\a.wav;<音频目录>\\b.wav
    set VU_CLONE_WAVS=<音频目录>\\tts_*.wav        （可选，支持 * 通配）
    venv\\Scripts\\python.exe tools\\check_existing_voiceprint.py
"""
from __future__ import annotations

import glob
import os
import sys
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import cli      # noqa: E402
from src import sv as S      # noqa: E402

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


# 旧声纹文件（例如 <旧声纹目录>\.voiceprint.npy）
OLD_VP = os.environ.get("VOICEUNLOCK_OLD_VP", "").strip()
if not OLD_VP:
    print("用法错误：请设置环境变量 VOICEUNLOCK_OLD_VP（旧声纹 .npy 路径）。例如：%s"
          % r"set VOICEUNLOCK_OLD_VP=<旧声纹目录>\.voiceprint.npy", file=sys.stderr)
    raise SystemExit(2)

# 你自己的真实录音（照抄、朗读之类）
REAL = _env_paths("VU_REAL_WAVS", r"set VU_REAL_WAVS=<音频目录>\a.wav;<音频目录>\b.wav")
# 你自己的 GPT-SoVITS 克隆音（不该匹配得太好；可选）
CLONE = _env_paths("VU_CLONE_WAVS", r"set VU_CLONE_WAVS=<音频目录>\tts_*.wav",
                   required=False)


def read_wav_any(path):
    """读任意 16bit wav，必要时重采样到 16k。

    [!] 这里的重采样是【诊断用】的简化实现（窗函数 sinc 低通 + 线性插值）。
    生产路径不这样做 —— 生产是直接请求 16k 由音频引擎重采样。
    """
    with wave.open(path, "rb") as w:
        sr, ch, n, sw = w.getframerate(), w.getnchannels(), w.getnframes(), w.getsampwidth()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError("只支持 16bit wav（%d bit）" % (sw * 8))
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    a = a / 32768.0
    if sr != S.SR:
        a = _resample(a, sr, S.SR)
    return a, sr


def _resample(a, sr_in, sr_out):
    import math
    g = math.gcd(int(sr_in), int(sr_out))
    up, down = sr_out // g, sr_in // g
    cutoff = 0.45 * sr_out / sr_in
    N = 64
    n = np.arange(-N // 2 + 1, N // 2 + 1)
    h = np.sinc(2 * cutoff * n) * np.hamming(len(n))
    h /= h.sum()
    a = np.convolve(a, h, mode="same")
    n_out = int(round(len(a) * sr_out / sr_in))
    return np.interp(np.arange(n_out) * (sr_in / sr_out),
                     np.arange(len(a)), a).astype(np.float32)


def main() -> int:
    cli.fix_console()
    if not os.path.isfile(OLD_VP):
        print("找不到 %s" % OLD_VP)
        return 1
    old = np.load(OLD_VP)
    old = np.asarray(old, dtype=np.float64).reshape(-1)
    old = old / np.linalg.norm(old)
    print("旧声纹 : %s  维度=%d  范数=%.6f" % (OLD_VP, old.size, np.linalg.norm(old)))

    emb = S.SpeakerEmbedder()
    print("新运行时: %s  (target T=%d)" % (os.path.basename(emb.onnx_path),
                                          emb.target_frames))
    T = emb.target_frames

    def score(path):
        try:
            a, sr0 = read_wav_any(path)
        except Exception as e:
            return None, "读取失败: %s" % str(e)[:60]
        need = S.samples_for_frames(T)
        if a.size < need:
            return None, "太短 (%.2f 秒 < %.2f 秒)" % (a.size / S.SR, need / S.SR)
        v, info = emb.embed_audio(a, S.SR)
        return S.cos(v, old), "%.2f 秒, 原 %dHz" % (a.size / S.SR, sr0)

    print("\n" + "=" * 88)
    print("1) 你的真实录音 vs 旧声纹  —— 判断能否沿用")
    print("=" * 88)
    print("%-44s %10s  %s" % ("文件", "cos(旧)", "说明"))
    real_scores = []
    for p in REAL:
        if not os.path.isfile(p):
            print("%-44s %10s  %s" % (os.path.basename(p), "-", "文件不存在"))
            continue
        c, note = score(p)
        if c is None:
            print("%-44s %10s  %s" % (os.path.basename(p)[:42], "-", note))
            continue
        real_scores.append(c)
        print("%-44s %10.4f  %s" % (os.path.basename(p)[:42], c, note))

    print("\n" + "=" * 88)
    print("2) 你自己的克隆音 vs 旧声纹  —— 量化克隆风险（不该高）")
    print("=" * 88)
    print("%-44s %10s  %s" % ("文件", "cos(旧)", "说明"))
    clone_scores = []
    for p in CLONE:
        if not os.path.isfile(p):
            print("%-44s %10s  %s" % (os.path.basename(p)[:42], "-", "文件不存在"))
            continue
        c, note = score(p)
        if c is None:
            print("%-44s %10s  %s" % (os.path.basename(p)[:42], "-", note))
            continue
        clone_scores.append(c)
        print("%-44s %10.4f  %s" % (os.path.basename(p)[:42], c, note))

    print("\n" + "=" * 88)
    print("结论")
    print("=" * 88)
    thr = 0.31
    if real_scores:
        lo, hi = min(real_scores), max(real_scores)
        print("真实录音: 最低 %.4f  最高 %.4f  (n=%d)" % (lo, hi, len(real_scores)))
        usable = hi >= thr
        print("  旧声纹 %s 阈值 %.2f" % ("达到" if usable else "未达到", thr))
        if usable and lo < thr:
            print("  [!] 部分录音达标、部分不达标 —— 说明旧前端与旧设备条件不一致，")
            print("    沿用会时灵时不灵，建议用新运行时重新登记（说一次话）。")
        elif usable:
            print("  [OK] 旧声纹在新运行时下仍然可用 —— 可以作为初始档案，")
            print("     但注意它绑定的是【当时那台设备】的信道，换设备仍会掉分（走跨设备兜底）。")
        else:
            print("  [!] 旧声纹在新前端下对不上 —— 正是『插值路径不同』那个老问题。")
            print("     只能重新登记一次（4 秒）。这不是重复劳动，是前端换了。")
    if clone_scores:
        cmax = max(clone_scores)
        print("克隆音  : 最高 %.4f" % cmax)
        if cmax >= thr:
            print("  [!] 你的 TTS 克隆音能跨过阈值 %.2f —— 这正是 §5.2 警告的风险，"
                  "解锁绝不能走混音总线，且需要防重放手段。" % thr)
        else:
            print("  [OK] 克隆音未跨过阈值，当前模型对自建音色有一定抵抗力（但别当安全保障）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
