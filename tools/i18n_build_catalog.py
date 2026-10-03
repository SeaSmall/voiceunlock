"""i18n_build_catalog.py -- 把分片翻译（logs/i18n_en_*.py）合并成 src/i18n_catalog.py

流程（可复跑）：
    tools/i18n_scan.py --json logs/i18n_scan.json --slices 5      # 出切片
    （人工/子代理逐片翻译成 logs/i18n_en_N.py）
    tools/i18n_build_catalog.py                                   # 合并 + 自检
    tools/i18n_audit.py                                           # 五项审计

为什么要有合并这一步而不是直接手写 src/i18n_catalog.py：
    切片是**由扫描器生成的**（每条都对应源码里真实存在的一行），合并时再逐条
    校验"中文键没被翻译改过、英文纯 ASCII、占位符个数对齐"——翻译是人/模型
    干的活，这一步是把关。手抄一遍既费事又会引入偏差。
"""
from __future__ import annotations

import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = re.compile(r"%[-+ #0]*[\d*]*(?:\.\d+)?[hlL]?[diouxXeEfFgGcrsa%]")

HEADER = '''# -*- coding: utf-8 -*-
"""i18n_catalog.py -- 中文 → 英文 消息目录（**由工具生成，但可手工维护**）

    生成： tools/i18n_scan.py --json logs/i18n_scan.json --slices N
           （逐片翻译 logs/i18n_en_N.py）
           tools/i18n_build_catalog.py
    校验： tools/i18n_audit.py        ← 五项审计，全过才返回 0

约定（违反任何一条 tools/i18n_audit.py 都会报）：
    * 键 = 源码里的中文消息**按行**原文（含缩进/对齐空格，逐字节相同）。
    * 值 = 英文，**必须纯 ASCII**（英文 Windows 控制台是 cp437，非 ASCII 即乱码）。
      符号替代：✓→[OK]、✗→[FAIL]、★→*、→→->、—→--、·→-、…→...
    * 键里带 printf 占位符（%s/%d/%.2f）的行，值里的占位符**个数相同且一律写 %s**：
      运行时是按"已格式化输出"反查模板的，捕获到的是字符串。
      整行匹配不上时还有一次"片段替换"兜底（argparse 的 --help 加了对齐空格）。
    * 占位符行不要手工改宽度（%-28s）：宽度只影响中文侧的渲染，英文侧拿到的
      已经是补好空格的字符串。
"""
from __future__ import annotations

from typing import List, Tuple

#: (中文原文, 英文) —— 顺序不重要，匹配时先整行精确、再模板、最后片段
CATALOG: List[Tuple[str, str]] = [
'''

# 动态拼接、扫描器抓不到原文的两条（agent.clear-password / enroll.remove）
EXTRA = [
    ("已删除", "Deleted"),
    ("本来就没有", "Nothing to delete"),
    ("没有这条档案", "No such profile"),
]


def main() -> int:
    files = sorted(glob.glob(os.path.join(ROOT, "logs", "i18n_en_*.py")),
                   key=lambda p: int(re.search(r"_(\d+)\.py$", p).group(1)))
    if not files:
        print("没有找到 logs/i18n_en_*.py")
        return 1

    sys.path.insert(0, os.path.join(ROOT, "logs"))
    rows, bad = [], []
    for p in files:
        mod = __import__(os.path.basename(p)[:-3])
        for zh, en in mod.ENTRIES:
            if not re.search(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]", zh):
                bad.append("键里没有中文（八成把英文填进键了）：%r" % (zh,))
            if zh == en:
                bad.append("键值相同（没翻）：%r" % (zh,))
            if any(ord(c) > 127 for c in en):
                bad.append("非 ASCII：%r -> %r" % (zh, en))
            n_zh = len([m for m in SPEC.finditer(zh) if m.group(0) != "%%"])
            n_en = len([m for m in SPEC.finditer(en) if m.group(0) != "%%"])
            if n_zh != n_en:
                bad.append("占位符不齐（%d vs %d）：%r -> %r" % (n_zh, n_en, zh, en))
            for m in SPEC.finditer(en):
                if m.group(0) not in ("%s", "%%"):
                    bad.append("英文占位符非 %%s：%r -> %r" % (zh, en))
            rows.append((zh, en))

    # 与扫描结果对齐：每一条"用户可见中文行"都必须被覆盖
    import json
    scan = os.path.join(ROOT, "logs", "i18n_scan.json")
    if os.path.isfile(scan):
        have = set(z for z, _ in rows) | set(z for z, _ in EXTRA)
        miss = [r for r in json.load(open(scan, encoding="utf-8"))
                if r["kind"] != "dynamic-cjk" and r["text"] not in have]
        for m in miss:
            bad.append("扫描到但目录里没有：%s:%s %r"
                       % (m["file"], m["line"], m["text"][:70]))
    else:
        print("警告：没有 logs/i18n_scan.json，跳过覆盖对齐检查")

    if bad:
        print("合并前自检不通过，%d 个问题：" % len(bad))
        for b in bad[:40]:
            print("  * " + b)
        return 1

    seen, final = set(), []
    for zh, en in rows + EXTRA:
        if zh in seen:
            continue
        seen.add(zh)
        final.append((zh, en))

    out = os.path.join(ROOT, "src", "i18n_catalog.py")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(HEADER)
        for zh, en in final:
            f.write("    (%r, %r),\n" % (zh, en))
        f.write("]\n")
    print("已写出 %s：%d 条（来自 %d 个分片）" % (out, len(final), len(files)))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    sys.exit(main())
