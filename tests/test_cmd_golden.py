"""golden 测试：拿**真实生产命令**当基准，逐 flag 比对 `core.cmd` 拼出来的 argv。

判据来自 `PROJECT_BRIEF.md` §12（P1 完成判据）和 `docs/OVERNIGHT.md`「防自证」。

基准是 `tests/fixtures/production_cmd.local.json` —— **人**从官方 GUI 生成的真实脚本里抽
出来的 22 个 flag + 17 个端口。它含站点坐标，所以不进 git、只在本机；
`scripts/check.sh` 第 1 步用 sha256 锁着它，**实现方改期望值 = 测试自己证明自己**。

这份测试文件里因此**一个站点标识符都没有**：ptxt 路径 / cell 名 / key / 端口名全部
**从 fixture 读出来喂给被测函数**，再断言它们原样出现在生成物里。比对是真的，
源码是干净的（CLAUDE.md 硬约束 1b）。

四条防自证配方（`docs/OVERNIGHT.md`）在这份文件里的落点：

1. 关键测试 = 断言"生成物等于期望值"的那些 —— 下面每一条 `test_golden_*` / `test_port_order_*`；
2. 期望值只从 fixture 读，**没有一处现算**；
3. 每条关键测试配一条同名 `_negative`：复制同一条输入构造路径、故意改坏一个值，
   断言比较逻辑**报告了**这处差异；
4. 凡有"忽略/排除"就有计数断言 + 过滤器测试 —— `--sparam` 不许把 `--sparamImpedance`
   一起吃掉（MVP 真踩过的那个 bug 的回归测试）。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from ewave_batch.core import cmd
from ewave_batch.model import (
    PLACEHOLDER_PTXT,
    PLACEHOLDER_VALUE,
    Axis,
    AxisValue,
    BatchOptions,
    Design,
    FlagConflictError,
    FlagLayers,
    PlanContext,
    PortMode,
    PortSpec,
    Run,
    SiteFacts,
    SpecError,
)

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

# ---------------------------------------------------------------------------
# 这三张表是"我们和官方为什么会不一样"的**全部**理由，写在这里好让它们可被断言。
# 名字打错的话 `test_partition_of_production_flags` 会当场红 —— 那正是 MVP 踩过的坑。
# ---------------------------------------------------------------------------

TOOL_COMPUTED_FLAGS = (
    "--nogui",
    "-m",
    "--workDir",
    "--gds",
    "--top",
    "--sparam",
    "--cadencePins",
)
"""机制层（`locked`）会自己算出来的 —— 用户和默认表都碰不到（§11「锁死」层）。"""

AXIS_FLAGS = ("--corner", "--temperature", "--emssTechFile")
"""被轴掌管的。⚠️ corner 轴同时改 `--corner` 和 `--emssTechFile`（BRIEF §7）。"""

IGNORED_AND_IN_FIXTURE = ("--workDir", "--gds")
"""`DEFAULT_DIFF_IGNORE` 里**官方也有**的那些 → 从 compared_count 里减掉这么多条。"""

IGNORED_AND_OURS_ONLY = ("--all", "--includePortOrder")
"""`DEFAULT_DIFF_IGNORE` 里**只有我们有**的（D1b / D1d）→ 官方那边根本没有这两个键。"""


@unittest.skipIf(FIXTURE is None, SKIP_REASON)
class GoldenCommandLine(unittest.TestCase):
    """正向 + 计数 + 过滤器 + 反向。

    每条 `test_x` 都配一条 `test_x_..._negative`，**两边走同一条输入构造路径**
    （`_build_plan`），反向那条只改其中一个入参 —— 排除"换了个东西测"。
    """

    # ---------------- 共用的输入构造路径（正反两向都走这一条）----------------

    def fixture_flags(self) -> dict:
        return dict(FIXTURE["flags"])

    def site_facts(self) -> SiteFacts:
        """把 fixture 里的站点坐标装进 `SiteFacts` —— 模拟 P2 的 `discover_site_facts`
        从官方 run 目录解析出来的结果。**值全部来自 fixture，测试源码里没有一个。**"""
        flags = self.fixture_flags()
        ptxt = str(flags["--emssTechFile"])
        corner = str(flags["--corner"])
        ptxt_dir, _, ptxt_name = ptxt.rpartition("/")
        template = ptxt_name.replace(corner, "{corner}")
        # 防空过：模板里必须真的出现了占位符，否则 corner 轴换文件名这件事根本没被测到。
        self.assertIn(
            "{corner}",
            template,
            "从 fixture 的 ptxt 文件名里没换出 {corner} 占位符 —— 这条测试会变成空过",
        )
        return SiteFacts(
            ewave_bin="ewave",
            ptxt=ptxt,
            ptxt_dir=ptxt_dir,
            ptxt_name_template=template,
            corner=corner,
            temperature=str(flags["--temperature"]),
            key=str(flags["--key"]),
        )

    def axes(self) -> tuple[Axis, Axis]:
        """corner 轴 + temperature 轴。取值里那个"另一个取值"是通用工艺角/温度，
        不是站点身份 —— 它只用来证明"轴换值时两个 flag 一起换"。"""
        flags = self.fixture_flags()
        corner = str(flags["--corner"])
        temperature = str(flags["--temperature"])
        corner_axis = Axis(
            name="corner",
            values=(
                AxisValue(
                    value=corner,
                    flags={"--corner": PLACEHOLDER_VALUE, "--emssTechFile": PLACEHOLDER_PTXT},
                ),
                AxisValue(
                    value="cworst",
                    flags={"--corner": PLACEHOLDER_VALUE, "--emssTechFile": PLACEHOLDER_PTXT},
                ),
            ),
            flags=("--corner", "--emssTechFile"),
            encoded_in_ewave_dir=True,
        )
        temperature_axis = Axis(
            name="temperature",
            values=(
                AxisValue(value=temperature, flags={"--temperature": PLACEHOLDER_VALUE}),
                AxisValue(value="25.0", flags={"--temperature": PLACEHOLDER_VALUE}),
            ),
            flags=("--temperature",),
            encoded_in_ewave_dir=True,
        )
        return corner_axis, temperature_axis

    def learned_defaults(self) -> dict:
        """"默认表" = 官方实际在用的 flag 减去机制层和轴掌管的那些（§11 规则 1：学，不写死）。"""
        flags = self.fixture_flags()
        skip = set(TOOL_COMPUTED_FLAGS) | set(AXIS_FLAGS)
        return {flag: value for flag, value in flags.items() if flag not in skip}

    def _build_plan(
        self,
        *,
        defaults_override: dict | None = None,
        extra_flags: dict | None = None,
        axis_values: dict | None = None,
        options: BatchOptions | None = None,
        resources: str = "",
    ):
        """**正反两向共用的**唯一一条输入构造路径。反向测试只改其中一个入参。"""
        flags = self.fixture_flags()
        cell = str(flags["--top"])
        design = Design(
            library="<lib>",
            cell=cell,
            view="<view>",
            key="d0",
            resources=resources,
            extra_flags=dict(extra_flags or {}),
        )
        corner_axis, temperature_axis = self.axes()
        run = Run(
            run_id="d0/base/golden",
            design_key="d0",
            axis_values=dict(
                axis_values
                or {
                    "corner": str(flags["--corner"]),
                    "temperature": str(flags["--temperature"]),
                }
            ),
            work_dir="/batch/runs/d0/base",
        )
        defaults = self.learned_defaults()
        if defaults_override is not None:
            defaults.update(defaults_override)
        ctx = PlanContext(
            design=design,
            facts=self.site_facts(),
            axes=(corner_axis, temperature_axis),
            defaults=defaults,
            options=options or BatchOptions(),
            batch_dir="/batch",
        )
        return cmd.build_command_plan(run, ctx)

    # ---------------- 结构：三张表把 22 个 flag 分完，一个不剩 ----------------

    def test_partition_of_production_flags(self) -> None:
        """机制层 + 轴 + 默认表 == 官方那 22 个 flag，**不重不漏**。

        这条是所有计数断言的地基：它保证"我们知道每一个官方 flag 由谁负责"。
        少列一个 → 那个 flag 会悄悄跑到别的层里去，或者干脆丢掉。
        """
        flags = self.fixture_flags()
        computed = set(TOOL_COMPUTED_FLAGS)
        axis = set(AXIS_FLAGS)
        defaults = set(self.learned_defaults())
        self.assertEqual(computed & axis, set(), "同一个 flag 不能既是机制层又是轴")
        self.assertEqual(computed & defaults, set())
        self.assertEqual(axis & defaults, set())
        self.assertEqual(
            computed | axis | defaults,
            set(flags),
            "三张表和官方命令对不上 —— 有 flag 没人负责，或者表里写了官方没有的名字",
        )
        self.assertEqual(len(flags), len(computed) + len(axis) + len(defaults))

    def test_ignore_lists_really_describe_the_fixture(self) -> None:
        """排除清单本身要被验证：名字打错、或者官方其实有这个 flag，都当场红。"""
        flags = self.fixture_flags()
        self.assertEqual(
            set(IGNORED_AND_IN_FIXTURE) | set(IGNORED_AND_OURS_ONLY),
            set(cmd.DEFAULT_DIFF_IGNORE),
            "两张排除表加起来必须正好是 DEFAULT_DIFF_IGNORE",
        )
        for flag in IGNORED_AND_IN_FIXTURE:
            self.assertIn(flag, flags, f"{flag} 被当成'官方也有的排除项'，但 fixture 里没有它")
        for flag in IGNORED_AND_OURS_ONLY:
            self.assertNotIn(flag, flags, f"{flag} 被当成'只有我们有'，但 fixture 里其实有它")

    # ---------------- 正向：逐 flag 相等 ----------------

    def test_golden_flags_match_production(self) -> None:
        """★ 主判据：生成的 flag 集合与官方那 22 个逐个相等（键和值都比）。"""
        plan = self._build_plan()
        expected = self.fixture_flags()
        diff = cmd.diff_flags(plan.flags, expected, ignore=cmd.DEFAULT_DIFF_IGNORE)

        self.assertTrue(
            diff.clean,
            f"和生产命令对不上：多给 {diff.only_actual}，少给 {diff.only_expected}，"
            f"取值不同 {[(d.flag, d.actual, d.expected) for d in diff.differing]}",
        )
        # 计数断言（配方 4）：真正比过的条数 = 官方 flag 数 − 官方也有的排除项数。
        self.assertEqual(
            diff.compared_count,
            len(expected) - len(IGNORED_AND_IN_FIXTURE),
            "参与比较的条数不对 —— 空集合的 diff 永远是绿的，这条专防'空得非常好看'",
        )
        self.assertEqual(diff.ignored, tuple(sorted(cmd.DEFAULT_DIFF_IGNORE)))
        # 我们比官方多的，**只能**是 D1b/D1d 那两个，多一个都要说清楚。
        self.assertEqual(set(plan.flags) - set(expected), set(IGNORED_AND_OURS_ONLY))
        self.assertEqual(len(plan.flags), len(expected) + len(IGNORED_AND_OURS_ONLY))

    def test_golden_flags_match_production_negative(self) -> None:
        """反向：把 `-e` 从 fixture 的值改坏一位，**必须且只**报这一处。"""
        expected = self.fixture_flags()
        broken_value = str(expected["-e"]) + "9"  # 从 fixture 的值变形，不写死数字
        plan = self._build_plan(defaults_override={"-e": broken_value})
        diff = cmd.diff_flags(plan.flags, expected, ignore=cmd.DEFAULT_DIFF_IGNORE)

        self.assertFalse(diff.clean, "改坏了 -e 却报一致 —— 比对逻辑是空的")
        self.assertEqual([d.flag for d in diff.differing], ["-e"])
        self.assertEqual(diff.differing[0].actual, broken_value)
        self.assertEqual(diff.differing[0].expected, expected["-e"])
        self.assertEqual(diff.only_actual, ())
        self.assertEqual(diff.only_expected, ())
        self.assertEqual(diff.compared_count, len(expected) - len(IGNORED_AND_IN_FIXTURE))

    def test_golden_missing_flag_is_reported_negative(self) -> None:
        """反向：用"显式缺席"（`False`）把 `--viaMode` 撤掉 → 必须报"少了 --viaMode"。"""
        expected = self.fixture_flags()
        self.assertIn("--viaMode", expected)
        plan = self._build_plan(extra_flags={"--viaMode": False})
        self.assertIs(plan.flags["--viaMode"], False, "False 应当留在 dict 里表示显式缺席")
        self.assertNotIn("--viaMode=1", plan.argv)

        diff = cmd.diff_flags(plan.flags, expected, ignore=cmd.DEFAULT_DIFF_IGNORE)
        self.assertFalse(diff.clean)
        self.assertEqual(diff.only_expected, ("--viaMode",), "少了的 flag 没被报出来")
        self.assertEqual(diff.only_actual, ())
        self.assertEqual(diff.differing, ())

    def test_golden_temperature_change_is_reported_negative(self) -> None:
        """反向：温度轴换一个取值 → `--temperature` 必须被报出来。

        温度是 run 的**身份**（进目录名）。目录名说 125、命令行说 25，正是原生 GUI 的坑。
        """
        expected = self.fixture_flags()
        other = "25.0"
        self.assertNotEqual(other, str(expected["--temperature"]))
        plan = self._build_plan(
            axis_values={"corner": str(expected["--corner"]), "temperature": other}
        )
        diff = cmd.diff_flags(plan.flags, expected, ignore=cmd.DEFAULT_DIFF_IGNORE)
        self.assertFalse(diff.clean)
        self.assertEqual([d.flag for d in diff.differing], ["--temperature"])
        self.assertEqual(diff.differing[0].actual, other)

    def test_corner_axis_changes_both_flags_negative(self) -> None:
        """反向（BRIEF §7）：corner 轴换值必须**同时**换掉 `--corner` 和 `--emssTechFile`。

        少改一个 = "目录名说 typical、实际用了别的工艺角"，而且跑得出来、数字也像。
        """
        expected = self.fixture_flags()
        plan = self._build_plan(
            axis_values={"corner": "cworst", "temperature": str(expected["--temperature"])}
        )
        diff = cmd.diff_flags(plan.flags, expected, ignore=cmd.DEFAULT_DIFF_IGNORE)
        self.assertEqual(
            sorted(d.flag for d in diff.differing),
            ["--corner", "--emssTechFile"],
            "换 corner 时 ptxt 文件名没跟着换 —— §7 那条'同时改两处'没实现",
        )
        self.assertNotIn(str(expected["--emssTechFile"]), " ".join(plan.argv))

    # ---------------- 站点坐标原样出现在 argv 里 ----------------

    def test_site_values_are_passed_through_verbatim(self) -> None:
        """ptxt 路径 / top cell / sparam 名 / key —— 喂进去什么，argv 里就该出现什么。

        这四个是**站点坐标**：它们从 fixture 读出来喂给被测函数，再断言原样出现 ——
        于是测试源码里零站点标识符，而比对仍然是真的。
        """
        flags = self.fixture_flags()
        plan = self._build_plan()
        argv = list(plan.argv)
        self.assertEqual(argv[0], "ewave")
        for flag in ("--emssTechFile", "--top", "--sparam", "--key", "--corner", "--temperature"):
            self.assertIn(f"{flag}={flags[flag]}", argv, f"{flag} 的值没原样进 argv")
        # 短 flag 是两项，不是 `-e=0.4`（生产就是这么写的）。
        self.assertIn("-e", argv)
        self.assertEqual(argv[argv.index("-e") + 1], flags["-e"])
        self.assertNotIn(f"-e={flags['-e']}", argv)
        # 裸 flag 不带值。
        self.assertIn("--nogui", argv)
        self.assertIn("-m", argv)
        # 我们**有意**不同的那两个：
        self.assertEqual(plan.flags["--workDir"], "/batch/runs/d0/base")
        self.assertNotEqual(plan.flags["--workDir"], flags["--workDir"])
        self.assertTrue(str(plan.flags["--gds"]).endswith(".gds"))
        self.assertNotEqual(plan.flags["--gds"], flags["--gds"])

    # ---------------- 过滤器：`--sparam` 不许吃掉 `--sparamImpedance` ----------------

    def test_ignore_is_exact_match_not_prefix(self) -> None:
        """★ MVP 那个真 bug 的回归测试（BRIEF §10 / OVERNIGHT 配方 4）。

        排除规则写 `--sparam` **前缀**会误伤 `--sparamImpedance`，两边同时被跳过，
        diff 空得非常好看但根本没比。**空过的测试比没测更坏。**
        """
        expected = self.fixture_flags()
        # 防空过：fixture 里必须真的存在"某个 flag 是另一个 flag 的前缀"这种情形。
        self.assertTrue(
            any(f != "--sparam" and f.startswith("--sparam") for f in expected),
            "fixture 里没有 --sparam 前缀的第二个 flag，这条回归测试会变成空过",
        )
        plan = self._build_plan()
        ignore = tuple(cmd.DEFAULT_DIFF_IGNORE) + ("--sparam",)
        diff = cmd.diff_flags(plan.flags, expected, ignore=ignore)

        self.assertIn("--sparam", diff.ignored)
        self.assertNotIn("--sparamImpedance", diff.ignored, "前缀误伤：--sparamImpedance 被一起忽略了")
        self.assertIn("--sparamImpedance", diff.same, "--sparamImpedance 根本没参与比较")
        self.assertEqual(
            diff.compared_count,
            len(expected) - len(IGNORED_AND_IN_FIXTURE) - 1,
            "多忽略了一条 —— 说明 ignore 在做前缀匹配",
        )
        self.assertNotIn("--sparamImpedance", cmd.DEFAULT_DIFF_IGNORE)

    def test_ignore_is_exact_match_not_prefix_negative(self) -> None:
        """反向：忽略 `--sparam` 的同时把 `--sparamImpedance` 改坏 → **必须**被报出来。

        如果 ignore 在做前缀匹配，这处差异会被静默吞掉，这条测试就变红。
        """
        expected = self.fixture_flags()
        broken = str(expected["--sparamImpedance"]) + "0"
        plan = self._build_plan(defaults_override={"--sparamImpedance": broken})
        ignore = tuple(cmd.DEFAULT_DIFF_IGNORE) + ("--sparam",)
        diff = cmd.diff_flags(plan.flags, expected, ignore=ignore)

        self.assertEqual([d.flag for d in diff.differing], ["--sparamImpedance"])
        self.assertEqual(diff.differing[0].actual, broken)
        self.assertFalse(diff.clean)

    # ---------------- 内置默认表 vs 真实生产值 ----------------

    def test_builtin_defaults_match_production(self) -> None:
        """`BUILTIN_DEFAULT_FLAGS` 里的每一条都要和真实生产命令对上（BRIEF §6）。

        这张表是"学不到默认表时的兜底"，兜底值要是错的，红区之外拼出来的命令就是错的。
        """
        expected = self.fixture_flags()
        builtin = dict(cmd.BUILTIN_DEFAULT_FLAGS)
        missing = [flag for flag in builtin if flag not in expected]
        self.assertEqual(missing, [], f"内置默认表里有官方命令没有的 flag: {missing}")
        diff = cmd.diff_flags(builtin, {flag: expected[flag] for flag in builtin})
        self.assertTrue(
            diff.clean,
            f"内置默认和生产值对不上: {[(d.flag, d.actual, d.expected) for d in diff.differing]}",
        )
        self.assertEqual(diff.compared_count, len(builtin))
        self.assertGreaterEqual(len(builtin), 10, "内置默认表被删空了？")

    def test_builtin_defaults_match_production_negative(self) -> None:
        """反向：把内置默认表的一个值改坏（改副本，不动模块级常量）→ 必须被报出来。"""
        expected = self.fixture_flags()
        builtin = dict(cmd.BUILTIN_DEFAULT_FLAGS)
        builtin["--viaMode"] = str(builtin["--viaMode"]) + "7"
        diff = cmd.diff_flags(builtin, {flag: expected[flag] for flag in builtin})
        self.assertFalse(diff.clean)
        self.assertEqual([d.flag for d in diff.differing], ["--viaMode"])

    # ---------------- 机制层 ----------------

    def test_locked_layer_covers_mechanism_flags(self) -> None:
        """`MECHANISM_FLAGS`（用户不许碰的）和我们实际会写的机制 flag 要一致。

        少写一个 = 某个机制没生效；多禁一个 = 用户被拦住却没人在管那个 flag。
        """
        from ewave_batch.model import MECHANISM_FLAGS

        self.assertEqual(set(cmd._LOCKED_FLAG_NAMES), set(MECHANISM_FLAGS))
        plan = self._build_plan()
        for flag in MECHANISM_FLAGS:
            self.assertIn(flag, plan.flags, f"机制 flag {flag} 没被写进命令")


@unittest.skipIf(FIXTURE is None, SKIP_REASON)
class GoldenPortOrder(unittest.TestCase):
    """★ D1b 的本机回归：`--all` 的字典序编号逐位复现官方 GUI 的 `-p` 顺序。

    这是"不依赖 GUI"成立的**全部依据**（BRIEF §5）。方法与
    `references/checks/check_port_order.py` 一致，只是这里拿的是真实的 pin 名（来自 fixture）。
    """

    def expected_ports(self) -> PortSpec:
        mapping = []
        for item in FIXTURE["port_order"]:
            port_id, _, pin = str(item).partition("=")
            mapping.append((port_id, pin))
        return PortSpec(
            mode=PortMode.EXPLICIT,
            mapping=tuple(mapping),
            signal_ports=tuple(str(p) for p in FIXTURE["signal_ports"]),
        )

    def test_port_order_matches_ascii_sort(self) -> None:
        expected = self.expected_ports()
        pins = [pin for _, pin in expected.mapping]

        predicted = cmd.predict_all_ports(pins)
        diff = cmd.diff_ports(predicted, expected)

        self.assertTrue(
            diff.matched,
            f"`--all` 的字典序编号与官方 -p 顺序对不上，第 {diff.first_mismatch_index} 位起分叉",
        )
        self.assertIsNone(diff.first_mismatch_index)
        # 计数断言（配方 4）：比过的位置数 = fixture 自己声明的端口数。
        self.assertEqual(diff.compared_count, FIXTURE["port_count"])
        self.assertEqual(len(expected.mapping), FIXTURE["port_count"])
        self.assertEqual(diff.only_actual, ())
        self.assertEqual(diff.only_expected, ())

    def test_port_order_matches_ascii_sort_negative(self) -> None:
        """反向：改用**大小写不敏感**排序 → 必须报告端口顺序变了。

        排除"两种排序碰巧都对"这种巧合 —— 那样的话上面那条就什么都没证明。
        """
        expected = self.expected_ports()
        pins = [pin for _, pin in expected.mapping]

        case_blind = sorted(pins, key=str.lower)
        self.assertNotEqual(
            case_blind, sorted(pins), "这批 pin 名下两种排序结果相同，反向测试会变成空过"
        )
        wrong = PortSpec(
            mode=PortMode.EXPLICIT,
            mapping=tuple((f"P{i:03d}", pin) for i, pin in enumerate(case_blind)),
        )
        diff = cmd.diff_ports(wrong, expected)
        self.assertFalse(diff.matched, "大小写不敏感排序竟然也算匹配 —— 比对逻辑没在看顺序")
        self.assertIsNotNone(diff.first_mismatch_index)
        self.assertEqual(diff.compared_count, FIXTURE["port_count"])

    def test_pin_case_change_is_reported_negative(self) -> None:
        """反向：把一个 pin 名的大小写改掉（**名字从 fixture 里取，不写死在源码**）
        → `diff_ports` 必须报告顺序变了，并指出是哪个 pin。"""
        expected = self.expected_ports()
        pins = [pin for _, pin in expected.mapping]
        lowercase = [pin for pin in pins if pin != pin.upper()]
        self.assertTrue(lowercase, "fixture 里没有含小写字母的 pin 名，这条测试会变成空过")
        victim = lowercase[0]
        mutated = [pin.upper() if pin == victim else pin for pin in pins]

        predicted = cmd.predict_all_ports(mutated)
        diff = cmd.diff_ports(predicted, expected)

        self.assertFalse(diff.matched, "改了一个 pin 名的大小写却报一致")
        self.assertIsNotNone(diff.first_mismatch_index)
        self.assertIn(victim.upper(), diff.only_actual)
        self.assertIn(victim, diff.only_expected)
        self.assertEqual(diff.compared_count, FIXTURE["port_count"])

    def test_all_ports_are_signal_ports(self) -> None:
        """官方给 17 个端口全加了 `-i` = 全是 signal port，而 `--all` 的原文正是
        "consider all ports as signal ports" —— 两者语义等价的那一半证据（D1b）。"""
        expected = self.expected_ports()
        port_ids = [port_id for port_id, _ in expected.mapping]
        self.assertEqual(set(expected.signal_ports), set(port_ids))
        self.assertEqual(len(expected.signal_ports), FIXTURE["port_count"])

        predicted = cmd.predict_all_ports([pin for _, pin in expected.mapping])
        self.assertEqual(predicted.signal_ports, tuple(port_ids))


class FlagLayerRules(unittest.TestCase):
    """五层合并的语义。期望值是手写字面量，来源是 `model.FlagLayers.MERGE_ORDER`
    与 BRIEF §11（不是 fixture —— 这里测的是规则本身，不是生产命令）。"""

    def test_merge_order_axis_beats_user_layers(self) -> None:
        layers = FlagLayers(
            builtin={"--x": "builtin"},
            defaults={"--x": "defaults"},
            extra={"--x": "extra"},
            axis={"--x": "axis"},
        )
        self.assertEqual(cmd.merge_flag_layers(layers)["--x"], "axis")

    def test_merge_order_locked_has_the_last_word(self) -> None:
        layers = FlagLayers(axis={"--x": "axis"}, locked={"--x": "locked"})
        self.assertEqual(cmd.merge_flag_layers(layers)["--x"], "locked")

    def test_merge_order_each_layer_beats_the_previous_one(self) -> None:
        self.assertEqual(
            cmd.merge_flag_layers(FlagLayers(builtin={"--x": "a"}, defaults={"--x": "b"}))["--x"],
            "b",
        )
        self.assertEqual(
            cmd.merge_flag_layers(FlagLayers(defaults={"--x": "b"}, extra={"--x": "c"}))["--x"],
            "c",
        )

    def test_false_survives_merge_and_is_not_rendered(self) -> None:
        """`False` = 显式缺席：合并后**留在 dict 里**（好让 diff 看见），但不渲染。"""
        layers = FlagLayers(builtin={"--equalCurrent": True}, axis={"--equalCurrent": False})
        merged = cmd.merge_flag_layers(layers)
        self.assertIs(merged["--equalCurrent"], False)
        self.assertEqual(cmd.render_flags(merged), [])

    def test_render_flags_is_deterministic_and_shaped_like_production(self) -> None:
        rendered = cmd.render_flags({"-e": "0.4", "--corner": "typical", "--nogui": True})
        self.assertEqual(rendered, ["--corner=typical", "--nogui", "-e", "0.4"])
        self.assertEqual(rendered, cmd.render_flags({"--nogui": True, "-e": "0.4", "--corner": "typical"}))


class ConflictDetection(unittest.TestCase):
    """§11 规则 2：Extra flags 里出现已经是轴的 flag、或机制 flag → 拒绝并说人话。"""

    def _axes(self) -> tuple[Axis, ...]:
        return (
            Axis(
                name="temperature",
                values=(AxisValue(value="25.0", flags={"--temperature": PLACEHOLDER_VALUE}),),
                flags=("--temperature",),
            ),
        )

    def test_extra_flag_that_is_an_axis_is_rejected(self) -> None:
        layers = FlagLayers(extra={"--temperature": "85"})
        conflicts = cmd.detect_flag_conflicts(layers, self._axes())
        self.assertEqual([c.flag for c in conflicts], ["--temperature"])
        self.assertTrue(conflicts[0].fatal)
        self.assertIn("temperature", conflicts[0].reason)

    def test_extra_mechanism_flag_is_rejected(self) -> None:
        layers = FlagLayers(extra={"--workDir": "/somewhere"})
        conflicts = cmd.detect_flag_conflicts(layers, self._axes())
        self.assertEqual([c.flag for c in conflicts], ["--workDir"])
        self.assertTrue(conflicts[0].fatal)

    def test_extra_emss_tech_file_is_rejected_but_axis_may_write_it(self) -> None:
        """`--emssTechFile` 用户不许写（USER_FORBIDDEN），但 corner 轴必须能写（§7）。"""
        self.assertEqual(
            [c.flag for c in cmd.detect_flag_conflicts(FlagLayers(extra={"--emssTechFile": "x"}), ())],
            ["--emssTechFile"],
        )
        self.assertEqual(
            cmd.detect_flag_conflicts(FlagLayers(axis={"--emssTechFile": "x"}, locked={}), ()), []
        )

    def test_harmless_extra_flag_is_accepted_negative(self) -> None:
        """反向：没人管的 flag 放进 Extra 是**允许**的（逃生口的意义）——
        冲突检测不许连这种也拦，否则它就只是个"什么都拒绝"的橡皮图章。"""
        layers = FlagLayers(extra={"--printDouble": True})
        self.assertEqual(cmd.detect_flag_conflicts(layers, self._axes()), [])

    def test_two_axes_owning_one_flag_is_a_warning(self) -> None:
        axes = (
            Axis(name="a", values=(AxisValue(value="1"),), flags=("-e",)),
            Axis(name="b", values=(AxisValue(value="2"),), flags=("-e",)),
        )
        conflicts = cmd.detect_flag_conflicts(FlagLayers(), axes)
        self.assertEqual([c.flag for c in conflicts], ["-e"])
        self.assertFalse(conflicts[0].fatal, "这条是提醒，不该把批次直接拦死")

    def test_axis_colliding_with_locked_is_fatal(self) -> None:
        conflicts = cmd.detect_flag_conflicts(
            FlagLayers(axis={"--workDir": "x"}, locked={"--workDir": "y"}), ()
        )
        self.assertEqual([c.flag for c in conflicts], ["--workDir"])
        self.assertTrue(conflicts[0].fatal)


class DiffFlagSemantics(unittest.TestCase):
    """`diff_flags` 自己的语义。期望值是手写字面量，来源是 model 里 `FlagDiff` 的 docstring。"""

    def test_absent_false_equals_missing_key(self) -> None:
        diff = cmd.diff_flags({"--x": False}, {})
        self.assertTrue(diff.clean)
        self.assertEqual(diff.compared_count, 0)

    def test_false_against_present_flag_is_reported(self) -> None:
        diff = cmd.diff_flags({"--x": False}, {"--x": True})
        self.assertEqual(diff.only_expected, ("--x",))
        self.assertEqual(diff.compared_count, 1)

    def test_true_is_not_the_same_as_the_string_one(self) -> None:
        diff = cmd.diff_flags({"--x": True}, {"--x": "1"})
        self.assertEqual([d.flag for d in diff.differing], ["--x"])

    def test_values_are_compared_as_strings_without_normalisation(self) -> None:
        self.assertFalse(cmd.diff_flags({"-e": "0.40"}, {"-e": "0.4"}).clean)
        self.assertTrue(cmd.diff_flags({"-e": "0.4"}, {"-e": "0.4"}).clean)

    def test_ignore_entry_that_matches_nothing_is_not_reported(self) -> None:
        diff = cmd.diff_flags({"--x": "1"}, {"--x": "1"}, ignore=("--nothing",))
        self.assertEqual(diff.ignored, ())
        self.assertEqual(diff.compared_count, 1)


class ResourceCoupling(unittest.TestCase):
    """`-R "cpu=N;…"` → `--parallel`（BRIEF §6：当前倍率 1:1，倍率是 BatchOptions 的可配置项）。"""

    def test_parse_resource_string(self) -> None:
        self.assertEqual(cmd.parse_resource_string("cpu=8;mem=64000"), {"cpu": "8", "mem": "64000"})
        self.assertEqual(cmd.parse_resource_string(""), {})
        self.assertEqual(cmd.parse_resource_string(" cpu = 8 ; "), {"cpu": "8"})
        self.assertEqual(cmd.parse_resource_string("cpu"), {"cpu": ""})

    def _plan_with_resources(self, resources: str, multiplier: float = 1.0):
        design = Design(library="<lib>", cell="<cell>", view="<view>", key="d0", resources=resources)
        ctx = PlanContext(
            design=design,
            facts=SiteFacts(ewave_bin="ewave"),
            options=BatchOptions(parallel_multiplier=multiplier),
            batch_dir="/batch",
        )
        run = Run(run_id="d0/base/x", design_key="d0", work_dir="/batch/runs/d0/base")
        return cmd.build_command_plan(run, ctx)

    def test_cpu_is_synced_to_parallel(self) -> None:
        plan = self._plan_with_resources("cpu=12;mem=1000")
        self.assertEqual(plan.flags["--parallel"], "12")
        self.assertIn("--parallel=12", plan.argv)

    def test_cpu_is_synced_to_parallel_negative(self) -> None:
        """反向：倍率不是 1 时 `--parallel` 必须跟着变 —— 否则这条耦合是死的。

        （kit 里 ALPS 那条 `-mt` 必须等于 `cpu` 的硬规则不适用于 eWave，照抄会损失一半算力。）
        """
        plan = self._plan_with_resources("cpu=12;mem=1000", multiplier=2.0)
        self.assertEqual(plan.flags["--parallel"], "24")

    def test_no_cpu_means_no_guess(self) -> None:
        plan = self._plan_with_resources("mem=1000")
        self.assertNotIn("--parallel", plan.flags)

    def test_design_resources_beat_site_defaults(self) -> None:
        design = Design(library="<lib>", cell="<cell>", view="<view>", key="d0", resources="cpu=4")
        ctx = PlanContext(
            design=design,
            facts=SiteFacts(ewave_bin="ewave", dsub_resources="cpu=99"),
            batch_dir="/batch",
        )
        run = Run(run_id="d0/base/x", design_key="d0", work_dir="/batch/runs/d0/base")
        self.assertEqual(cmd.build_command_plan(run, ctx).flags["--parallel"], "4")


class PlanGuards(unittest.TestCase):
    """拼命令之前的几道闸。"""

    def _ctx(self, **overrides) -> PlanContext:
        design = overrides.pop(
            "design", Design(library="<lib>", cell="<cell>", view="<view>", key="d0")
        )
        facts = overrides.pop("facts", SiteFacts(ewave_bin="ewave"))
        return PlanContext(design=design, facts=facts, batch_dir="/batch", **overrides)

    def test_missing_ewave_bin_is_refused(self) -> None:
        from ewave_batch.model import DiscoveryError

        run = Run(run_id="d0/base/x", design_key="d0", work_dir="/batch/runs/d0/base")
        with self.assertRaises(DiscoveryError):
            cmd.build_command_plan(run, self._ctx(facts=SiteFacts()))

    def test_missing_work_dir_is_refused(self) -> None:
        """`--workDir` 空 = eWave 写进当前目录 = 同 corner/temp 的组合互相静默覆盖。"""
        run = Run(run_id="d0/base/x", design_key="d0")
        with self.assertRaises(SpecError):
            cmd.build_command_plan(run, self._ctx())

    def test_unknown_axis_on_run_is_refused(self) -> None:
        run = Run(
            run_id="d0/base/x",
            design_key="d0",
            axis_values={"ghost": "on"},
            work_dir="/batch/runs/d0/base",
        )
        with self.assertRaises(SpecError):
            cmd.build_command_plan(run, self._ctx())

    def test_axis_value_outside_the_axis_is_refused(self) -> None:
        axis = Axis(name="t", values=(AxisValue(value="25.0"),), flags=("--temperature",))
        with self.assertRaises(SpecError):
            cmd.resolve_axis_flags(axis, "85.0", SiteFacts())

    def test_extra_flag_conflict_raises_from_build_command_plan(self) -> None:
        design = Design(
            library="<lib>",
            cell="<cell>",
            view="<view>",
            key="d0",
            extra_flags={"--workDir": "/elsewhere"},
        )
        run = Run(run_id="d0/base/x", design_key="d0", work_dir="/batch/runs/d0/base")
        with self.assertRaises(FlagConflictError):
            cmd.build_command_plan(run, self._ctx(design=design))

    def test_explicit_port_spec_renders_p_and_i(self) -> None:
        """D1b 留的口子：`--all` 表达不了接地端口，所以显式 `-p`/`-i` 必须还能用。"""
        spec = PortSpec(
            mode=PortMode.EXPLICIT,
            mapping=(("P000", "a"), ("P001", "b")),
            signal_ports=("P000",),
        )
        design = Design(
            library="<lib>", cell="<cell>", view="<view>", key="d0", port_spec=spec
        )
        run = Run(run_id="d0/base/x", design_key="d0", work_dir="/batch/runs/d0/base")
        plan = cmd.build_command_plan(run, self._ctx(design=design))
        argv = list(plan.argv)
        self.assertNotIn("--all", argv)
        self.assertNotIn("--all", plan.flags)
        self.assertEqual(argv[-6:], ["-p", "P000=a", "-p", "P001=b", "-i", "P000"])

    def test_include_port_order_can_be_turned_off(self) -> None:
        run = Run(run_id="d0/base/x", design_key="d0", work_dir="/batch/runs/d0/base")
        plan = cmd.build_command_plan(
            run, self._ctx(options=BatchOptions(include_port_order=False))
        )
        self.assertNotIn("--includePortOrder", plan.flags)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
