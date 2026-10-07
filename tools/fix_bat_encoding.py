# -*- coding: utf-8 -*-
"""把 launcher-local.bat 规范化为 cmd.exe 能可靠解析的形式。

教训：cmd.exe 按系统 ANSI 代码页（本机 GBK）解析 .bat。
UTF-8 无 BOM 的中文注释会被逐字节误读成命令，导致满屏
"不是内部或外部命令"。因此批处理必须：
  1) 纯 ASCII（或带正确的 GBK 编码）；
  2) CRLF 行尾；
  3) 不含 BOM。
本脚本把源文件转成满足这三条的形式。
"""
import os
import sys

SRC = r"E:\code\luxun-qq\_runtime\napcat\napcat\launcher-local.bat"


def main() -> int:
    if not os.path.exists(SRC):
        print(f"源文件不存在：{SRC}")
        return 1

    raw = open(SRC, "rb").read()

    # 去掉可能存在的 BOM
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        print("已移除 UTF-8 BOM")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        print(f"源文件不是 UTF-8：{e}")
        return 1

    # 检查是否含非 ASCII
    non_ascii = sorted({ch for ch in text if ord(ch) > 127})
    if non_ascii:
        print(f"发现非 ASCII 字符 {len(non_ascii)} 种：{''.join(non_ascii[:40])}")
        print("→ 批处理不支持，请改用纯英文。中止。")
        return 1

    # 统一成 CRLF
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")

    with open(SRC, "wb") as f:
        f.write(text.encode("ascii"))

    check = open(SRC, "rb").read()
    print(f"已规范化：{len(check)} 字节")
    print(f"  纯 ASCII : {all(b < 128 for b in check)}")
    print(f"  CRLF 行尾: {check.count(b'\\r\\n')} 行")
    print(f"  无 BOM   : {not check.startswith(b'\\xef\\xbb\\xbf')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
