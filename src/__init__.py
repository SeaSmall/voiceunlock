"""voiceunlock -- 声纹解锁运行时包

只依赖：numpy + onnxruntime + kaldi_native_fbank + soundcard。
【不依赖】torch / funasr / 任何其它服务。

时间维契约见 tools/vu_common.py 与 src/sv.py 顶部说明。
"""
__version__ = "0.1.0"
