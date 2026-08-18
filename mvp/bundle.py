#!/usr/bin/env python3
"""把 mvp/redzone/ 打包成三种形态，覆盖三条可能的 Windows→红区 通道。

| 产物 | 用哪条通道 |
|---|---|
| `dist/ewave_mvp.tar.gz` + `.sha256` | **首选** —— 与 SNP_RLC_Extractor 的交付形状一致，走你已有的那条上传通道 |
| `mvp_pack.sh` | 只能粘文本时。698 行自解包脚本，quoted heredoc 逐字节保真 |
| `dist/ewave_mvp.tar.gz.b64` | 粘贴通道会改行尾/吃字符时。base64 无元字符、无编码风险，体积还只有 sh 版的 1/4 |

三者内容完全相同。为什么不建 GitHub：`cfg.sh` 里是工号/内网路径/项目代号/端口名，
CLAUDE.md 硬约束 #1 —— 本地 git 可以，remote 不行。

用法（Windows 本机）：
    python mvp/bundle.py

依赖：stdlib only（红区无装包权限，本项目全程如此）。
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "redzone"
OUT = HERE / "mvp_pack.sh"
DIST = HERE / "dist"
ROOT = "ewave_mvp_scripts"  # tar 解开后的目录名
FIXED_MTIME = 1704067200  # 2024-01-01T00:00:00Z，固定但合理（0 会让 GNU tar 报警告）

# 顺序有意义：cfg 先落地，README 最后（跑完好读）
FILES = [
    "cfg.sh",
    "site.example.sh",
    "run.sh",
    "gdsout_setup.tmpl",
    "step0_probe.sh",
    "step1_strmout.sh",
    "step2_memestimate.sh",
    "step3_runs.sh",
    "step4_verify.sh",
    "README.txt",
]

HEAD = """#!/bin/sh
# Ewave_helper MVP 自解包脚本 —— 由 mvp/bundle.py 生成，勿手工编辑。
# ⚠️ 红区资料（含工号/项目代号/内网路径），勿外传。
#
#   sh mvp_pack.sh [目标目录]      默认解到 ./ewave_mvp_scripts
#
# 解完先读 README.txt，再核 cfg.sh，然后逐步跑 step0..step4。
set -e
DEST="${1:-./ewave_mvp_scripts}"
mkdir -p "$DEST"
cd "$DEST"
echo "解包到: `pwd`"
"""

TAIL = """
chmod +x *.sh 2>/dev/null || true
echo
echo "解包完成。文件："
ls -la
echo
echo "下一步："
echo "  1) 读 README.txt"
echo "  2) 核对 cfg.sh 里的红区坐标"
echo "  3) sh step0_probe.sh 2>&1 | tee step0.out"
"""


def read_lf(name: str) -> bytes:
    """读文件并强制 LF —— CRLF 会让红区的 sh 报 "invalid option name" 之类的鬼话。"""
    text = (SRC / name).read_text(encoding="utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def build_tarball() -> Path:
    """tar.gz + .sha256，形状对齐 SNP_RLC_Extractor/deploy 的交付。

    mtime 钉成一个**固定且合理**的值：内容没变时 sha256 也不变（好核对），
    同时避开 GNU tar 对 1970 时间戳的 "implausibly old time stamp" 警告
    —— 那个警告无害但会把真正的错误淹掉。
    """
    DIST.mkdir(exist_ok=True)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name in FILES:
            data = read_lf(name)
            info = tarfile.TarInfo(f"{ROOT}/{name}")
            info.size = len(data)
            info.mtime = FIXED_MTIME
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    gz = gzip.compress(raw.getvalue(), compresslevel=9, mtime=FIXED_MTIME)

    tgz = DIST / "ewave_mvp.tar.gz"
    tgz.write_bytes(gz)
    digest = hashlib.sha256(gz).hexdigest()
    # GNU `sha256sum -c` 格式，LF，无 BOM
    (DIST / "ewave_mvp.tar.gz.sha256").write_bytes(
        f"{digest}  ewave_mvp.tar.gz\n".encode()
    )
    b64 = base64.b64encode(gz).decode()
    wrapped = "\n".join(b64[i : i + 76] for i in range(0, len(b64), 76))
    (DIST / "ewave_mvp.tar.gz.b64").write_bytes((wrapped + "\n").encode())

    print(f"写出 {tgz}  ({len(gz)} 字节)  sha256={digest[:16]}…")
    print(f"写出 {DIST / 'ewave_mvp.tar.gz.sha256'}")
    print(f"写出 {DIST / 'ewave_mvp.tar.gz.b64'}  ({len(wrapped)} 字节, 粘贴退路)")
    return tgz


def main() -> int:
    missing = [n for n in FILES if not (SRC / n).is_file()]
    if missing:
        print(f"缺文件: {missing}", file=sys.stderr)
        return 1

    build_tarball()

    parts = [HEAD]
    for name in FILES:
        text = (SRC / name).read_text(encoding="utf-8")
        # 统一 LF —— CRLF 会让红区的 sh 报 "invalid option name" 之类的鬼话
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        delim = "__EWAVE_MVP_EOF__"
        if delim in text:
            print(f"{name}: 内容撞上了 heredoc 定界符", file=sys.stderr)
            return 1
        # quoted delimiter ⇒ heredoc 内不做任何展开，逐字节保真
        parts.append(f"cat > '{name}' <<'{delim}'\n{text}")
        if not text.endswith("\n"):
            parts.append("\n")
        parts.append(f"{delim}\n")
    parts.append(TAIL)

    OUT.write_text("".join(parts), encoding="utf-8", newline="\n")
    n = sum(1 for _ in OUT.read_text(encoding="utf-8").splitlines())
    print(f"写出 {OUT}  ({n} 行, {OUT.stat().st_size} 字节, LF)")
    print()
    print("红区侧三选一：")
    print("  A 有文件通道（推荐，同 Snp_analyzer 的走法）")
    print("      sha256sum -c ewave_mvp.tar.gz.sha256 && tar xzf ewave_mvp.tar.gz")
    print("  B 只能粘文本")
    print("      粘 mvp_pack.sh 存盘 → sh mvp_pack.sh")
    print("  C 粘贴通道会改行尾/吃字符")
    print("      粘 ewave_mvp.tar.gz.b64 存盘 →")
    print("      tr -d '\\r' < ewave_mvp.tar.gz.b64 | base64 -d | tar xzf -")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
