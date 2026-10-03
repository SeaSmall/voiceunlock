"""verify_exe_strings.py -- 把冻结版 exe 里的 Python 模块**解出来**，查里面有没有本机路径/个人串。

为什么必须这么做：PYZ 是压缩的 —— 直接按字节 grep exe 什么都查不到
（实测：连 `peer-not-allowed` 这种源码字面量都不在 exe 的可见字节里）。
所以只能把模块反序列化回来，逐个常量看。

做法：PyInstaller 自己的 reader 把 CArchive 里的 PYZ 取出来 → 再取每个模块的 code 对象
→ 递归遍历 co_consts / docstring（字符串常量才是真正随包出去的东西；注释不会）。
"""
import marshal
import os
import re
import sys
import types

sys.stdout.reconfigure(errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- 通用模式：与本机无关，谁跑结果都一样 ----
PATTERNS = {
    "盘符绝对路径": re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\\/]"),
    "用户目录": re.compile(r"[Cc]:[\\/]Users[\\/]"),
    "机器名": re.compile(r"DESKTOP-[A-Z0-9]{7}"),
    "内网 IP": re.compile(r"\b(?:192\.168|10)\.\d{1,3}\.\d{1,3}(?:\.\d{1,3})?\b"),
    "邮箱": re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}"),
    "手机号": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "密钥/令牌": re.compile(
        r"(?:sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}"
        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)"),
    "用户数据文件": re.compile(r"\.voiceprint\.npy|credential\.bin"),
}

# 可选：本机专有 token 清单（一行一个，'#' 开头是注释）。
# 放 logs\ 下是有意的 —— logs\ 不进仓库，本机标识不会跟着代码一起发布。
EXTRA_FILE = os.path.join(ROOT, "logs", "sensitive_tokens.txt")


def load_extra_tokens(path=None):
    """读可选的本机 token 清单；文件不存在就返回空列表（不报错、不改退出码）。

    格式：一行一个 token，`#` 开头或行尾都是注释（行尾注释必须剥掉，
    否则 token 会带上注释文字而永远匹配不到）。
    """
    path = path or EXTRA_FILE
    toks = []
    if not os.path.isfile(path):
        return toks
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            toks.append(line)
    return toks


EXE = sys.argv[1] if len(sys.argv) > 1 else r"dist\VoiceUnlock\VoiceUnlockAgent.exe"


def iter_strings(code, depth=0):
    """递归取出 code 对象里的所有字符串常量。"""
    for c in code.co_consts:
        if isinstance(c, str):
            yield c
        elif isinstance(c, types.CodeType):
            yield from iter_strings(c, depth + 1)


def main():
    from PyInstaller.archive.readers import CArchiveReader
    a = CArchiveReader(EXE)
    names = list(a.toc)
    print("CArchive 条目：%d 个，PYZ 在不在：%s"
          % (len(names), any("PYZ" in n for n in names)))

    # 取出 PYZ 到临时文件，再用 ZlibArchiveReader 打开
    import os
    import tempfile
    pyz_name = [n for n in names if "PYZ" in n][0]
    blob = None
    for meth in ("extract", "open_embedded_archive"):
        if hasattr(a, meth):
            try:
                blob = getattr(a, meth)(pyz_name)
                print("用 %s() 取 PYZ：%s" % (meth, type(blob).__name__))
                break
            except Exception as e:
                print("  %s() 不可用：%r" % (meth, e))
    if blob is None:
        print("取不到 PYZ，放弃这个校验（源码扫描仍然是权威依据）")
        return 2

    if isinstance(blob, (bytes, bytearray)):
        tmp = os.path.join(tempfile.gettempdir(), "vu_pyz_check.pyz")
        open(tmp, "wb").write(blob)
        from PyInstaller.archive.readers import ZlibArchiveReader
        z = ZlibArchiveReader(tmp)
    else:                       # 已经是 reader 对象
        z = blob

    mods = [n for n in z.toc if n.startswith("src.")]
    print("PYZ 里的 src.* 模块：%d 个" % len(mods))
    extra = load_extra_tokens()
    hits = 0
    for m in sorted(mods):
        try:
            code = z.extract(m)
        except Exception as e:
            print("  解不出 %s: %r" % (m, e))
            continue
        if isinstance(code, types.CodeType):
            ss = list(iter_strings(code))
        else:
            ss = [str(code)]
        for s in ss:
            for why, pat in PATTERNS.items():
                mt = pat.search(s)
                if mt:
                    print("  [命中] %-22s %-12s %-26s %r"
                          % (m, why, mt.group(0)[:26], s[:90]))
                    hits += 1
            for tok in extra:
                if tok in s:
                    print("  [命中] %-22s %-12s %-26s %r"
                          % (m, "本机 token", tok[:26], s[:90]))
                    hits += 1
    print("=== 冻结版模块内的敏感串命中：%d ===" % hits)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
