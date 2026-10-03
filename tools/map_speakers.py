"""map_speakers.py -- 把一堆音频文件的"说话人归属"测出来（谁和谁是同一个人）

为什么需要：我先前把某个音频目录里几个 wav 当成了"本人的录音"，实际查证发现
    _spkA1/_spkA2 是 _test_sv.py 把 audio (25).wav 切成两半得到的，
    而 audio (25).wav 是 GPT-SoVITS zh 模型的【参考音频】（prewarm.py 里的 ref），
    ref_ja_1.wav 是 ja 模型的参考音频（_test_sv.py 里被标成"说话人B"）。
    => 这些文件里到底有没有"本人的声音"，必须靠测量而不是推断。

这个脚本把每个文件用我们运行时算一个向量，打印两两相似度矩阵 + 聚类，
从而回答"这里面有几个不同的说话人、谁和谁一组"。

用法：
    venv\\Scripts\\python.exe tools\\map_speakers.py

要测哪些文件：默认读环境变量 VU_SPEAKER_WAVS（多个路径用 ';' 分隔，支持 glob），
例如  set VU_SPEAKER_WAVS=<音频目录>\\a.wav;<音频目录>\\*.wav
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

from src import cli     # noqa: E402
from src import sv as S  # noqa: E402


def _env_paths(var, example):
    """读环境变量里的路径清单（多个用 ';' 分隔，支持 * 通配）；没设就报用法错误。"""
    raw = os.environ.get(var, "").strip()
    if not raw:
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


FILES = _env_paths("VU_SPEAKER_WAVS",
                   r"set VU_SPEAKER_WAVS=<音频目录>\a.wav;<音频目录>\b.wav")


def read_any(path):
    with wave.open(path, "rb") as w:
        sr, ch, n, sw = w.getframerate(), w.getnchannels(), w.getnframes(), w.getsampwidth()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError("非 16bit")
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    a = a / 32768.0
    if sr != S.SR:
        import math
        g = math.gcd(int(sr), S.SR)
        cutoff = 0.45 * S.SR / sr
        N = 64
        n2 = np.arange(-N // 2 + 1, N // 2 + 1)
        h = np.sinc(2 * cutoff * n2) * np.hamming(len(n2))
        h /= h.sum()
        a = np.convolve(a, h, mode="same")
        n_out = int(round(len(a) * S.SR / sr))
        a = np.interp(np.arange(n_out) * (sr / S.SR), np.arange(len(a)), a).astype(np.float32)
    return a, sr


def main() -> int:
    cli.fix_console()
    emb = S.SpeakerEmbedder()
    need = S.samples_for_frames(emb.target_frames)
    names, vecs = [], []
    print("运行时 %s  T=%d\n" % (os.path.basename(emb.onnx_path), emb.target_frames))
    for p in FILES:
        tag = os.path.basename(p)
        if not os.path.isfile(p):
            print("  [跳过] %-42s 不存在" % tag)
            continue
        try:
            a, sr = read_any(p)
        except Exception as e:
            print("  [跳过] %-42s %s" % (tag, e))
            continue
        if a.size < need:
            print("  [跳过] %-42s 太短 %.2fs" % (tag, a.size / S.SR))
            continue
        v, _ = emb.embed_audio(a, S.SR)
        # 标签：取前 2 秒（embed_audio 也是取前 2.015 秒），保证与向量一致
        names.append(tag)
        vecs.append(v)
        print("  %-42s %.2fs 原%dk" % (tag, a.size / S.SR, sr // 1000))

    n = len(vecs)
    M = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            M[i, j] = S.cos(vecs[i], vecs[j])

    print("\n两两相似度矩阵：")
    short = [x[:11] for x in names]
    print(" " * 13 + "".join("%12s" % s for s in short))
    for i in range(n):
        print("%-12s " % short[i] + "".join("%12.3f" % M[i, j] for j in range(n)))

    # 简单聚类：>0.5 视为同一说话人
    THR = 0.5
    cluster = [-1] * n
    cid = 0
    for i in range(n):
        if cluster[i] != -1:
            continue
        cluster[i] = cid
        for j in range(i + 1, n):
            if cluster[j] == -1 and M[i, j] > THR:
                cluster[j] = cid
        cid += 1
    print("\n按 cos > %.2f 聚类：" % THR)
    for c in range(cid):
        members = [names[i] for i in range(n) if cluster[i] == c]
        print("  组%d: %s" % (c + 1, " | ".join(members)))
    print("\n注意：同一组 = 同一说话人（或同一段音频的切片）；")
    print("      不同组之间的高相似度才说明可能是同一个人换了条件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
