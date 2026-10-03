"""audio.py -- 多设备并发采集 + 同源去重

三条设计要点（都来自本机实测）：
  1. **直接请求 16000 Hz**：实测 4 个设备对 16000/48000/96000 的请求全部成功，
     由 WASAPI 音频引擎负责重采样。这绕开了老代码里 np.interp 线性降采样
     （96000->16000 无抗混叠）那条路 —— 它正是"登记与运行时插值方式不同、
     本人相似度掉到 0.15"的根因。
  2. **单路失败不影响整体**：某路被独占/被拔就跳过并记录，其余照常判定。
  3. **同源去重**：虚拟声卡会让同一段声音同时出现在多个端点上，
     按"每路一票"投票会把一次说话计成 2~3 票，阈值形同虚设。
     这里按【能量包络的互相关】把同源归组，判定永远按"信号源"投票。
"""
from __future__ import annotations

import threading
import time
import wave
import warnings

import numpy as np

from . import sv as S


# ------------------------------------------------------------------ 读文件（测试用）

def read_wav(path: str):
    """读 16bit wav -> (float32 单声道 ∈ [-1,1], 采样率)。不做 1<<15 放大。"""
    with wave.open(path, "rb") as w:
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a / 32768.0, sr


# ------------------------------------------------------------------ 电平 / VAD

def rms(a: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float32).reshape(-1)
    if a.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(a * a)))


def envelope(a: np.ndarray, hop: int = S.FRAME_SHIFT_SAMPLES) -> np.ndarray:
    """按 10ms 为一格的能量包络（用于 VAD 与同源判定）。"""
    a = np.asarray(a, dtype=np.float32).reshape(-1)
    n = len(a) // hop
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    blk = a[:n * hop].reshape(n, hop)
    return np.sqrt(np.mean(blk * blk, axis=1) + 1e-12).astype(np.float32)


def speech_stats(a: np.ndarray) -> dict:
    """这段音频里"像人说话"的程度：包络峰值、活跃帧占比、是否基本静音。

    绝对阈值不可靠（本机 WO Mic 的电平低到 peak 4e-4），所以用相对判据：
    活跃 = 包络 > max(峰值*0.15, 1e-5)。
    """
    e = envelope(a)
    if e.size == 0:
        return {"peak": 0.0, "active_ratio": 0.0, "silent": True}
    peak = float(e.max())
    thr = max(peak * 0.15, 1e-5)
    active = float((e > thr).mean())
    return {"peak": peak, "active_ratio": active, "silent": peak < 1e-5}


# ------------------------------------------------------------------ 并发采集

def capture_multi(devs, frames: int | None = None, sr: int = S.SR,
                  timeout_s: float = 6.0, channels: int = 1,
                  samples: int | None = None):
    """并发从多路设备各采一段。

    ★ 两个入口必须分清，否则长度会算错（真实踩过）：
          samples_for_frames(T) * n   !=   samples_for_frames(T * n)
      因为帧数对采样数不是线性的（N 采样 -> 1 + (N-400)/160 帧）。
      要"n 段、每段 T 帧"，请用 SpeakerEmbedder.samples_for_segments(n) 拿到
      采样数并从 samples= 传进来；**不要把 T*n 当帧数传给 frames**。

    返回 (samples, errors)：
        samples: {device_id: np.ndarray(float32)}  只在成功时出现
        errors : {device_id: str}
    """
    if samples is not None:
        need = int(samples)
    elif frames is not None:
        need = S.samples_for_frames(int(frames))
    else:
        raise ValueError("必须给 frames 或 samples 之一")
    out, errs = {}, {}
    lock = threading.Lock()

    def one(d):
        did = d["id"]
        try:
            import soundcard as sc  # noqa: F401  (确保依赖被触发)
            got, left = [], need
            empties = 0
            deadline = time.time() + timeout_s
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with d["dev"].recorder(samplerate=sr, channels=channels) as rec:
                    while left > 0:
                        blk = np.asarray(rec.record(numframes=min(left, sr // 10)),
                                         dtype=np.float32)
                        if blk.ndim > 1:
                            blk = blk[:, 0]
                        if blk.size == 0:
                            # ⚠ 绝不能在这里 break —— 踩过：一次空读就 break，
                            # 剩下的被零填充，于是"尾巴几段"是同一段静音，
                            # 声纹分数全塌成 ~0 且两段完全相同。宁可重试到超时。
                            empties += 1
                            if empties > 100 or time.time() > deadline:
                                break
                            time.sleep(0.02)
                            continue
                        empties = 0
                        got.append(blk)
                        left -= blk.size
                        if time.time() > deadline:
                            break
            a = np.concatenate(got) if got else np.zeros(0, dtype=np.float32)
            if a.size < need:
                # 短读【必须响亮失败】：悄悄补零会让静音去参与声纹比对，
                # 结果是"分数莫名很低"，比直接报错难查得多。
                with lock:
                    errs[did] = "短读 %d/%d 采样（设备没给够，请重试）" % (a.size, need)
                return
            with lock:
                out[did] = a[:need].astype(np.float32)
        except Exception as e:                     # 单路失败不影响其它路
            with lock:
                errs[did] = "%s: %s" % (type(e).__name__, str(e)[:120])

    threads = [threading.Thread(target=one, args=(d,), daemon=True) for d in devs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout_s)
    for t, d in zip(threads, devs):
        if t.is_alive() and d["id"] not in out:
            errs.setdefault(d["id"], "超时未返回")
    return out, errs


def wait_for_speech(devs, timeout_s: float = 45.0, poll_s: float = 0.25,
                    abs_floor: float = 0.003, rel_factor: float = 6.0,
                    warmup_rounds: int = 5, logger=None):
    """常驻监听**电平**：等到有人开口就返回触发的设备；超时返回 None。

    ★ 门限是【每设备自适应】的：threshold = max(该设备近期电平中位数 * rel, abs_floor)

    为什么不能用"所有设备的最小值"当本底（踩过）：
        本机有一路死设备（WO Mic，peak 恒为 ~1e-6），取 min 会把本底钉死在 0，
        门限于是永远等于绝对下限（3e-5），**环境噪声就能触发** —— 结果是拿噪声去
        比对声纹（分数 0.04~0.09），再连着失败 3 次把声纹通道锁 300 秒。
        改成每设备自己的中位数后，死设备只代表它自己，不会污染别人。
    为什么要 warmup_rounds：先攒够几轮纯本底再允许触发，否则开机第一轮就可能误触。
    """
    log = logger or (lambda m: None)
    frames = max(int(poll_s / 0.01), 5)
    t0 = time.time()
    rounds = 0
    hist = {}                      # device_id -> 近期 peak 列表
    warned = False
    while time.time() - t0 < timeout_s:
        got, errs = capture_multi(devs, frames=frames, timeout_s=poll_s + 2.0)
        rounds += 1
        if not got:
            # 一条数据都没拿到时必须报错（踩过：import time 缺失导致疯狂空转，
            # 外面只看到"没检测到说话声"，查了很久）
            if not warned and errs:
                log("[listen] 所有设备都取不到数据！逐路错误：%s"
                    % list(errs.items())[:2])
                warned = True
            time.sleep(0.2)
            continue
        for d in devs:
            a = got.get(d["id"])
            if a is None:
                continue
            pk = speech_stats(a)["peak"]
            h = hist.setdefault(d["id"], [])
            if len(h) >= warmup_rounds:
                med = float(np.median(h[-20:]))
                thr = max(med * rel_factor, abs_floor)
                if pk > thr:
                    log("[listen] 电平触发：%s peak=%.6f > 门限 %.6f"
                        "（该设备本底中位 %.6f，第 %d 轮）"
                        % (d["name"][:26], pk, thr, med, rounds))
                    return d
            h.append(pk)
    log("[listen] %.0f 秒内没检测到说话声（共 %d 轮）" % (timeout_s, rounds))
    return None


def trim_to_speech(a: np.ndarray, pad_s: float = 0.15,
                   rel: float = 0.15, floor: float = 1e-5):
    """剪掉首尾静音，只留真正说话的部分（带一点前后留白）。

    为什么需要：电平触发是在**开口那一瞬间**触发的，之后我们固定录 N 秒。
    如果人只说了一两秒，剩下的都是静音 —— 那几段静音的 embedding 会完全一样，
    判定必然失败。先剪掉静音，才能知道"实际有多少可用语音"。
    """
    e = envelope(a)
    if e.size == 0:
        return a
    thr = max(float(e.max()) * rel, floor)
    idx = np.nonzero(e > thr)[0]
    if idx.size == 0:
        return a[:0]
    pad = int(pad_s * 100)
    i0 = max(0, int(idx[0]) - pad)
    i1 = min(len(e), int(idx[-1]) + 1 + pad)
    return a[i0 * S.FRAME_SHIFT_SAMPLES:i1 * S.FRAME_SHIFT_SAMPLES]


# ------------------------------------------------------------------ 同源去重

def same_source_score(a: np.ndarray, b: np.ndarray, max_lag_frames: int = 30) -> float:
    """两段音频是否来自同一个物理声源：能量包络互相关（允许 ±300ms 延迟差）。

    不同设备有不同的延迟与时钟，直接按采样点对齐做相关是没用的，所以用包络 + 滞后搜索。
    """
    ea, eb = envelope(a), envelope(b)
    L = min(ea.size, eb.size)
    if L < 10:
        return 0.0
    ea, eb = ea[:L].astype(np.float64), eb[:L].astype(np.float64)
    ea -= ea.mean()
    eb -= eb.mean()
    best = 0.0
    for lag in range(-max_lag_frames, max_lag_frames + 1):
        if lag >= 0:
            x, y = ea[lag:], eb[:L - lag]
        else:
            x, y = ea[:L + lag], eb[-lag:]
        if x.size < 10:
            continue
        nx, ny = np.linalg.norm(x), np.linalg.norm(y)
        if nx <= 0 or ny <= 0:
            continue
        best = max(best, float(np.dot(x, y) / (nx * ny)))
    return best


def group_same_source(samples: dict, threshold: float = 0.9) -> list:
    """把同一物理声源的多路采集归成一组。

    返回 [ {"ids": [...], "samples": [...]}, ... ]，每组的"代表性信号"取能量最强的
    那一路（同源时各路的语音内容相同，取最强的一路信噪比最好）。
    """
    items = [(k, v) for k, v in samples.items()]
    groups = []
    for did, a in items:
        placed = False
        for g in groups:
            if same_source_score(a, g["rep"]) > threshold:
                g["ids"].append(did)
                g["samples"].append(a)
                if rms(a) > rms(g["rep"]):
                    g["rep"] = a
                placed = True
                break
        if not placed:
            groups.append({"ids": [did], "samples": [a], "rep": a})
    # 每个信号源内部再把峰值对齐到它的代表信号
    for g in groups:
        g["peak"] = float(envelope(g["rep"]).max()) if g["rep"].size else 0.0
    groups.sort(key=lambda g: -g["peak"])
    return groups
