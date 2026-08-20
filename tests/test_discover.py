"""`core.discover`：从官方 run 目录**运行时解析**站点坐标。

## 这份测试的先天矛盾，和它的解法

`discover.py` 的全部意义是 CLAUDE.md 硬约束 1b：坐标不手抄、现场解析。
于是它的测试卡在一个矛盾上 —— 要验解析对不对就得有输入，而真实输入
（`references/probes/`、`references/ewave_donau_kit/`）是**红区资料，永不进 git**。

解法：**形状进 git，值不进 git**。`tests/fixtures/offdir_synthetic/` 是一份合成的假官方
run 目录，格式逐项照真实文件（tab 分隔、value 带引号、裸 flag、`--x=y` 与 `-e 0.4` 混用、
末尾剥色管道、`dsub -A/-q/-R` 三元组），**值全是明显的占位符**（`MY_LIB` / `/fake/pdk/…`）。
那个目录的 `README.md` 写了每个假值是什么、为什么 ptxt 路径是个陷阱。

于是本文件里的期望值全是**手写字面量**（= 我写进 fixture 的那些假值），
不许拿被测函数自己解析一遍当期望值（防自证配方 2）。

另有两条**交叉验证**，本机有红区 fixture 时才跑，缺文件优雅 skip：
`ProductionCrossCheck` 拿真实生产脚本喂 `discover_site_facts`，
期望值取自 `tests/fixtures/production_cmd.local.json`（人从真实命令抽出来的那份）。

## 防自证四配方在这里落到哪

1. 关键测试 = 断言「解析结果 == 期望值」的那些（本文件里几乎全是）。
2. 期望值只有手写字面量 / 红区 fixture 两种来源。
3. 每条关键测试配一条 `_negative`：**同一条输入构造路径**，故意改坏一个值，断言被抓到。
4. 有过滤器的地方（ptxt 换 corner 的字符串替换、`learn_default_flags` 的剔除规则）
   单独测过滤器本身**没误伤**，外加计数断言（字段数 / 端口数 / flag 数）——
   空集合的 diff 永远是绿的。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ewave_batch.core import cmd, discover
from ewave_batch.model import DiscoveryError, MECHANISM_FLAGS, PortMode

ROOT = Path(__file__).resolve().parents[1]
OFFDIR = ROOT / "tests" / "fixtures" / "offdir_synthetic"
SETUP_PATH = OFFDIR / "gdsout_setup"
RUNSH_PATH = OFFDIR / "run_ewave_typical_-40_0.sh"
REMOTE_PATH = OFFDIR / "remote_run_ewave.sh"

# --------------------------------------------------------------------------
# ★ 期望值：全部是**手写字面量**，就是我写进 tests/fixtures/offdir_synthetic/ 的那些假值。
#   改 fixture 就要改这里，两边一起改 —— 这个摩擦是有意的。
# --------------------------------------------------------------------------

EXP_LIBRARY = "MY_LIB"
EXP_TOPCELL = "MY_CELL"
EXP_VIEW = "layout_em"
EXP_LAYERMAP = "/fake/pdk/tech/FAKEPDK.layermap"
EXP_RUNDIR = "/fake/wa/ewave_simulation/MY_LIB_MY_CELL_layout_em"
EXP_STRMFILE = "MY_CELL.gds"
EXP_LOGFILE = "/fake/wa/ewave_simulation/MY_LIB_MY_CELL_layout_em/gds_out.log"

EXP_PTXT = (
    "/fake/pdk/apps/ewave/ewaveinterface/process/typical/typical_v2/ptxt_enc/"
    "FAKEPDK_atypical_typical_V1.0_encrypted_package.ptxt"
)
EXP_PTXT_DIR = "/fake/pdk/apps/ewave/ewaveinterface/process/typical/typical_v2/ptxt_enc"
EXP_PTXT_NAME_TEMPLATE = "FAKEPDK_atypical_{corner}_V1.0_encrypted_package.ptxt"
EXP_PDK_ROOT = "/fake/pdk"
EXP_KEY = "000000"
EXP_CORNER = "typical"
EXP_TEMPERATURE = "-40.0"
EXP_EWAVE_DIR = "typical_-40_0"

EXP_PORT_MAPPING = (
    ("P000", "MY_GND"),
    ("P001", "MY_INN"),
    ("P002", "MY_INP"),
    ("P003", "my_bias"),
    ("P004", "my_tune"),
)
EXP_SIGNAL_PORTS = ("P001", "P002", "P003", "P004")

EXP_DSUB_ACCOUNT = "fake_account"
EXP_DSUB_QUEUE = "fake_queue"
EXP_DSUB_RESOURCES = "cpu=20;mem=100000"

# 计数断言（配方 4）—— 数字是**从 fixture 里数出来的**，写死在这里。
# 空集合的 diff 永远好看，这几条专防那个。
EXP_GDSOUT_FIELD_COUNT = 24
EXP_PORT_COUNT = 5
EXP_SIGNAL_COUNT = 4
EXP_OFFICIAL_FLAG_COUNT = 21   # 2026-08-19：合成基准跟着新默认去掉了 --equalCurrent
EXP_PRODUCTION_FLAG_COUNT = 15  # 21 - 6 个站点身份项（见 _SITE_IDENTITY_FLAGS）
EXP_LEARNED_FLAG_COUNT = 3

# 学出来的默认表 —— 正好是 BRIEF §11「默认表」那一层：影响结果、基本不动、不进目录名。
EXP_LEARNED_DEFAULTS = {
    "--labelDepth": "0",
    "--viaMode": "1",
    "--sparamImpedance": "50",
}

# `gdsout_setup` 里随 design 变的 7 个字段所在的**行号**（0 基，照 fixture 的顺序）。
# 手写而不是现算：templatize 只许改这 7 行，行号本身就是被断言的东西。
VARYING_LINE_INDICES = (0, 1, 2, 3, 4, 12, 14)
VARYING_LINE_KEYS = ("runDir", "library", "topCell", "view", "strmFile", "logFile", "layerMap")
MAXVERTICES_LINE_INDEX = 6  # `maxVertices 200` —— 反向测试拿它当"被改坏的那一行"

EXP_PLACEHOLDERS = {
    "@@RUNDIR@@",
    "@@LIBRARY@@",
    "@@TOPCELL@@",
    "@@VIEW@@",
    "@@STRMFILE@@",
    "@@LOGFILE@@",
    "@@LAYERMAP@@",
}

# --- 红区 fixture（不进 git，缺了就 skip）-----------------------------------
PRODUCTION_JSON = ROOT / "tests" / "fixtures" / "production_cmd.local.json"
KIT_RUNSH = ROOT / "references" / "ewave_donau_kit" / "ewave" / "run_examples" / "run_ewave_typical_125_0.sh"
KIT_REMOTE = ROOT / "references" / "ewave_donau_kit" / "ewave" / "run_examples" / "remote_run_ewave.sh"

PRODUCTION_SKIP = (
    "本机没有 tests/fixtures/production_cmd.local.json 或 references/ 下的真实官方脚本 —— "
    "它们是人从红区取回的证据，含站点坐标所以永不进 git（公开克隆者看到这条 skip 是正常的）。"
    "合成 fixture 那几条测试不受影响，解析逻辑照样被验。"
)
HAVE_PRODUCTION = PRODUCTION_JSON.exists() and KIT_RUNSH.exists()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def changed_line_indices(left: str, right: str) -> list[int]:
    """两段文本逐行比，返回不同的行号。行数不同直接抛 —— 行数变了本身就是失败。

    正反两向共用这一条比较路径（配方 3），反向那条靠它证明"逐字保留"的检查是有牙的。
    """
    left_lines = left.splitlines(keepends=True)
    right_lines = right.splitlines(keepends=True)
    if len(left_lines) != len(right_lines):
        raise AssertionError(
            f"行数都不一样了（{len(left_lines)} vs {len(right_lines)}）—— "
            "templatize 只许改值，不许增删行"
        )
    return [i for i, (a, b) in enumerate(zip(left_lines, right_lines)) if a != b]


# ==========================================================================
# gdsout_setup 解析
# ==========================================================================


class GdsoutSetupParsing(unittest.TestCase):
    """`parse_gdsout_setup`：tab 分隔、value 带引号、裸 flag。"""

    def fields(self, text: str | None = None) -> dict[str, str]:
        """正反两向共用的唯一输入路径。"""
        return discover.parse_gdsout_setup(read_text(SETUP_PATH) if text is None else text)

    def test_field_count(self) -> None:
        """计数断言（配方 4）：24 个字段，一个不多一个不少。

        少认一个字段 = 渲染出来的 gdsout_setup 少一行 = GDS 内容变了，而且跑得出来。
        """
        self.assertEqual(len(self.fields()), EXP_GDSOUT_FIELD_COUNT)

    def test_varying_fields(self) -> None:
        """7 个随 design 变的字段（D1c）。期望值是手写字面量。"""
        fields = self.fields()
        self.assertEqual(fields["runDir"], EXP_RUNDIR)
        self.assertEqual(fields["library"], EXP_LIBRARY)
        self.assertEqual(fields["topCell"], EXP_TOPCELL)
        self.assertEqual(fields["view"], EXP_VIEW)
        self.assertEqual(fields["strmFile"], EXP_STRMFILE)
        self.assertEqual(fields["logFile"], EXP_LOGFILE)
        self.assertEqual(fields["layerMap"], EXP_LAYERMAP)

    def test_verbatim_fields(self) -> None:
        """必须逐字复现的那些（D1c）—— 它们是 eWave/strmout 的**工具语义**，不是站点身份，
        所以可以写进源码（CLAUDE.md 硬约束 1b）。任一个错了 GDS 内容就变，
        而且跑得出来、数字也像。"""
        fields = self.fields()
        self.assertEqual(fields["hierDepth"], "32")
        self.assertEqual(fields["maxVertices"], "200")
        self.assertEqual(fields["strmVersion"], "5")
        self.assertEqual(fields["case"], "preserve")
        self.assertEqual(fields["convertDot"], "ignore")
        self.assertEqual(fields["convertPin"], "geometry")
        self.assertEqual(fields["pinAttNum"], "1")

    def test_bare_flag_and_empty_values(self) -> None:
        """`arrayInstToScalar` 是**没有值**的裸 flag（开启即生效）→ 空串，但键必须在。
        `""` 这种空值也一样：键在、值是空串。"""
        fields = self.fields()
        self.assertIn("arrayInstToScalar", fields)
        self.assertEqual(fields["arrayInstToScalar"], "")
        for empty in ("refLibList", "cellMap", "fontMap", "propMap", "userSkillFile",
                      "viaMap", "summaryFile", "techLib", "objectMap"):
            self.assertEqual(fields[empty], "", f"{empty} 应当是空串")

    def test_varying_fields_negative(self) -> None:
        """反向：把 fixture 文本里 `maxVertices 200` 改成 500 —— 必须**且只**报这一处。

        同一条输入路径（`self.fields`），只改一个值。若解析器其实没在看行内容
        （比如按行号取值），这条会绿得很好看，所以要断言"差异恰好是那一个键"。
        """
        text = read_text(SETUP_PATH)
        tampered = text.replace("maxVertices\t\t200", "maxVertices\t\t500")
        self.assertNotEqual(tampered, text, "fixture 里没有 maxVertices 200 —— 这条反向测试会空过")
        before, after = self.fields(), self.fields(tampered)
        differing = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        self.assertEqual(differing, {"maxVertices"})
        self.assertEqual(after["maxVertices"], "500")
        self.assertEqual(len(after), EXP_GDSOUT_FIELD_COUNT)

    def test_comments_and_blank_lines_are_skipped(self) -> None:
        """`#` 开头的行和空行不算字段。期望值手写。"""
        text = "# a comment\n\n\tlibrary\t\t\"X\"\n#\ttopCell\t\t\"Y\"\n"
        self.assertEqual(discover.parse_gdsout_setup(text), {"library": "X"})

    def test_quote_stripping_is_paired_only(self) -> None:
        """只剥**成对**的首尾引号。单边引号原样留着 —— 宁可留着也别切错。"""
        text = "\ta\t\"quoted\"\n\tb\t'single'\n\tc\tbare\n\td\t\"unbalanced\n\te\t\"\"\n"
        self.assertEqual(
            discover.parse_gdsout_setup(text),
            {"a": "quoted", "b": "single", "c": "bare", "d": '"unbalanced', "e": ""},
        )

    def test_first_key_wins(self) -> None:
        """同名 key 取第一条（照 cfg.sh 的 awk `print; exit`）—— 别静默换成最后一条。"""
        text = "\tlibrary\t\"first\"\n\tlibrary\t\"second\"\n"
        self.assertEqual(discover.parse_gdsout_setup(text)["library"], "first")

    def test_spaces_instead_of_tabs(self) -> None:
        """真实文件里 `propMap     ""` 用的是空格不是 tab，两种都要认。"""
        self.assertEqual(discover.parse_gdsout_setup('\tpropMap     ""\n'), {"propMap": ""})


# ==========================================================================
# gdsout_setup 模板化 —— D1c 的要害在「其余逐字保留」
# ==========================================================================


class GdsoutTemplatize(unittest.TestCase):
    """`templatize_gdsout_setup`：只换 7 个字段，其余**逐字节**不动。"""

    def source(self) -> str:
        return read_text(SETUP_PATH)

    def test_exactly_seven_lines_change(self) -> None:
        """★ 正向 + 计数：模板与原文逐行比，**只有那 7 行**不同。

        这条同时是"其余逐字保留"的机器判据 —— 剩下 17 行必须一个字符都没变
        （`convertPin "geometry"` / `case "preserve"` / `maxVertices 200` 全在里面）。
        """
        text = self.source()
        changed = changed_line_indices(discover.templatize_gdsout_setup(text), text)
        self.assertEqual(changed, list(VARYING_LINE_INDICES))
        self.assertEqual(len(changed), len(VARYING_LINE_KEYS))

    def test_changed_lines_are_the_seven_varying_keys(self) -> None:
        """被改的那 7 行，key 必须正好是那 7 个（防"改对了行数、改错了行"）。"""
        lines = self.source().splitlines()
        keys = [lines[i].strip().split()[0] for i in VARYING_LINE_INDICES]
        self.assertEqual(keys, list(VARYING_LINE_KEYS))

    def test_placeholders(self) -> None:
        """7 个 `@@…@@` 占位符齐全，且**站点值一个都没留下**。"""
        template = discover.templatize_gdsout_setup(self.source())
        self.assertEqual(set(re.findall(r"@@[A-Z_]+@@", template)), EXP_PLACEHOLDERS)
        for value in (EXP_RUNDIR, EXP_LIBRARY, EXP_TOPCELL, EXP_VIEW,
                      EXP_STRMFILE, EXP_LOGFILE, EXP_LAYERMAP):
            self.assertNotIn(value, template, f"模板里还留着 {value!r} —— 那是站点坐标")

    def test_quotes_and_indent_are_preserved(self) -> None:
        """占位符要留在**原来的引号里面**、缩进和 key/value 之间的空白照抄。"""
        template = discover.templatize_gdsout_setup(self.source())
        self.assertIn('\trunDir\t\t\t"@@RUNDIR@@"', template)
        self.assertIn('\tlayerMap\t\t"@@LAYERMAP@@"', template)

    def test_crlf_line_endings_survive(self) -> None:
        """行尾照抄。红区文件是 LF，但万一有人在 Windows 上碰过，别把它悄悄改掉。"""
        text = '\tlibrary\t\t"X"\r\n\tmaxVertices\t\t200\r\n'
        out = discover.templatize_gdsout_setup(text)
        self.assertEqual(out, '\tlibrary\t\t"@@LIBRARY@@"\r\n\tmaxVertices\t\t200\r\n')

    def test_exactly_seven_lines_change_negative(self) -> None:
        """反向：把输入的 `maxVertices 200` 改坏成 500，再和模板逐行比 ——
        比较逻辑必须**多报出**第 6 行。

        这条证明正向那条不是空过：如果哪天 templatize 顺手动了某个非占位字段，
        正向的 `changed == 那 7 行` 一定会红。正反两向走同一个 `changed_line_indices`。
        """
        text = self.source()
        tampered = text.replace("maxVertices\t\t200", "maxVertices\t\t500")
        self.assertNotEqual(tampered, text, "fixture 里没有 maxVertices 200 —— 反向测试会空过")
        changed = changed_line_indices(discover.templatize_gdsout_setup(text), tampered)
        self.assertEqual(changed, sorted(VARYING_LINE_INDICES + (MAXVERTICES_LINE_INDEX,)))


class TemplatePlaceholderContract(unittest.TestCase):
    """跨模块契约：`discover` 造模板，`tools.strmout` 填模板 —— 占位符名字必须对得上。

    两个模块是 P2 里**并行**写的，占位符名各写一份 → 天然有漂移风险。
    对不上的后果是 `render_gdsout_setup` 抛 `SpecError`（它会检查残留占位符），
    但那要等到运行时；在这里当场抓住便宜得多。
    """

    def strmout(self):
        # 这里**故意不 try/except**：P2 两个模块都已落地，兜底的 skipTest 语义已经过期。
        # 留着的代价是「谁把 tools.strmout 改出 ImportError，这条跨模块交叉校验就静默变
        # skip 而不是变红」—— 而它正是防「两边各写各的 token、各自单测全绿、接起来才炸」
        # 的唯一一道机器判据。宁可让 ImportError 直接把测试炸红。
        from ewave_batch.tools import strmout

        return strmout

    def test_placeholder_names_agree(self) -> None:
        strmout = self.strmout()
        tokens = set(re.findall(r"@@[A-Z_]+@@", str(strmout.GDSOUT_PLACEHOLDERS)))
        # 原来这里是 `if not tokens: self.skipTest(...)` —— 形状一变就**静默跳过**。
        # 但"两边 token 是不是同一套"正是这条测试唯一要答的问题：跳过它等于把答案抹掉。
        # token 集合为空只有两种可能：形状真变了（那要有人来改这条测试），
        # 或者取 token 的正则失效了（那更该红）。两种都不该无声。
        self.assertTrue(
            tokens,
            "GDSOUT_PLACEHOLDERS 里一个 @@…@@ token 都没取到 —— "
            "要么占位符形状变了、要么这条测试的正则失效了，两种都得有人看，不许静默跳过",
        )
        self.assertEqual(tokens, EXP_PLACEHOLDERS)

    def test_round_trip(self) -> None:
        """★ 端到端：官方 setup → `templatize` → `render` 回同样的 7 个值 → **逐字节回到原文**。

        填回去的 7 个值是**手写字面量**（就是 fixture 里那些假值），不是从解析结果里取的。
        """
        from ewave_batch.model import GdsoutFields

        strmout = self.strmout()
        original = read_text(SETUP_PATH)
        rendered = strmout.render_gdsout_setup(
            discover.templatize_gdsout_setup(original),
            GdsoutFields(
                run_dir=EXP_RUNDIR,
                library=EXP_LIBRARY,
                top_cell=EXP_TOPCELL,
                view=EXP_VIEW,
                strm_file=EXP_STRMFILE,
                log_file=EXP_LOGFILE,
                layer_map=EXP_LAYERMAP,
            ),
        )
        self.assertEqual(changed_line_indices(rendered, original), [])

    def test_round_trip_negative(self) -> None:
        """反向：填回去时把 `view` 换成别的值 —— 往返比较必须报出第 3 行（view 那行）。"""
        from ewave_batch.model import GdsoutFields

        strmout = self.strmout()
        original = read_text(SETUP_PATH)
        rendered = strmout.render_gdsout_setup(
            discover.templatize_gdsout_setup(original),
            GdsoutFields(
                run_dir=EXP_RUNDIR,
                library=EXP_LIBRARY,
                top_cell=EXP_TOPCELL,
                view="layout",  # ← 唯一改动。view 不是常量，见 model.Design.view 的注释
                strm_file=EXP_STRMFILE,
                log_file=EXP_LOGFILE,
                layer_map=EXP_LAYERMAP,
            ),
        )
        self.assertEqual(changed_line_indices(rendered, original), [VARYING_LINE_INDICES[3]])


# ==========================================================================
# remote_run_ewave.sh —— dsub 三元组
# ==========================================================================


class DsubOptions(unittest.TestCase):
    """`parse_dsub_options`：`-A` / `-q` / `-R`。"""

    def parse(self, text: str | None = None) -> dict[str, str]:
        return discover.parse_dsub_options(read_text(REMOTE_PATH) if text is None else text)

    def test_three_options(self) -> None:
        options = self.parse()
        self.assertEqual(options["account"], EXP_DSUB_ACCOUNT)
        self.assertEqual(options["queue"], EXP_DSUB_QUEUE)
        self.assertEqual(options["resources"], EXP_DSUB_RESOURCES)
        self.assertEqual(len(options), 3)  # 计数断言：正好三条，没多解析出别的

    def test_three_options_negative(self) -> None:
        """反向：把 `-q <queue>` 从脚本里删掉 —— 必须**少**这个键（而不是给个空串），
        且另外两个纹丝不动。

        缺的键就不给是有意的：调用方靠 `in` 判断"官方到底有没有指定队列"，
        空串会让"没写"和"写了个空的"看起来一样。
        """
        text = read_text(REMOTE_PATH)
        tampered = text.replace(f"-q {EXP_DSUB_QUEUE} ", "")
        self.assertNotEqual(tampered, text, f"脚本里没有 -q {EXP_DSUB_QUEUE} —— 反向测试会空过")
        options = self.parse(tampered)
        self.assertNotIn("queue", options)
        self.assertEqual(options["account"], EXP_DSUB_ACCOUNT)
        self.assertEqual(options["resources"], EXP_DSUB_RESOURCES)
        self.assertEqual(len(options), 2)

    def test_resources_keeps_semicolons(self) -> None:
        """`-R` 的值里有 `;`，必须整段取回来（切一半 = 内存申请少一位数）。"""
        self.assertIn(";", self.parse()["resources"])
        self.assertEqual(cmd.parse_resource_string(self.parse()["resources"])["cpu"], "20")

    def test_bare_resources_without_quotes(self) -> None:
        """`-R` 不带引号时也认。期望值手写。"""
        self.assertEqual(discover.parse_dsub_options("dsub -R cpu=4 -I x.sh"), {"resources": "cpu=4"})

    def test_missing_everything(self) -> None:
        self.assertEqual(discover.parse_dsub_options("dsub -I ./x.sh\n"), {})

    def test_commented_out_decoy_does_not_win(self) -> None:
        """过滤器测试：注释掉的旧参数不许赢过真正那行 `dsub`。

        官方脚本里常留着"上次用的是别的队列"这种注释；整文件 grep 会把它当真，
        然后我们拿一个作废的队列去提交 —— 作业排在那儿不动，而命令看起来完全正常。
        """
        text = (
            "#!/bin/sh\n"
            "# 上次用的是 -q old_queue -A old_account\n"
            'dsub -A real_account -q real_queue -R "cpu=8" -I ./x.sh\n'
        )
        self.assertEqual(
            discover.parse_dsub_options(text),
            {"account": "real_account", "queue": "real_queue", "resources": "cpu=8"},
        )

    def test_commented_out_decoy_does_not_win_negative(self) -> None:
        """反向：同一段文本，把真正那行 `dsub` 删掉 —— 解析结果必须**变成**注释里那份。

        证明上一条不是"反正只有一个 -q 所以碰巧对了"：诱饵确实在文本里、确实解析得出来，
        只是被 dsub 行盖住了。
        """
        text = "#!/bin/sh\n# 上次用的是 -q old_queue -A old_account\n"
        self.assertEqual(
            discover.parse_dsub_options(text), {"account": "old_account", "queue": "old_queue"}
        )


# ==========================================================================
# ★★ ptxt 换 corner —— 本模块最危险的地方（字符串替换）
# ==========================================================================


class PtxtCornerFilter(unittest.TestCase):
    """corner 轴要**同时改两处**（BRIEF §7）：`--corner=` 和 `--emssTechFile=` 的文件名。

    危险在于后者是字符串替换，而 corner 名（`typical`）在真实路径里到处都是。
    fixture 的 ptxt 路径是**故意造的陷阱**，`typical` 出现四次：

        …/process/typical/typical_v2/ptxt_enc/FAKEPDK_atypical_typical_V1.0_…ptxt
                  ~~~~~~~ ~~~~~~~~~~           ~~~~~~~~ ~~~~~~~
                  目录段   目录段(子串)          子串     ★ 只有这一处该换

    换错一处 = 「目录名说 typical、实际用了别的工艺角」，而且**跑得出来、数字也像**。
    """

    def facts(self):
        """正反两向共用的唯一输入路径。"""
        return discover.discover_site_facts(str(OFFDIR))

    def test_name_template_only_touches_the_basename(self) -> None:
        """★ 解析出来的模板是手写字面量 —— 目录段的 `typical` / `typical_v2` 都还在，
        basename 里的 `atypical` 也还在，只有真正那一段变成了 `{corner}`。"""
        facts = self.facts()
        self.assertEqual(facts.ptxt, EXP_PTXT)
        self.assertEqual(facts.ptxt_dir, EXP_PTXT_DIR)
        self.assertEqual(facts.ptxt_name_template, EXP_PTXT_NAME_TEMPLATE)

    def test_path_for_other_corner(self) -> None:
        """★ 换成另一个工艺角 → 期望值是**手写字面量**的完整路径。

        （`cworst` 是通用工艺角名，不是站点身份 —— `core.matrix.builtin_axis_catalog()`
        里就有它。）
        """
        self.assertEqual(
            discover.ptxt_path_for_corner(self.facts(), "cworst"),
            "/fake/pdk/apps/ewave/ewaveinterface/process/typical/typical_v2/ptxt_enc/"
            "FAKEPDK_atypical_cworst_V1.0_encrypted_package.ptxt",
        )

    def test_only_one_segment_changes(self) -> None:
        """过滤器测试（配方 4）：逐段比 —— 只有**最后一段**变，且段内只有**一个 token** 变。"""
        facts = self.facts()
        new = discover.ptxt_path_for_corner(facts, "cworst")
        old_segments, new_segments = facts.ptxt.split("/"), new.split("/")
        self.assertEqual(len(old_segments), len(new_segments))
        changed_segments = [i for i, (a, b) in enumerate(zip(old_segments, new_segments)) if a != b]
        self.assertEqual(changed_segments, [len(old_segments) - 1], "只许动 basename")
        old_tokens = old_segments[-1].split("_")
        new_tokens = new_segments[-1].split("_")
        self.assertEqual(len(old_tokens), len(new_tokens))
        changed_tokens = [i for i, (a, b) in enumerate(zip(old_tokens, new_tokens)) if a != b]
        self.assertEqual(len(changed_tokens), 1, f"basename 里换了 {len(changed_tokens)} 个 token")

    def test_substrings_are_not_damaged(self) -> None:
        """过滤器测试（配方 4）：`typical` 的三个"像但不是"的出现全都必须活着。

        这条是 `--sparam` 误伤 `--sparamImpedance` 那个 bug 的同类回归：
        前缀/子串匹配会把不该动的一起动了，而结果照样跑得出来。
        """
        new = discover.ptxt_path_for_corner(self.facts(), "cworst")
        self.assertIn("/process/typical/", new, "目录段 typical 被误伤了")
        self.assertIn("/typical_v2/", new, "目录段 typical_v2（子串形态）被误伤了")
        self.assertIn("FAKEPDK_atypical_", new, "basename 里的 atypical（子串形态）被误伤了")

    def test_naive_replace_would_have_been_wrong(self) -> None:
        """把朴素写法钉在这里：`path.replace(corner, new)` 会把四处一起换掉。

        断言我们的结果**不等于**朴素结果 —— 否则说明过滤器根本没生效。
        """
        facts = self.facts()
        naive = facts.ptxt.replace(EXP_CORNER, "cworst")
        self.assertNotEqual(discover.ptxt_path_for_corner(facts, "cworst"), naive)
        self.assertEqual(naive.count("cworst"), 4, "陷阱路径本身失效了 —— 这组测试会空过")

    def test_path_for_other_corner_negative(self) -> None:
        """反向：换 corner 之后路径**必须真的变了**；换回原 corner 必须逐字节回到原路径。

        「换了 corner 但 ptxt 没跟着变」是本项目最怕的那种静默错误 ——
        目录名说一个工艺角，实际算的是另一个。
        """
        facts = self.facts()
        other = discover.ptxt_path_for_corner(facts, "cworst")
        self.assertNotEqual(other, facts.ptxt, "换了 corner 而 ptxt 没变 —— §7 的两处只改了一处")
        self.assertEqual(discover.ptxt_path_for_corner(facts, EXP_CORNER), facts.ptxt)

    def test_missing_template_is_refused(self) -> None:
        """模板为空 = 没认出 corner → 必须抛，**不许**返回一条不随 corner 变的路径。"""
        from ewave_batch.model import SiteFacts

        with self.assertRaises(DiscoveryError) as caught:
            discover.ptxt_path_for_corner(SiteFacts(ptxt=EXP_PTXT), "cworst")
        self.assertIn("Next", str(caught.exception))

    def test_template_without_placeholder_is_refused(self) -> None:
        """手工拼的 SiteFacts 里模板没有 `{corner}` → 同样拒绝（那会静默返回不变的路径）。"""
        from ewave_batch.model import SiteFacts

        facts = SiteFacts(ptxt=EXP_PTXT, ptxt_dir=EXP_PTXT_DIR, ptxt_name_template="no_corner.ptxt")
        with self.assertRaises(DiscoveryError):
            discover.ptxt_path_for_corner(facts, "cworst")

    def test_corner_absent_from_ptxt_name_warns_and_refuses(self) -> None:
        """ptxt 文件名里根本没有 corner（换 PDK 版本后有可能）→ 模板留空 + 一条 warning，
        之后 `ptxt_path_for_corner` 拒绝。**不许**默默返回原路径。"""
        with tempfile.TemporaryDirectory() as tmp:
            copied = copy_offdir(tmp)
            rewrite(
                Path(copied) / RUNSH_PATH.name,
                EXP_PTXT,
                EXP_PTXT_DIR + "/FAKEPDK_nocornerhere_V1.0_encrypted_package.ptxt",
            )
            facts = discover.discover_site_facts(copied)
            self.assertEqual(facts.ptxt_name_template, "")
            self.assertTrue(
                any("corner" in w for w in facts.warnings),
                f"没有 warning 提醒 corner 认不出来：{facts.warnings}",
            )
            with self.assertRaises(DiscoveryError):
                discover.ptxt_path_for_corner(facts, "cworst")

    def test_corner_appearing_twice_in_name_is_flagged(self) -> None:
        """basename 里 corner 出现两次 → 两处都换（歧义不该静默挑一处），但要记 warning。"""
        with tempfile.TemporaryDirectory() as tmp:
            copied = copy_offdir(tmp)
            doubled = EXP_PTXT_DIR + "/FAKEPDK_typical_mid_typical_V1.0_encrypted_package.ptxt"
            rewrite(Path(copied) / RUNSH_PATH.name, EXP_PTXT, doubled)
            facts = discover.discover_site_facts(copied)
            self.assertEqual(
                facts.ptxt_name_template, "FAKEPDK_{corner}_mid_{corner}_V1.0_encrypted_package.ptxt"
            )
            self.assertTrue(any("2" in w and "corner" in w for w in facts.warnings), facts.warnings)


# ==========================================================================
# 主路径：官方 run 目录 → SiteFacts
# ==========================================================================


def copy_offdir(destination_root: str) -> str:
    """把合成 fixture 拷一份到临时目录（反向测试要改内容，**绝不动 fixture 本体**）。"""
    target = os.path.join(destination_root, "offdir")
    shutil.copytree(str(OFFDIR), target)
    return target


def rewrite(path: Path, old: str, new: str) -> None:
    """临时副本里的定点替换 + 一条防空过断言。"""
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise AssertionError(f"{path.name} 里没有 {old!r} —— 反向测试会空过")
    path.write_text(text.replace(old, new), encoding="utf-8", newline="")


class SiteFactsFromSyntheticDir(unittest.TestCase):
    """`discover_site_facts` 端到端。期望值全是手写字面量。"""

    def facts(self, directory: str | None = None):
        return discover.discover_site_facts(str(OFFDIR) if directory is None else directory)

    def test_gdsout_coordinates(self) -> None:
        facts = self.facts()
        self.assertEqual(facts.library, EXP_LIBRARY)
        self.assertEqual(facts.top_cell, EXP_TOPCELL)
        self.assertEqual(facts.view, EXP_VIEW)
        self.assertEqual(facts.layer_map, EXP_LAYERMAP)

    def test_run_script_coordinates(self) -> None:
        facts = self.facts()
        self.assertEqual(facts.ptxt, EXP_PTXT)
        self.assertEqual(facts.ptxt_dir, EXP_PTXT_DIR)
        self.assertEqual(facts.ptxt_name_template, EXP_PTXT_NAME_TEMPLATE)
        self.assertEqual(facts.pdk_root, EXP_PDK_ROOT)
        self.assertEqual(facts.key, EXP_KEY)
        self.assertEqual(facts.corner, EXP_CORNER)
        self.assertEqual(facts.temperature, EXP_TEMPERATURE)

    def test_ewave_dir_name(self) -> None:
        """eWave 自己会建的那层目录：`<corner>_<temp 的小数点换下划线>`。
        我们控制不了这个名字，只能预测它去里面找产物（BRIEF §7）。"""
        self.assertEqual(self.facts().ewave_dir_name, EXP_EWAVE_DIR)

    def test_ports(self) -> None:
        """★ 端口表：**顺序就是映射**（Touchstone 只按 P00x 排列，名字被丢掉）。

        计数断言（配方 4）：`-p` 5 个、`-i` 4 个 —— 两个数字**故意不一样**，
        比错了列表会当场露馅。
        """
        spec = self.facts().official_port_spec
        self.assertEqual(spec.mode, PortMode.EXPLICIT)
        self.assertEqual(spec.mapping, EXP_PORT_MAPPING)
        self.assertEqual(spec.signal_ports, EXP_SIGNAL_PORTS)
        self.assertEqual(len(spec.mapping), EXP_PORT_COUNT)
        self.assertEqual(len(spec.signal_ports), EXP_SIGNAL_COUNT)

    def test_official_flags(self) -> None:
        """计数断言：官方命令里 22 个 flag，一个不少（`-p`/`-i` 走 port_spec，不在这里）。"""
        facts = self.facts()
        self.assertEqual(len(facts.official_flags), EXP_OFFICIAL_FLAG_COUNT)
        self.assertIs(facts.official_flags["--nogui"], True)
        # `--equalCurrent` 现在**不在**合成基准里（2026-08-19 用户把默认改成 OFF，
        # 合成基准跟着对齐）。裸 flag 的解析形状由 `--nogui` / `-m` 继续覆盖。
        self.assertNotIn("--equalCurrent", facts.official_flags)
        self.assertIs(facts.official_flags["--nogui"], True)
        self.assertEqual(facts.official_flags["-e"], "0.5")
        self.assertEqual(facts.official_flags["--emssTechFile"], EXP_PTXT)
        self.assertEqual(facts.official_flags["--sparamImpedance"], "50")

    def test_production_flags_drop_site_identity_only(self) -> None:
        """`production_flags` = official 减掉 6 个站点身份项，**别的一个都不许少**。"""
        facts = self.facts()
        self.assertEqual(len(facts.production_flags), EXP_PRODUCTION_FLAG_COUNT)
        for gone in ("--emssTechFile", "--gds", "--top", "--workDir", "--sparam", "--key"):
            self.assertNotIn(gone, facts.production_flags)
        # 过滤器没误伤：`--sparam` 走了，`--sparamImpedance` 必须还在。
        self.assertEqual(facts.production_flags["--sparamImpedance"], "50")

    def test_dsub_triplet(self) -> None:
        facts = self.facts()
        self.assertEqual(facts.dsub_account, EXP_DSUB_ACCOUNT)
        self.assertEqual(facts.dsub_queue, EXP_DSUB_QUEUE)
        self.assertEqual(facts.dsub_resources, EXP_DSUB_RESOURCES)

    def test_source_files_point_at_the_right_file(self) -> None:
        """出错时要能一眼看出该去改哪个文件。"""
        facts = self.facts()
        self.assertTrue(facts.source_files["library"].endswith("gdsout_setup"))
        self.assertTrue(facts.source_files["ptxt"].endswith(RUNSH_PATH.name))
        self.assertTrue(facts.source_files["dsub_queue"].endswith(REMOTE_PATH.name))

    def test_temperature_is_really_parsed_negative(self) -> None:
        """反向：临时副本里把 `--temperature=-40.0` 改成 `125.0` ——
        `temperature` **和** `ewave_dir_name` 必须一起变，别的字段一个都不许变。

        `ewave_dir_name` 是被预测出来的目录名；它没跟着变就意味着我们会去错的目录里找产物。
        """
        with tempfile.TemporaryDirectory() as tmp:
            copied = copy_offdir(tmp)
            rewrite(Path(copied) / RUNSH_PATH.name, "--temperature=-40.0", "--temperature=125.0")
            facts = self.facts(copied)
            self.assertEqual(facts.temperature, "125.0")
            self.assertEqual(facts.ewave_dir_name, "typical_125_0")
            # 别的坐标纹丝不动 —— 排除"换了个东西测"。
            self.assertEqual(facts.corner, EXP_CORNER)
            self.assertEqual(facts.ptxt, EXP_PTXT)
            self.assertEqual(facts.library, EXP_LIBRARY)

    def test_ports_are_really_parsed_negative(self) -> None:
        """反向：把第 2、3 个 `-p` 的**次序**对调 —— 解析结果必须在第 1 位就分叉。

        pin 集合没变、命令照样跑得出来、数字还挺像 —— 顺序错位正是最难发现的那类，
        所以必须靠**位置**抓（`diff_ports` 而不是集合比较）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            copied = copy_offdir(tmp)
            script = Path(copied) / RUNSH_PATH.name
            first, second = "-p 'P001=MY_INN'", "-p 'P002=MY_INP'"
            text = script.read_text(encoding="utf-8")
            self.assertIn(first, text)
            self.assertIn(second, text)
            swapped = text.replace(first, "\x00").replace(second, first).replace("\x00", second)
            script.write_text(swapped, encoding="utf-8", newline="")

            spec = self.facts(copied).official_port_spec
            from ewave_batch.model import PortSpec

            diff = cmd.diff_ports(spec, PortSpec(mode=PortMode.EXPLICIT, mapping=EXP_PORT_MAPPING))
            self.assertFalse(diff.matched, "端口次序对调了却报一致 —— 比对没在看顺序")
            self.assertEqual(diff.first_mismatch_index, 1)
            self.assertEqual(diff.compared_count, EXP_PORT_COUNT)
            self.assertEqual(diff.only_actual, ())  # 集合没变，只有顺序错了

    def test_discovery_writes_nothing(self) -> None:
        """只读：解析前后目录里的文件名 / 大小 / mtime 逐字节相同。

        `<workarea>/ewave_simulation/` 是设计师的 spine（CLAUDE.md 硬约束 4，只读），
        而 discovery 正是唯一会去碰它的代码路径。
        """
        def snapshot() -> list[tuple[str, int, int]]:
            out = []
            for name in sorted(os.listdir(str(OFFDIR))):
                stat = os.stat(str(OFFDIR / name))
                out.append((name, stat.st_size, stat.st_mtime_ns))
            return out

        before = snapshot()
        self.facts()
        self.assertEqual(snapshot(), before)


class DiscoveryErrors(unittest.TestCase):
    """三种硬失败，每条都要带一句「下一步怎么办」（cfg.sh 的 `offdir_help` 是样板）。

    理由很实际：红区路径长得离谱，打错一个字符后面全废，而用户是隔着气隙手工敲的 ——
    错误消息里没有下一步，就要多跑一个来回。
    """

    def test_empty_path(self) -> None:
        with self.assertRaises(DiscoveryError) as caught:
            discover.discover_site_facts("")
        message = str(caught.exception)
        self.assertIn("Next", message)
        self.assertIn("gdsout_setup", message)

    def test_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DiscoveryError) as caught:
                discover.discover_site_facts(os.path.join(tmp, "nope"))
            self.assertIn("Next", str(caught.exception))

    def test_directory_without_gdsout_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DiscoveryError) as caught:
                discover.discover_site_facts(tmp)
            message = str(caught.exception)
            self.assertIn("gdsout_setup", message)
            self.assertIn("Next", message)

    def test_error_lists_nearby_candidates(self) -> None:
        """报错时顺手把附近的候选列出来 —— 让机器找，别让人手打长路径。"""
        with tempfile.TemporaryDirectory() as tmp:
            copy_offdir(tmp)  # tmp/offdir/gdsout_setup
            empty = os.path.join(tmp, "empty")
            os.makedirs(empty)
            with self.assertRaises(DiscoveryError) as caught:
                discover.discover_site_facts(empty)
            self.assertIn(os.path.join(tmp, "offdir"), str(caught.exception))

    def test_missing_run_script_is_soft(self) -> None:
        """`run_ewave_*.sh` 缺失是**软失败**：记 warning、字段留空，不炸掉整个批次规划。"""
        with tempfile.TemporaryDirectory() as tmp:
            copied = copy_offdir(tmp)
            os.remove(os.path.join(copied, RUNSH_PATH.name))
            facts = discover.discover_site_facts(copied)
            self.assertEqual(facts.library, EXP_LIBRARY)  # gdsout 那半照样解析出来
            self.assertEqual(facts.ptxt, "")
            self.assertTrue(any(RUNSH_PATH.name[:10] in w for w in facts.warnings), facts.warnings)

    def test_remote_script_is_not_mistaken_for_the_run_script(self) -> None:
        """`remote_run_ewave.sh` 是**外层**提交脚本，不许被当成内层 `run_ewave_*.sh`。

        认错了 ptxt / 端口表全解析不出来，而且 `discover` 不会报错 —— 静默失败。
        """
        with tempfile.TemporaryDirectory() as tmp:
            copied = copy_offdir(tmp)
            os.remove(os.path.join(copied, RUNSH_PATH.name))
            facts = discover.discover_site_facts(copied)
            self.assertEqual(facts.official_command_line, "")
            self.assertEqual(facts.official_flags, {})
            # 而 remote 那半仍然解析得出来 —— 证明文件确实还在，不是"都没读到所以都空"。
            self.assertEqual(facts.dsub_queue, EXP_DSUB_QUEUE)


# ==========================================================================
# 默认表：从官方 run 目录学，不写死在源码（§11 规则 1）
# ==========================================================================


class LearnDefaultFlags(unittest.TestCase):
    """`learn_default_flags`：剔掉站点项 / 机制层 / 轴掌管的 flag，剩下的就是默认表。"""

    def facts(self, directory: str | None = None):
        return discover.discover_site_facts(str(OFFDIR) if directory is None else directory)

    def test_learned_defaults(self) -> None:
        """★ 正向 + 计数：正好 3 条，逐条等于手写字面量。

        这 3 条正是 BRIEF §11「默认表」那一层点名的东西（`--viaMode` / `--sparamImpedance`
        / `--labelDepth`）—— 影响结果、基本不动、不进目录名。
        """
        learned = discover.learn_default_flags(self.facts())
        self.assertEqual(learned, EXP_LEARNED_DEFAULTS)
        self.assertEqual(len(learned), EXP_LEARNED_FLAG_COUNT)

    def test_sparam_does_not_eat_sparamimpedance(self) -> None:
        """★★ 过滤器测试（配方 4）—— MVP 那个真 bug 的回归。

        排除规则里写 `--sparam` **前缀**会把 `--sparamImpedance` 一起吃掉，
        两边同时被跳过 → diff 空得非常好看但根本没比（BRIEF §10）。
        剔除必须是**精确名**匹配：`--sparam` 走，`--sparamImpedance` 留。
        """
        learned = discover.learn_default_flags(self.facts())
        self.assertNotIn("--sparam", learned)
        self.assertIn("--sparamImpedance", learned)
        self.assertEqual(learned["--sparamImpedance"], "50")

    def test_axis_flags_are_not_learned(self) -> None:
        """轴掌管的 flag 由**轴**给（它们是 run 的身份、会进目录名）。
        学进默认表就会出现「默认表说一个值、轴说另一个值」的双重记账。"""
        learned = discover.learn_default_flags(self.facts())
        for axis_flag in ("--corner", "--temperature", "--equalCurrent", "-e", "-d",
                          "--viaMergeSpace", "--relativeTolerance",
                          "--relativeCurrentTolerance", "--multiSweep", "--parallel"):
            self.assertNotIn(axis_flag, learned, f"{axis_flag} 是轴掌管的，不该进默认表")

    def test_mechanism_flags_are_not_learned(self) -> None:
        """机制层由工具自己按 run 算（`--workDir` / `--all` / `--nogui` …）。"""
        learned = discover.learn_default_flags(self.facts())
        for mechanism in MECHANISM_FLAGS:
            self.assertNotIn(mechanism, learned)

    def test_both_input_paths_agree(self) -> None:
        """`production_flags` 和 `official_flags` 两条入口必须学出同一份 ——
        剔除是幂等的集合运算，不是"看运气从哪儿读"。"""
        from dataclasses import replace as _replace

        facts = self.facts()
        via_official = discover.learn_default_flags(_replace(facts, production_flags={}))
        self.assertEqual(via_official, discover.learn_default_flags(facts))

    def test_learned_defaults_negative_value(self) -> None:
        """反向 ①：临时副本里把 `--viaMode=1` 改成 2 —— 学出来的必须是 2。

        这条证明默认表**真的是学来的**，不是源码里写死的常量
        （§11 规则 1 的全部意义：换 PDK 版本自动跟上）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            copied = copy_offdir(tmp)
            rewrite(Path(copied) / RUNSH_PATH.name, "--viaMode=1", "--viaMode=2")
            learned = discover.learn_default_flags(self.facts(copied))
            self.assertEqual(learned["--viaMode"], "2")
            self.assertEqual(len(learned), EXP_LEARNED_FLAG_COUNT)

    def test_learned_defaults_negative_missing(self) -> None:
        """反向 ②：把 `--labelDepth=0` 从官方命令里删掉 —— 学出来的必须**少一条**。

        计数断言在这里最值钱：如果实现偷偷拿 `BUILTIN_DEFAULT_FLAGS` 兜了底，
        条数不会变，这条会红。
        """
        with tempfile.TemporaryDirectory() as tmp:
            copied = copy_offdir(tmp)
            rewrite(Path(copied) / RUNSH_PATH.name, " --labelDepth=0", "")
            learned = discover.learn_default_flags(self.facts(copied))
            self.assertNotIn("--labelDepth", learned)
            self.assertEqual(len(learned), EXP_LEARNED_FLAG_COUNT - 1)


# ==========================================================================
# 工具路径 / 候选目录
# ==========================================================================


class FindTool(unittest.TestCase):
    """`find_tool`：`shutil.which` 优先，环境变量兜底，**绝不写死绝对路径**。

    本机（Windows）没有 `ewave`/`strmout`，红区才有 —— 所以两条分支都靠注入替身走，
    一条都不依赖本机真的装了什么。
    """

    def test_which_wins(self) -> None:
        with mock.patch("shutil.which", return_value="/somewhere/on/path/ewave") as which:
            self.assertEqual(discover.find_tool("ewave"), "/somewhere/on/path/ewave")
        which.assert_called()

    def test_env_fallback_when_not_on_path(self) -> None:
        """PATH 上没有时退到 `<NAME>_BIN` / `<NAME>_ABS`（后者是 cfg.sh 用的名字）。"""
        self.assertEqual(
            discover.find_tool("ewave", env={"PATH": "", "EWAVE_BIN": "/opt/x/ewave"}),
            "/opt/x/ewave",
        )
        self.assertEqual(
            discover.find_tool("strmout", env={"PATH": "", "STRMOUT_ABS": "/opt/y/strmout"}),
            "/opt/y/strmout",
        )

    def test_bin_beats_abs(self) -> None:
        self.assertEqual(
            discover.find_tool("ewave", env={"PATH": "", "EWAVE_BIN": "/a/ewave", "EWAVE_ABS": "/b/ewave"}),
            "/a/ewave",
        )

    def test_missing_returns_none(self) -> None:
        """找不到返回 None（**不是**返回裸名字）—— 调用方要能区分"没装"和"装在 PATH 上"。"""
        self.assertIsNone(discover.find_tool("ewave", env={"PATH": ""}))

    def test_env_is_not_leaked_from_the_real_process(self) -> None:
        """传了 `env` 就只看 `env`：本机真实 PATH 上万一有同名东西也不许被找到。"""
        with mock.patch.dict(os.environ, {"EWAVE_BIN": "/should/not/be/used"}):
            self.assertIsNone(discover.find_tool("ewave", env={"PATH": ""}))

    def test_no_hardcoded_tool_path_in_source(self) -> None:
        """源码里不许出现工具的绝对路径（CLAUDE.md 硬约束 1b）。

        判据：`discover.py` 里没有 `/…/ewave` 或 `/…/strmout` 这种以可执行文件结尾的绝对路径。
        """
        source = (ROOT / "ewave_batch" / "core" / "discover.py").read_text(encoding="utf-8")
        hits = re.findall(r"[\"'](/[A-Za-z0-9_./-]*/(?:ewave|strmout))[\"']", source)
        self.assertEqual(hits, [], f"源码里写死了工具路径：{hits}")


class SuggestOfficialDirs(unittest.TestCase):
    """`suggest_official_dirs`：用户打错一个字符后面全废，让机器找（cfg.sh 的 `suggest_offdir`）。"""

    def build(self, root: str) -> dict[str, str]:
        """造一棵深度 1/2/3 各放一个 `gdsout_setup` 的树。返回名字 → 绝对路径。"""
        made: dict[str, str] = {}
        for name, parts in (
            ("d1", ("a",)),
            ("d2", ("a", "b")),
            ("d3", ("a", "b", "c")),
        ):
            path = os.path.join(root, *parts)
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "gdsout_setup"), "w", encoding="utf-8") as fh:
                fh.write("\tlibrary\t\"X\"\n")
            made[name] = os.path.abspath(path)
        return made

    def test_depth_limit(self) -> None:
        """`max_depth` 与 `find -maxdepth` 同义：**gdsout_setup 这个文件**的深度上限。

        默认 3 → `root/a/b/gdsout_setup` 进得来，`root/a/b/c/gdsout_setup` 进不来。
        """
        with tempfile.TemporaryDirectory() as tmp:
            made = self.build(tmp)
            found = discover.suggest_official_dirs(tmp)
            self.assertEqual(found, sorted([made["d1"], made["d2"]]))

    def test_depth_limit_negative(self) -> None:
        """反向：把 `max_depth` 加到 4 —— 深一层那个必须出现。

        证明深度真的是个参数，不是"反正只找得到两个"。
        """
        with tempfile.TemporaryDirectory() as tmp:
            made = self.build(tmp)
            found = discover.suggest_official_dirs(tmp, max_depth=4)
            self.assertEqual(found, sorted([made["d1"], made["d2"], made["d3"]]))

    def test_sorted_and_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.build(tmp)
            found = discover.suggest_official_dirs(tmp)
            self.assertEqual(found, sorted(set(found)))

    def test_missing_root_is_empty_not_an_error(self) -> None:
        """"帮忙找候选"失败不该盖掉真正的那条错误 —— 返回空 list，不抛。"""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(discover.suggest_official_dirs(os.path.join(tmp, "nope")), [])
        self.assertEqual(discover.suggest_official_dirs(""), [])

    def test_finds_the_real_fixture(self) -> None:
        """拿仓库里的合成 fixture 当靶子：从 `tests/fixtures` 起算它在深度 2。"""
        found = discover.suggest_official_dirs(str(ROOT / "tests" / "fixtures"))
        self.assertIn(os.path.abspath(str(OFFDIR)), found)


# ==========================================================================
# 交叉验证：真实生产脚本（红区 fixture，缺文件优雅 skip）
# ==========================================================================


@unittest.skipUnless(HAVE_PRODUCTION, PRODUCTION_SKIP)
class ProductionCrossCheck(unittest.TestCase):
    """★ 拿**真实**的官方脚本喂 `discover_site_facts`，期望值取自人抽出来的 golden fixture。

    合成 fixture 验的是"解析器认不认得这个格式"，这一组验的是"我们对格式的理解
    跟真实文件对不对得上" —— 合成 fixture 是我自己造的，它天然不能证明这一点。

    源码里**一个真实取值都没有**：期望值从 `production_cmd.local.json` 读，
    输入从 `references/` 读，两者都不进 git。
    """

    def setUp(self) -> None:
        self.fixture = json.loads(PRODUCTION_JSON.read_text(encoding="utf-8"))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.offdir = os.path.join(self.tmp.name, "official")
        os.makedirs(self.offdir)
        # gdsout_setup 用合成那份（真实那份是红区证据，形状一样，这里只是让闸门过）。
        shutil.copyfile(str(SETUP_PATH), os.path.join(self.offdir, "gdsout_setup"))
        shutil.copyfile(str(KIT_RUNSH), os.path.join(self.offdir, "run_ewave_from_kit.sh"))
        if KIT_REMOTE.exists():
            shutil.copyfile(str(KIT_REMOTE), os.path.join(self.offdir, "remote_run_ewave.sh"))

    def facts(self):
        return discover.discover_site_facts(self.offdir)

    def test_flags_match_the_human_extracted_golden(self) -> None:
        """逐 flag 比 —— 多认一个、少认一个、值不对，都当场红。"""
        facts = self.facts()
        diff = cmd.diff_flags(facts.official_flags, dict(self.fixture["flags"]))
        self.assertTrue(
            diff.clean,
            f"多 {diff.only_actual}，少 {diff.only_expected}，"
            f"值不同 {[(d.flag, d.actual, d.expected) for d in diff.differing]}",
        )
        # 计数断言（配方 4）：真的比了 N 条，不是两边都空所以好看。
        self.assertEqual(diff.compared_count, len(self.fixture["flags"]))
        self.assertEqual(len(facts.official_flags), len(self.fixture["flags"]))

    def test_flags_match_the_human_extracted_golden_negative(self) -> None:
        """反向：把真实脚本里的 `-e 0.4` 改成 `-e 0.5` —— 必须**且只**报这一处。

        （`-e` 的取值是 eWave 的工具语义，不是站点坐标，可以写进源码。）
        """
        script = Path(self.offdir) / "run_ewave_from_kit.sh"
        rewrite(script, "-e 0.4", "-e 0.5")
        diff = cmd.diff_flags(self.facts().official_flags, dict(self.fixture["flags"]))
        self.assertFalse(diff.clean)
        self.assertEqual([d.flag for d in diff.differing], ["-e"])
        self.assertEqual(diff.compared_count, len(self.fixture["flags"]))

    def test_port_order_matches_the_golden(self) -> None:
        """端口**顺序**就是映射（BRIEF §5）。计数取自 fixture 的 `port_count`。"""
        from ewave_batch.model import PortSpec

        expected_mapping = tuple(
            tuple(str(item).partition("=")[::2]) for item in self.fixture["port_order"]
        )
        spec = self.facts().official_port_spec
        diff = cmd.diff_ports(spec, PortSpec(mode=PortMode.EXPLICIT, mapping=expected_mapping))
        self.assertTrue(diff.matched, f"第 {diff.first_mismatch_index} 位起分叉")
        self.assertEqual(diff.compared_count, self.fixture["port_count"])
        self.assertEqual(len(spec.mapping), self.fixture["port_count"])
        self.assertEqual(
            list(spec.signal_ports), [str(p) for p in self.fixture["signal_ports"]]
        )

    def test_ptxt_template_is_derived_from_the_real_path(self) -> None:
        """真实 ptxt 路径也要能认出 corner 那一段（合成 fixture 是我造的，证明不了这条）。

        断言是**结构性**的（源码里不许出现真实取值）：模板里有 `{corner}`、
        把原 corner 填回去必须逐字节回到原路径、换成别的 corner 必须真的变。
        """
        facts = self.facts()
        self.assertIn("{corner}", facts.ptxt_name_template)
        self.assertEqual(discover.ptxt_path_for_corner(facts, facts.corner), facts.ptxt)
        other = discover.ptxt_path_for_corner(facts, "cworst")
        self.assertNotEqual(other, facts.ptxt)
        # 只动 basename：目录部分逐字节不变。
        self.assertEqual(os.path.dirname(other), os.path.dirname(facts.ptxt))

    def test_learned_defaults_match_the_builtin_table(self) -> None:
        """从真实命令学出来的默认表，与源码里那张兜底表**凡是两边都有的键值必须相等**。

        BRIEF §10 记的"13 条逐条一致"就是这件事的 shell 版；这里是它的 Python 版。
        不相等说明要么兜底表抄错了，要么解析口径不对。
        """
        learned = discover.learn_default_flags(self.facts())
        overlap = set(learned) & set(cmd.BUILTIN_DEFAULT_FLAGS)
        self.assertTrue(overlap, "两张表一个键都不重叠 —— 这条测试会空过")
        for flag in sorted(overlap):
            self.assertEqual(learned[flag], cmd.BUILTIN_DEFAULT_FLAGS[flag], flag)

    @unittest.skipUnless(KIT_REMOTE.exists(), "本机没有 kit 里的 remote_run_ewave.sh（红区资料）")
    def test_dsub_triplet_shape(self) -> None:
        """dsub 三元组：断言**形状**（三个键都有、`-R` 里有 `cpu=`），
        绝不断言取值 —— 账号/队列是站点身份，写进测试源码就泄漏了。"""
        facts = self.facts()
        self.assertTrue(facts.dsub_account)
        self.assertTrue(facts.dsub_queue)
        self.assertRegex(facts.dsub_resources, r"cpu=\d+")
        self.assertIn("mem", cmd.parse_resource_string(facts.dsub_resources))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
