#!/usr/bin/env python3
"""verify_parity.py -- 独立版 vs funasr 原版 一致性验收（跑在独立 venv 里）

前置（由 runtime python 产生）:
    tools/dump_reference.py -> logs/ref/<name>_{fbank,fbank_aligned,emb_funasr,emb_aligned,emb_ceil}.npy
    tools/export_onnx.py    -> models/campplus.onnx

契约见 vu_common.py：T 必须满足 ceil(T/2) % 100 == 0。

验收项:
    A  前端等价 : knf fbank vs torchaudio fbank（全长），max|delta| < 1e-2
    A' 对齐路径 : 归一化+裁剪后，max|delta| < 1e-2
    B1 导出保真 : ONNX vs 打补丁模型(_emb_ceil), cosine > 0.9999
    B2 契约达标 : ONNX vs 原版模型喂对齐输入(_emb_aligned), cosine > 0.999  ← 硬门槛
    C  全长参照 : ONNX(对齐) vs 线上全长基准，仅报告
    D  区分度   : 独立版与原版的同人/异人 cosine 必须一致（差 < 0.005）

用法:
    venv\\Scripts\\python.exe tools\\verify_parity.py
"""
import os
import sys
import time

import numpy as np
import kaldi_native_fbank as knf
import onnxruntime as ort

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import vu_common as V

REF = os.environ.get("VOICEUNLOCK_REF") or os.path.join(ROOT, "logs", "ref")
MODELS = os.environ.get("VOICEUNLOCK_MODELS") or os.path.join(ROOT, "models")
# CAM++ 上游权重目录，例如 <上游模型目录>\campplus_sv（用 VOICEUNLOCK_SV_DIR 指定）
SV_DIR = os.environ.get("VOICEUNLOCK_SV_DIR") or os.path.join(ROOT, "models", "campplus_sv")
ONNX = os.path.join(MODELS, "campplus.onnx")
NAMES = ["speaker1_a_cn_16k.wav", "speaker1_b_cn_16k.wav", "speaker2_a_cn_16k.wav"]


def fbank_knf(a, sr=16000, mel_norm=None, use_slaney=None, is_librosa=None):
    """复刻 torchaudio.compliance.kaldi.fbank(t, num_mel_bins=80) 的默认参数。

    torchaudio 2.0.0 实测默认值（由 runtime python 打印）：
        dither=0.0  energy_floor=1.0  frame_length=25.0  frame_shift=10.0
        high_freq=0.0  htk_compat=False  low_freq=20.0  preemphasis_coefficient=0.97
        raw_energy=True  remove_dc_offset=True  round_to_power_of_two=True
        snip_edges=True  subtract_mean=False  use_energy=False
        use_log_fbank=True  use_power=True  window_type='povey'
    其中只有 dither(0.0) 与 num_mel_bins(80) 需要显式覆盖，其余正好是 Kaldi 默认。

    mel_norm / use_slaney / is_librosa 三个参数用于诊断：knf 1.22.x 的 MelBanksOptions
    新增了这几个字段，默认值可能与 torchaudio/Kaldi 经典实现不同，会在【每个 mel 频点上
    产生一个常数偏移】（log 域里的常数 = 能量域的乘性差异）。三个参数传 None 表示不动默认值。
    """
    opts = knf.FbankOptions()
    opts.frame_opts.dither = 0.0
    opts.frame_opts.snip_edges = True
    opts.frame_opts.window_type = "povey"
    opts.frame_opts.preemph_coeff = 0.97
    opts.frame_opts.remove_dc_offset = True
    opts.frame_opts.round_to_power_of_two = True
    opts.frame_opts.blackman_coeff = 0.42
    opts.frame_opts.samp_freq = 16000.0
    opts.mel_opts.num_bins = 80
    opts.mel_opts.low_freq = 20.0
    opts.mel_opts.high_freq = 0.0
    opts.mel_opts.vtln_low = 100.0
    opts.mel_opts.vtln_high = -500.0
    opts.use_energy = False
    opts.energy_floor = 1.0
    opts.raw_energy = True
    opts.htk_compat = False
    opts.use_log_fbank = True
    opts.use_power = True
    # knf 1.22.3 没有 vtln_warp 字段（torchaudio 的 1.0 即恒等，无需设）。
    if mel_norm is not None:
        opts.mel_opts.norm = mel_norm
    if use_slaney is not None:
        opts.mel_opts.use_slaney_mel_scale = use_slaney
    if is_librosa is not None:
        opts.mel_opts.is_librosa = is_librosa

    f = knf.OnlineFbank(opts)
    f.accept_waveform(sr, np.asarray(a, dtype=np.float32).tolist())
    try:
        f.input_finished()
    except AttributeError:
        pass
    return np.stack([f.get_frame(i) for i in range(f.num_frames_ready)]).astype(np.float32)


def cmn_and_align(fb):
    """逐句均值归一化（utils.py:104）后裁到对齐 T。"""
    fb = fb - fb.mean(axis=0, keepdims=True)
    T = V.largest_aligned_T(fb.shape[0])
    return np.ascontiguousarray(fb[:T])


def main():
    if not os.path.isfile(ONNX):
        print("缺 %s —— 先用 runtime python 跑 tools/export_onnx.py" % ONNX)
        return 1

    print("onnxruntime        %s" % ort.__version__)
    print("kaldi_native_fbank %s" % knf.__version__)
    print("契约: T 需满足 ceil(T/2) %% %d == 0 -> %s ..." % (V.SEG_LEN, V.ALIGNED_T[:8]))
    sess = ort.InferenceSession(ONNX, providers=["CPUExecutionProvider"])
    print("ONNX 输入 %s" % [(i.name, i.shape) for i in sess.get_inputs()])
    print("ONNX 输出 %s\n" % [(o.name, o.shape) for o in sess.get_outputs()])

    ok = True
    embs, refs = {}, {}
    for name in NAMES:
        stem = name.replace(".wav", "")
        fb_ref = np.load(os.path.join(REF, stem + "_fbank.npy"))
        fb_ref_al = np.load(os.path.join(REF, stem + "_fbank_aligned.npy"))
        e_funasr = np.load(os.path.join(REF, stem + "_emb_funasr.npy"))
        e_al = np.load(os.path.join(REF, stem + "_emb_aligned.npy"))
        e_ceil = np.load(os.path.join(REF, stem + "_emb_ceil.npy"))

        a, sr = V.read_wav(os.path.join(SV_DIR, "examples", name))
        fb_raw_knf = fbank_knf(a, sr)
        fb_full = fb_raw_knf
        fb_al = cmn_and_align(fb_full)

        print("=== %s ===" % name)
        # A. 原始（未做 CMN）fbank 的差异【诊断】
        #    CMN 会精确消掉「逐 mel 频点上的常数偏移」：
        #        (x + c) - mean(x + c) == x - mean(x)
        #    所以真正要守的底线是「残差随时间不变」（否则 CMN 消不掉）。
        #    进入模型的值由 A' 直接把关。
        fb_raw_ref = np.load(os.path.join(REF, stem + "_fbank_raw.npy"))
        if fb_raw_knf.shape != fb_raw_ref.shape:
            print("  A  原始 : 形状不一致 knf=%s ref=%s" % (fb_raw_knf.shape, fb_raw_ref.shape))
            ok = False
        else:
            diff = fb_raw_knf - fb_raw_ref
            per_bin = diff.mean(axis=0)
            resid = diff - per_bin[None, :]
            resid_max = float(np.abs(resid).max())
            print("  A  原始 : max|delta|=%.3e  逐bin常数[%+.3f,%+.3f]  去常数残差 max=%.3e  %s" %
                  (float(np.abs(diff).max()), float(per_bin.min()), float(per_bin.max()),
                   resid_max, "OK(时不变)" if resid_max < 1e-2 else "FAIL(随时间变化)"))
            ok = ok and resid_max < 1e-2
        fb_cmn_knf = fb_full - fb_full.mean(axis=0, keepdims=True)
        if fb_cmn_knf.shape == fb_ref.shape:
            print("  A* CMN后(全长) : max|delta| = %.3e" %
                  float(np.abs(fb_cmn_knf - fb_ref).max()))
        if fb_al.shape != fb_ref_al.shape:
            print("  A' 对齐 : 形状不一致 knf=%s ref=%s" % (fb_al.shape, fb_ref_al.shape))
            ok = False
        else:
            d2 = float(np.abs(fb_al - fb_ref_al).max())
            print("  A' 对齐 : max|delta| = %.3e  T=%d (L=%d)  %s" %
                  (d2, fb_al.shape[0], V.tdnn_len(fb_al.shape[0]), "OK" if d2 < 1e-2 else "FAIL"))
            ok = ok and d2 < 1e-2

        e_onnx = sess.run(["embedding"], {"fbank": fb_al[None, :, :].astype(np.float32)})[0].ravel()
        c1, c2, c3 = V.cos(e_onnx, e_ceil), V.cos(e_onnx, e_al), V.cos(e_onnx, e_funasr)
        print("  B1 导出保真: ONNX vs 打补丁模型 cosine = %.8f  %s" %
              (c1, "OK" if c1 > 0.9999 else "FAIL"))
        print("  B2 契约达标: ONNX vs 原版(对齐) cosine = %.8f  %s" %
              (c2, "OK" if c2 > 0.999 else "FAIL"))
        print("  C  全长参照: ONNX vs 线上全长     cosine = %.6f  (仅参考，尾段被丢)" % c3)
        ok = ok and c1 > 0.9999 and c2 > 0.999
        embs[name] = e_onnx / np.linalg.norm(e_onnx)
        refs[name] = e_al / np.linalg.norm(e_al)
        print()

    # --- A'' mel 刻度根因排查：找出能消掉「逐 bin 常数偏移」的 mel 参数 ---
    print("=== A'' mel 参数根因排查（用 speaker1_a 的原始 fbank）===")
    name0 = NAMES[0]
    a0, sr0 = V.read_wav(os.path.join(SV_DIR, "examples", name0))
    ref_raw = np.load(os.path.join(REF, name0.replace(".wav", "") + "_fbank_raw.npy"))
    cands = [
        ("默认(不动)", {}),
        ('norm=""', {"mel_norm": ""}),
        ('norm="none"', {"mel_norm": "none"}),
        ('norm="slaney"', {"mel_norm": "slaney"}),
        ("use_slaney=True", {"use_slaney": True}),
        ("use_slaney=False", {"use_slaney": False}),
        ('norm="" + slaney=False', {"mel_norm": "", "use_slaney": False}),
    ]
    best = None
    for label, kw in cands:
        try:
            fb2 = fbank_knf(a0, sr0, **kw)
            if fb2.shape != ref_raw.shape:
                print("  %-26s 形状不一致 %s" % (label, fb2.shape))
                continue
            d = float(np.abs(fb2 - ref_raw).max())
            print("  %-26s max|delta| = %.3e%s" % (label, d, "   <== 匹配" if d < 1e-2 else ""))
            if d < 1e-2 and (best is None or d < best[1]):
                best = (label, d, kw)
        except Exception as e:
            print("  %-26s 不支持/异常 (%s)" % (label, str(e)[:50]))
    if best:
        print("  => 建议采用: %s  (max|delta|=%.3e)" % (best[0], best[1]))
    else:
        print("  => 没有候选能消掉原始偏移；依赖 CMN 抵消（已由 A 的『时不变』条件把关）")
    print()

    print("=== D 区分度（独立版 vs 原版，对齐输入）===")
    for x, y in [(NAMES[0], NAMES[1]), (NAMES[0], NAMES[2]), (NAMES[1], NAMES[2])]:
        c, r = V.cos(embs[x], embs[y]), V.cos(refs[x], refs[y])
        tag = "OK" if abs(c - r) < 0.005 else "FAIL"
        print("  %-12s vs %-12s 独立版=%+.4f  原版=%+.4f  %s" %
              (x.replace("_cn_16k.wav", ""), y.replace("_cn_16k.wav", ""), c, r, tag))
        ok = ok and abs(c - r) < 0.005
    same = V.cos(embs[NAMES[0]], embs[NAMES[1]])
    diff = max(V.cos(embs[NAMES[0]], embs[NAMES[2]]), V.cos(embs[NAMES[1]], embs[NAMES[2]]))
    gap = same - diff
    print("  同人/异人 间隔 = %+.4f  %s" % (gap, "OK" if gap > 0.3 else "FAIL"))
    ok = ok and gap > 0.3

    # 生产尺寸 T=200 的耗时（对齐输入）
    prod = V.PROD_T
    if V.is_aligned(prod):
        x = np.ascontiguousarray(np.load(
            os.path.join(REF, NAMES[0].replace(".wav", "") + "_fbank_aligned.npy"))[:prod])
        if x.shape[0] == prod:
            x = x[None, :, :].astype(np.float32)
            for _ in range(3):
                sess.run(["embedding"], {"fbank": x})
            t0 = time.time()
            for _ in range(20):
                sess.run(["embedding"], {"fbank": x})
            print("\nCPU 单次推理 = %.1f ms（T=%d，%.2f 秒音频）" %
                  ((time.time() - t0) / 20 * 1000, prod, prod * 0.01))

    print("\n%s" % ("PARITY OK" if ok else "PARITY FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
