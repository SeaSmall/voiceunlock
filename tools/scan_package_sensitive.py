"""scan_package_sensitive.py -- 交付前扫一遍"安装包里有没有不该带出去的东西"。

起因：用户要求"检查包内是否有个人敏感信息，有就删了"。这类东西会从三个不同的缝里漏出去，
**每个缝要用不同的查法**，只做其中一个都会得出错误的"干净"结论：

  ① 包里的**文件**（数据文件本身）
     → 列清单：产物目录里除了 exe/_internal/许可文本，不该出现
       profiles.json / credential.bin / *.npy / *.log / *.wav 这类用户数据。
  ② 包里的**未压缩字节**（DLL、manifest、随包文本）
     → 按真实敏感串逐字节搜（含 UTF-16LE：Windows 二进制里字符串常是宽字符）。
  ③ 包里的**压缩字节**（PyInstaller 的 PYZ）
     → ★ 直接 grep exe 是查不到的（实测连 `peer-not-allowed` 都不在可见字节里）。
       必须把 PYZ 解出来、反序列化每个模块的 code，遍历它的**字符串常量**；
       源码里的 `#` 注释不会进包，而 **docstring 会**（optimize=0）。

真实踩过的例子：
  * 某个模块里写死 `[A-Za-z]:\\...` 形式的模型绝对路径（字符串字面量，会进包）；
  * 几个模块的 **docstring** 里写着项目绝对路径 —— 这些模块都在包内，所以路径真的会随包出去。

本机专有串（机器名、口令、私人目录名……）**不写进本脚本**：把它们每行一个 token 写进
`logs\\sensitive_tokens.txt`（`#` 开头是注释），脚本会在通用模式之外自动加载比对。

用法：
    python tools\\scan_package_sensitive.py            # 三项全查，命中返回 1
    python tools\\scan_package_sensitive.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import marshal
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- 通用模式：与本机无关，谁跑结果都一样（宽松正则在二进制里全是噪声，所以逐条收紧）----
# 本机专有串一律不进本文件 —— 见下面的 EXTRA_FILE。
#
# ★ 这里只放"出现在代码/产物里就一定是问题"的形状。两条刻意不收：
#   * `C:\...`（系统盘）：`C:\ProgramData`、`C:\Windows\System32\...` 这类在正常程序里
#     本来就该出现，当泄露串搜只会淹掉真问题 → 只把**非系统盘**当信号。
#   * `credential.bin` / `profiles.json` 这类**本程序自己要用的文件名**：它们必然会出现在
#     源码的字符串常量里（src\dpapi.py 等），所以只做第①项"产物里有没有这个文件"的检查，
#     不走字符串比对（见 check_files_inventory 与 EXTRA_FILE 的 `file:` 前缀）。
TOKEN_PATTERNS = {
    "非系统盘绝对路径": re.compile(r"(?<![A-Za-z0-9])[D-Zd-z]:[\\/][^\s\\/]"),
    "用户目录": re.compile(r"[Cc]:[\\/]Users[\\/]"),
    "机器名": re.compile(r"DESKTOP-[A-Z0-9]{7}"),
    "内网 IP": re.compile(r"\b(?:192\.168|10)\.\d{1,3}\.\d{1,3}(?:\.\d{1,3})?\b"),
    "邮箱": re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}"),
    "手机号": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "密钥/令牌": re.compile(
        r"(?:sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}"
        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)"),
}

# 可选：本机专有 token 清单（一行一个 token，'#' 开头是注释）。
# 故意放在 logs\ 下 —— logs\ 不进仓库，本机标识就不会跟着代码一起被发布。
EXTRA_FILE = os.path.join(ROOT, "logs", "sensitive_tokens.txt")


def load_extra_tokens(path=None):
    """读可选的本机 token 清单；文件不存在就返回空列表（不报错、不影响退出码）。

    格式：一行一个 token，`#` 开头或行尾都是注释（行尾注释必须剥掉，
    否则 token 会带上注释文字而永远匹配不到）。

    ★ `file:` 前缀表示"**文件名**清单"：只用于第①项（产物里有没有这个文件），
      **不参与**文本/字节/PYZ 的字符串比对 —— 否则 `credential.bin`、
      `profiles.json` 这种"我们自己程序要用的文件名"会在代码里到处命中
      （它们本来就该出现在 `src/dpapi.py` 等的字符串常量里），把真问题淹掉。
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
            if line.startswith("file:"):
                toks.append(("用户数据文件", "file:" + line[5:].strip()))
            else:
                toks.append(("本机 token", line))
    return toks


def _scan_text(text, extra):
    """在一段文本上跑通用模式 + 外部 token，返回 [(why, token), ...]。

    `file:` 前缀的条目在这里被跳过（它们只用于文件清单检查）。
    """
    out = []
    for why, pat in TOKEN_PATTERNS.items():
        for m in pat.finditer(text):
            out.append((why, m.group(0)))
    for why, tok in extra:
        if tok.startswith("file:"):
            continue
        if tok in text:
            out.append((why, tok))
    return out


def _text_views(blob):
    """同一段字节的两种读法：ASCII/UTF-8 与 UTF-16LE（PE 里的宽字符串很常见）。"""
    views = [blob.decode("latin-1", errors="replace")]
    try:
        views.append(blob.decode("utf-16-le", errors="ignore"))
    except Exception:
        pass
    return views


# 源码扫描用（会进包的模块）；注释里的命中不算问题（编译时会被丢掉）。
# 判据与 TOKEN_PATTERNS 保持一致：只认**非系统盘**路径，不把本程序自己用的文件名
# （credential.bin / profiles.json）当泄露串 —— 它们本来就该出现在代码里。
SRC_PATTERNS = [
    ("非系统盘绝对路径", r"(?<![A-Za-z0-9])[D-Zd-z]:[\\/][^\s\\/]"),
    ("用户目录", r"[Cc]:[\\/]Users[\\/]"),
    ("机器名", r"DESKTOP-[A-Z0-9]{7}"),
    ("内网 IP", r"\b(?:192\.168|10)\.\d{1,3}\.\d{1,3}(?:\.\d{1,3})?\b"),
    ("邮箱", r"[\w.+-]+@[\w-]+\.[a-z]{2,}"),
    ("手机号", r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    ("密钥/令牌", r"sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}"),
]

# 允许出现在包里的名字（产品自己的标识，不是个人信息）
ALLOW_IN_EXE = {"voiceunlock_agent_task.xml", "voiceunlock"}


def package_files():
    out = []
    dist = os.path.join(ROOT, "dist")
    for base, _d, fs in os.walk(os.path.join(dist, "VoiceUnlock")):
        out += [os.path.join(base, f) for f in fs]
    for f in ("使用说明.txt", "THIRD-PARTY-NOTICES.txt", "LICENSE-Apache-2.0.txt"):
        out.append(os.path.join(ROOT, "packaging", f))
    pd = os.path.join(ROOT, "provider", "build", "Release")
    if os.path.isdir(pd):
        out += [os.path.join(pd, f) for f in os.listdir(pd)
                if f.lower().endswith(".dll")]
    if os.path.isdir(dist):
        out += [os.path.join(dist, f) for f in os.listdir(dist)
                if f.lower().startswith("voiceunlock-setup")]
    return [p for p in out if os.path.isfile(p)]


def check_files_inventory():
    """① 产物目录里有没有用户数据文件（扩展名判断 + token 文件里的 `file:` 清单）。"""
    bad = []
    root = os.path.join(ROOT, "dist", "VoiceUnlock")
    data_ext = {".json", ".npy", ".bin", ".log", ".wav", ".pcm", ".db", ".sqlite",
                ".csv", ".pkl", ".dat"}
    names = {t[5:] for _w, t in load_extra_tokens() if t.startswith("file:")}
    for base, _d, fs in os.walk(root):
        for f in fs:
            rel = os.path.relpath(os.path.join(base, f), root)
            if rel.startswith("_internal"):
                continue
            if os.path.splitext(f)[1].lower() in data_ext or f in names:
                bad.append(rel)
    print("[① 文件清单] 非 _internal 的数据类文件：%s"
          % (bad if bad else "无（只有两份许可文本）✓"))
    return [{"kind": "file", "what": b} for b in bad]


def check_bytes():
    """② 包内字节里的敏感串。

    ★ 按文件类型分两种查法（踩过：在压缩的安装包/二进制上跑通用正则，
      随机字节会命中一堆假阳 —— 比如把 `x:\\d` 当成"非系统盘路径"、
      把随机字节当成内网 IP。噪声会淹掉真问题）：
        * **文本文件**（.txt/.md/.iss/.spec/.ps1/.cmd/.json）：通用正则 + token 清单
        * **二进制**（exe/dll/pyd/onnx）：只查 token 清单里的**具体已知串**
          （机器名、口令、内网地址、个人目录）—— 这类串一旦出现就是真问题，
          而且不会因为压缩数据而误报。
    """
    TEXT_EXT = {".txt", ".md", ".iss", ".spec", ".ps1", ".cmd", ".json", ".cjs",
                ".py", ".def", ".h", ".cpp"}
    # 许可/NOTICE 正文里出现第三方作者的邮箱是**应该的**（那是归属信息，必须随包），
    # 不算泄露 —— 但**具体 token**（机器名/口令/个人目录）仍然照查。
    LICENSEISH = re.compile(r"(?i)(licen[cs]e|notice|copying|dist-info[/\\])")
    hits = []
    seen = set()
    extra = load_extra_tokens()
    files = package_files()
    for f in files:
        try:
            b = open(f, "rb").read()
        except OSError:
            continue
        rel = os.path.relpath(f, ROOT)
        is_text = os.path.splitext(f)[1].lower() in TEXT_EXT
        is_lic = bool(LICENSEISH.search(rel))
        for view in _text_views(b):
            if is_text and not is_lic:
                found = _scan_text(view, extra)
            else:
                found = [("本机 token", t) for _w, t in extra
                         if not t.startswith("file:") and t in view]
            for why, token in found:
                key = (rel, why, token)
                if key in seen:
                    continue
                seen.add(key)
                hits.append({"kind": "bytes", "why": why, "token": token, "file": rel})
    print("[② 包内字节] 扫 %d 个文件（文本走通用正则、二进制/许可正文只查具体串），命中 %d 处"
          % (len(files), len(hits)))
    for h in hits:
        print("    %s  %s  <- %s" % (h["why"], h["token"], h["file"]))
    return hits


def check_sources():
    """③ 会被编译进 exe 的源码（注释不算，docstring/字符串算）。"""
    files = []
    for base, _d, fs in os.walk(os.path.join(ROOT, "src")):
        files += [os.path.join(base, f) for f in fs if f.endswith(".py")]
    files += [os.path.join(ROOT, "packaging", "vu_cli.py"),
              os.path.join(ROOT, "packaging", "vu_agent.py")]
    hits = []
    extra = load_extra_tokens()
    for f in files:
        for i, line in enumerate(open(f, encoding="utf-8"), 1):
            if line.lstrip().startswith("#"):
                continue                      # 注释不进包
            for why, pat in SRC_PATTERNS:
                if re.search(pat, line):
                    hits.append({"kind": "src", "why": why,
                                 "file": os.path.relpath(f, ROOT), "line": i,
                                 "text": line.strip()[:120]})
            for why, tok in extra:
                if tok in line:
                    hits.append({"kind": "src", "why": why,
                                 "file": os.path.relpath(f, ROOT), "line": i,
                                 "text": line.strip()[:120]})
    print("[③ 会进包的源码] 扫 %d 个文件，命中 %d 处" % (len(files), len(hits)))
    for h in hits:
        print("    %s  %s:%d  %s" % (h["why"], h["file"], h["line"], h["text"]))
    return hits


def _iter_strings(code):
    for c in code.co_consts:
        if isinstance(c, str):
            yield c
        elif isinstance(c, types.CodeType):
            yield from _iter_strings(c)


def check_pyz(exe):
    """④ 把 PYZ 里的模块解出来，遍历字符串常量（这才是压缩层里的真相）。"""
    try:
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    except Exception as e:
        print("[④ 冻结模块] 跳过（PyInstaller 不可用：%r）" % e)
        return []
    import tempfile
    hits = []
    extra = load_extra_tokens()
    a = CArchiveReader(exe)
    pyz = [n for n in a.toc if "PYZ" in n]
    if not pyz:
        print("[④ 冻结模块] 跳过（没找到 PYZ）")
        return []
    blob = a.extract(pyz[0])
    tmp = os.path.join(tempfile.gettempdir(), "vu_pyz_scan.pyz")
    open(tmp, "wb").write(blob if isinstance(blob, (bytes, bytearray)) else b"")
    z = ZlibArchiveReader(tmp)
    mods = [n for n in z.toc if n.startswith("src.")]
    for m in sorted(mods):
        try:
            code = z.extract(m)
        except Exception:
            continue
        if not isinstance(code, types.CodeType):
            continue
        for s in _iter_strings(code):
            if s in ALLOW_IN_EXE:
                continue
            for why, tok in _scan_text(s, extra):
                hits.append({"kind": "pyz", "why": why, "module": m,
                             "string": s[:120]})
    print("[④ 冻结模块] %s：解出 %d 个 src.* 模块，命中 %d 处"
          % (os.path.basename(exe), len(mods), len(hits)))
    for h in hits:
        print("    %s  %s  %r" % (h["why"], h["module"], h["string"]))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    print("=== 安装包敏感信息扫描（三项 + 冻结模块）===")
    hits = []
    hits += check_files_inventory()
    hits += check_bytes()
    hits += check_sources()
    for exe in ("VoiceUnlockAgent.exe", "VoiceUnlock.exe"):
        p = os.path.join(ROOT, "dist", "VoiceUnlock", exe)
        if os.path.isfile(p):
            hits += check_pyz(p)

    print("\n=== 合计 %d 处 ===" % len(hits))
    if hits:
        print("有命中，交付前必须处理。")
    else:
        print("干净：包内没有本机路径 / 账户 / 口令 / 用户数据。")
    if a.json:
        json.dump(hits, open(a.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("明细：%s" % a.json)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
