"""find_my_voice.py -- 只用你给的 .voiceprint.npy，扫本机所有可能含你声音的录音

立场：用户明确说"我发你了我的声纹，你还去别处找什么"。
      那这个脚本就【只做一件事】：拿他给的那份声纹，去比本机每一个可能含他声音的录音，
      每个文件切成滑窗，取最高相似度。

判读：
    * 某个文件里如果【有你的声音】且声纹有效 -> 分数会明显抬起（通常 >0.4）
    * 全都是低分 -> 两条可能：
        (a) 本机确实没有你的录音（最可能：参考音频是别人的，通话录音里也没你）
        (b) 声纹文件本身没有可用信息
      两者靠"是否有你的录音"区分；一旦有一段确认是你的录音，结论立刻明确。

用法：
    先设两个环境变量，再跑：
        set VU_VOICEPRINT=<声纹文件>      例如 <旧声纹目录>\\.voiceprint.npy
        set VU_SCAN_ROOTS=<扫描根目录>    多个用 ';' 分隔，例如 <目录1>;<目录2>
    venv\\Scripts\\python.exe tools\\find_my_voice.py
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


# 要查的那份声纹文件（例如 <旧声纹目录>\.voiceprint.npy）
VP = os.environ.get("VU_VOICEPRINT", "").strip()
if not VP:
    print("用法错误：请设置环境变量 VU_VOICEPRINT（声纹 .npy 路径）。例如：%s"
          % r"set VU_VOICEPRINT=<旧声纹目录>\.voiceprint.npy", file=sys.stderr)
    raise SystemExit(2)

# 要扫描的录音目录（例如 <目录1>;<目录2>）
ROOTS = _env_paths("VU_SCAN_ROOTS", r"set VU_SCAN_ROOTS=<目录1>;<目录2>")
EXTS = ("*.wav", "*.WAV", "*.mp3", "*.silk", "*.amr", "*.ogg", "*.m4a", "*.flac")

# 已知是【别人的克隆音/其参考音频】——列出来但标注，避免误读
KNOWN_OTHER = {
    "audio (25).wav": "GPT-SoVITS zh 参考音频（别人的音色）",
    "ref_ja_1.wav": "GPT-SoVITS ja 参考音频（别人的音色）",
    "_ref1.wav": "= ref_ja_1.wav 的 16k 版",
    "_ref2.wav": "同 ja 参考音色",
    "_spkA1.wav": "audio(25) 的前半",
    "_spkA2.wav": "audio(25) 的后半",
}


def read_wav16k(path):
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
        nn = np.arange(-N // 2 + 1, N // 2 + 1)
        h = np.sinc(2 * cutoff * nn) * np.hamming(len(nn))
        h /= h.sum()
        a = np.convolve(a, h, mode="same")
        n_out = int(round(len(a) * S.SR / sr))
        a = np.interp(np.arange(n_out) * (sr / S.SR), np.arange(len(a)), a).astype(np.float32)
        sr = S.SR
    return a, sr


def main() -> int:
    cli.fix_console()
    if not os.path.isfile(VP):
        print("找不到 %s" % VP)
        return 1
    vp = np.load(VP)
    vp = np.asarray(vp, dtype=np.float64).ravel()
    vp = vp / np.linalg.norm(vp)
    print("声纹文件: %s   维度=%d  范数=%.6f" % (VP, vp.size, np.linalg.norm(vp)))

    emb = S.SpeakerEmbedder()
    T = emb.target_frames
    need = S.samples_for_frames(T)
    hop = S.SR  # 1 秒滑一步
    print("运行时  : %s  窗口 %d 帧 (%.2f 秒)，滑窗步长 1 秒\n"
          % (os.path.basename(emb.onnx_path), T, need / S.SR))

    found = []
    for r in ROOTS:
        for e in EXTS:
            found += glob.glob(os.path.join(r, "**", e), recursive=True)
    seen, files = set(), []
    for f in found:
        k = os.path.normcase(os.path.abspath(f))
        if k not in seen:
            seen.add(k)
            files.append(f)

    wavs = [f for f in files if f.lower().endswith(".wav")]
    others = [f for f in files if not f.lower().endswith(".wav")]
    print("扫到 %d 个 wav（另有 %d 个非 wav，本环境读不了，仅列出路径）\n"
          % (len(wavs), len(others)))

    rows = []
    for f in wavs:
        tag = os.path.basename(f)
        try:
            a, sr = read_wav16k(f)
        except Exception as ex:
            rows.append((tag, None, "读不了: %s" % str(ex)[:40], f))
            continue
        if a.size < need:
            rows.append((tag, None, "太短 %.2fs" % (a.size / S.SR), f))
            continue
        best = -9.0
        pos = 0.0
        i = 0
        while True:
            chunk = a[i * hop:i * hop + need]
            if chunk.size < need:
                break
            try:
                v, _ = emb.embed_audio(chunk, S.SR)
                c = S.cos(v, vp)
                if c > best:
                    best, pos = c, i * hop / S.SR
            except Exception:
                pass
            i += 1
        note = KNOWN_OTHER.get(tag, "")
        rows.append((tag, best, note or ("%d 个窗口" % i), f))

    rows.sort(key=lambda r: -(r[1] if r[1] is not None else -9))
    print("%-34s %10s  %s" % ("文件", "最高cos", "说明"))
    print("-" * 100)
    for tag, sc, note, f in rows:
        s = "%.4f" % sc if sc is not None else "-"
        print("%-34s %10s  %s" % (tag[:32], s, note[:56]))

    if others:
        print("\n非 wav（含 QQ 语音 silk/amr 的可能——这些需要转码才能测）:")
        for f in others[:20]:
            print("  %s" % f)

    print("\n" + "=" * 100)
    print("判读")
    print("=" * 100)
    hi = max((r[1] for r in rows if r[1] is not None), default=None)
    if hi is None:
        print("  没有可测的 wav。")
    elif hi < 0.15:
        print("  所有文件最高只有 %.4f —— 全部贴近 0。" % hi)
        print("  两种解释，需要一段【确认是你的】录音才能区分：")
        print("    (a) 本机确实没有你的录音（已知参考音频都是别人的音色）")
        print("    (b) 声纹文件本身没有可用信息")
        print("  注意：如果连你自己刚录的一段也只有这么低，那就是 (b)。")
    else:
        print("  出现 %.4f 的窗口 —— 这个文件里很可能【有你的声音】，声纹是有效的。" % hi)
        for tag, sc, note, f in rows:
            if sc is not None and sc >= 0.3:
                print("    %s  %.4f  %s" % (tag, sc, f))
    return 0


if __name__ == "__main__":
    sys.exit(main())
