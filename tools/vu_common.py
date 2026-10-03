#!/usr/bin/env python3
"""vu_common.py -- 两侧解释器共享的工具与契约

为什么单独一个模块：导 ONNX 的脚本跑在 funasr 环境，验收脚本跑在独立 venv，
两边必须对"时间维对齐""怎么读 wav""怎么算 cosine"有**同一份**定义，
否则今天改一边、明天忘了另一边，验收就会假过。

★ 时间维契约（T = fbank 帧数）
    CAM++ 的 xvector 只有首个 TDNNLayer 做 stride=2 降采样（components.py:129-139），
    TransitLayer / FCM 都是 kernel=1，长度不变（components.py:264-282）。
    因此进入 CAMLayer.seg_pooling 的长度是
        L = floor((T - 1) / 2) + 1 = ceil(T / 2)
    而 seg_pooling 用 seg_len=100 分段（components.py:174）。
    官方实现用 ceil_mode=True，PyTorch 的 ONNX symbolic 在 ceil_mode 为真时需要
    【静态】时间维（get_pool_ceil_padding），动态轴导出必然失败。
    由于 T 对齐时没有残缺尾段、ceil_mode=True 与 False 结果完全相同，
    导出时改用 ceil_mode=False，代价是运行时必须保证 L % 100 == 0：
        T ∈ {199, 200, 399, 400, 599, 600, 799, 800, ...}
    T 不对齐时模型会当场报维度错（响亮失败），不会静默算错。
"""

SEG_LEN = 100

# 生产用帧数：200 帧 = 2.0 秒（10ms 帧移）。
# 一次解锁采集两段投票，合计约 4 秒说话时间。
PROD_T = 200


def read_wav(path):
    """读 16bit wav -> (float32 单声道 ∈ [-1,1], 采样率)。

    注意：这里【不做】1<<15 放大 —— funasr 的 load_audio_text_image_video
    交给 fbank 的就是 [-1,1] 量程，两边必须一致。
    """
    import wave

    import numpy as np
    with wave.open(path, "rb") as w:
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a / 32768.0, sr


def cos(a, b):
    import numpy as np
    x = np.asarray(a, dtype=np.float64).ravel()
    y = np.asarray(b, dtype=np.float64).ravel()
    return float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y)))


def tdnn_len(T):
    """首个 TDNN(stride=2, kernel=5, padding=2) 之后的长度。"""
    return (T - 1) // 2 + 1


def is_aligned(T):
    """T 是否满足 L % SEG_LEN == 0。"""
    return T > 0 and tdnn_len(T) % SEG_LEN == 0


def largest_aligned_T(n, min_t=SEG_LEN * 2):
    """返回 <= n 的最大对齐 T；找不到（或小于 min_t）返回 0。"""
    for T in range(int(n), 0, -1):
        if is_aligned(T):
            return T if T >= min_t else 0
    return 0


ALIGNED_T = [T for T in range(1, 1001) if is_aligned(T)]

if __name__ == "__main__":
    print("SEG_LEN      =", SEG_LEN)
    print("生产 T        =", PROD_T, "帧 =", PROD_T * 0.01, "秒  对齐 =", is_aligned(PROD_T))
    print("<=1000 的对齐 T:", ALIGNED_T)
    for T in (200, 300, 370, 400, 600):
        print("  T=%-4d L=%-4d 对齐=%s" % (T, tdnn_len(T), is_aligned(T)))
