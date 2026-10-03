"""bootstrap_profile.py -- 用你已有的历史录音引导出声纹档案（不需要重新说话）

为什么需要这个：
    旧的那份 .voiceprint.npy 是 2026-10-02 16:08 那次【有 bug 的登记】留下的
    （登记用 48000/单声道、运行时用 96000/立体声），13 分钟后 enroll_voice.py 才被修正，
    而修正后没有重新登记 —— 目录里只有那一个 .npy。实测它与你所有真实录音的 cos
    都在 ±0.03 以内，且与静音、噪声、官方示例说话人同样都是 ~0。**不可用。**

但是你有干净的本人录音，所以可以：
    1. 先用它们互相验证（同一批录音之间是否一致 —— 这是判断"它们是不是同一个人"的关键）
    2. 用它们引导出一份档案（挂在 id `legacy:recordings` 上）
    3. 这份档案会走【跨设备兜底】参与判定（因为跨设备兜底是对所有档案取最大），
       所以 USB Audio / WO Mic 等新设备立刻就能用；第一次解锁成功后会被自动登记成一等。

用法（两个录音清单都从环境变量读，不用改脚本）：
    set VU_GENUINE_WAVS=<音频目录>\\ref1.wav;<音频目录>\\ref2.wav
    set VU_IMPOSTOR_WAVS=<音频目录>\\tts.wav        （可选；不设就跳过第 2 节）
    venv\\Scripts\\python.exe tools\\bootstrap_profile.py            # 只测量，不写档
    venv\\Scripts\\python.exe tools\\bootstrap_profile.py --write    # 测量并写入档案
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

from src import cli                      # noqa: E402
from src import sv as S                  # noqa: E402
from src.profiles import ProfileStore    # noqa: E402

LEGACY_ID = "legacy:recordings"


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


# 你自己的录音（尽量取干净、单独一个人的）
GENUINE = _env_paths("VU_GENUINE_WAVS",
                     r"set VU_GENUINE_WAVS=<音频目录>\ref1.wav;<音频目录>\ref2.wav")
# 外来声音：官方示例说话人 + 你自己的 TTS 克隆音（可选，不设就只做第 1 节）
IMPOSTOR = _env_paths("VU_IMPOSTOR_WAVS",
                      r"set VU_IMPOSTOR_WAVS=<音频目录>\tts.wav", required=False)


def read_any(path):
    with wave.open(path, "rb") as w:
        sr, ch, n, sw = w.getframerate(), w.getnchannels(), w.getnframes(), w.getsampwidth()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError("只支持 16bit wav")
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    a = a / 32768.0
    if sr != S.SR:
        a = _resample(a, sr, S.SR)
    return a, sr


def _resample(a, sr_in, sr_out):
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


def main() -> int:
    cli.fix_console()
    write = "--write" in sys.argv
    emb = S.SpeakerEmbedder()
    T = emb.target_frames
    need = S.samples_for_frames(T)

    print("运行时: %s  T=%d (%.2f 秒/段)" % (os.path.basename(emb.onnx_path),
                                             T, need / S.SR))

    def embed_file(p):
        a, sr0 = read_any(p)
        if a.size < need:
            return None, "太短 %.2fs" % (a.size / S.SR)
        v, _ = emb.embed_audio(a, S.SR)
        return v, "%.2fs 原%dk" % (a.size / S.SR, sr0 // 1000)

    print("\n" + "=" * 90)
    print("1) 你自己的录音：两两相似度（关键 —— 它们必须是同一个人）")
    print("=" * 90)
    names, vecs = [], []
    for p in GENUINE:
        if not os.path.isfile(p):
            print("  [跳过] %s（不存在）" % os.path.basename(p))
            continue
        v, note = embed_file(p)
        if v is None:
            print("  [跳过] %s（%s）" % (os.path.basename(p), note))
            continue
        names.append(os.path.basename(p))
        vecs.append(v)
        print("  %-28s %s" % (names[-1], note))

    if len(vecs) < 2:
        print("\n可用录音不足 2 个，无法引导档案。")
        return 1

    n = len(vecs)
    M = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            M[i, j] = S.cos(vecs[i], vecs[j])
    print("\n相似度矩阵：")
    print("      " + "".join("%9s" % nm[:8] for nm in names))
    for i in range(n):
        print("%-6s" % names[i][:6] + "".join("%9.3f" % M[i, j] for j in range(n)))

    off = [M[i, j] for i in range(n) for j in range(i + 1, n)]
    self_min, self_med, self_max = float(min(off)), float(np.median(off)), float(max(off))
    print("\n本人内部: 最低 %.4f  中位 %.4f  最高 %.4f  (n=%d 对)" %
          (self_min, self_med, self_max, len(off)))

    # 离群检查：跟别人最不像的那个
    mean_to_others = [(M[i].sum() - 1.0) / (n - 1) for i in range(n)]
    worst = int(np.argmin(mean_to_others))
    print("最不像其它录音的是: %s (平均 %.3f)" % (names[worst], mean_to_others[worst]))

    print("\n" + "=" * 90)
    print("2) 外来声音：对你这批录音的最高相似度（应当低于本人内部最低值）")
    print("=" * 90)
    imp = []
    for p in IMPOSTOR:
        if not os.path.isfile(p):
            continue
        v, note = embed_file(p)
        if v is None:
            continue
        c = max(S.cos(v, x) for x in vecs)
        imp.append((os.path.basename(p), c))
        print("  %-30s max cos = %.4f   %s" % (os.path.basename(p), c, note))
    imp_max = max(c for _, c in imp) if imp else None

    print("\n" + "=" * 90)
    print("3) 结论与阈值")
    print("=" * 90)

    # 先查重复样本（不同文件名、同一段音频会让统计虚高）
    dups = [(names[i], names[j], M[i, j])
            for i in range(n) for j in range(i + 1, n) if M[i, j] > 0.999]
    for a, b, c in dups:
        print("  [!] %s 与 %s 相似度 %.4f —— 是同一段音频，样本数要按去重后算" % (a, b, c))

    if imp_max is not None and imp_max >= self_min:
        print("  [!] 外来声音最高分 %.4f >= 本人内部最低分 %.4f" % (imp_max, self_min))
        print("      => 用原始 cosine 【无法分开】换条件的本人 与 这个外来声音。")
        if imp_max > 0.5:
            print("      而这个高分外来声音极可能是【你自己音色的 TTS 克隆音】")
            print("      （本机就有 GPT-SoVITS，且 audio (25).wav 正是它的 zh 参考音频）。")
            print("      代价：把克隆音的参考音频拿去登记，会让克隆分更高 —— 等于自己喂它。")
        print("      => 结论：声纹不能作为唯一因子。要么堵死克隆音进入判定的通道")
        print("         （trusted/untrusted 白名单、只用物理麦），要么引入专门的")
        print("         活体/反欺诈模型。调阈值解决不了。详见方案 §16.7。")
        thr = round(max(0.20, self_min - 0.10), 2)
        print("      若仍要引导档案，只能取保守阈值 %.2f（仅作便捷因素）。" % thr)
    elif self_min >= 0.35 and imp_max is not None and imp_max < self_min:
        thr = round((self_min + imp_max) / 2.0, 2)
        print("  [OK] 本人内部最低 %.4f，外来最高 %.4f —— 可分。" % (self_min, imp_max))
        print("  建议阈值 = %.2f（官方锚点 0.31）" % thr)
    elif self_min >= 0.25:
        thr = round(max(0.20, self_min - 0.10), 2)
        print("  [!] 本人内部差异偏大（最低 %.4f），或用的是不同设备的录音。" % self_min)
        print("  取保守阈值 = %.2f；建议之后用真实设备重录一遍提高质量。" % thr)
    else:
        print("  [!] 本人内部最低只有 %.4f —— 这批录音可能不是同一个人/同一条件，" % self_min)
        print("      不建议用它们引导档案。请用 --device 在目标设备上正式登记。")
        return 1

    mean = np.mean(np.stack(vecs), axis=0)
    mean = mean / np.linalg.norm(mean)

    st = ProfileStore()
    if write:
        st.put(LEGACY_ID, "历史录音引导（非设备绑定）", mean, thr,
               samples=len(vecs), self_min=self_min, other_max=imp_max,
               source="bootstrap",
               extra={"note": "由 tools/bootstrap_profile.py 从历史录音引导；"
                              "跨设备兜底时对所有设备生效"})
        st.save()
        print("\n  已写入档案: %s" % st.path)
        print("  id = %s" % LEGACY_ID)
        print("\n  下一步：直接试一次判定（新设备会走跨设备兜底，需说满 3 段）")
        print("      .\\venv\\Scripts\\python.exe -m src.enroll --test")
        print("  第一次解锁成功后，USB Audio 会被【自动登记】成一等设备。")
    else:
        print("\n  （未写入。加 --write 才落盘）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
