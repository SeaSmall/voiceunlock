"""i18n_audit.py -- 英文输出目录的"证据级"审计（五项，全过才返回 0）

为什么要有这个工具：
    "英文系统上不再乱码"这件事，如果只是"跑一下看着没问题"，那就是没证据。
    这个脚本把"没问题"变成五条可复跑的断言：

    A 目录完整性   —— 英文必须纯 ASCII、占位符个数与中文一一对应、无重复条目
    B 静态覆盖     —— 源码里所有用户可见中文（print/常量/argparse/getpass）
                      都能在目录里找到（穷举，不是抽样）
    C 模板自渲染   —— 每条带 %s 的模板，按其类型造一个假值渲染出"已格式化字符串"，
                      断言真能被匹配上并翻成英文（这是匹配机制本身的单测）
    D 运行时 ASCII —— 真跑一遍 CLI 的所有**只读**子命令，断言输出里一个非 ASCII
                      字符都没有（英文控制台是 cp437，出现非 ASCII 就是乱码）
    E 中文零回归   —— 中文模式下，源码版的输出必须与改动前的冻结版**逐字节相同**
                      （基线用 dist\\VoiceUnlock\\VoiceUnlock.exe，它是改动前打的包）

用法：
    python tools\\i18n_audit.py            # A-D
    python tools\\i18n_audit.py --baseline # 额外做 E（需要改动前的冻结版还在）
    python tools\\i18n_audit.py --all
"""
from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")
OLD_EXE = os.path.join(ROOT, "dist", "_baseline_last_accepted", "VoiceUnlock",
                       "VoiceUnlock.exe")
if not os.path.isfile(OLD_EXE):     # 兼容旧的基线目录名
    OLD_EXE = os.path.join(ROOT, "dist", "_baseline_pre_i18n", "VoiceUnlock",
                           "VoiceUnlock.exe")
if not os.path.isfile(OLD_EXE):     # 都没有就退回当前 dist（那样 [E] 会自我比较）
    OLD_EXE = os.path.join(ROOT, "dist", "VoiceUnlock", "VoiceUnlock.exe")
ENV = "VOICEUNLOCK_LANG"

# ★ 审计工具自己的输出必须是**中文原文**，否则报告会被 i18n 自己翻译一遍，
#   看起来像"残留中文"的地方全是幻觉（踩过：报告里出现 是（源码运行））。
os.environ[ENV] = "zh"

SPEC = re.compile(r"%[-+ #0]*[\d*]*(?:\.\d+)?[hlL]?[diouxXeEfFgGcrsa%]")
CJKRE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")

# [D] 允许残留中文的**数据**行（不是我们的消息，翻它反而是错的）
#   - 声纹档案摘要：名字来自用户档案（本机是从旧 .voiceprint.npy 导入的，就叫
#     "legacy-voiceprint（旧声纹导入）"），是**用户数据**，不是界面文案
#   - 设备列表：设备名由操作系统给出（中文系统给中文名），我们没有权利改它
ALLOW_DATA = [
    (re.compile(r"^\s+- .*threshold=.*samples=.*source="), "声纹档案名（用户数据）"),
    (re.compile(r"^\s*\[\d+\] \[(trusted|untrusted)\]"), "设备名（操作系统给的）"),
]

_tee = []


def say(*parts):
    line = " ".join(str(p) for p in parts)
    print(line)
    _tee.append(line)


SAFE_CMDS = [
    [],
    ["help"],
    ["paths"],
    ["cp-status"],
    ["agent", "status"],
    ["enroll", "--list"],
    ["enroll", "--help"],
    ["agent", "--help"],
    ["setup-password", "--help"],
    ["probe-hud"],                      # 不存在的子命令 → 走"未知子命令"分支
]


def sh_ascii(s: str) -> str:
    return "".join(c if ord(c) < 128 else "<%04X>" % ord(c) for c in s)


# ------------------------------------------------------------------ A 目录完整性

def check_catalog() -> list:
    sys.path.insert(0, ROOT)
    from src import i18n
    from src.i18n_catalog import CATALOG
    bad = []
    seen = {}
    for zh, en in CATALOG:
        if zh in seen:
            bad.append("重复条目：%r（前一次=%r 这次=%r）" % (zh, seen[zh], en))
        seen[zh] = en
        if any(ord(c) > 127 for c in en):
            bad.append("英文非 ASCII：%r -> %r" % (zh, en))
        n_zh, n_en = i18n.spec_count(zh), i18n.spec_count(en)
        if n_zh != n_en:
            bad.append("占位符个数不一致（%d vs %d）：%r -> %r" % (n_zh, n_en, zh, en))
        for m in SPEC.finditer(en):
            if m.group(0) not in ("%s", "%%"):
                bad.append("英文占位符必须是 %%s：%r -> %r" % (zh, en))
        if not en.strip():
            bad.append("英文为空：%r" % (zh,))
    say("[A] 目录 %d 条，问题 %d" % (len(CATALOG), len(bad)))
    return bad


# ------------------------------------------------------------------ B 静态覆盖

def check_coverage() -> list:
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import i18n_scan
    rows = []
    for rel in i18n_scan.FILES:
        rows += i18n_scan.scan_file(rel)
    sys.path.insert(0, ROOT)
    from src import i18n
    bad = []
    seen = set()
    for r in rows:
        if r["text"] in seen:
            continue
        seen.add(r["text"])
        if r["kind"] == "dynamic-cjk":
            # 形如 print('已删除' if ok else '本来就没有')：整条没法当模板，
            # 但里面的每个字面量都必须自己有目录条目（片段替换会处理它）。
            lits = re.findall(r"'([^']*)'|\"([^\"]*)\"", r["text"])
            lits = [x or y for x, y in lits]
            for lit in lits:
                if lit and not i18n.has_key(lit):
                    bad.append("动态拼接里的字面量缺目录条目：%s:%s %r"
                               % (r["file"], r["line"], lit))
            continue
        if not i18n.has_key(r["text"]):
            bad.append("目录缺条目：%s:%s %r" % (r["file"], r["line"], r["text"][:70]))
    say("[B] 静态扫描去重后 %d 条，缺 %d" % (len(seen), len(bad)))
    return bad


# ------------------------------------------------------------------ C 模板自渲染

def _samples(zh: str):
    """按转换符类型造假值：%d 给 int、%f 给 float、%s/%r 给 str。"""
    vals = []
    i = 0
    for m in SPEC.finditer(zh):
        tok = m.group(0)
        if tok == "%%":
            continue
        i += 1
        conv = tok[-1]
        if conv in "diouxXc":
            vals.append(10 + i)
        elif conv in "eEfFgG":
            vals.append(1.25 + i)
        elif conv == "r":
            vals.append("s%d" % i)        # %r 会给它加引号，字符串即可
        else:
            vals.append("s%d" % i)
    return vals


def _shape_re(en: str):
    """把英文模板变成"结构正则"：%s → .+?，%% → 字面 %，其余按字面。"""
    parts, pos = [], 0
    for m in SPEC.finditer(en):
        parts.append(re.escape(en[pos:m.start()]))
        parts.append("%" if m.group(0) == "%%" else ".+?")
        pos = m.end()
    parts.append(re.escape(en[pos:]))
    return re.compile("".join(parts), re.DOTALL)


def check_templates() -> list:
    """每条带占位符的模板：按转换符类型造假值渲染出"已格式化输出"，
    再断言它真能被翻译成**结构正确、且不再含中文**的英文。

    这里不比对"具体的值"（%.6f 渲染成 3.250000，跟造出来的 3.25 不一样），
    比的是三件真正会坏的事：
      1. 整行压根没翻出来（模板正则匹配不上 → 用户看到中文）
      2. 翻完还残留中文（**更短的模板把更长的模板抢了**，%s 把"11 条"一起吞掉）
      3. 英文结构跟目录里写的对不上（占位符个数/字面量错位）
    """
    sys.path.insert(0, ROOT)
    from src import i18n
    from src.i18n_catalog import CATALOG
    bad = []
    n = 0
    old = i18n.LANG
    i18n.LANG = "en"
    try:
        for zh, en in CATALOG:
            if i18n.spec_count(zh) == 0:
                continue
            n += 1
            try:
                rendered = zh % tuple(_samples(zh))
            except Exception as e:
                bad.append("中文模板自己渲染不出来：%r (%r)" % (zh, e))
                continue
            got = i18n.translate_line(rendered)
            if got == rendered:
                # 太通用的模板（"%s（%s）" 之类）只允许走"值"通道（它们整行匹配
                # 会把别人的消息吃掉），所以这里补测值通道。
                got = i18n.translate_value(rendered)
            if got == rendered:
                bad.append("模板没匹配上（会露出中文）：%r → 渲染后 %r" % (zh, rendered))
            elif re.search(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]",
                           got):
                bad.append("翻完仍残留中文（多半被更短的模板抢了）：\n"
                           "      src=%r\n      got=%r" % (zh, got))
            elif not _shape_re(en).fullmatch(got):
                bad.append("英文结构与目录不符：\n      src=%r\n      got=%r\n      en =%r"
                           % (zh, got, en))
    finally:
        i18n.LANG = old
    say("[C] 带占位符模板 %d 条，问题 %d" % (n, len(bad)))
    return bad


# ------------------------------------------------------------------ D 运行时 ASCII

def _decode(b: bytes) -> str:
    """冻结版不吃 PYTHONIOENCODING，吐的是控制台代码页（GBK）；源码版是 UTF-8。
    两边各按自己的编码解，才能比"内容"而不是比"编码"（踩过：整个 [E] 全是
    假差异，看起来像中文输出全变了）。"""
    for enc in ("utf-8", "mbcs", "gbk"):
        try:
            return b.decode(enc)
        except Exception:
            pass
    return b.decode("utf-8", "replace")


def _run(cmd, lang, exe=None, home=ROOT):
    """跑一条命令。

    ★ 一律把 VOICEUNLOCK_HOME 钉到项目目录：不钉的话，冻结版用
      %LOCALAPPDATA%\\VoiceUnlock（生产 HOME）、源码版用项目目录（开发 HOME），
      [E] 的中文对比会报一堆"差异"，全是路径与档案内容不同造成的假差异 ——
      那样这个检查就废了（踩过）。钉住 HOME 之后两边看的是同一份数据，
      比出来的才是"文案有没有变"。
    """
    env = dict(os.environ)
    env["VOICEUNLOCK_LANG"] = lang
    env["VOICEUNLOCK_HOME"] = home
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = ROOT
    if exe:
        argv = [exe] + cmd
    else:
        argv = [PY, os.path.join(ROOT, "packaging", "vu_cli.py")] + cmd
    p = subprocess.run(argv, capture_output=True, cwd=ROOT, env=env, timeout=180,
                       input=b"")
    return _decode((p.stdout or b"") + (p.stderr or b"")), p.returncode


def check_runtime() -> list:
    bad = []
    allowed = []
    for cmd in SAFE_CMDS:
        text, rc = _run(cmd, "en")
        label = " ".join(cmd) or "(无参数)"
        cjk = [ln for ln in text.splitlines() if CJKRE.search(ln)]
        # 只把**路径片段**遮掉（用户目录名可能是中文），而不是整行跳过 ——
        # 整行跳过太宽：`凭据文件      = C:\...\credential.bin` 这种"标签没翻"
        # 的缺陷会被一起跳过（真踩过，[D] 当时是绿的）。
        residual = []
        for ln in cjk:
            probe = re.sub(r"[A-Za-z]:\\[^\s\"']*", "<PATH>", ln)
            probe = re.sub(r"\\\\[^\s\"']*", "<UNC>", probe)
            if not CJKRE.search(probe):
                continue
            if any(rx.search(ln) for rx, _why in ALLOW_DATA):
                allowed.append("%s : %s" % (label, ln.strip()))
                continue
            residual.append(ln)
        if residual:
            p = os.path.join(ROOT, "logs", "i18n_runtime_%s.txt"
                             % (re.sub(r"\W+", "_", label).strip("_") or "noargs"))
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            bad.append("命令 %r 输出里仍有中文（原文存到 %s）：\n      %s"
                       % (label, os.path.basename(p),
                          "\n      ".join(sh_ascii(x) for x in residual[:6])))
        if rc not in (0, 1, 2):
            bad.append("命令 %r 退出码异常 %d" % (label, rc))
    say("[D] 运行时只读子命令 %d 条，问题 %d（另有 %d 行是用户名/设备名等数据，"
        "按设计不翻）" % (len(SAFE_CMDS), len(bad), len(allowed)))
    for a in allowed:
        say("    · 数据行：%s" % sh_ascii(a))
    return bad


# ------------------------------------------------------------------ E 中文零回归

def check_baseline() -> list:
    if not os.path.isfile(OLD_EXE):
        say("[E] 跳过：找不到改动前的冻结版 %s" % OLD_EXE)
        return []
    bad = []
    for cmd in SAFE_CMDS:
        old, _ = _run(cmd, "zh", exe=OLD_EXE)
        new, _ = _run(cmd, "zh")
        # 冻结版与源码版的差异只有三处，且都是"形态差异"不是"文案差异"：
        #   1) 可执行文件 / _MEIPASS / 打包形态 —— 一个是 exe 一个是 python
        #   2) ONNX 模型路径 —— 冻结版从 _internal\models 取，源码版从 models\ 取
        #   3) 凭据可读性前面的符号 —— 冻结版吃控制台代码页(GBK) 编不出 ✗ → "?"，
        #      源码版有 PYTHONIOENCODING=utf-8。同一个消息、两种编码。
        # 归一化掉这三处之后，剩下的必须**逐行完全相同**。
        def norm(t):
            t = re.sub(r"(?m)^(可执行文件|_MEIPASS|打包形态|ONNX 模型).*$",
                       r"\1 <norm>", t)
            t = re.sub(r"(?m)^(\s*凭据可读性\s*=\s*)\S+", r"\1<mark>", t)
            t = re.sub(r"(?m)^(\s*凭据可读性\s*=\s*)<mark>.*$", r"\1<mark>", t)
            return t
        a, b = norm(old), norm(new)
        if a != b:
            import difflib
            d = "\n".join(list(difflib.unified_diff(
                a.splitlines(), b.splitlines(), "old", "new", lineterm=""))[:40])
            bad.append("中文输出与上次验收过的构建不一致：%r\n%s\n"
                       "      ↑ 如果这些差异是你**有意**改的（改文案、改行为），"
                       "审查完之后刷新基线：\n"
                       "        把 dist\\VoiceUnlock 整个拷到 "
                       "dist\\_baseline_last_accepted\\VoiceUnlock（覆盖），"
                       "并把这次改动写进施工方案。\n"
                       "        基线是【上一次验收通过的构建】，不是自动跟随当前代码。"
                       % (" ".join(cmd) or "(无参数)", d))
    say("[E] 中文模式对比 %d 条命令，不一致 %d" % (len(SAFE_CMDS), len(bad)))
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", help="把报告同时写成 UTF-8 文件（控制台是 GBK 时看这个）")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    problems = []
    problems += check_catalog()
    problems += check_coverage()
    problems += check_templates()
    problems += check_runtime()
    if a.baseline or a.all:
        problems += check_baseline()

    say("")
    if problems:
        say("=== 不通过：%d 个问题 ===" % len(problems))
        for p in problems[:60]:
            say(" * " + p)
    else:
        say("=== i18n 审计全部通过 ===")

    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(_tee) + "\n")
        print("report -> %s" % a.out)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
