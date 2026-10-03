"""sv.py -- 声纹提取（独立版）：fbank -> ONNX -> 192 维

只依赖 numpy + onnxruntime + kaldi_native_fbank。
【不依赖】torch / funasr（实测 `import funasr` 单独要 100+ 秒）。

与 funasr 原版逐项一致的路径（已由 tools/verify_parity.py 验收，cos=1.00000000）：
    load_audio_text_image_video(fs=16000)  -> float32 ∈ [-1,1]，【没有】1<<15 放大
    Kaldi.fbank(au.unsqueeze(0), num_mel_bins=80)
        window_type='povey'  dither=0.0  energy_floor=1.0
        low_freq=20  high_freq=0  preemph_coeff=0.97
        snip_edges=True  use_power=True  use_log_fbank=True  htk_compat=False
        （除 dither 与 num_mel_bins=80 外全是 Kaldi 默认）
    feature -= feature.mean(dim=0)                     # 逐句均值归一化（CMN）
    CAMPPlus.forward(x), x=(B,T,80) -> (B,192)

★★ 时间维契约（本项目独有的坑，务必读完再改）★★
  CAM++ 的 xvector 只有首个 TDNNLayer 做 stride=2 降采样，TransitLayer/FCM 都是 kernel=1，
  所以进入 CAMLayer.seg_pooling 的长度是
      L = floor((T-1)/2) + 1 = ceil(T/2)
  而 seg_pooling 用 seg_len=100 分段。官方用 ceil_mode=True，会让 PyTorch 的 ONNX 导出
  必须【静态】时间维（get_pool_ceil_padding），动态轴导出必然失败。
  由于 T 对齐时没有残缺尾段、ceil_mode=True 与 False 数值完全相同（实测 cos=1.00000000），
  导出时改用了 ceil_mode=False 以保住动态轴，代价是：
      运行时 T 必须满足 ceil(T/2) % 100 == 0  ->  T ∈ {199,200,399,400,599,600,...}
  不对齐会【当场报维度错】，不会静默算错 —— 这是刻意的设计。
"""
from __future__ import annotations

import os
import sys
import threading

import numpy as np

# ---- 与 kaldi fbank 相关的常量（16k）----
SR = 16000
FRAME_LENGTH_MS = 25.0
FRAME_SHIFT_MS = 10.0
FRAME_LENGTH_SAMPLES = 400     # 25ms @16k
FRAME_SHIFT_SAMPLES = 160      # 10ms @16k
N_MELS = 80
SEG_LEN = 100                  # CAM++ seg_pooling 的段长（作用在 TDNN 之后的时间轴）

def _default_onnx() -> str:
    """模型路径解析：环境变量 -> 用户目录 -> 开发目录 -> 打包内部。

    ★ 打包形态必须这么找：安装目录通常在 Program Files 下（只读），而
    %LOCALAPPDATA%\\VoiceUnlock\\models 是用户可写的（可以自己换模型），
    随包分发的那份则躺在 PyInstaller 的 _MEIPASS 里。
    """
    env = os.environ.get("VOICEUNLOCK_MODELS")
    if env:
        return os.path.join(env, "campplus.onnx")
    proj = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "models")
    home = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                        "VoiceUnlock", "models")
    cands = [os.path.join(home, "campplus.onnx"), os.path.join(proj, "campplus.onnx")]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(os.path.join(meipass, "models", "campplus.onnx"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return cands[0]


DEFAULT_ONNX = _default_onnx()


# ---------------------------------------------------------------- 帧数/采样数换算

def frames_for_samples(n: int) -> int:
    """snip_edges=True 时，n 个采样点能出多少帧 fbank。"""
    if n < FRAME_LENGTH_SAMPLES:
        return 0
    return 1 + (n - FRAME_LENGTH_SAMPLES) // FRAME_SHIFT_SAMPLES


def samples_for_frames(t: int) -> int:
    """要【恰好】得到 t 帧 fbank，需要采集多少采样点。

    因为 (t-1)*shift 正好是 shift 的整数倍，所以这个数是精确的、不多不少。
    t=200 -> 32240 采样 = 2.015 秒
    """
    if t <= 0:
        return 0
    return FRAME_LENGTH_SAMPLES + (t - 1) * FRAME_SHIFT_SAMPLES


# ---------------------------------------------------------------- 对齐契约

def tdnn_len(t: int) -> int:
    """首个 TDNN(stride=2, kernel=5, padding=2) 之后的长度。"""
    return (t - 1) // 2 + 1


def is_aligned(t: int) -> bool:
    return t > 0 and tdnn_len(t) % SEG_LEN == 0


def aligned_at_most(t: int) -> int:
    """<= t 的最大对齐帧数；没有则 0。"""
    while t > 0:
        if is_aligned(t):
            return t
        t -= 1
    return 0


ALIGNED_FRAMES = tuple(t for t in range(1, 2001) if is_aligned(t))


def _require_aligned(t: int) -> None:
    if not is_aligned(t):
        raise ValueError(
            "帧数 %d 不满足时间维契约 ceil(T/2) %% %d == 0；合法值如 %s"
            % (t, SEG_LEN, list(ALIGNED_FRAMES[:8])))


# ---------------------------------------------------------------- 前端

def fbank(a: np.ndarray, sr: int = SR) -> np.ndarray:
    """算 80 维 log-mel fbank，参数与 funasr 用的 torchaudio 默认值逐项一致。"""
    import kaldi_native_fbank as knf

    if sr != SR:
        raise ValueError("只支持 16k（收到 %d）；请在采集时直接请求 16000" % sr)

    opts = knf.FbankOptions()
    opts.frame_opts.dither = 0.0            # Kaldi 默认 1.0，torchaudio 默认 0.0
    opts.frame_opts.snip_edges = True
    opts.frame_opts.window_type = "povey"   # 不是 hamming
    opts.frame_opts.preemph_coeff = 0.97
    opts.frame_opts.remove_dc_offset = True
    opts.frame_opts.round_to_power_of_two = True
    opts.frame_opts.blackman_coeff = 0.42
    opts.frame_opts.samp_freq = float(SR)
    opts.mel_opts.num_bins = N_MELS
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
    # knf 1.22.x 的 MelBanksOptions 有 norm / use_slaney_mel_scale / is_librosa，
    # 实测改不改结果都是 1.9e-4 的差异（默认即正确），所以不要动它们。

    f = knf.OnlineFbank(opts)
    f.accept_waveform(sr, np.ascontiguousarray(a, dtype=np.float32).tolist())
    try:
        f.input_finished()
    except AttributeError:
        pass
    n = f.num_frames_ready
    if n == 0:
        return np.zeros((0, N_MELS), dtype=np.float32)
    return np.stack([f.get_frame(i) for i in range(n)]).astype(np.float32)


def cmn(fb: np.ndarray) -> np.ndarray:
    """逐句均值归一化（funasr campplus/utils.py:104 的 feature -= feature.mean(0)）。"""
    return fb - fb.mean(axis=0, keepdims=True)


# ---------------------------------------------------------------- 声纹提取器

class SpeakerEmbedder:
    """加载一次 ONNX，反复提取 192 维声纹。线程安全（ORT 会话本身可重入）。"""

    def __init__(self, onnx_path: str | None = None, target_frames: int = 200,
                 intra_threads: int | None = None):
        import onnxruntime as ort

        path = onnx_path or DEFAULT_ONNX
        if not os.path.isfile(path):
            raise FileNotFoundError("找不到 ONNX 模型：%s" % path)
        so = ort.SessionOptions()
        if intra_threads:
            so.intra_op_num_threads = int(intra_threads)
        so.log_severity_level = 3
        self.session = ort.InferenceSession(path, sess_options=so,
                                            providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        self.onnx_path = path
        _require_aligned(target_frames)
        self.target_frames = target_frames
        self._lock = threading.Lock()

    # -- 原始接口：拿现成 fbank 出向量
    def embed_fbank(self, fb: np.ndarray, do_cmn: bool = True) -> np.ndarray:
        fb = np.asarray(fb, dtype=np.float32)
        if fb.ndim != 2 or fb.shape[1] != N_MELS:
            raise ValueError("fbank 形状应为 (T,%d)，收到 %s" % (N_MELS, fb.shape))
        _require_aligned(fb.shape[0])
        x = cmn(fb) if do_cmn else fb
        x = np.ascontiguousarray(x[None, :, :], dtype=np.float32)
        out = self.session.run([self.output_name], {self.input_name: x})[0]
        v = np.asarray(out, dtype=np.float64).reshape(-1)
        n = float(np.linalg.norm(v))
        if n <= 0:
            raise RuntimeError("ONNX 返回了零向量")
        return (v / n).astype(np.float64)

    # -- 常用接口：给一段音频，出向量
    def embed_audio(self, a: np.ndarray, sr: int = SR,
                    target_frames: int | None = None,
                    strict: bool = True):
        """返回 (单位化后的 192 维向量, info)。info 里带 padded/cropped 供调用方判断质量。"""
        T = int(target_frames or self.target_frames)
        _require_aligned(T)
        need = samples_for_frames(T)

        a = np.asarray(a, dtype=np.float32).reshape(-1)
        info = {"sr": sr, "samples_in": int(a.size), "frames": T,
                "padded": 0, "cropped": 0}
        if sr != SR:
            raise ValueError("只支持 16k 采集（收到 %d）" % sr)
        if a.size < need:
            info["padded"] = int(need - a.size)
            if strict:
                raise ValueError("音频太短：要 %d 采样(%d 帧=%.3f 秒)，只有 %d"
                                 % (need, T, need / SR, a.size))
            a = np.pad(a, (0, info["padded"]))
        elif a.size > need:
            info["cropped"] = int(a.size - need)
            a = a[:need]

        fb = fbank(a, sr)
        if fb.shape[0] != T:
            raise RuntimeError("fbank 出了 %d 帧，期望 %d 帧（契约破裂）"
                               % (fb.shape[0], T))
        return self.embed_fbank(fb), info

    # -- 多段接口：一次连续录音切成 n 段，每段独立 CMN 出向量
    def samples_for_segments(self, n_segments: int, target_frames: int | None = None) -> int:
        """一次录 n 段需要多少采样点（连续录，之后切开）。"""
        T = int(target_frames or self.target_frames)
        return samples_for_frames(T) * int(n_segments)

    def embed_segments(self, a: np.ndarray, n_segments: int = 2, sr: int = SR,
                       target_frames: int | None = None, strict: bool = True):
        """把一段连续录音切成 n_segments 段，各段独立 CMN 后出向量。

        为什么不是"让用户说两遍"：一次连续录 4 秒再切开，用户只需要说一遍。
        为什么每段独立 CMN：CMN 是逐句均值归一化，段的边界就是"句"的边界；
        登记侧必须用完全相同的切法，否则两边分布不一致。
        """
        T = int(target_frames or self.target_frames)
        _require_aligned(T)
        n = max(int(n_segments), 1)
        need = samples_for_frames(T) * n

        a = np.asarray(a, dtype=np.float32).reshape(-1)
        info = {"sr": sr, "samples_in": int(a.size), "frames": T, "segments": n,
                "padded": 0, "cropped": 0}
        if sr != SR:
            raise ValueError("只支持 16k 采集（收到 %d）" % sr)
        if a.size < need:
            info["padded"] = int(need - a.size)
            if strict:
                raise ValueError("音频太短：%d 段要 %d 采样(%.2f 秒)，只有 %d"
                                 % (n, need, need / SR, a.size))
            a = np.pad(a, (0, info["padded"]))
        elif a.size > need:
            info["cropped"] = int(a.size - need)
            a = a[:need]

        step = samples_for_frames(T)
        embs = []
        for i in range(n):
            chunk = a[i * step:(i + 1) * step]
            fb = fbank(chunk, sr)
            if fb.shape[0] != T:
                raise RuntimeError("第 %d 段出了 %d 帧，期望 %d（契约破裂）"
                                   % (i, fb.shape[0], T))
            embs.append(self.embed_fbank(fb))
        return embs, info

    # -- 挑"语音最密"的窗口：短句场景比硬切前 N 秒可靠得多
    def embed_best_window(self, a: np.ndarray, sr: int = SR,
                          target_frames: int | None = None,
                          hop_frames: int = 25, rel: float = 0.15,
                          floor: float = 1e-5):
        """在这段音频里滑动 2 秒窗口，挑【含语音帧最多】的那一段再出向量。

        为什么需要（实测）：旧声纹是在**连续语音**上登记的。若从触发点硬切 2 秒，
        短句里会混进大半静音，统计池化偏了 —— 同一个人的分数会从 0.24 掉到 0.09。
        多录 3 倍、挑最密的那 2 秒，就能把"语音/静音比例"拉回与登记时相近。
        """
        T = int(target_frames or self.target_frames)
        step = samples_for_frames(T)
        hop = samples_for_frames(hop_frames)
        a = np.asarray(a, dtype=np.float32).reshape(-1)
        if a.size < step:
            raise ValueError("音频太短：要 %d 采样，只有 %d" % (step, a.size))

        # 全局能量包络 -> 门限 -> 数每个候选窗口里有多少帧像语音
        e = np.sqrt(np.mean(a[: (a.size // hop) * hop].reshape(-1, hop) ** 2,
                            axis=1) + 1e-12)
        thr = max(float(e.max()) * rel, floor) if e.size else floor
        best_i, best_n, best_rms = 0, -1, 0.0
        i = 0
        while i + step <= a.size:
            lo = i // hop
            hi = min((i + step) // hop, e.size)
            n = int((e[lo:hi] > thr).sum())
            if n > best_n:
                seg = a[i:i + step]
                best_i, best_n = i, n
                best_rms = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2)))
            i += hop
        seg = a[best_i:best_i + step]
        fb = fbank(seg, sr)
        if fb.shape[0] != T:
            raise RuntimeError("fbank 出了 %d 帧，期望 %d" % (fb.shape[0], T))
        return self.embed_fbank(fb), {"offset_samples": best_i,
                                      "speech_frames": best_n, "rms": best_rms}

    # -- 兼容旧接口命名
    def embed(self, a: np.ndarray, sr: int = SR, **kw):
        return self.embed_audio(a, sr, **kw)[0]


def cos(a, b) -> float:
    x = np.asarray(a, dtype=np.float64).ravel()
    y = np.asarray(b, dtype=np.float64).ravel()
    return float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y)))


if __name__ == "__main__":
    print("SR                     =", SR)
    print("samples_for_frames(200)=", samples_for_frames(200),
          "(%.3f 秒)" % (samples_for_frames(200) / SR))
    print("frames_for_samples(%d) = %d" % (samples_for_frames(200),
                                           frames_for_samples(samples_for_frames(200))))
    print("对齐帧数(<=2000)        =", ALIGNED_FRAMES[:10], "...")
    print("DEFAULT_ONNX           =", DEFAULT_ONNX, os.path.isfile(DEFAULT_ONNX))
