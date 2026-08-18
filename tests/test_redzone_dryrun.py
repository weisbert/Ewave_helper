"""`ewave_batch.redzone_dryrun` 的测试 —— 端到端跑合成的「官方 run 目录」。

## 期望值从哪来（防自证配方 2）

全部是**手写字面量**，来源只有两处，每条都在注释里写明：

* `tests/fixtures/offdir_synthetic/`（P2 造的合成官方目录，README.md 逐条记了假值和字段数）；
* 成文规则：`core.cmd.render_flags` 的排序规则、`core.cmd.DEFAULT_DIFF_IGNORE` 的四条、
  BRIEF §5 的归档布局、BRIEF §11 的四层合并顺序。

**没有一个期望值是拿被测函数的输出存下来的。** `GOLDEN_ARGV` 是这样推出来的：
先把合成 fixture 里那 22 个官方 flag 逐个归到「哪一层给」（机制层 / 轴 / 学来的默认表 /
源码内置），再补上我们比官方多的两个（`--all` / `--includePortOrder=1`，D1b/D1d），
最后按 `render_flags` 的成文顺序排（flag 名 `sorted()`；ASCII 下 `--x` 全排在 `-x` 前面；
`True` → 裸 flag；长 flag → `--k=v` 一项；短 flag → 两项）。

## 这份测试**测不到**什么（写在最前面，免得读的人高估它）

合成 fixture 证明不了「真实红区目录长什么样」—— 那正是 `redzone_dryrun` 存在的理由。
这里能证明的是：**给定一个形状正确的官方目录，我们解析→拼命令→比对这条链是通的，
而且改坏任何一处都会被报出来。** 真实目录的形态差异（多一个字段、少一份脚本、
ptxt 里 corner 出现两次）只有到红区才知道。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ewave_batch import redzone_dryrun as rz
from ewave_batch.core import cmd

ROOT = Path(__file__).resolve().parents[1]
OFFDIR = ROOT / "tests" / "fixtures" / "offdir_synthetic"
RUN_SCRIPT_NAME = "run_ewave_typical_-40_0.sh"

# ---------------------------------------------------------------------------
# 工具解析的注入口 —— 期望值不许依赖"跑测试的这台机器上有没有 ewave"
# ---------------------------------------------------------------------------

NO_TOOLS_ENV: dict[str, str] = {}
"""注入给 `core.discover.find_tool` 的环境：**空的**。

`find_tool` 的契约是「传了 env 就只看 env」⇒ 空 env = 没有 PATH、没有
`EWAVE_BIN`/`EWAVE_ABS`/`STRMOUT_BIN`/`STRMOUT_ABS` ⇒ 两个工具都解析不出来
⇒ argv[0] 走通用程序名占位那条分支。

🚨 **这一格是这份文件全部 argv 期望值的地基。** 不注入的话 `find_tool` 读的是真实环境，
而红区 `ma ewave/…` 之后 PATH 上就有 `ewave` ⇒ `argv[0]` 变成绝对路径 ⇒
`GOLDEN_ARGV` 这类断言在**红区**（唯一真正重要的机器）当场红，而本机看不见。
2026-08-18 的复审就是抓的这个洞，钉死它的是 `ToolNameInjection`。
"""

FAKE_EWAVE_ABS = "/fake/abs/path/ewave"
FAKE_STRMOUT_ABS = "/fake/abs/path/strmout"
""""工具解析得出来"那一侧的注入值 —— 手写的假绝对路径（红区的真路径是站点坐标，
源码里永远不写死，见 CLAUDE.md 硬约束 1b）。形状照红区常态：`ma` 出模块之后
`command -v ewave` 给的就是这样一条绝对路径。"""

LEAKED_EWAVE_ABS = "/fake/abs/path/leaked_ewave"
LEAKED_STRMOUT_ABS = "/fake/abs/path/leaked_strmout"
"""反向那条用的"真实环境里躺着的值" —— 同样是手写假值。
它扮演的是红区那台机器：`ma ewave/…` 之后环境里真的有这么一条绝对路径。"""

TOOLS_RESOLVED_ENV: dict[str, str] = {
    "EWAVE_ABS": FAKE_EWAVE_ABS,
    "STRMOUT_ABS": FAKE_STRMOUT_ABS,
}
""""解析得出来"的注入环境。用 `*_ABS` 是有讲究的：那正是已经进了 git 的
`mvp/redzone/cfg.sh` 在用的变量名 ⇒ 这条分支在红区是**常态**而不是边角。"""

BLANK_TOOL_ENVIRON = {
    "PATH": "",
    "EWAVE_BIN": "",
    "EWAVE_ABS": "",
    "STRMOUT_BIN": "",
    "STRMOUT_ABS": "",
}
"""给**没有注入口**的那条路径（`rz.main()` 用的是真实 `os.environ`）用的补丁。

`shutil.which(..., path="")` 在 3.8+ 一律返回 None（空 PATH 不退回 `os.defpath`），
四个兜底变量也清空 ⇒ 解析不出工具 ⇒ 占位名。同样是**注入**，只是注入点是 `os.environ`。
"""


@contextlib.contextmanager
def _no_tools_environ():
    """临时把真实环境里跟工具解析有关的那几格清空。

    `rz.main()` 的签名是冻结的（`model.main(argv=None) -> int`，CLI/GUI 共用），
    不能为了测试加参数 ⇒ 走这条路让它也确定。**不是** skip、也不是放宽断言。
    """
    with mock.patch.dict(os.environ, BLANK_TOOL_ENVIRON):
        yield


# ---------------------------------------------------------------------------
# 手写期望值 —— 全部来自 tests/fixtures/offdir_synthetic/（值）与成文规则（顺序/分层）
# ---------------------------------------------------------------------------

BATCH_ROOT = "/tmp/ewb"
BATCH_NAME = "dryrun"
BATCH_DIR = "/tmp/ewb/dryrun"
"""落点：一个**不存在**的路径。测试里一个目录都不该被建出来，所以指哪儿都行 ——
指一个不存在的地方反而能让"偷偷 mkdir"当场露馅（`test_nothing_is_created`）。"""

DESIGN_KEY = "MY_LIB_MY_CELL_layout_em"
"""= `slugify("<library>_<topCell>_<view>")`，取值抄自 fixture 的 gdsout_setup。"""

PTXT = (
    "/fake/pdk/apps/ewave/ewaveinterface/process/typical/typical_v2/ptxt_enc/"
    "FAKEPDK_atypical_typical_V1.0_encrypted_package.ptxt"
)
"""fixture 的 `--emssTechFile` 原值。corner 轴只有 `typical` 一个取值 ⇒ 换完还是它自己，
但**换的路径不同**：我们是 `ptxt_dir + name_template.replace("{corner}", …)` 拼出来的，
拼错一段就会和这条字面量对不上（`typical` 在这条路径里出现了 4 次，见 fixture 的 README）。"""

GOLDEN_ARGV: tuple[str, ...] = (
    "ewave",  # 通用程序名占位 —— `NO_TOOLS_ENV` 注入了一个"什么都解析不出来"的环境，
    #           所以这一格与本机 PATH 上有没有 ewave **无关**（见 NO_TOOLS_ENV / ToolNameInjection）
    # ---- 长 flag，按 flag 名 sorted() ----
    "--all",  # 机制层（D1b）—— 官方那条没有，我们有
    "--cadencePins=1",  # 机制层
    "--corner=typical",  # 轴（取值来自 fixture 的 --corner）
    f"--emssTechFile={PTXT}",  # 轴（corner 轴同时改这一个，BRIEF §7）
    "--equalCurrent",  # 源码内置默认（True → 裸 flag）
    f"--gds={BATCH_DIR}/gds/{DESIGN_KEY}.gds",  # 机制层（BRIEF §5 归档布局）
    "--includePortOrder=1",  # 机制层（D1d）—— 官方那条没有，我们有
    "--key=000000",  # 从 SiteFacts.key 补回默认表层（见 redzone_dryrun.KEY_FLAG）
    "--labelDepth=0",  # 学自 fixture 的默认表
    "--multiSweep=adaptive,0:0.1:40",  # 源码内置默认
    "--nogui",  # 机制层
    "--parallel=20",  # 由 remote_run_ewave.sh 的 -R "cpu=20" × 1.0 推出来（跨文件）
    "--relativeCurrentTolerance=0.001",  # 源码内置默认
    "--relativeTolerance=1e-05",  # 源码内置默认
    "--sparam=MY_CELL",  # 机制层 = Design.cell
    "--sparamImpedance=50",  # 学自 fixture 的默认表
    "--temperature=-40.0",  # 轴
    "--top=MY_CELL",  # 机制层 = Design.cell
    "--viaMergeSpace=0.4",  # 源码内置默认
    "--viaMode=1",  # 学自 fixture 的默认表
    f"--workDir={BATCH_DIR}/runs/{DESIGN_KEY}/base",  # 机制层（D2：每个组合一个独立 workDir）
    # ---- 短 flag（ASCII 下 `--x` 全在 `-x` 前面），每个两项 ----
    "-d",
    "0.4",  # 源码内置默认
    "-e",
    "0.4",  # 源码内置默认
    "-m",  # 机制层（True → 裸 flag）
)
"""没给 --spec 时那唯一一个 run 的完整 argv。27 项。"""

EXPECTED_ARGV_LEN = 27
"""= 1 个程序名 + 21 个长 flag + 2×2 个短 flag(带值) + 1 个裸短 flag。人数出来的。"""

EXPECTED_OFFICIAL_FLAGS = 22
"""fixture 的 README 写死的：那条官方命令有 22 个 ewave flag。"""

EXPECTED_COMPARED_FLAGS = 20
"""参与比较的条数 = 22 − 2。

减掉的 2 条是 `DEFAULT_DIFF_IGNORE` 里**官方也有**的（`--workDir` / `--gds`）；
另外两条 `--all` / `--includePortOrder` 只有我们有，官方那边根本没这个键 ⇒
它们不在"两边键的并集"里，不占 compared_count。

🚨 这条断言专防「空得非常好看」：diff 全绿但其实一条都没比（BRIEF §10 的真 bug）。
"""

EXPECTED_SELF_PROVING = ("--key", "--labelDepth", "--sparamImpedance", "--viaMode")
"""「学自本目录」那一堆 —— 取值就是从被比对的同一份脚本里学来的 ⇒ **结构上必然相等**。

人工推导：fixture 的 22 个 flag 里，既不属于机制层、也不被任何一根内置轴掌管、
也没有被源码内置默认盖住的，只有 `--labelDepth` / `--viaMode` / `--sparamImpedance`
（= `core.discover.learn_default_flags` 学出来的那 3 条，fixture README 的字段表也是这么写的），
外加我们从 `SiteFacts.key` 补回来的 `--key`。

`test_learned_flag_mutation_is_structurally_invisible` 把这条"必然相等"演给人看。
"""

EXPECTED_INDEPENDENT = 16
"""真独立验证的条数 = 20 − 4。**这个数字才是这趟比对的含金量。**"""

EXPECTED_PORT_COUNT = 5
"""fixture 里 `-p` 的个数（README 写死：5 个 -p、4 个 -i）。"""

EXPECTED_GDSOUT_COMPARED = 17
"""gdsout_setup 往返自检比了几个字段 = 24 − 7。
24 是 fixture README 写死的字段数，7 是 `GDSOUT_PLACEHOLDERS`（随 design 变的那些）。"""

EXPECTED_FALLBACK_COMPARED = 8
"""兜底模板对照比了几个字段 = `tools.strmout.GDSOUT_CRITICAL_FIELDS` 的条数（D1c 点名的 8 个）。"""

EXPECTED_IGNORED = ("--all", "--gds", "--includePortOrder", "--workDir")
"""`DEFAULT_DIFF_IGNORE` 排序后的样子。断言它**没多吃**别的 flag（防前缀误伤，配方 4）。"""


# ---------------------------------------------------------------------------
# 共用的输入构造路径 —— 正反两向都走这一条（配方 3）
# ---------------------------------------------------------------------------


def _report(offdir, **kwargs):
    """唯一的一条报告构造路径。反向测试只改 `offdir`（那份被改坏的副本）。

    `env` 默认注入 `NO_TOOLS_ENV` —— 工具解析**不读真实环境**，于是这份文件里所有
    argv 期望值在任何机器上都成立（`ToolNameInjection` 把这条钉死）。
    """
    kwargs.setdefault("batch_root", BATCH_ROOT)
    kwargs.setdefault("batch_name", BATCH_NAME)
    kwargs.setdefault("env", NO_TOOLS_ENV)
    return rz.build_report(str(offdir), **kwargs)


def _copy_offdir(dest_parent: str, *, name: str = "off") -> str:
    """把合成 fixture 复制一份到 tempdir（**绝不改仓库里那份**）。"""
    dest = os.path.join(dest_parent, name)
    shutil.copytree(OFFDIR, dest)
    return dest


def _mutate_run_script(offdir: str, old: str, new: str) -> None:
    """在副本的官方脚本里把 `old` 换成 `new`。找不到 `old` 就当场失败 ——
    fixture 改了而测试没跟上时，这条断言比"测试神秘变绿"好得多。"""
    path = os.path.join(offdir, RUN_SCRIPT_NAME)
    text = Path(path).read_text(encoding="utf-8")
    assert old in text, f"合成 fixture 里没有 {old!r} —— fixture 变了，期望值要一起改"
    Path(path).write_text(text.replace(old, new, 1), encoding="utf-8", newline="")


def _snapshot(root: str) -> dict:
    """目录树的「文件 → (大小, mtime_ns)」快照。只读守卫的判据。"""
    seen = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            stat = os.stat(full)
            seen[os.path.relpath(full, root)] = (stat.st_size, stat.st_mtime_ns)
    return seen


# ---------------------------------------------------------------------------
# 正向：端到端
# ---------------------------------------------------------------------------


class EndToEnd(unittest.TestCase):
    """合成官方目录 → 完整 argv + 落地目录 + 自带比对。"""

    def setUp(self) -> None:
        self.report = _report(OFFDIR)

    # ---- argv ----

    def test_argv_is_the_golden_one(self) -> None:
        """★ 主判据：那唯一一个 run 的 argv 与手写的 `GOLDEN_ARGV` 逐项相等。"""
        self.assertEqual(len(self.report.stage_two), 1, "没给 spec ⇒ 官方那一次跑重放一遍 = 1 个 run")
        argv = self.report.stage_two[0].plan.argv
        self.assertEqual(list(argv), list(GOLDEN_ARGV))

    def test_argv_length(self) -> None:
        """计数断言：argv 的项数。多一项少一项都说明分层或渲染规则变了。"""
        self.assertEqual(len(self.report.stage_two[0].plan.argv), EXPECTED_ARGV_LEN)
        self.assertEqual(len(GOLDEN_ARGV), EXPECTED_ARGV_LEN)

    def test_argv_has_exactly_one_all_flag(self) -> None:
        """`--all` 恰好一次 —— `tools.ewave.render_ports` 与机制层各给一次就会有两个，
        而 eWave 多半照单全收、`.sNp` 里看不出来。"""
        argv = self.report.stage_two[0].plan.argv
        self.assertEqual(list(argv).count("--all"), 1)
        self.assertEqual([t for t in argv if t == "-p"], [], "--all 模式下不该有 -p")

    def test_key_is_restored_from_site_facts(self) -> None:
        """`--key` 必须出现在 argv 里（取值抄自 fixture 的 `--key=000000`）。

        它是 P1/P2 之间那处集成缺口的回归测试：「学默认表」把它当站点身份剔掉了、
        源码又不许写死它 ⇒ 不补回来的话生成的命令就缺 `--key`，而官方那条有。
        """
        self.assertIn("--key=000000", self.report.stage_two[0].plan.argv)
        self.assertEqual(self.report.facts.key, "000000")

    # ---- 落地目录 ----

    def test_landing_dirs(self) -> None:
        """落地目录逐条等于手写字面量（形状抄 BRIEF §5「归档布局」那棵树）。"""
        paths = self.report.stage_two[0].paths
        self.assertEqual(paths.run_dir, f"{BATCH_DIR}/runs/{DESIGN_KEY}/base")
        self.assertEqual(paths.ewave_dir, f"{BATCH_DIR}/runs/{DESIGN_KEY}/base/typical_-40_0")
        self.assertEqual(paths.cmd_sh, f"{BATCH_DIR}/runs/{DESIGN_KEY}/base/cmd_typical_-40_0.sh")
        self.assertEqual(paths.run_log, f"{BATCH_DIR}/runs/{DESIGN_KEY}/base/run_typical_-40_0.log")
        self.assertEqual(paths.design_gds, f"{BATCH_DIR}/gds/{DESIGN_KEY}.gds")
        self.assertEqual(paths.design_gdsout, f"{BATCH_DIR}/gdsout/{DESIGN_KEY}.gdsout_setup")
        self.assertEqual(
            paths.sparam_prefix, f"{BATCH_DIR}/sparam/{DESIGN_KEY}__base__typical_-40_0"
        )

    def test_stage_one_command(self) -> None:
        """阶段 1 恰好是 MVP 实测跑通的那条形状：`strmout -templateFile <file>`，没有第二个 flag。"""
        self.assertEqual(len(self.report.stage_one), 1)
        stage_one = self.report.stage_one[0]
        self.assertEqual(
            list(stage_one.plan.argv),
            ["strmout", "-templateFile", f"{BATCH_DIR}/gdsout/{DESIGN_KEY}.gdsout_setup"],
        )
        self.assertEqual(stage_one.plan.cwd, f"{BATCH_DIR}/cdswork")

    def test_rendered_gdsout_keeps_the_critical_lines(self) -> None:
        """D1c：渲染出来的 setup 里那几行必须逐字还在（错一个 mesh 就变，而且跑得出来）。"""
        rendered = self.report.stage_one[0].rendered_setup
        for line in ('\tmaxVertices\t\t200\n', '\tcase\t"preserve"\n', '\tconvertPin\t\t"geometry"\n'):
            self.assertIn(line, rendered)
        # 7 个随 design 变的字段被换成我们的落点：runDir 是目录、strmFile 是文件名
        # （`tools.strmout.gdsout_fields_for_design` 把 `design_gds` 拆成这两半）。
        self.assertIn(f'\trunDir\t\t\t"{BATCH_DIR}/gds"\n', rendered)
        self.assertIn(f'\tstrmFile\t\t"{DESIGN_KEY}.gds"\n', rendered)
        self.assertIn('\tlibrary\t\t"MY_LIB"\n', rendered)
        self.assertIn('\tview\t\t"layout_em"\n', rendered)
        self.assertNotIn("@@", rendered, "占位符必须全部换掉")

    # ---- 自带比对 ----

    def test_comparison_is_clean(self) -> None:
        """★ 自带比对：与官方那条真实命令逐 flag 一致、端口逐位一致。"""
        comparison = self.report.comparison
        self.assertEqual(comparison.status, "clean", comparison.reason)
        self.assertEqual(self.report.exit_code, rz.EXIT_OK)

    def test_compared_count_matches_the_fixture(self) -> None:
        """计数断言（配方 4）—— 专防「diff 空得非常好看但根本没比」。"""
        diff = self.report.comparison.flag_diff
        self.assertIsNotNone(diff)
        self.assertEqual(len(self.report.facts.official_flags), EXPECTED_OFFICIAL_FLAGS)
        self.assertEqual(diff.compared_count, EXPECTED_COMPARED_FLAGS)
        self.assertEqual(len(diff.same), EXPECTED_COMPARED_FLAGS, "clean 时全部条目都该落进 same")
        self.assertEqual(diff.ignored, EXPECTED_IGNORED)
        self.assertEqual(diff.compared_count, EXPECTED_OFFICIAL_FLAGS - 2)

    def test_ignore_is_exact_match_not_prefix(self) -> None:
        """忽略表按精确名匹配 —— `--sparam` 在忽略表里**不**存在，
        而 `--sparamImpedance` 必须真的被比过（MVP 那个真 bug 的回归）。"""
        diff = self.report.comparison.flag_diff
        self.assertNotIn("--sparam", diff.ignored)
        self.assertNotIn("--sparamImpedance", diff.ignored)
        self.assertIn("--sparam", diff.same)
        self.assertIn("--sparamImpedance", diff.same)
        self.assertNotIn("--sparam", cmd.DEFAULT_DIFF_IGNORE)

    def test_self_proving_split_is_reported(self) -> None:
        """比对结果被诚实地分成两堆，两堆加起来 == 参与比较的条数。"""
        comparison = self.report.comparison
        self.assertEqual(comparison.self_proving, EXPECTED_SELF_PROVING)
        self.assertEqual(len(comparison.self_proving), 4)
        self.assertEqual(len(comparison.independent), EXPECTED_INDEPENDENT)
        self.assertEqual(
            len(comparison.self_proving) + len(comparison.independent),
            comparison.flag_diff.compared_count,
            "两堆必须正好把参与比较的 flag 分完，一条不重不漏",
        )
        # `--parallel` 也落在默认表层，但取值来自**另一个文件**（remote 脚本的 -R cpu=）
        # ⇒ 它必须算独立验证，不许被误判成自证。
        self.assertIn("--parallel", comparison.independent)

    def test_port_order_matches(self) -> None:
        """D1b 在真实数据上重跑一遍：`--all` 的预测顺序 == 官方 `-p` 顺序，逐位。"""
        port = self.report.comparison.port_diff
        self.assertIsNotNone(port)
        self.assertTrue(port.matched)
        self.assertIsNone(port.first_mismatch_index)
        self.assertEqual(port.compared_count, EXPECTED_PORT_COUNT)
        self.assertEqual(len(self.report.facts.official_port_spec.mapping), EXPECTED_PORT_COUNT)

    def test_grounded_ports_are_warned_about(self) -> None:
        """官方用 `-i` 挑了 4/5 个 signal port ⇒ `--all` 表达不了接地端口，必须报警告。"""
        warnings = self.report.comparison.warnings
        self.assertEqual(len(warnings), 1)
        self.assertIn("-i", warnings[0])
        self.assertIn("--all", warnings[0])

    def test_gdsout_round_trip_and_fallback_counts(self) -> None:
        """gdsout 两条比对的计数（24−7 与 D1c 的 8）。"""
        comparison = self.report.comparison
        self.assertEqual(comparison.gdsout_diff.compared_count, EXPECTED_GDSOUT_COMPARED)
        self.assertTrue(comparison.gdsout_diff.clean)
        self.assertEqual(comparison.fallback_diff.compared_count, EXPECTED_FALLBACK_COMPARED)
        self.assertTrue(comparison.fallback_diff.clean)

    # ---- 报告文本 ----

    def test_report_text_contains_the_argv_and_the_dirs(self) -> None:
        """BRIEF §12 要的两样东西（完整 argv + 落地目录）必须真的出现在输出里。"""
        text = rz.format_report(self.report)
        self.assertIn(" ".join(GOLDEN_ARGV), text)
        self.assertIn(f"{BATCH_DIR}/runs/{DESIGN_KEY}/base/typical_-40_0", text)
        self.assertIn(f"{BATCH_DIR}/gdsout/{DESIGN_KEY}.gdsout_setup", text)
        self.assertIn("strmout -templateFile", text)

    def test_report_text_states_the_conclusion_and_next_step(self) -> None:
        """末尾要有一眼能看懂的结论 + 明确的下一步 + 「没写任何文件」的交代。"""
        text = rz.format_report(self.report)
        self.assertIn("[5/5] 结论", text)
        self.assertIn("✅ 一致。", text)
        self.assertIn("下一步：", text)
        self.assertIn("这趟没有写任何文件", text)
        self.assertIn("永远不提交任何 job", text)

    def test_report_text_discloses_the_self_proving_part(self) -> None:
        """诚实交代必须印在报告里 —— 不然读的人会高估这趟比对。"""
        text = rz.format_report(self.report)
        self.assertIn("结构上必然相等", text)
        self.assertIn("真独立验证", text)

    def test_write_ledger_has_no_duplicates(self) -> None:
        """落点清单去过重（同一个路径登记两次只会让人以为要写两遍）。"""
        paths = [path for path, _ in self.report.ledger.entries]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertIn(f"{BATCH_DIR}/batch.json", paths)
        self.assertIn(f"{BATCH_DIR}/cdswork/cds.lib", paths)


# ---------------------------------------------------------------------------
# argv[0] 从哪来 —— 全靠注入，不读真实环境
# ---------------------------------------------------------------------------


class ToolNameInjection(unittest.TestCase):
    """★ 这份文件全部 argv 期望值的地基，钉成一条会红的测试。

    2026-08-18 复审打回的就是这里：`GOLDEN_ARGV[0]` 那格原本写死 `"ewave"`，
    而它在本机成立**只是因为本机 PATH 上没有 eWave**。红区 `ma ewave/…` 之后
    `core.discover.find_tool` 解析得出来（`shutil.which` 命中，或
    `EWAVE_BIN`/`EWAVE_ABS`/`STRMOUT_BIN`/`STRMOUT_ABS` 命中 —— `EWAVE_ABS` 正是
    已进 git 的 `mvp/redzone/cfg.sh` 在用的名字），`argv[0]` 就变成绝对路径
    ⇒ 那几条断言在**唯一真正重要的那台机器**上是红的，而我们在这里看不见。

    下面这几条走同一条报告构造路径（`_report`），**只改注入的 `env`**：
    解析不出来 ⇒ 占位名；解析得出来 ⇒ 那条绝对路径。全靠注入 ⇒ 任何机器上结果相同。
    """

    def test_argv0_is_the_placeholder_when_no_tool_resolves(self) -> None:
        """注入一个空环境（没 PATH、没四个兜底变量）⇒ 两个 argv[0] 都是通用程序名。"""
        report = _report(OFFDIR, env=NO_TOOLS_ENV)
        self.assertEqual(report.stage_two[0].plan.argv[0], rz.GENERIC_EWAVE_PROGRAM)
        self.assertEqual(report.stage_one[0].plan.argv[0], rz.GENERIC_STRMOUT_PROGRAM)
        self.assertEqual(report.facts.ewave_bin, "ewave")
        self.assertEqual(report.facts.strmout_bin, "strmout")

    def test_argv0_is_the_absolute_path_when_the_tool_resolves(self) -> None:
        """注入 `EWAVE_ABS`/`STRMOUT_ABS`（红区常态）⇒ 两个 argv[0] 都是那条绝对路径。

        整趟报告仍然跑通、结论仍然一致 —— 工具解析这件事只该动 argv[0] 那一格。
        """
        report = _report(OFFDIR, env=TOOLS_RESOLVED_ENV)
        self.assertEqual(report.stage_two[0].plan.argv[0], FAKE_EWAVE_ABS)
        self.assertEqual(report.stage_one[0].plan.argv[0], FAKE_STRMOUT_ABS)
        self.assertEqual(report.facts.ewave_bin, FAKE_EWAVE_ABS)
        self.assertEqual(report.comparison.status, "clean", report.comparison.reason)
        self.assertEqual(report.exit_code, rz.EXIT_OK)
        # 报告文本里也是这条绝对路径（BRIEF §12 要的"完整 argv"）。
        self.assertIn(FAKE_EWAVE_ABS + " --all", rz.format_report(report))

    def test_tool_resolution_changes_argv0_and_nothing_else(self) -> None:
        """计数/内容锚：两种情形的 argv 除了第 0 项**逐项相同**，且都等于 `GOLDEN_ARGV[1:]`。

        没有这一条，上面两条可以在"整条命令其实塌了"的情况下照样绿。
        """
        placeholder = _report(OFFDIR, env=NO_TOOLS_ENV).stage_two[0].plan.argv
        resolved = _report(OFFDIR, env=TOOLS_RESOLVED_ENV).stage_two[0].plan.argv
        self.assertEqual(len(placeholder), len(resolved))
        self.assertEqual(len(placeholder), EXPECTED_ARGV_LEN)
        self.assertNotEqual(placeholder[0], resolved[0])
        self.assertEqual(list(placeholder)[1:], list(resolved)[1:])
        self.assertEqual(list(placeholder)[1:], list(GOLDEN_ARGV)[1:])

    def test_environment_wins_only_when_nothing_is_injected_negative(self) -> None:
        """★ 洞的机器判据（防空过 + 反向）：同一份 OFFDIR，只改「注入还是不注入」。

        * **不注入** ⇒ 真实环境里那个 `EWAVE_ABS` 真的会赢 —— 这就是红区上发生的事，
          把它演出来，证明上面三条不是在测一件本来就不会发生的事；
        * **注入** ⇒ 环境一个字都不看，argv 逐项等于 `GOLDEN_ARGV`。

        两边共用 `OFFDIR` 和落点，差别只有一个 `env` 入参。
        """
        polluted = dict(BLANK_TOOL_ENVIRON)
        polluted["EWAVE_ABS"] = LEAKED_EWAVE_ABS
        polluted["STRMOUT_ABS"] = LEAKED_STRMOUT_ABS
        with mock.patch.dict(os.environ, polluted):
            leaked = rz.build_report(
                str(OFFDIR), batch_root=BATCH_ROOT, batch_name=BATCH_NAME
            )
            injected = _report(OFFDIR)
        self.assertEqual(
            leaked.stage_two[0].plan.argv[0],
            LEAKED_EWAVE_ABS,
            "不注入时环境说了算 —— 这正是被打回的那个洞（红区 PATH 上有 ewave 时同款）",
        )
        self.assertEqual(leaked.stage_one[0].plan.argv[0], LEAKED_STRMOUT_ABS)
        self.assertEqual(injected.stage_two[0].plan.argv[0], rz.GENERIC_EWAVE_PROGRAM)
        self.assertEqual(list(injected.stage_two[0].plan.argv), list(GOLDEN_ARGV))


# ---------------------------------------------------------------------------
# 反向：故意改坏一个值，比对必须报出来
# ---------------------------------------------------------------------------


class EndToEndNegative(unittest.TestCase):
    """每条都走 `_report()` 这同一条构造路径，只改「输入是哪份目录」。"""

    def _mutated_report(self, old: str, new: str):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        offdir = _copy_offdir(tmp)
        _mutate_run_script(offdir, old, new)
        return _report(offdir)

    def test_builtin_default_mismatch_is_reported_negative(self) -> None:
        """把官方的 `--viaMergeSpace=0.4` 改成 0.8 → 我们仍给 0.4（源码内置）⇒ 必须报差异。

        选它而不是选 `--viaMode` 是有讲究的：`--viaMode` 的取值是**学**来的，
        改了官方脚本两边一起变、永远绿（见 `SelfProvingHonesty`）。
        `--viaMergeSpace` 被 mesh 轴掌管 ⇒ 学不进默认表 ⇒ 取值来自源码常量，
        这才是一次真的比较。
        """
        report = self._mutated_report("--viaMergeSpace=0.4", "--viaMergeSpace=0.8")
        diff = report.comparison.flag_diff
        self.assertEqual(report.comparison.status, "diff")
        self.assertEqual(report.exit_code, rz.EXIT_DIFF)
        self.assertEqual([d.flag for d in diff.differing], ["--viaMergeSpace"])
        self.assertEqual(diff.differing[0].actual, "0.4")
        self.assertEqual(diff.differing[0].expected, "0.8")
        # 计数不变：只是某一条的取值不同，参与比较的条数还是那么多。
        self.assertEqual(diff.compared_count, EXPECTED_COMPARED_FLAGS)
        self.assertIn("--viaMergeSpace", report.comparison.independent)

    def test_cross_file_parallel_mismatch_is_reported_negative(self) -> None:
        """把官方的 `--parallel=20` 改成 40 → 我们仍按 remote 脚本的 `-R "cpu=20"` 给 20。

        这是**跨文件**校验：两个值来自两个不同的文件，对不上说明站点把
        `--parallel` 和 `cpu=` 解耦了（BRIEF §6 那个 1× / 2× 的坑）。
        """
        report = self._mutated_report("--parallel=20", "--parallel=40")
        diff = report.comparison.flag_diff
        self.assertEqual(report.comparison.status, "diff")
        self.assertEqual([d.flag for d in diff.differing], ["--parallel"])
        self.assertEqual((diff.differing[0].actual, diff.differing[0].expected), ("20", "40"))

    def test_missing_official_flag_is_reported_negative(self) -> None:
        """把官方的 `--sparamImpedance=50` 整个删掉 → 我们多给了一条 ⇒ 报 only_actual。

        ⚠️ 计数**不变**（还是 20）：`compared_count` 数的是两边键的**并集**，
        而我们这边仍然有 `--sparamImpedance`（`core.cmd.BUILTIN_DEFAULT_FLAGS` 兜底给的）
        ⇒ 并集没缩。顺带说明了一件事：官方那边删掉一个 flag 不会让这条比对"少比一项"，
        它会变成一条 `only_actual` 差异被报出来 —— 这正是我们要的。
        """
        report = self._mutated_report(" --sparamImpedance=50", "")
        diff = report.comparison.flag_diff
        self.assertEqual(report.comparison.status, "diff")
        self.assertEqual(diff.only_actual, ("--sparamImpedance",))
        self.assertEqual(len(report.facts.official_flags), EXPECTED_OFFICIAL_FLAGS - 1)
        self.assertEqual(diff.compared_count, EXPECTED_COMPARED_FLAGS)

    def test_swapped_ports_are_reported_negative(self) -> None:
        """只把两个 pin 的**位置**对调 → 集合完全相同，只有逐位比对抓得到。

        这类错最要命：`.sNp` 里看不出来（Touchstone 只按 P00x 排、名字被丢掉），
        下游拿去做批量对比会得到"看起来很像"的错误结论。
        """
        report = self._mutated_report(
            "-p 'P001=MY_INN' -p 'P002=MY_INP'", "-p 'P001=MY_INP' -p 'P002=MY_INN'"
        )
        port = report.comparison.port_diff
        self.assertEqual(report.comparison.status, "diff")
        self.assertEqual(report.exit_code, rz.EXIT_DIFF)
        self.assertEqual(port.first_mismatch_index, 1)
        self.assertFalse(port.matched)
        self.assertEqual(port.only_actual, (), "pin 集合没变 —— 差集是空的，只有位置抓得到")
        self.assertEqual(port.only_expected, ())
        self.assertEqual(port.compared_count, EXPECTED_PORT_COUNT)

    def test_gdsout_critical_field_change_shows_up_in_fallback_negative(self) -> None:
        """把官方 `gdsout_setup` 的 `maxVertices 200` 改成 500 → 兜底模板对照必须报出来。

        往返自检那一路**不会**报（模板就是从这份文件模板化来的 —— 这正是它被标成
        "往返、不是独立验证"的原因）；能抓到它的是「源码常量 vs 这个站点」那一路。
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        offdir = _copy_offdir(tmp)
        setup = os.path.join(offdir, "gdsout_setup")
        text = Path(setup).read_text(encoding="utf-8")
        self.assertIn("maxVertices\t\t200", text)
        Path(setup).write_text(
            text.replace("maxVertices\t\t200", "maxVertices\t\t500", 1), encoding="utf-8", newline=""
        )
        report = _report(offdir)
        fallback = report.comparison.fallback_diff
        self.assertEqual([d.flag for d in fallback.differing], ["maxVertices"])
        self.assertEqual(fallback.compared_count, EXPECTED_FALLBACK_COMPARED)
        self.assertTrue(report.comparison.gdsout_diff.clean, "往返那一路看不见它 —— 这是有意的")

    def test_missing_run_script_degrades_to_unavailable(self) -> None:
        """目录里没有 `run_ewave_*.sh` → 没有基准 ⇒ 退 3，但 argv 和落地目录照样打印。"""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        offdir = _copy_offdir(tmp)
        os.remove(os.path.join(offdir, RUN_SCRIPT_NAME))
        report = _report(offdir)
        self.assertEqual(report.comparison.status, "unavailable")
        self.assertEqual(report.exit_code, rz.EXIT_NO_BASELINE)
        self.assertEqual(len(report.stage_two), 1, "没有官方命令也要照样规划出 run")
        self.assertEqual(len(report.stage_one), 1)
        text = rz.format_report(report)
        self.assertIn("没能比对", text)


# ---------------------------------------------------------------------------
# 诚实：哪一部分的比对是"结构上必然相等"的
# ---------------------------------------------------------------------------


class SelfProvingHonesty(unittest.TestCase):
    """把"自证"这件事从一句话变成一条会红的测试。"""

    def test_learned_flag_mutation_is_structurally_invisible(self) -> None:
        """把官方的 `--viaMode=1` 改成 2 → 比对**依然全绿**，因为我们的取值也是学它的。

        这不是 bug，是这条比对的固有边界。测试把它钉死，是为了让报告里那句
        「其中 N 条结构上必然相等，不算独立验证」有据可查 —— 而不是一句自谦。
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        offdir = _copy_offdir(tmp)
        _mutate_run_script(offdir, "--viaMode=1", "--viaMode=2")
        report = _report(offdir)
        self.assertEqual(report.comparison.status, "clean", "学来的值改了两边一起变 —— 必然还是绿的")
        self.assertIn("--viaMode", report.comparison.self_proving)
        self.assertIn("--viaMode=2", report.stage_two[0].plan.argv, "而且我们真的跟着改了")

    def test_independent_flags_do_not_include_the_learned_ones(self) -> None:
        """两堆不许有交集（有交集就说明分类逻辑写反了，报出来的含金量是假的）。"""
        report = _report(OFFDIR)
        comparison = report.comparison
        self.assertEqual(set(comparison.self_proving) & set(comparison.independent), set())


# ---------------------------------------------------------------------------
# 只读守卫
# ---------------------------------------------------------------------------


class ReadOnlyGuard(unittest.TestCase):
    """CLAUDE.md 硬约束 4：设计师的 spine 只读，写一个字节都是违约。"""

    def _spine_offdir(self) -> tuple[str, str]:
        """把合成 fixture 放进一个真的叫 `ewave_simulation` 的目录里 —— 红区的真实形状。"""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        spine = os.path.join(tmp, "wa", "ewave_simulation")
        os.makedirs(spine)
        offdir = _copy_offdir(spine, name="MY_LIB_MY_CELL_layout_em")
        return tmp, offdir

    def test_nothing_is_written_when_offdir_lives_in_the_spine(self) -> None:
        """★ 跑之前记下整棵树的文件集 + 大小 + mtime，跑之后逐个比对相同。"""
        tmp, offdir = self._spine_offdir()
        before = _snapshot(tmp)
        self.assertTrue(before, "快照是空的 —— 那这条测试什么都没验（防空过）")

        buffer = io.StringIO()
        with _no_tools_environ(), contextlib.redirect_stdout(buffer):
            code = rz.main(["--offdir", offdir, "--batch-root", os.path.join(tmp, "batches")])

        self.assertEqual(code, rz.EXIT_OK)
        self.assertEqual(_snapshot(tmp), before, "OFFDIR 所在的那棵树被动过了 —— 违反硬约束 4")
        self.assertFalse(
            os.path.exists(os.path.join(tmp, "batches")), "落点目录被建出来了 —— dry-run 不该建目录"
        )
        # 程序名这一格由 `_no_tools_environ()` 定死成占位名 ⇒ 与本机 PATH 无关。
        # 断言仍然是"整条命令的开头"，没有放宽成只找 `--all`。
        self.assertIn("ewave --all", buffer.getvalue())

    def test_nothing_is_created_under_the_batch_root(self) -> None:
        """落点指向一个**不存在**的路径，跑完它必须还是不存在。"""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        target = os.path.join(tmp, "no", "such", "place")
        _report(OFFDIR, batch_root=target)
        self.assertFalse(os.path.exists(os.path.join(tmp, "no")))

    def test_batch_root_inside_the_spine_is_refused_negative(self) -> None:
        """落点选在 spine 里 → 当场拒绝（而且是在打印任何命令**之前**）。"""
        tmp, offdir = self._spine_offdir()
        buffer, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(err):
            code = rz.main(
                ["--offdir", offdir, "--batch-root", os.path.join(tmp, "wa", "ewave_simulation", "b")]
            )
        self.assertEqual(code, rz.EXIT_ERROR)
        self.assertEqual(buffer.getvalue(), "", "被拒绝时不该打印任何命令 —— 免得有人照着手工跑")
        self.assertIn("ewave_simulation", err.getvalue())

    def test_batch_root_inside_the_offdir_is_refused_negative(self) -> None:
        """落点选在 OFFDIR 里 → 同样拒绝（OFFDIR 是只读输入）。"""
        with self.assertRaises(rz.ReadOnlyViolation):
            _report(OFFDIR, batch_root=str(OFFDIR / "sub"))

    def test_spine_match_is_a_whole_component_not_a_substring(self) -> None:
        """过滤器本身的测试（配方 4）：只有**整层目录名**叫 `ewave_simulation` 才算。

        写成子串匹配的话 `ewave_simulations_archive` / `my_ewave_simulation_backup`
        这种正常目录会被误伤，用户会被一条看不懂的拒绝挡住而落点其实没问题。
        """
        self.assertTrue(rz._in_spine("/wa/ewave_simulation/lib_cell_view"))
        self.assertTrue(rz._in_spine("/wa/ewave_simulation"))
        self.assertTrue(rz._in_spine("C:\\wa\\ewave_simulation\\x"), "Windows 分隔符也要认")
        self.assertFalse(rz._in_spine("/wa/ewave_simulations_archive/x"))
        self.assertFalse(rz._in_spine("/wa/my_ewave_simulation_backup/x"))
        self.assertFalse(rz._in_spine("/wa/ewave_batches/x"))

    def test_ledger_validates_even_when_deduplicating(self) -> None:
        """去重不许把闸门跳过去：同一个非法路径登记两次，第二次也要抛。"""
        ledger = rz.WriteLedger(offdir="/some/offdir")
        ledger.record("/ok/a", "第一次")
        ledger.record("/ok/a", "第二次（重复）")
        self.assertEqual(len(ledger.entries), 1)
        for _ in range(2):
            with self.assertRaises(rz.ReadOnlyViolation):
                ledger.record("/some/offdir/x", "落在 offdir 里")


# ---------------------------------------------------------------------------
# spec 模式 + CLI
# ---------------------------------------------------------------------------


class SpecMode(unittest.TestCase):
    """给了 spec 时展开整个矩阵，但比对基准仍然是"官方那一格"。"""

    SPEC = """{
      "batch_name": "m",
      "designs": [{"library": "MY_LIB", "cell": "MY_CELL", "view": "layout_em"}],
      "axes": {"corner": ["typical"], "temperature": ["-40.0", "125.0"],
               "equalCurrent": ["on", "off"]}
    }"""
    """JSON 而不是 YAML：本机没装 PyYAML（红区装了 6.0.1），JSON 是成文的退路。
    取值抄自合成 fixture 的 gdsout_setup / run 脚本。"""

    def _spec_report(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "spec.json")
        Path(path).write_text(self.SPEC, encoding="utf-8")
        return _report(OFFDIR, spec_path=path)

    def test_matrix_expands_and_dirs_do_not_collide(self) -> None:
        """2 温度 × 2 equalCurrent = 4 个 run，四个落点互不相同（D2：静默覆盖是要消灭的坑）。"""
        report = self._spec_report()
        self.assertEqual(len(report.stage_two), 4)
        ewave_dirs = [item.paths.ewave_dir for item in report.stage_two]
        self.assertEqual(len(set(ewave_dirs)), 4)
        cmd_files = [item.paths.cmd_sh for item in report.stage_two]
        self.assertEqual(len(set(cmd_files)), 4, "同一个 run_dir 下的命令留档不许互相覆盖")

    def test_toggle_axis_off_cancels_the_default(self) -> None:
        """`equalCurrent: off` 必须把默认表里的 `--equalCurrent` **抵消掉**（INTERFACES 契约 1）。

        目录名说 off 而命令行说 on，正是本工具要消灭的那个坑。
        """
        report = self._spec_report()
        by_slug = {item.run.axes_slug: item.plan.argv for item in report.stage_two}
        self.assertIn("--equalCurrent", by_slug["eqI-on"])
        self.assertNotIn("--equalCurrent", by_slug["eqI-off"])

    def test_per_design_official_dir_gets_its_own_coordinates(self) -> None:
        """spec 里给某个 design 单独指了官方目录 → 它的默认表要从**它自己**那份目录学。

        坐标是 per-design 的（每个 design 有自己的端口集合、自己的 top cell、
        甚至可能自己的 ptxt）。这里把第二份目录的 `--viaMode` 改成 2，
        两个 design 的 argv 必须一个 1 一个 2 —— 混用一份坐标的话两边都会是 1。
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        other = _copy_offdir(tmp, name="other")
        _mutate_run_script(other, "--viaMode=1", "--viaMode=2")
        spec = (
            '{"batch_name": "m", "designs": ['
            '{"library": "MY_LIB", "cell": "MY_CELL", "view": "layout_em"},'
            '{"library": "MY_LIB", "cell": "MY_CELL2", "view": "layout_em",'
            f' "official_run_dir": {json.dumps(other.replace(os.sep, "/"))}}}'
            '], "axes": {"corner": ["typical"], "temperature": ["-40.0"]}}'
        )
        path = os.path.join(tmp, "spec.json")
        Path(path).write_text(spec, encoding="utf-8")
        report = _report(OFFDIR, spec_path=path)

        by_cell = {
            item.run.design_key: item.plan.argv for item in report.stage_two
        }
        self.assertEqual(len(by_cell), 2)
        self.assertIn("--viaMode=1", by_cell["MY_LIB_MY_CELL_layout_em"])
        self.assertIn("--viaMode=2", by_cell["MY_LIB_MY_CELL2_layout_em"])
        # 比对基准仍然是 --offdir 那一份（对照 run 用的是它的 design 三元组）。
        self.assertEqual(report.comparison.reference_run_id, f"{DESIGN_KEY}/base/typical_-40_0")

    def test_comparison_still_uses_the_official_grid_point(self) -> None:
        """比对基准与 spec 无关 —— 永远是"官方那一格"，否则比出来的差异没有意义。"""
        report = self._spec_report()
        self.assertEqual(report.comparison.status, "clean")
        self.assertEqual(report.comparison.reference_run_id, f"{DESIGN_KEY}/base/typical_-40_0")
        self.assertEqual(report.comparison.flag_diff.compared_count, EXPECTED_COMPARED_FLAGS)


class Cli(unittest.TestCase):
    def test_offdir_is_required(self) -> None:
        parser = rz.build_parser()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args([])

    def test_bad_offdir_exits_one_with_a_next_step(self) -> None:
        """目录不对 → 退 1，并且消息里要有"下一步怎么办"（红区没人能来问我们）。"""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = rz.main(["--offdir", os.path.join(tmp, "nope")])
        self.assertEqual(code, rz.EXIT_ERROR)
        self.assertIn("下一步", err.getvalue())

    def test_limit_truncates_the_detailed_listing(self) -> None:
        """`--limit N` 只影响详细打印，run 的条数不变（截断的那些仍然列 id 和落点）。"""
        report = _report(OFFDIR, limit=1)
        text = rz.format_report(report)
        self.assertEqual(len(report.stage_two), 1)
        self.assertIn("run 1/1", text)

    def test_survives_ascii_only_stdout(self) -> None:
        """★ 红区的 `LANG=C` 陷阱：ASCII-only 的 stdout 下整条命令必须照样退 0。

        判据是机器可判的（`PYTHONIOENCODING=ascii`，与 `scripts/check.sh` 第 4 步同款）。
        不做 `ascii_safe_stdio()` 的话，本模块输出里第一个中文字就让进程 `UnicodeEncodeError`
        退 1 —— 而那是**开发机上全绿、只在红区发作**的一类坑。

        顺带断言 stdout 里真的出现了 `?`（中文被降级的痕迹）：没有它就说明这一跑
        其实没走 ASCII 分支，这条测试是空过的。
        """
        stdout = self._ascii_subprocess_stdout(tools_resolve=True)
        self.assertIn(FAKE_EWAVE_ABS.encode("ascii") + b" --all", stdout)
        self.assertIn(FAKE_STRMOUT_ABS.encode("ascii") + b" -templateFile", stdout)

    def test_survives_ascii_only_stdout_with_placeholder_names(self) -> None:
        """同一条子进程路径，走**另一个**分支：工具一个都解析不出来 ⇒ 占位程序名。

        为什么两条都要有子进程级覆盖：上一条走的是红区常态（`ma` 之后工具在 PATH 上，
        argv[0] 是绝对路径），这条走的是本机/纯净容器常态（什么都找不到，argv[0] 是通用名）。
        只测一条的话，另一条分支在真正的 CLI 入口上就没有机器判据 ——
        而 `main()` 是冻结签名、没有注入口，正是最容易悄悄坏掉的那条路。
        """
        stdout = self._ascii_subprocess_stdout(tools_resolve=False)
        self.assertIn(rz.GENERIC_EWAVE_PROGRAM.encode("ascii") + b" --all", stdout)
        self.assertIn(rz.GENERIC_STRMOUT_PROGRAM.encode("ascii") + b" -templateFile", stdout)

    def _ascii_subprocess_stdout(self, *, tools_resolve: bool) -> bytes:
        """在 ASCII-only 的子进程里跑一遍 CLI，返回 stdout。

        ⚠️ 环境必须**从 `BLANK_TOOL_ENVIRON` 起搭，不能从 `os.environ` 起搭**。
        2026-08-18 夜跑 P2 复审抓到的真洞就在这儿：原来写的是
        `dict(os.environ, PATH="", EWAVE_ABS=…)` —— 清了 `PATH` 和 `*_ABS`，
        **却把父进程的 `EWAVE_BIN`/`STRMOUT_BIN` 原样继承了下来**，而 `find_tool`
        先看 `*_BIN`。于是在一台设了 `EWAVE_BIN` 的机器上，子进程解析出的是
        那台机器的路径，断言当场失败。
        判据：`EWAVE_BIN=/other/place/ewave STRMOUT_BIN=/other/place/strmout
        python -m unittest discover -s tests -t .` 必须仍然 OK。
        """
        env = {
            k: v
            for k, v in os.environ.items()
            # 只留跑得起 python 的那几格（Windows 上缺 SYSTEMROOT 会起不来），
            # 其余一律不继承 —— 继承什么就等于让那台机器替测试做决定。
            if k in ("SYSTEMROOT", "SystemRoot", "COMSPEC", "TEMP", "TMP", "PYTHONHOME")
        }
        env.update(BLANK_TOOL_ENVIRON)
        env["PYTHONIOENCODING"] = "ascii"
        if tools_resolve:
            # 空 PATH ⇒ `shutil.which` 一律 None ⇒ 走 `*_ABS` 兜底 ⇒ 程序名确定。
            env["EWAVE_ABS"] = FAKE_EWAVE_ABS
            env["STRMOUT_ABS"] = FAKE_STRMOUT_ABS
        proc = subprocess.run(
            [sys.executable, "-m", "ewave_batch.redzone_dryrun", "--offdir", str(OFFDIR)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
        )
        self.assertEqual(proc.returncode, rz.EXIT_OK, proc.stderr.decode("ascii", "replace"))
        self.assertIn(
            b"?", proc.stdout, "没看到降级痕迹 —— 这一跑没走 ASCII 分支，测试是空过的"
        )
        self.assertFalse(
            (ROOT / "ewave_batches").exists(), "默认落点被建出来了 —— dry-run 不该建目录"
        )
        return proc.stdout

    def test_exit_codes_are_distinct(self) -> None:
        """四个退出码互不相同 —— 文档和脚本都按它们判。"""
        codes = (rz.EXIT_OK, rz.EXIT_ERROR, rz.EXIT_DIFF, rz.EXIT_NO_BASELINE)
        self.assertEqual(len(set(codes)), 4)
        self.assertEqual(codes, (0, 1, 2, 3))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
