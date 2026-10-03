"""fix_unattend.py -- 修掉无人值守应答文件里 Windows 10 已废弃的元素，并重建 ISO

背景（实测）：
    子代理写的 Autounattend.xml 在 windowsPE / specialize 两段都是好的
    （分区、镜像选择、启用 Administrator、清空密码、关 UAC 都成功执行了），
    但 **oobeSystem 段被判非法**，setup 弹出：
        "Windows could not parse or process unattend answerfile
         [C:\\Windows\\Panther\\unattend.xml] for pass [oobeSystem]"
    于是整个安装卡在那个对话框上等点击 —— 磁盘不再增长，看起来像"还在装"。

    被删掉的四个都是 Win10 上已废弃 / 容易判非法的写法：
      SkipMachineOOBE / SkipUserOOBE   —— Win10 已废弃，新版 setup 直接判非法
      NetworkLocation                  —— 已移除的设置
      AutoLogon（含空的 <Value>）      —— 空密码的 AutoLogon 常被判非法；
                                          而且 specialize 段已经用注册表
                                          (AutoAdminLogon/DefaultUserName/DefaultPassword)
                                          设好了自动登录，这一块本来就是多余的

用法：
    python tools/fix_unattend.py --in <VM目录>\\Win10Auto\\Autounattend_orig.xml \
                                 --out <VM目录>\\Win10Auto\\autounattend.iso
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import pycdlib


def patch(xml: str):
    removed = []
    for pat, name in (
        (r"[ \t]*<SkipMachineOOBE>.*?</SkipMachineOOBE>\s*\n", "SkipMachineOOBE"),
        (r"[ \t]*<SkipUserOOBE>.*?</SkipUserOOBE>\s*\n", "SkipUserOOBE"),
        (r"[ \t]*<NetworkLocation>.*?</NetworkLocation>\s*\n", "NetworkLocation"),
        (r"[ \t]*<AutoLogon>.*?</AutoLogon>\s*\n", "AutoLogon"),
    ):
        xml, n = re.subn(pat, "", xml, flags=re.S)
        if n:
            removed.append("%s ×%d" % (name, n))
    return xml, removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True, help="原始 Autounattend.xml")
    ap.add_argument("--out", dest="dst", required=True, help="要写出的 ISO 路径")
    ap.add_argument("--xml-out", default="", help="顺便把修好的 XML 也存一份")
    ap.add_argument("--inspect", default="", help="只检查这个 ISO 的结构，不写文件")
    a = ap.parse_args()

    if a.inspect:
        iso = pycdlib.PyCdlib()
        iso.open(a.inspect)
        print("=== 原 ISO 结构 ===")
        print("  PVD 卷标:", iso.pvd.volume_identifier.decode(errors="replace").strip())
        for name, gen in (("iso9660", iso.get_iso9660_facility),
                          ("joliet", iso.get_joliet_facility),
                          ("rock_ridge", iso.get_rock_ridge_facility),
                          ("udf", iso.get_udf_facility)):
            try:
                print("  %-12s = %r" % (name, gen))
            except Exception as e:
                print("  %-12s = <不可用 %s>" % (name, e))
        print("=== 根目录 ===")
        for child in iso.list_children(iso_path="/"):
            if child is None or child.is_dir():
                continue
            print("   ", child.file_identifier().decode(errors="replace"))
        iso.close()

    with open(a.src, "r", encoding="utf-8-sig") as f:
        xml = f.read()
    fixed, removed = patch(xml)
    print("=== 已删除的废弃元素 ===")
    print("  " + (", ".join(removed) if removed else "（没有匹配到，检查一下原文）"))
    if a.xml_out:
        with open(a.xml_out, "w", encoding="utf-8") as f:
            f.write(fixed)
        print("  修好的 XML 已存:", a.xml_out)

    if not a.dst:
        return 0

    if os.path.exists(a.dst):
        os.remove(a.dst)
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, vol_ident="ANSWER")
    # ISO9660 层面用大写+;1，Joliet 层面保留正确大小写 —— Windows setup 走 Joliet 名字
    iso.add_file(a.src if not a.xml_out else a.xml_out,
                 iso_path="/AUTOUNATTEND.XML;1",
                 joliet_path="/Autounattend.xml")
    iso.write(a.dst)
    iso.close()
    print("=== 新 ISO ===")
    print("  %s  (%d 字节)" % (a.dst, os.path.getsize(a.dst)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
