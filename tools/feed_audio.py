"""feed_audio.py -- 给虚拟机当"假麦克风"：把 wav 播到某个主机播放设备，并/或从某个录音设备测电平。

背景（这是 HANDOFF 坑 9 的更正方向）：
    VM 的声卡 `sound.autodetect=TRUE` 时，**麦克风输入取的是主机的"默认录音设备"**。
    本机默认录音设备是 `VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)` —— 一个虚拟设备。
    虚拟设备平时当然是静音（峰值 7e-6），于是上一轮得出"VM 麦克风是假的、做不了声学测试"。
    真相是：**只要往它的输入端灌音频，它就有信号**。VoiceMeeter 的 VAIO 通路是
    `VoiceMeeter Input`(播放侧) → `VoiceMeeter Output`(录音侧)（需要 VoiceMeeter 在跑）。

用法：
    python tools\\feed_audio.py devices                        # 列出主机播放/录音设备
    python tools\\feed_audio.py probe --rec 关键字 --secs 3      # 只录音，打印峰值/活跃比例
    python tools\\feed_audio.py loop --wav X.wav --secs 4        # 同时播+录，打印峰值（自检通路）
    python tools\\feed_audio.py feed --wav X.wav --secs 60      # 循环播给 VM 听（前台跑，Ctrl+C 停）
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import wave

import numpy as np
import soundcard as sc

DEFAULT_PLAY = "VoiceMeeter Input"
DEFAULT_REC = "VoiceMeeter Output"


def _dev(kind: str, needle: str):
    """按名字子串找设备（找不到就列出候选，别让人瞎猜）。"""
    cands = sc.all_speakers() if kind == "play" else sc.all_microphones()
    for d in cands:
        if needle.lower() in d.name.lower():
            return d
    print("[FAIL] 没找到%s设备（关键字 %r）。现有：" % ("播放" if kind == "play" else "录音", needle))
    for d in cands:
        print("   ", d.name)
    return None


def _read_wav(path: str):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit("只支持 16-bit PCM wav（这个文件是 %d 字节/采样）" % width)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


def _stats(a: np.ndarray, sr: int) -> str:
    if a.size == 0:
        return "（没录到任何采样）"
    peak = float(np.max(np.abs(a)))
    rms = float(np.sqrt(np.mean(a ** 2)))
    # 20ms 窗的活跃比例（与 src/verify.py 同一口径：窗 RMS 超过静音门限的占比）
    win = max(1, int(0.02 * sr))
    n = a.size // win
    blocks = a[:n * win].reshape(n, win)
    br = np.sqrt((blocks ** 2).mean(axis=1))
    active = float((br > max(1e-4, 0.02 * float(np.max(br)))).mean()) if n else 0.0
    return "峰值=%.6f  RMS=%.6f  活跃比例=%.2f  采样=%d" % (peak, rms, active, a.size)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["devices", "probe", "loop", "feed"])
    # 要灌给 VM 的 wav：用 --wav 指定，或设环境变量 VU_WAV（例如 <音频目录>\probe.wav）
    ap.add_argument("--wav", default=os.environ.get("VU_WAV", ""),
                    help="要播放的 wav 路径（--wav 或环境变量 VU_WAV）")
    ap.add_argument("--play", default=DEFAULT_PLAY)
    ap.add_argument("--rec", default=DEFAULT_REC)
    ap.add_argument("--secs", type=float, default=4.0)
    ap.add_argument("--sr", type=int, default=16000)
    a = ap.parse_args()

    if a.action == "devices":
        print("=== 播放设备 ===")
        for d in sc.all_speakers():
            print("   ", d.name)
        print("=== 录音设备 ===")
        for d in sc.all_microphones(include_loopback=True):
            print("    %-58s loopback=%s" % (d.name, getattr(d, "isloopback", False)))
        try:
            print("默认播放 =", sc.default_speaker().name)
            print("默认录音 =", sc.default_microphone().name)
        except Exception as e:
            print("默认设备读不到：", e)
        return 0

    if a.action == "probe":
        mic = _dev("rec", a.rec)
        if mic is None:
            return 2
        with mic.recorder(samplerate=a.sr, channels=1) as r:
            time.sleep(0.2)
            data = r.record(numframes=int(a.secs * a.sr))
        print("录音设备 %s / %.1f 秒：%s" % (mic.name, a.secs, _stats(data, a.sr)))
        return 0

    if not a.wav:
        print("用法错误：loop/feed 需要 --wav <文件>（或设置环境变量 VU_WAV）", file=sys.stderr)
        return 2
    data, sr = _read_wav(a.wav)
    print("wav = %s  %.2f 秒 @ %d Hz" % (a.wav, data.size / sr, sr))
    spk = _dev("play", a.play)
    mic = _dev("rec", a.rec)
    if spk is None or mic is None:
        return 2

    if a.action == "loop":
        stop = time.time() + a.secs + 1.0
        got = {}

        def _rec():
            with mic.recorder(samplerate=a.sr, channels=1) as r:
                time.sleep(0.3)
                got["a"] = r.record(numframes=int(a.secs * a.sr))

        th = threading.Thread(target=_rec)
        th.start()
        time.sleep(0.35)
        while time.time() < stop:
            spk.play(data, samplerate=sr)
        th.join()
        print("播 %s → 录 %s：%s" % (spk.name, mic.name, _stats(got.get("a", np.zeros(0)), a.sr)))
        return 0

    # feed：循环播，前台跑，给 VM 那边"有人在说话"的假象
    t0 = time.time()
    n = 0
    try:
        while time.time() - t0 < a.secs:
            spk.play(data, samplerate=sr)
            n += 1
    except KeyboardInterrupt:
        pass
    print("已播 %d 遍（%.1f 秒）→ %s" % (n, time.time() - t0, spk.name))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    sys.exit(main())
