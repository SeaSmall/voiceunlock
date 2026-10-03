"""selftest.py -- 运行时自测（**不需要麦克风**，可在任何机器上跑）

它做四件事：
  1. 契约单测：帧数/采样数换算、对齐判定、非法 T 必须报错
  2. 基准对齐：用运行时模块复算 funasr 的基准答案，cos 必须 > 0.9999
     （基准由 tools/dump_reference.py 在 funasr 环境生成）
  3. 生产路径：embed_audio(target=200) 走一遍，检查同人/异人区分度是否正常
  4. 同源去重：人造"延迟+衰减的同一信号"应当被判为同源，不同音频应当判为不同源

用法：
    .\\venv\\Scripts\\python.exe -m src.selftest
    （在项目根目录下，或把该目录加进 PYTHONPATH）
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import audio as A          # noqa: E402
from src import sv as S             # noqa: E402

# 例子音频目录：优先环境变量，否则按【项目根目录的同级 models\campplus_sv】推导。
# ★ 绝不写死盘符：那既是构建机的私有信息，也会让这个自测在别人的机器上必然失败。
_SV_DIR = (os.environ.get("VOICEUNLOCK_SV_DIR")
           or os.path.join(ROOT, os.pardir, "models", "campplus_sv"))
EXAMPLES = os.path.join(_SV_DIR, "examples")
REF = os.path.join(ROOT, "logs", "ref")
NAMES = ["speaker1_a_cn_16k.wav", "speaker1_b_cn_16k.wav", "speaker2_a_cn_16k.wav"]

FAILS = []


def check(label, ok, detail=""):
    print("  [%s] %-38s %s" % ("OK" if ok else "FAIL", label, detail))
    if not ok:
        FAILS.append(label)
    return ok


def section(t):
    print("\n" + "=" * 72)
    print("### %s" % t)
    print("=" * 72)


def main() -> int:
    section("1. 时间维契约单测")
    check("samples_for_frames(200) == 32240", S.samples_for_frames(200) == 32240,
          "= %d (%.3f 秒)" % (S.samples_for_frames(200), S.samples_for_frames(200) / S.SR))
    for T in (199, 200, 399, 400, 599, 600):
        n = S.samples_for_frames(T)
        check("往返 T=%d -> %d 采样 -> %d 帧" % (T, n, S.frames_for_samples(n)),
              S.frames_for_samples(n) == T)
    check("is_aligned(200) 为真", S.is_aligned(200))
    check("is_aligned(300) 为假（L=150）", not S.is_aligned(300),
          "L=%d" % S.tdnn_len(300))
    check("aligned_at_most(370) == 200", S.aligned_at_most(370) == 200,
          "= %d" % S.aligned_at_most(370))
    try:
        S.SpeakerEmbedder(target_frames=300)
        check("非法 T=300 必须报错", False, "竟然没报错")
    except ValueError as e:
        check("非法 T=300 必须报错", True, str(e)[:60])

    section("2. 加载 ONNX")
    emb = S.SpeakerEmbedder()
    print("  模型      : %s (%.1f MB)" % (emb.onnx_path,
                                          os.path.getsize(emb.onnx_path) / 1e6))
    print("  输入/输出 : %s -> %s" % (emb.input_name, emb.output_name))
    print("  目标帧数  : %d (%.3f 秒)" % (emb.target_frames,
                                          S.samples_for_frames(emb.target_frames) / S.SR))

    section("3. 基准对齐（运行时模块 vs funasr 基准）")
    refs = {}
    for name in NAMES:
        stem = name.replace(".wav", "")
        ref_path = os.path.join(REF, stem + "_emb_aligned.npy")
        if not os.path.isfile(ref_path):
            check("基准存在: %s" % stem, False, "缺 %s" % ref_path)
            continue
        ref = np.load(ref_path)
        a, sr = A.read_wav(os.path.join(EXAMPLES, name))
        fb_full = S.fbank(a, sr)                       # 全长
        fb_cmn = S.cmn(fb_full)                        # 全长 CMN（与基准同协议）
        T = S.aligned_at_most(fb_cmn.shape[0])
        v = emb.embed_fbank(fb_cmn[:T], do_cmn=False)  # 已 CMN，别再 CMN 一次
        c = S.cos(v, ref)
        check("%s (T=%d)" % (stem, T), c > 0.9999, "cos=%.8f" % c)
        refs[name] = v

    if len(refs) == 3:
        same = S.cos(refs[NAMES[0]], refs[NAMES[1]])
        d1 = S.cos(refs[NAMES[0]], refs[NAMES[2]])
        d2 = S.cos(refs[NAMES[1]], refs[NAMES[2]])
        check("同人 > 0.4", same > 0.4, "cos=%.4f" % same)
        check("异人 < 0.1", max(d1, d2) < 0.1, "cos=%.4f / %.4f" % (d1, d2))

    section("4. 生产路径 embed_audio(target=%d)" % emb.target_frames)
    prod = {}
    for name in NAMES:
        a, sr = A.read_wav(os.path.join(EXAMPLES, name))
        t0 = time.time()
        v, info = emb.embed_audio(a, sr)
        dt = (time.time() - t0) * 1000
        st = A.speech_stats(a[:S.samples_for_frames(emb.target_frames)])
        prod[name] = v
        print("  %-24s 入=%d 裁掉=%d 出=192  %.1fms  峰值=%.4f 活跃=%.2f" %
              (name.replace("_cn_16k.wav", ""), info["samples_in"], info["cropped"],
               dt, st["peak"], st["active_ratio"]))
    if len(prod) == 3:
        same = S.cos(prod[NAMES[0]], prod[NAMES[1]])
        d1 = S.cos(prod[NAMES[0]], prod[NAMES[2]])
        d2 = S.cos(prod[NAMES[1]], prod[NAMES[2]])
        imp = max(d1, d2)
        # 这里要验的不是"异人分数必须多低"（那是拍脑袋），
        # 而是【判定阈值能否把本人和他人分开】—— 官方 yesOrno_thr = 0.31。
        thr = 0.31
        check("生产路径 本人 > 判定阈值 %.2f" % thr, same > thr, "cos=%.4f" % same)
        check("生产路径 他人 < 判定阈值 %.2f" % thr, imp < thr,
              "cos=%.4f / %.4f" % (d1, d2))
        check("同人-异人 间隔 > 0.3", same - imp > 0.3, "间隔=%.4f" % (same - imp))
        print("  （生产路径的异人分比基准路径略高是正常的：CMN 只跨 2 秒，")
        print("    段越短区分度越低。两段投票 + 3 次失败锁定会再抬高攻击成本。）")

    # 严格模式：音频不足必须报错
    try:
        emb.embed_audio(np.zeros(1000, dtype=np.float32), strict=True)
        check("音频过短必须报错", False, "竟然没报错")
    except ValueError as e:
        check("音频过短必须报错", True, str(e)[:56])

    # 采集长度必须等于切片长度（踩过：samples_for_frames(T)*n != samples_for_frames(T*n)）
    for nseg in (1, 3):
        want = emb.samples_for_segments(nseg)
        try:
            embs, info = emb.embed_segments(np.zeros(want, dtype=np.float32),
                                            n_segments=nseg)
            ok_len = (info["padded"] == 0 and info["cropped"] == 0
                      and len(embs) == nseg)
            check("采集长度==切片长度 n=%d (需 %d 采样)" % (nseg, want), ok_len,
                  "padded=%d cropped=%d 段数=%d" % (info["padded"], info["cropped"],
                                                    len(embs)))
        except Exception as e:
            check("采集长度==切片长度 n=%d" % nseg, False, str(e)[:60])

    section("5. 采样级性能（T=%d）" % emb.target_frames)
    a, sr = A.read_wav(os.path.join(EXAMPLES, NAMES[0]))
    sub = a[:S.samples_for_frames(emb.target_frames)]
    for _ in range(3):
        emb.embed_audio(sub, sr)
    t0 = time.time()
    N = 20
    for _ in range(N):
        emb.embed_audio(sub, sr)
    per = (time.time() - t0) / N * 1000
    print("  fbank + ONNX 合计 %.1f ms/次（%.3f 秒音频）" % (per, len(sub) / sr))
    check("单次 < 60ms", per < 60, "%.1f ms" % per)

    section("6. 同源去重")
    a1, sr = A.read_wav(os.path.join(EXAMPLES, NAMES[0]))
    a2, sr = A.read_wav(os.path.join(EXAMPLES, NAMES[2]))
    n = S.samples_for_frames(emb.target_frames)
    a1 = a1[:n]
    delayed = np.pad(a1, (1600, 0))[:n] * 0.6          # 延迟 100ms + 衰减
    s_same = A.same_source_score(a1, delayed)
    s_diff = A.same_source_score(a1, a2[:n])
    check("同源（延迟+衰减）> 0.9", s_same > 0.9, "score=%.4f" % s_same)
    check("异源 < 0.6", s_diff < 0.6, "score=%.4f" % s_diff)
    groups = A.group_same_source({"devA": a1, "devB": delayed, "devC": a2[:n]})
    check("归组结果 = 2 组", len(groups) == 2,
          " -> ".join("+".join(g["ids"]) for g in groups))
    check("低电平段被判静音", A.speech_stats(np.zeros(16000, dtype=np.float32))["silent"])

    section("7. 自动建档的取窗与质量闸（踩过的坑）")
    # 坑：原来建档是拿【原始缓冲的前 2 秒】去嵌入。常驻监听采的是 3 倍长缓冲、
    #     语音在中间 —— 于是建档算的是开头那段静音，建出来的是噪声，
    #     之后该设备怎么考都只有 0.24，而阈值又是默认的 0.31，本人被自己的档案拒了。
    import shutil                              # noqa: PLC0415
    import tempfile                            # noqa: PLC0415

    from src.profiles import ProfileStore      # noqa: PLC0415
    from src.verify import State, Verifier     # noqa: PLC0415

    tmp = tempfile.mkdtemp(prefix="vu-enroll-")
    try:
        ver = Verifier(store=ProfileStore(os.path.join(tmp, "p.json")),
                       embedder=emb,
                       state=State(os.path.join(tmp, "s.json")),
                       logger=lambda m: None)
        n = S.samples_for_frames(emb.target_frames)          # 2.015 秒
        speech, sr = A.read_wav(os.path.join(EXAMPLES, NAMES[0]))
        speech = speech[:n]
        # (a) 前后各 4 秒静音，语音在正中间
        padded = np.concatenate([np.zeros(n * 2, dtype=np.float32), speech,
                                 np.zeros(n * 2, dtype=np.float32)])
        vec, info = ver._enroll_vector(padded)
        v_direct = emb.embed_audio(speech)[0]
        c = float(np.dot(vec, v_direct))
        check("语音在中间 -> 能建档", vec.size == 192, "192 维")
        check("建出来的是【语音那段】而不是开头静音（cos>0.98）", c > 0.98,
              "cos=%.4f（旧代码会拿开头静音，cos 会很低）" % c)

        # (b) 长静音里一个 50ms 的"喀哒"：能剪出一段，但根本不够语音 -> 必须拒绝
        blip = np.zeros(n * 4, dtype=np.float32)
        blip[n * 2: n * 2 + int(0.05 * S.SR)] = 0.2
        try:
            ver._enroll_vector(blip)
            check("只要一声喀哒 -> 拒绝建档", False, "居然建档成功了（质量闸没生效）")
        except ValueError as e:
            check("只要一声喀哒 -> 拒绝建档", True, str(e)[:56])

        # (c) 纯静音 -> 拒绝
        try:
            ver._enroll_vector(np.zeros(n * 4, dtype=np.float32))
            check("纯静音 -> 拒绝建档", False, "居然建档成功了")
        except ValueError as e:
            check("纯静音 -> 拒绝建档", True, str(e)[:56])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 72)
    if FAILS:
        print("SELFTEST FAILED (%d 项): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("SELFTEST OK —— 运行时内核可用，且不依赖 torch/funasr")
    return 0


if __name__ == "__main__":
    sys.exit(main())
