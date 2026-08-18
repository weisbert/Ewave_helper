"""`tools.ewave` 的测试 —— 阶段 2 的**薄封装**面。

这个模块薄，但它薄的**方式**是有风险的：端口渲染和程序名解析各有一份"真身"在别处
（`core.cmd._render_ports` / `SiteFacts.ewave_bin`），封装层最容易犯的错就是"再做一遍"——
端口 argv 接两次、`--all` 出现两次。eWave 多半会很高兴地照单全收，
而 `.sNp` 里根本看不出端口被重复声明过（端口映射只存在于命令行，BRIEF §5）。
⇒ 这份文件里**计数断言**的密度比别处高。

四条防自证配方（`docs/OVERNIGHT.md`）在这份文件里的落点：

1. **关键测试** = `test_render_ports_*`（端口 argv 等于期望值）、
   `test_build_ewave_plan_argv_golden`（整条 argv 等于期望值）；
2. **期望值来源**：端口部分优先用 `tests/fixtures/production_cmd.local.json`
   （人从真实生产命令抽的，含站点坐标 ⇒ 不进 git，缺失时优雅 skip）；
   不依赖 fixture 的那条 golden argv 是**手写**的，逐条注明出处
   （BRIEF §6「已知的生产默认值」+ §5 机制层 + `render_flags` 的排序规则）；
3. **反向验证**：每条关键测试配一条 `_negative`，与正向共用输入构造路径
   （`_ctx()` / `_run()` / `_port_spec()`），只改坏一个值；
4. **计数断言**：`--all` 恰好一次、`-p` 恰好 N 次、`FlagDiff.compared_count`、
   `PortDiff.compared_count`。

🚨 本文件零站点标识符：不依赖 fixture 的地方全用显式假值（`TESTCELL` / `/tmp/...` /
`pinA`）；依赖 fixture 的地方**只从 fixture 读**，一个真实取值都不写进源码。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from ewave_batch.core import cmd, template
from ewave_batch.model import (
    BatchOptions,
    CommandPlan,
    Design,
    PlanContext,
    PortMode,
    PortSpec,
    Run,
    SiteFacts,
    Stage,
    ToolMissingError,
)
from ewave_batch.tools import ewave

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "production_cmd.local.json"

SKIP_REASON = (
    "本机没有 tests/fixtures/production_cmd.local.json —— 那是人从真实生产命令抽出来的 "
    "golden 基准，含站点坐标所以不进 git（公开克隆者看到这条 skip 是正常的；"
    "红区/本机开发请把它放回 tests/fixtures/）"
)


def _load_fixture() -> dict | None:
    if not FIXTURE_PATH.exists():
        return None
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


FIXTURE = _load_fixture()

# --------------------------------------------------------------------------
# 手写的假值
# --------------------------------------------------------------------------

FAKE_EWAVE_BIN = "/tmp/fakebin/ewave"
FAKE_BATCH_DIR = "/tmp/ewb"
FAKE_WORK_DIR = "/tmp/ewb/runs/d0/base"
FAKE_CELL = "TESTCELL"
FAKE_KEY = "000000"
"""`--key` 的假值 —— 抄自 `tests/fixtures/offdir_synthetic/run_ewave_typical_-40_0.sh`
（那份合成官方目录里的值全是假的，README 逐条记了）。

🚨 真实的 key 是站点坐标，源码和测试里都不许出现（硬约束 1b）。这里要的只是
"喂进去什么就该原样出现在 argv 里"，用什么假值都行。
"""

HAND_PORT_SPEC = PortSpec(
    mode=PortMode.EXPLICIT,
    mapping=(("P000", "pinA"), ("P001", "pinB"), ("P002", "pinC")),
    signal_ports=("P000", "P001", "P002"),
)
"""手写的显式端口 —— pin 名是本文件自造的（`pinA`…），与任何真实 design 无关。"""

HAND_PORT_ARGV = [
    "-p", "P000=pinA",
    "-p", "P001=pinB",
    "-p", "P002=pinC",
    "-i", "P000",
    "-i", "P001",
    "-i", "P002",
]
"""**期望值：手写。** 形状的三处出处：

* `-p P00x=<pin>` 一对一对给 —— BRIEF §5「端口映射不在 .sNp 里，在命令行里」，
  官方那条生产命令就是这么写的；
* `-i` 后面跟的是**端口号**（`P000`）而不是 pin 名 —— 由
  `tests/fixtures/production_cmd.local.json` 的 `signal_ports` 字段坐实
  （那 17 个值全是 `P0xx` 形状）。D1b 引的 help 原文也是这个语义：
  "`-i` 在 `-p` 集合里挑 signal port，其余接地"，挑的是端口不是 pin；
* 先把 `-p` 全给完再给 `-i` —— 官方命令的顺序，且 `-i` 引用的端口号必须已经声明过。
"""


def _design(*, port_spec: PortSpec | None = None, resources: str = "") -> Design:
    return Design(
        library="TESTLIB",
        cell=FAKE_CELL,
        view="testview",
        key="d0",
        resources=resources,
        port_spec=port_spec,
    )


def _run() -> Run:
    return Run(run_id="d0/base/typ_25", design_key="d0", work_dir=FAKE_WORK_DIR)


def _ctx(
    *,
    ewave_bin: str = FAKE_EWAVE_BIN,
    port_spec: PortSpec | None = None,
    resources: str = "",
    key: str = "",
) -> PlanContext:
    """**正反两向共用的**唯一一条输入构造路径。反向测试只改其中一个入参。

    刻意保持"最小上下文"：没有轴、没有学来的默认表、没有 Extra flags ——
    于是合并结果 = 内置默认（BRIEF §6）+ 机制层（§5），**可以逐条手写出来**。
    """
    return PlanContext(
        design=_design(port_spec=port_spec, resources=resources),
        facts=SiteFacts(ewave_bin=ewave_bin, key=key),
        options=BatchOptions(),
        batch_dir=FAKE_BATCH_DIR,
    )


# --------------------------------------------------------------------------
# 关键测试：端口 argv
# --------------------------------------------------------------------------


class RenderPortsHandWritten(unittest.TestCase):
    def test_render_ports_explicit(self) -> None:
        self.assertEqual(ewave.render_ports(HAND_PORT_SPEC), HAND_PORT_ARGV)

    def test_render_ports_explicit_negative_swapped_pins(self) -> None:
        """同一条构造路径，只把两个 pin 换个位置 —— 断言比较逻辑报告了**位置**。

        为什么必须报位置而不只是差集：pin 集合完全一样、只是顺序变了，
        Touchstone 里看不出任何异常，`.sNp` 却整份错位（BRIEF §5「`--all` 的代价」）。
        """
        swapped = PortSpec(
            mode=PortMode.EXPLICIT,
            mapping=(("P000", "pinB"), ("P001", "pinA"), ("P002", "pinC")),
            signal_ports=HAND_PORT_SPEC.signal_ports,
        )
        argv = ewave.render_ports(swapped)
        self.assertNotEqual(argv, HAND_PORT_ARGV)
        self.assertEqual(argv[1], "P000=pinB")

        diff = cmd.diff_ports(swapped, HAND_PORT_SPEC)
        self.assertFalse(diff.matched)
        self.assertEqual(diff.first_mismatch_index, 0)
        # 集合相同 ⇒ 差集为空。**只有位置能抓到这种错位**，所以计数断言在这里是承重的。
        self.assertEqual(diff.only_actual, ())
        self.assertEqual(diff.only_expected, ())
        self.assertEqual(diff.compared_count, 3)

    def test_render_ports_counts(self) -> None:
        """计数断言：`-p` 恰好 3 次、`-i` 恰好 3 次，长度恰好 12。"""
        argv = ewave.render_ports(HAND_PORT_SPEC)
        self.assertEqual(argv.count("-p"), len(HAND_PORT_SPEC.mapping))
        self.assertEqual(argv.count("-i"), len(HAND_PORT_SPEC.signal_ports))
        self.assertEqual(len(argv), 2 * (len(HAND_PORT_SPEC.mapping) + len(HAND_PORT_SPEC.signal_ports)))

    def test_render_ports_all_mode_is_empty(self) -> None:
        """`ALL` 在这里**不产出** `--all`。

        它已经在机制层的 flag dict 里（`core.cmd._locked_flags`），由 `render_flags` 渲染。
        这里再给一次，整条命令里就会有两个 `--all` ——
        见 `test_build_ewave_plan_has_exactly_one_all_flag`。
        """
        self.assertEqual(ewave.render_ports(PortSpec()), [])
        self.assertEqual(ewave.render_ports(PortSpec(mode=PortMode.ALL)), [])

    def test_render_ports_explicit_without_signal_ports(self) -> None:
        """`signal_ports` 空 ⇒ 一个 `-i` 都不给（照 eWave help 原文：不给 `-i` 就是不做挑选）。"""
        spec = PortSpec(mode=PortMode.EXPLICIT, mapping=(("P000", "pinA"),))
        self.assertEqual(ewave.render_ports(spec), ["-p", "P000=pinA"])


@unittest.skipIf(FIXTURE is None, SKIP_REASON)
class RenderPortsAgainstProductionFixture(unittest.TestCase):
    """拿**真实生产命令**里那 17 个端口当基准。取值全部从 fixture 读，源码里一个都没有。"""

    def _expected(self) -> PortSpec:
        assert FIXTURE is not None
        return PortSpec(
            mode=PortMode.EXPLICIT,
            mapping=tuple((f"P{index:03d}", str(pin)) for index, pin in enumerate(FIXTURE["port_order"])),
            signal_ports=tuple(str(p) for p in FIXTURE["signal_ports"]),
        )

    def test_render_ports_matches_production_shape(self) -> None:
        assert FIXTURE is not None
        spec = self._expected()
        argv = ewave.render_ports(spec)

        # 逐位断言：第 i 个 `-p` 的值必须是 fixture 里第 i 个 pin。
        for index, pin in enumerate(FIXTURE["port_order"]):
            with self.subTest(index=index):
                self.assertEqual(argv[2 * index], "-p")
                self.assertEqual(argv[2 * index + 1], f"P{index:03d}={pin}")
        # 计数断言：条数对得上 fixture 数出来的 17 + 17，一条不多一条不少。
        count = int(FIXTURE["port_count"])
        self.assertEqual(len(FIXTURE["port_order"]), count)
        self.assertEqual(argv.count("-p"), count)
        self.assertEqual(argv.count("-i"), len(FIXTURE["signal_ports"]))
        self.assertEqual(len(argv), 2 * (count + len(FIXTURE["signal_ports"])))

    def test_render_ports_matches_production_shape_negative(self) -> None:
        """同一条构造路径，只把第 0 个 pin 改名 —— 断言比较逻辑报告了它。"""
        expected = self._expected()
        broken_mapping = (("P000", "notarealpin"), *expected.mapping[1:])
        broken = PortSpec(
            mode=PortMode.EXPLICIT,
            mapping=broken_mapping,
            signal_ports=expected.signal_ports,
        )
        self.assertNotEqual(ewave.render_ports(broken), ewave.render_ports(expected))

        diff = cmd.diff_ports(broken, expected)
        self.assertFalse(diff.matched)
        self.assertEqual(diff.first_mismatch_index, 0)
        self.assertEqual(diff.only_actual, ("notarealpin",))
        self.assertEqual(len(diff.only_expected), 1)
        self.assertEqual(diff.compared_count, int(FIXTURE["port_count"]))


# --------------------------------------------------------------------------
# 程序名
# --------------------------------------------------------------------------


class EwaveProgram(unittest.TestCase):
    def test_program_comes_from_facts(self) -> None:
        self.assertEqual(ewave.ewave_program(SiteFacts(ewave_bin=FAKE_EWAVE_BIN)), FAKE_EWAVE_BIN)

    def test_program_negative_empty_facts_raises(self) -> None:
        """坐标缺失时**必须炸**，不许退回一个写死的路径（硬约束 1b）。

        这里刻意不做 PATH 回退：`command -v` 那一步的家在 `core.discover.find_tool`，
        而且本机 PATH 上永远没有 `ewave`、红区上永远有 —— 在这里回退会让这条测试
        在两台机器上给出不同答案，而 `scripts/check.sh` 是要在红区也跑的。
        """
        with self.assertRaises(ToolMissingError):
            ewave.ewave_program(SiteFacts())

    def test_no_hardcoded_tool_path_in_source(self) -> None:
        """源码里不许出现绝对路径形状的工具坐标（硬约束 1b 的机器判据）。"""
        source = Path(ewave.__file__).read_text(encoding="utf-8")
        for needle in ("/usr/bin/ewave", "/opt/", "/software/"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)


# --------------------------------------------------------------------------
# 关键测试：整条 argv
# --------------------------------------------------------------------------

GOLDEN_ARGV = (
    FAKE_EWAVE_BIN,
    "--all",
    "--cadencePins=1",
    "--equalCurrent",
    "--gds=/tmp/ewb/gds/d0.gds",
    "--includePortOrder=1",
    "--labelDepth=0",
    "--multiSweep=adaptive,0:0.1:40",
    "--nogui",
    "--relativeCurrentTolerance=0.001",
    "--relativeTolerance=1e-05",
    "--sparam=TESTCELL",
    "--sparamImpedance=50",
    "--top=TESTCELL",
    "--viaMergeSpace=0.4",
    "--viaMode=1",
    "--workDir=/tmp/ewb/runs/d0/base",
    "-d", "0.4",
    "-e", "0.4",
    "-m",
)
"""**期望值：手写**，由三份文档逐条推出来（没有一处抄自被测代码的输出）：

1. **值**从 BRIEF §6「已知的生产默认值」那一串抄：
   `--labelDepth=0 -e 0.4 -d 0.4 --viaMergeSpace=0.4 --equalCurrent --viaMode=1
   --multiSweep=adaptive,0:0.1:40 --sparamImpedance=50 --relativeTolerance=1e-05
   --relativeCurrentTolerance=0.001`（`--parallel` / `--key` 不在这里：前者要 `-R` 的
   `cpu=` 才推得出来，本 ctx 没给；后者是站点身份，只能从官方 run 目录学）；
2. **机制层**从 BRIEF §5 / D1b / D1d 抄：`--nogui -m --workDir=… --gds=… --top=<cell>
   --sparam=<cell> --cadencePins=1 --all --includePortOrder=1`；
3. **顺序**按 `core.cmd.render_flags` 的成文规则：flag 名 `sorted()`（ASCII 下 `--x`
   全部排在 `-x` 前面），`True` → 裸 flag，长 flag → `--k=v` 一项，短 flag → `-k`,`v` 两项。

⚠️ 这条测试与 `tests/test_cmd_golden.py` 有意重叠：那边验的是"我们和**真实生产命令**
对得上"（需要 fixture），这边验的是"整条 argv 逐字节等于手写期望"（不需要 fixture，
公开克隆者和红区都能跑）。两条一起挂 = 值错了；只挂这一条 = 排序或渲染规则变了。
"""

GOLDEN_FLAG_NAMES = (
    "--all",
    "--cadencePins",
    "--equalCurrent",
    "--gds",
    "--includePortOrder",
    "--labelDepth",
    "--multiSweep",
    "--nogui",
    "--relativeCurrentTolerance",
    "--relativeTolerance",
    "--sparam",
    "--sparamImpedance",
    "--top",
    "--viaMergeSpace",
    "--viaMode",
    "--workDir",
    "-d",
    "-e",
    "-m",
)
"""手写的 flag 名清单 —— 19 个 = 内置默认 10（BRIEF §6）+ 机制层 9（§5 / D1b / D1d）。

计数断言的锚：合并出来的 flag 集合必须恰好是这些。少一个 = 某一层没被合进去，
多一个 = 有人往里塞了没写进文档的东西 —— 两种都不会有任何运行期报错。
"""


class EwavePlan(unittest.TestCase):
    def test_build_ewave_plan_argv_golden(self) -> None:
        plan = ewave.build_ewave_plan(_run(), _ctx())
        self.assertIsInstance(plan, CommandPlan)
        self.assertEqual(plan.argv, GOLDEN_ARGV)
        self.assertEqual(plan.stage, Stage.SOLVE)
        self.assertEqual(plan.run_id, "d0/base/typ_25")
        self.assertEqual(plan.design_key, "d0")
        self.assertEqual(plan.work_dir, FAKE_WORK_DIR)
        self.assertEqual(plan.argv[0], ewave.ewave_program(_ctx().facts))

    def test_build_ewave_plan_argv_golden_negative_wrong_work_dir(self) -> None:
        """同一条构造路径，只改坏 `work_dir` —— 断言比较逻辑报告了这一处，且**真的比了 19 条**。

        选 `--workDir` 来改坏不是随便挑的：它是我们绕开"同 corner/temp 静默覆盖"的
        全部手段（D2）。它错了，两个组合的产物会互相覆盖，而两条命令都退 0。
        """
        run = _run()
        run.work_dir = "/tmp/ewb/runs/d0/WRONG"
        plan = ewave.build_ewave_plan(run, _ctx())
        self.assertNotEqual(plan.argv, GOLDEN_ARGV)

        # 期望值**不从被测函数取**（那就是自己证明自己）：把手写的 GOLDEN_ARGV 交给
        # `core.template.parse_command_line` 反解成 flag dict —— 一条独立的解析路径。
        expected_flags = template.parse_command_line(" ".join(GOLDEN_ARGV)).flags
        self.assertEqual(len(expected_flags), len(GOLDEN_FLAG_NAMES))
        diff = cmd.diff_flags(plan.flags, expected_flags)
        self.assertFalse(diff.clean)
        self.assertEqual([d.flag for d in diff.differing], ["--workDir"])
        self.assertEqual(diff.differing[0].actual, "/tmp/ewb/runs/d0/WRONG")
        self.assertEqual(diff.compared_count, len(GOLDEN_FLAG_NAMES))
        self.assertEqual(diff.ignored, ())

    def test_flag_names_match_hand_written_list(self) -> None:
        """计数断言：合并出来的 flag 集合恰好是手写的那 19 个，一个不多一个不少。"""
        plan = ewave.build_ewave_plan(_run(), _ctx())
        self.assertEqual(sorted(plan.flags), sorted(GOLDEN_FLAG_NAMES))
        self.assertEqual(len(plan.flags), len(GOLDEN_FLAG_NAMES))

    def test_build_ewave_plan_has_exactly_one_all_flag(self) -> None:
        """计数断言（本模块最容易犯的错）：`--all` 恰好一次。

        `render_ports(ALL)` 返回空 list、`--all` 只由机制层的 flag dict 出 ——
        两边都出一次的话，argv 里就是两个 `--all`。
        """
        plan = ewave.build_ewave_plan(_run(), _ctx())
        self.assertEqual(list(plan.argv).count("--all"), 1)

    def test_build_ewave_plan_appends_explicit_ports_exactly_once(self) -> None:
        """显式端口只接一次（不是"薄封装再接一遍"）—— 且接在 argv 尾巴上、保序。"""
        ctx = _ctx(port_spec=HAND_PORT_SPEC)
        plan = ewave.build_ewave_plan(_run(), ctx)
        argv = list(plan.argv)
        self.assertEqual(argv[-len(HAND_PORT_ARGV):], HAND_PORT_ARGV)
        self.assertEqual(argv.count("-p"), len(HAND_PORT_SPEC.mapping))
        self.assertEqual(argv.count("-i"), len(HAND_PORT_SPEC.signal_ports))
        self.assertEqual(argv.count("P000=pinA"), 1)
        # 显式端口模式下不该再有 `--all`（D1b：`--all` 表达不了接地端口，两者互斥）。
        self.assertEqual(argv.count("--all"), 0)
        self.assertEqual(plan.port_spec, HAND_PORT_SPEC)

    def test_build_ewave_plan_negative_missing_ewave_bin(self) -> None:
        with self.assertRaises(ToolMissingError):
            ewave.build_ewave_plan(_run(), _ctx(ewave_bin=""))

    def test_parallel_appears_only_when_resources_give_cpu(self) -> None:
        """`--parallel` 从 `-R` 的 `cpu=` 推（1:1，BRIEF §6 的修正值），没给就不瞎写。"""
        self.assertNotIn("--parallel", ewave.build_ewave_plan(_run(), _ctx()).flags)
        plan = ewave.build_ewave_plan(_run(), _ctx(resources="cpu=20;mem=100000"))
        self.assertEqual(plan.flags["--parallel"], "20")
        self.assertIn("--parallel=20", plan.argv)

    def test_key_comes_from_site_facts(self) -> None:
        """`--key` 由 `SiteFacts.key` 给（`core.cmd.build_flag_layers` 的默认表层）。

        它是 BRIEF §6「已知的生产默认值」里的一员 —— 官方那条命令有它，我们也必须有。
        但它的**取值是站点身份**：`core.discover.learn_default_flags` 把它从学到的默认表里
        剔掉了，`core.cmd.BUILTIN_DEFAULT_FLAGS` 又不许写死它 ⇒ 不特意补就谁都不给，
        端到端拼出来的命令缺 `--key`，而官方那条有。这条测试就是那处集成缺口的回归判据。

        形状与 `--parallel` 完全一致：**值来自站点发现，不来自源码常量。**
        """
        plan = ewave.build_ewave_plan(_run(), _ctx(key=FAKE_KEY))
        self.assertEqual(plan.flags[cmd.KEY_FLAG], FAKE_KEY)
        self.assertIn(f"--key={FAKE_KEY}", plan.argv)
        # 计数断言：只多出 `--key` 这一个 flag，别的一条都没动
        # （"补 key 顺手改了别的层"不会有任何运行期报错）。
        self.assertEqual(sorted(plan.flags), sorted((*GOLDEN_FLAG_NAMES, "--key")))
        self.assertEqual(len(plan.argv), len(GOLDEN_ARGV) + 1)

    def test_key_is_not_invented_when_facts_have_none_negative(self) -> None:
        """`SiteFacts.key` 为空 ⇒ **一个 `--key` 都不许出现**（宁可缺，也不许编）。

        与正向那条共用 `_ctx()`，只改 `key` 这一个入参。
        编出来的 key 会让 run 直接失败，失败原因极难查；更要命的是它会把一个**假的
        站点坐标**写进 `cmd_<corner>_<temp>.sh` 留档，后面谁看谁信。
        """
        plan = ewave.build_ewave_plan(_run(), _ctx(key=""))
        self.assertNotIn(cmd.KEY_FLAG, plan.flags)
        self.assertEqual([t for t in plan.argv if t.startswith("--key")], [])
        # 兜底默认表里也不许躺着一个 key（硬约束 1b 的机器判据）。
        self.assertNotIn(cmd.KEY_FLAG, cmd.BUILTIN_DEFAULT_FLAGS)
        self.assertEqual(plan.argv, GOLDEN_ARGV)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
