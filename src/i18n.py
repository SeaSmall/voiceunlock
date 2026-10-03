"""i18n.py -- 输出语言自动切换（中文系统说中文，非中文系统说英文）

★ 为什么要有这个（这是真缺陷，不是锦上添花）：
    目标机器不一定是中文 Windows。英文版 Windows 的控制台代码页是 437，
    我们满屏中文 → 用户看到的是乱码（或一串 `?`），排障第一步就卡死。
    实测：英文 Win10 + cp437 打印中文，全部变 `?`（errors=replace 兜住了崩溃，
    但信息量为零）。所以【按系统 UI 语言自动选语言】是必须的。

设计（三条硬约束，决定了实现形态）：

  1. **不改 250 处 print 调用点**。这是个已验收过的项目（冻结版 parity=1.0、
     判定自测通过、VM 里装过一遍）。为了翻译去重写所有调用点，等于把回归风险
     引进来。所以：在**输出层**换实现 —— 把 builtins.print / builtins.input 和
     argparse 的打印口套一层，翻译"要打印的那一条消息"。
  2. **中文系统上必须是零改动**。中文 Windows 上 install() 直接返回、什么都不
     打补丁（连翻译表都不查）。用户自己的机器行为与这一版之前**逐字节一致**。
  3. **翻译绝不能把工具搞崩**。任何匹配/格式化异常都吞掉，返回原文（宁可露出
     中文，也不能因为翻译抛异常让 setup-password 半途死掉）。

匹配规则（与 tools/i18n_scan.py 的扫描口径一一对应）：
    * 目录里的"消息"是**按行**存的：一条 print 里若含 \\n，逐行翻译。
    * 无占位符的行 → 整行精确匹配。
    * 带 %s/%d/%.2f 的行 → 由中文模板生成正则（占位符位置捕获 `(.+?)`），
      英文模板用同样个数的 %s 回填。**所以英文模板里的 %s 个数必须与中文
      模板的占位符个数相同**（i18n_audit 会逐条断言）。
    * 英文一律纯 ASCII：连 ✓ ✗ ★ 这种符号都不用（cp437 也渲染得出来）。

语言探测顺序：环境变量 VOICEUNLOCK_LANG > 系统 UI 语言 > 控制台代码页。
    VOICEUNLOCK_LANG=zh / en 可强制（排障与自动化测试都靠它，见 i18n_audit）。
"""
from __future__ import annotations

import builtins
import ctypes
import os
import re
import sys

__all__ = ["LANG", "detect_lang", "install", "translate_text", "translate_line",
           "translate_value", "tr", "T", "has_entry", "catalog_stats"]

ENV = "VOICEUNLOCK_LANG"

# 中日韩统一表意文字 + 全角标点/符号 + CJK 标点 + 制表符块
CJK = re.compile(r"[\u2500-\u257f\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff"
                 r"\uff00-\uffef\u2190-\u21ff\u2605\u2606\u2713\u2717\u2718]")

# %s / %d / %.2f / %-28s / %% ...（与 tools/i18n_scan.py 保持同一套正则）
SPEC = re.compile(r"%[-+ #0]*[\d*]*(?:\.\d+)?[hlL]?[diouxXeEfFgGcrsa%]")


# ------------------------------------------------------------------ 语言探测

def _ui_langid() -> int:
    try:
        return int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
    except Exception:
        return 0


def _console_cp() -> int:
    try:
        return int(ctypes.windll.kernel32.GetConsoleOutputCP())
    except Exception:
        return 0


def detect_lang() -> str:
    """返回 'zh' 或 'en'。"""
    forced = (os.environ.get(ENV) or "").strip().lower()
    if forced in ("zh", "cn", "zh-cn", "zh_cn", "chinese", "0"):
        return "zh"
    if forced in ("en", "en-us", "english", "1"):
        return "en"
    langid = _ui_langid()
    if langid and (langid & 0x3FF) == 0x04:       # LANG_CHINESE
        return "zh"
    if langid == 0 and _console_cp() in (936, 54936):   # 探测不到就退回代码页
        return "zh"
    return "en"


LANG = detect_lang()


# ------------------------------------------------------------------ 目录 → 匹配表

def _spec_spans(text: str):
    """返回 [(start, end, is_literal_percent)]，按出现顺序。"""
    out = []
    for m in SPEC.finditer(text):
        out.append((m.start(), m.end(), m.group(0) == "%%"))
    return out


def spec_count(text: str) -> int:
    return len([1 for _s, _e, lit in _spec_spans(text) if not lit])


def _to_regex(zh: str):
    """中文模板 → 匹配"已格式化输出"的正则。

    ★ 占位符用 `(.*?)` 而不是 `(.+?)`：**值可能是空串**。
        print("凭据文件      = %s %s" % (st.path, "" if st.exists() else "(还没保存)"))
      凭据存在时第二个 %s 就是空的，整行变成 "...credential.bin "（末尾一个空格）。
      用 `(.+?)` 会匹配不上 → 这一行原样露出中文（真踩过：只有"凭据不存在"的
      机器上是好的，一旦保存过凭据就变回中文）。
    """
    parts = []
    pos = 0
    for s, e, is_lit in _spec_spans(zh):
        parts.append(re.escape(zh[pos:s]))
        parts.append("%" if is_lit else "(.*?)")
        pos = e
    parts.append(re.escape(zh[pos:]))
    return re.compile("".join(parts), re.DOTALL)


_IDE = re.compile(r"[\u4e00-\u9fff]")


def literal_part(zh: str) -> str:
    """模板里"除占位符以外的部分"。"""
    parts, pos = [], 0
    for s, e, is_lit in _spec_spans(zh):
        parts.append(zh[pos:s])
        if is_lit:
            parts.append("%")
        pos = e
    parts.append(zh[pos:])
    return "".join(parts)


def _too_generic(zh: str) -> bool:
    """这个模板是不是"太通用"、不该拿去做整行匹配？

    ★ 这是抓出来的真实故障（不是假想）：
        enroll.py 里有一句 `head = "%s（%s）" % (head, who)`，它也进了目录。
        于是英文模式下**任何** "xxx（yyy）" 形状的行都被它整行吃掉：
            "  --device DEVICE    只登记指定设备（序号或 endpoint id）"
        变成了 "  --device DEVICE    只登记指定设备 (序号或 endpoint id)"
        —— 中文一个字没翻，只有括号被换成 ASCII，比不翻还难看。
    判据：字面部分（去掉占位符后）至少 2 个字符，且至少含一个汉字。
        只剩标点的模板（"（）"、"："）一律不做整行匹配。
    """
    lit = literal_part(zh).strip()
    return len(lit) < 2 or not _IDE.search(lit)


_EXACT = {}          # zh 行（无占位符）→ en 行
_KEYS = set()        # 目录里的全部中文键（has_key 用：查"这句在不在目录里"）
_FRAG = []           # (zh, en) 按 zh 长度降序：整行匹配不上时的"片段替换"兜底
_PATTERNS = []       # (正则, 占位符数, en 模板)，构建后按中文模板长度降序
_PATSORT = []        # 同上，带长度，排序用
_BAD = []            # 占位符个数对不上的条目（审计会报）
_GENERIC = []        # 太通用、被禁止做整行模板的条目（见 _too_generic 的说明）
_VPATTERNS = []      # 上面这批"只做值匹配"的模板


def _build() -> None:
    from .i18n_catalog import CATALOG
    for zh, en in CATALOG:
        _KEYS.add(zh)
        n_zh = spec_count(zh)
        if n_zh == 0:
            _EXACT[zh] = en
            _FRAG.append((zh, en))
            continue
        if spec_count(en) != n_zh:
            _BAD.append((zh, en))
            continue
        if _too_generic(zh):
            _GENERIC.append((zh, en))       # 只留精确匹配/值匹配，不做整行模板
            continue
        _PATSORT.append((_to_regex(zh), n_zh, en, len(zh)))
    # 片段替换只在"整行没匹配上"时才跑（argparse 的 --help 会把我们的 help=
    # 文字前面加上对齐用的空格和参数名，整行匹配必然落空）。长的先替换，
    # 免得短条目把长条目的一部分先吃掉。
    _FRAG.sort(key=lambda p: -len(p[0]))
    # 模板也一样：**长的先试**。否则 "声纹档案      = %s" 会把
    # "声纹档案      = 11 条" 整条吃掉（%s 连"11 条"一起吞），英文里就漏出中文。
    _PATSORT.sort(key=lambda p: -p[3])
    _PATTERNS.extend((rx, n, en) for rx, n, en, _l in _PATSORT)
    _PATSORT.clear()
    # 被 _too_generic 拦下的模板：整行不敢用，但"值"里可以用（见 translate_value）
    for zh, en in _GENERIC:
        _VPATTERNS.append((_to_regex(zh), spec_count(zh), en))


def catalog_stats() -> dict:
    return {"exact": len(_EXACT), "patterns": len(_PATTERNS),
            "generic_blocked": len(_GENERIC), "bad": len(_BAD), "lang": LANG}


def has_key(text: str) -> bool:
    """这条**原文**在目录里吗（按字符串比，不做正则反查）。

    审计用的是这个：扫描器给的是"模板原文"（含 %d/%% 字面），拿模板原文去跑
    模板正则属于自欺 —— 只有 %% 这种转义会露馅。查目录就老老实实查键。
    """
    return text in _KEYS


def has_entry(line: str) -> bool:
    """这一行（**已格式化的输出**）能被翻译吗。不看当前语言。"""
    if not CJK.search(line):
        return True
    if line in _EXACT:
        return True
    for rx, n, _en in _PATTERNS:
        m = rx.fullmatch(line)
        if m and len(m.groups()) == n:
            return True
    return False


def _fragment_pass(line: str) -> str:
    """整行匹配不上时的兜底：把行内已知的中文片段逐个换成英文。

    典型场景只有 argparse：`  --user USER           账户名（默认当前用户）`
    前面那截对齐空格和参数名是 argparse 加的，整行当然匹配不上模板。
    """
    out = line
    for _ in range(8):
        if not CJK.search(out):
            break
        for zh, en in _FRAG:
            if zh in out:
                out = out.replace(zh, en)
                break
        else:
            break
    return out


def translate_value(s: str, _depth: int = 0) -> str:
    """翻译"被塞进消息里的值"。

    ★ 为什么必须单独有一套：很多中文不是 print 的第一个参数，而是**值**。
        print("凭据          = %s" % st.info())     ← st.info() 整句中文
        print("锁屏状态      = %s" % ("是" if ...))  ← 值是"是"/"否"
        raise RuntimeError("CryptUnprotectData 失败: err=%d（...）")
      模板翻好了、值还是中文，用户照样看不懂。所以值也走一遍目录，
      只不过这一步用**子串**匹配（值常常是一句话里嵌着目录里的模板），
      并且允许往下钻一层（"准备录音（麦克风）" = 值里套值）。
    """
    if LANG == "zh" or not s or not CJK.search(s):
        return s
    en = _EXACT.get(s)
    if en is not None:
        return en
    if _depth < 3:
        for rx, n, tpl in _PATTERNS:
            m = rx.search(s)
            if not m:
                continue
            try:
                inner = tpl % tuple(translate_value(g, _depth + 1) for g in m.groups())
            except Exception:
                break
            return s[:m.start()] + inner + s[m.end():]
        for rx, n, tpl in _VPATTERNS:          # 太通用、整行不敢用，值里可以用
            m = rx.search(s)
            if not m:
                continue
            try:
                inner = tpl % tuple(translate_value(g, _depth + 1) for g in m.groups())
            except Exception:
                break
            return s[:m.start()] + inner + s[m.end():]
    return _fragment_pass(s)


def translate_line(line: str) -> str:
    if LANG == "zh" or not line:
        return line
    if not CJK.search(line):
        return line
    en = _EXACT.get(line)
    if en is not None:
        return en
    for rx, n, tpl in _PATTERNS:
        m = rx.fullmatch(line)
        if not m:
            continue
        try:
            return tpl % tuple(translate_value(g) for g in m.groups())
        except Exception:
            return line          # 占位符对不上：宁可露中文，不许崩
    return _fragment_pass(line)


def translate_text(s: str) -> str:
    if LANG == "zh" or not s or not CJK.search(s):
        return s
    try:
        if "\n" not in s:
            return translate_line(s)
        return "\n".join(translate_line(x) for x in s.split("\n"))
    except Exception:
        return s


def tr(zh: str) -> str:
    """单条消息翻译（给 getpass 提示这类不走 print 的地方用）。"""
    return translate_line(zh)


def T(zh: str, en: str) -> str:
    """调用点自带英文（新代码用；老代码走目录）。"""
    return en if LANG == "en" else zh


# ------------------------------------------------------------------ 打补丁

_ORIG_PRINT = builtins.print
_ORIG_INPUT = builtins.input
_patched = False


def _print(*args, **kw):
    if args:
        try:
            new = tuple(translate_text(a) if isinstance(a, str) else a for a in args)
            if new != args:
                args = new
        except Exception:
            pass
    return _ORIG_PRINT(*args, **kw)


def _input(prompt=""):
    try:
        if isinstance(prompt, str):
            prompt = translate_text(prompt)
    except Exception:
        pass
    return _ORIG_INPUT(prompt)


def _patch_argparse() -> None:
    """argparse 的 help/报错不走 print（直接 file.write），单独套一层。"""
    try:
        import argparse
        orig = argparse.ArgumentParser._print_message

        def _pm(self, message, file=None):
            try:
                message = translate_text(message)
            except Exception:
                pass
            return orig(self, message, file)

        argparse.ArgumentParser._print_message = _pm
    except Exception:
        pass


def install(force: str | None = None) -> str:
    """按当前语言装好输出层。中文系统上是一个真正的 no-op。"""
    global LANG, _patched
    if not _EXACT and not _PATTERNS:
        _build()
    if force in ("zh", "en"):
        LANG = force
    if LANG == "zh":
        return LANG
    if not _patched:
        builtins.print = _print
        builtins.input = _input
        _patch_argparse()
        _patched = True
    return LANG


_build()
if LANG == "en":
    install()
