"""i18n_scan.py -- 穷举"用户可能看到的中文"，做英文目录的清单与覆盖审计。

为什么需要它（不能靠 grep）：
    本项目不改 print 调用点，而是在**输出层**翻译（见 src/i18n.py）。
    输出层翻译唯一的致命失败模式是"某条消息漏出目录，英文机器上露出中文"，
    所以必须能**静态穷举**所有用户可见中文，并逐条断言目录里有它。

扫什么（这几条就是"用户能看见的全部出口"）：
    1. print(...) 的**每个**字面量参数（含 % 拼接 / f-string）
    2. 被 print(名字) 整体打印的模块级字符串常量（如 vu_cli.HELP）
    3. argparse 的 help= / description= / epilog=（--help 与报错走这条路）
    4. getpass.getpass(...) 的提示（它绕开 print 直接写 stderr）
    5. input(...) 的提示（我们在 i18n 里补了 builtins.input，但仍要进目录）

用法：
    python tools/i18n_scan.py                  # 看清单 + 统计
    python tools/i18n_scan.py --json x.json    # 机器可读（切片交给翻译）
    python tools/i18n_scan.py --audit          # 断言目录覆盖（0 = 全覆盖）
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CJK = re.compile(r"[\u2500-\u257f\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff"
                 r"\uff00-\uffef\u2190-\u21ff\u2605\u2606\u2713\u2717\u2718]")
SPEC = re.compile(r"%[-+ #0]*[\d*]*(?:\.\d+)?[hlL]?[diouxXeEfFgGcrsa%]")

FILES = [
    "packaging/vu_cli.py", "packaging/vu_agent.py",
    "src/cli.py", "src/agent.py", "src/enroll.py", "src/install.py",
    "src/setup_password.py", "src/devices.py", "src/dpapi.py", "src/config.py",
    "src/session.py", "src/sv.py", "src/profiles.py", "src/selftest_decision.py",
    "src/verify.py", "src/win32pipe.py", "src/hooks.py", "src/audio.py",
    "src/parity.py", "src/selftest.py",
]


def spec_count(text: str) -> int:
    return len([m for m in SPEC.finditer(text) if m.group(0) != "%%"])


LOGISH = re.compile(r"(^|\.)_?log$|logger|Log$")


def _expand(node):
    """把表达式归纳成 (模板, 占位符数or None)。None 占位符数 = f-string/.format。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, spec_count(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) \
            and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
        return node.left.value, spec_count(node.left.value)
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            parts.append(v.value if (isinstance(v, ast.Constant)
                                     and isinstance(v.value, str)) else "%s")
        return "".join(parts), None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "format" and isinstance(node.func.value, ast.Constant) \
            and isinstance(node.func.value.value, str):
        return node.func.value.value, None
    return None


def _parents(tree):
    """自建带父链的遍历（ast.walk 不给父节点，而"是不是 print 的实参"要靠父节点）。"""
    stack = [(tree, [])]
    while stack:
        node, parents = stack.pop()
        yield node, parents
        for child in ast.iter_child_nodes(node):
            stack.append((child, parents + [node]))


def _enclosing_call(parents):
    for p in reversed(parents):
        if isinstance(p, ast.Call):
            return p
    return None


def _in_docstring(node, parents):
    """node 是文档字符串吗：父是 Expr，祖父是 def/module/class，且该 Expr 是其 body[0]。"""
    if len(parents) < 2:
        return False
    expr, owner = parents[-1], parents[-2]
    if not isinstance(expr, ast.Expr) or expr.value is not node:
        return False
    if not isinstance(owner, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
        return False
    return bool(owner.body) and owner.body[0] is expr


def scan_file(rel: str):
    path = os.path.join(ROOT, rel)
    if not os.path.isfile(path):
        return []
    tree = ast.parse(open(path, encoding="utf-8").read())
    rows = []

    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    consts[t.id] = node.value.value

    def add(lineno, kind, text, specs=None):
        for line in text.split("\n"):
            if not CJK.search(line):
                continue
            rows.append({"file": rel, "line": lineno, "kind": kind, "text": line,
                         "specs": (spec_count(line) if specs is None else specs)})

    # ---- 第一遍：print / getpass / input / argparse（结构化，能认出"模板"）
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else (
            f.attr if isinstance(f, ast.Attribute) else "")

        if name == "print":
            for i, arg in enumerate(node.args):
                if i == 0 and isinstance(arg, ast.Name) and arg.id in consts:
                    add(node.lineno, "const", consts[arg.id])
                    continue
                got = _expand(arg)
                if got:
                    add(node.lineno, "literal", got[0], got[1])
                elif CJK.search(ast.unparse(arg)):
                    rows.append({"file": rel, "line": node.lineno, "kind": "dynamic-cjk",
                                 "text": ast.unparse(arg), "specs": -1})
            continue

        if name in ("getpass", "input"):
            for arg in node.args[:1]:
                got = _expand(arg)
                if got:
                    add(node.lineno, "prompt", got[0], got[1])
            continue

        for kw in node.keywords:
            if kw.arg in ("help", "description", "epilog", "usage"):
                got = _expand(kw.value)
                if got:
                    add(node.lineno, "argparse", got[0], got[1])

    # ---- 第二遍：**所有**其余中文字面量
    # 为什么必须扫这些：消息里的"值"也是中文。例如
    #     print("凭据          = %s" % st.info())      ← st.info() 里是一整句中文
    #     print("锁屏状态      = %s" % ("是" if ... )) ← 值是"是"/"否"
    # 它们不是打印调用的第一个参数，只扫第一个参数就会在英文机上露出来。
    # 排除的只有两类：文档字符串（不输出）、日志回调的实参（进日志文件，不跟
    # 语言走 —— 而且 _log 是"控制台 + 文件"同一行，翻它反而让两处不一致）。
    for node, parents in _parents(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        text = node.value
        if not CJK.search(text) or _in_docstring(node, parents):
            continue
        call = _enclosing_call(parents)
        if call is not None:
            cf = call.func
            cname = cf.id if isinstance(cf, ast.Name) else (
                cf.attr if isinstance(cf, ast.Attribute) else "")
            if LOGISH.search(cname or ""):
                continue                      # 日志行：跳过
            # ★ print(...) 里的**值**不能跳！第一遍只收第一个参数（模板），
            #   而 `print("状态 = %s" % ("是" if x else "否"))` 里的"是/否"、
            #   `("存在" if disp else "缺失")` 这类**值**全都在第一个参数之外。
            #   跳了它们，英文机上就会出现 "Credential Providers : 缺失"。
            #   重复的（模板本身）由后面的按文本去重吃掉。
        kind = "raise" if any(isinstance(p, ast.Raise) for p in parents) else "other"
        add(node.lineno, kind, text)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--slices", type=int, default=0, help="切成 N 份写 logs/i18n_slice_*.json")
    a = ap.parse_args()

    rows = []
    for rel in FILES:
        rows += scan_file(rel)

    # 去重（同一条消息在多处出现只翻译一次），但保留首次出现位置
    seen, uniq = set(), []
    for r in rows:
        if r["text"] in seen:
            continue
        seen.add(r["text"])
        uniq.append(r)

    by_kind = {}
    for r in uniq:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1

    if a.json:
        json.dump(uniq, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("已写出 %s（去重后 %d 条）" % (a.json, len(uniq)))

    if a.slices:
        need = [r for r in uniq if r["kind"] != "dynamic-cjk"]
        try:                                    # 已经有译文的条目不用再翻一遍
            sys.path.insert(0, ROOT)
            from src.i18n_catalog import CATALOG
            known = set(z for z, _e in CATALOG)
            before = len(need)
            need = [r for r in need if r["text"] not in known]
            print("  已有译文 %d 条，待翻译 %d 条" % (before - len(need), len(need)))
        except Exception as e:
            print("  （读不到现有目录，按全新翻译：%r）" % (e,))
        n = a.slices
        per = (len(need) + n - 1) // n
        for i in range(n):
            chunk = need[i * per:(i + 1) * per]
            if not chunk:
                continue
            p = os.path.join(ROOT, "logs", "i18n_slice_%d.json" % (i + 1))
            json.dump(chunk, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            print("  slice %d -> %s (%d 条)" % (i + 1, p, len(chunk)))

    print("去重后用户可见中文行 = %d" % len(uniq))
    for k in sorted(by_kind):
        print("   %-12s %d" % (k, by_kind[k]))

    if a.audit:
        sys.path.insert(0, ROOT)
        from src import i18n
        miss = [r for r in uniq if not i18n.has_entry(r["text"])]
        print("\n=== 英文目录覆盖审计 ===")
        if not miss:
            print("OK：%d 条全部覆盖" % len(uniq))
            return 0
        print("缺 %d 条：" % len(miss))
        for m in miss[:100]:
            print("  %-28s:%-5s %s" % (m["file"], m["line"], m["text"][:88]))
        return 1

    for r in uniq[:50]:
        print("%-26s:%-5s %-9s %s" % (r["file"], r["line"], r["kind"], r["text"][:90]))
    return 0


if __name__ == "__main__":
    try:                      # 控制台是 GBK 时别把清单打印搞崩（有 ✗ ✓ 这类字符）
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    sys.exit(main())
