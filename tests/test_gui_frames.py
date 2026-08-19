# -*- coding: utf-8 -*-
"""P5 三版布局的测试 —— `gui/frames/{stacked,tabbed,split}.py`。

这份测试盯三件事：

1. **三版一致**：`LAYOUT_NAME` 互不相同、`build_frame` / `main` 签名逐字一致、
   `SECTIONS` 完全相同。这是「三个 agent 各写各的、界面手感不一致」唯一的机器判据
   （Phase 0 报备过的已知风险）。
2. **headless 建得起来**：`EWB_SMOKE=1 python -m gui.frames.<v>` 退 0，且控件树真的建了
   （`scripts/check.sh` 第 5 步跑的就是这条命令）。
3. **坏参数当场报错**：`build_frame` 拿到不是 tkinter 容器的 parent、或不满足
   `GuiBridgeProtocol` 的 bridge 时抛 `TypeError`，**不许静默建出半个界面**。

期望值的来源（防自证配方 2 —— 一条都不许从被测代码自己算出来）：

* 八个 section 名 → `mockups/_ui.py` 里的 `build_*` 方法（界面草图是设计成果，
  本测试把它当 fixture 反查，见 `SectionSet.test_sections_match_the_mockup_builders`）；
* 三个布局名与两条签名 → `docs/INTERFACES.md`「全部冻结签名」那一节，手抄成字面量；
* bridge 的 10 个方法 → `ewave_batch.model.GuiBridgeProtocol`（Phase 0 冻结面，别人写的）。

⚠️ `gui/frames/split.py` 由并行 agent 写。本文件**不 import 它的实现细节**，
只在它存在时把它一起拉进一致性比对；不存在时只比 stacked / tabbed 两版并说明原因。
"""

from __future__ import annotations

import importlib
import re
import subprocess
import sys
import unittest
from pathlib import Path

from ewave_batch import model
from ewave_batch.__main__ import _protocol_members, compare_signatures, normalize_signature

ROOT = Path(__file__).resolve().parents[1]

# 手抄自 docs/INTERFACES.md「常量 / 类：谁负责给出什么」表里的 `gui.app.LAYOUTS`。
EXPECTED_LAYOUT_NAMES = ("stacked", "tabbed", "split")

# 手抄自 docs/INTERFACES.md「全部冻结签名」→ gui.frames.*。
EXPECTED_BUILD_FRAME_SIG = "(parent, bridge) -> object"
EXPECTED_MAIN_SIG = "(argv=) -> int"

# 八个顶层构件。来源：mockups/_ui.py 的 build_* 方法（build_menubar 除外，见下面那条测试）。
EXPECTED_SECTIONS = (
    "batchbar",
    "designs",
    "settings",
    "resources",
    "runs",
    "detail",
    "actionbar",
    "statusbar",
)

# 本 agent 负责的两版 —— 它们**必须**在，缺了就是红，不是 skip。
OWNED_LAYOUTS = ("stacked", "tabbed")

MOCKUP_UI = ROOT / "mockups" / "_ui.py"


def load_layouts() -> tuple[dict[str, object], list[str]]:
    """import 得进来的布局模块 + 缺席的名字。

    故意**不**吞掉非 ImportError 的异常：模块存在但 import 时炸了是真 bug，该原样抛。
    """
    found: dict[str, object] = {}
    missing: list[str] = []
    for name in EXPECTED_LAYOUT_NAMES:
        try:
            found[name] = importlib.import_module("gui.frames." + name)
        except ImportError:
            missing.append(name)
    return found, missing


LAYOUTS, MISSING_LAYOUTS = load_layouts()

BRIDGE_SKIP = (
    "本机拿不到「真共用层 + 真 GuiState」—— gui._ui / gui.state 由并行 agent 写，"
    "缺席、没装 tkinter、或构造签名变了都算平台性跳过（本布局自己的测试仍然全跑）。"
)


def real_shared_layer_and_bridge() -> tuple[object | None, object | None]:
    """产品路径要用的两样：共用层模块 + 一个真 bridge。拿不到返回 `(None, None)`。

    ⚠️ **只有集成测试**用这个函数去碰并行 agent 的模块；其余测试一律 `StubBridge`，
    免得别人的模块一动，这份布局测试就跟着红。
    """
    try:
        shared = importlib.import_module("gui._ui")
        from gui.state import GuiState
    except ImportError:
        return None, None
    try:
        bridge = GuiState()
    except TypeError:  # 构造签名不是我们冻结的东西，变了不算本模块的错
        return None, None
    return shared, bridge


def tk_skip_reason() -> str:
    """能不能起 Tk。不能就返回一句原因（平台降级，不是把失败藏起来）。"""
    try:
        import tkinter
    except ImportError as exc:  # pragma: no cover - 本机装了 tkinter
        return "本机没装 tkinter（%s）—— 无 $DISPLAY 的纯 ssh 会话里这是正常的" % exc
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:  # pragma: no cover - 本机有显示
        return "起不了 Tk（%s）—— headless 机器上这是正常的" % exc
    root.destroy()
    return ""


TK_SKIP = tk_skip_reason()


def section_gap(actual: tuple[str, ...], expected: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """两组 section 的差集：`(缺的, 多的)`。空空 = 一致。

    单独写成函数，是为了让正向和反向两条测试**走同一条比较路径**（防自证配方 3）——
    正向用 `assertEqual(..., ((), ()))` 而反向故意弄坏一个值，比的是同一个函数。
    """
    return (
        tuple(sorted(set(expected) - set(actual))),
        tuple(sorted(set(actual) - set(expected))),
    )


class StubBridge:
    """`model.GuiBridgeProtocol` 的测试替身 —— 数据由测试注入，行为全是 no-op。

    **不 import 并行 agent 的 `gui.state.GuiState`**：那是移动目标，而且 frame 的测试
    本来就该只依赖 Protocol。
    """

    def __init__(self, designs=(), axes=(), runs=()) -> None:
        self._designs = tuple(designs)
        self._axes = tuple(axes)
        self._runs = tuple(runs)
        self.calls: list[str] = []

    def load_spec(self, path: str) -> None:
        self.calls.append("load_spec")

    def plan(self) -> None:
        self.calls.append("plan")

    def start(self, *, dry_run: bool = False) -> None:
        self.calls.append("start")

    def tick(self):
        self.calls.append("tick")
        return None

    def cancel(self) -> None:
        self.calls.append("cancel")

    def runs(self):
        return self._runs

    def designs(self):
        return self._designs

    def axes(self):
        return self._axes

    def command_text(self, run_id: str) -> str:
        return ""

    def summary(self) -> dict:
        return {}


class FakeParent:
    """假的 tkinter 容器 —— 只为让 `describe_argument_problems` 认它是个 widget。

    参数检查在建控件**之前**跑，所以坏 bridge 的那几条测试不需要真 Tk。
    """

    tk = object()


def make_axis(name: str, values: tuple[str, ...]) -> model.Axis:
    """用冻结面的真 dataclass 造一根轴（不是自己编的假类 —— 字段名漂了要当场红）。"""
    return model.Axis(name=name, values=tuple(model.AxisValue(value=v) for v in values))


def make_design(cell: str) -> model.Design:
    return model.Design(library="MY_LIB", cell=cell, view="MY_VIEW")


def make_run(index: int) -> model.Run:
    return model.Run(run_id="r%02d" % index, design_key="d")


# --------------------------------------------------------------------------
# 1. 三版一致性
# --------------------------------------------------------------------------


class LayoutIdentity(unittest.TestCase):
    """三版必须是三版：名字互不相同，但对外的形状一模一样。"""

    def test_owned_layouts_import(self) -> None:
        for name in OWNED_LAYOUTS:
            self.assertIn(name, LAYOUTS, "gui/frames/%s.py import 不进来" % name)

    def test_split_is_present(self) -> None:
        if MISSING_LAYOUTS == ["split"]:
            self.skipTest(
                "gui/frames/split.py 由并行 agent 同阶段写，本机此刻还没有。"
                "阶段收尾时它必须在 —— 那时这条会自动转成硬断言。"
            )
        self.assertEqual(MISSING_LAYOUTS, [], "布局模块缺席：%s" % MISSING_LAYOUTS)

    def test_layout_names_are_distinct_and_match_module_name(self) -> None:
        names = {}
        for module_name, module in LAYOUTS.items():
            names[module_name] = module.LAYOUT_NAME
        # 每个模块的 LAYOUT_NAME 必须等于自己的模块名（否则 gui.app.launch("split")
        # 拿到的可能是别的布局，而且没人看得出来）。
        for module_name, layout_name in names.items():
            self.assertEqual(layout_name, module_name)
        self.assertEqual(
            len(set(names.values())), len(names), "LAYOUT_NAME 撞名了：%r" % (names,)
        )
        # 计数断言：真的比了这么多个模块，不是空集合比出来的好看结果。
        self.assertEqual(len(names), len(EXPECTED_LAYOUT_NAMES) - len(MISSING_LAYOUTS))
        self.assertGreaterEqual(len(names), len(OWNED_LAYOUTS))

    def test_build_frame_signature_is_the_frozen_one(self) -> None:
        compared = 0
        for module_name, module in LAYOUTS.items():
            actual = normalize_signature(module.build_frame)
            self.assertIsNone(
                compare_signatures(EXPECTED_BUILD_FRAME_SIG, actual),
                "%s.build_frame 签名漂了：%s" % (module_name, actual),
            )
            compared += 1
        self.assertEqual(compared, len(LAYOUTS))
        self.assertGreaterEqual(compared, len(OWNED_LAYOUTS))
        # 冻结面自己也得是这个形状 —— 手抄的字面量和 model.py 对不上就是抄错了。
        self.assertEqual(normalize_signature(model.build_frame), EXPECTED_BUILD_FRAME_SIG)

    def test_build_frame_signature_negative(self) -> None:
        """反向：多一个参数就必须被同一条比较路径报出来。"""

        def build_frame(parent: object, bridge: object, extra: object = None) -> object:
            return None

        reason = compare_signatures(EXPECTED_BUILD_FRAME_SIG, normalize_signature(build_frame))
        self.assertIsNotNone(reason, "签名比对没抓到多出来的参数 —— 那这条闸门是空的")
        self.assertIn("extra", reason)

    def test_main_signature_is_the_frozen_one(self) -> None:
        compared = 0
        for module_name, module in LAYOUTS.items():
            actual = normalize_signature(module.main)
            self.assertIsNone(
                compare_signatures(EXPECTED_MAIN_SIG, actual),
                "%s.main 签名漂了：%s" % (module_name, actual),
            )
            compared += 1
        self.assertEqual(compared, len(LAYOUTS))
        self.assertEqual(normalize_signature(model.main), EXPECTED_MAIN_SIG)

    def test_main_signature_negative(self) -> None:
        def main(args: object = None) -> int:
            return 0

        reason = compare_signatures(EXPECTED_MAIN_SIG, normalize_signature(main))
        self.assertIsNotNone(reason, "参数改名了都没报 —— 那这条闸门是空的")
        self.assertIn("args", reason)


class SectionSet(unittest.TestCase):
    """三版必须暴露**同一组**顶层构件 —— 布局只决定摆在哪，不决定有没有。"""

    def test_sections_match_the_mockup_builders(self) -> None:
        """期望值反查草图：`mockups/_ui.py` 有哪些 `build_*`，section 就该是哪些。"""
        if not MOCKUP_UI.is_file():
            self.skipTest("本机没有 mockups/_ui.py（部署包里它被 export-ignore 了，正常）")
        text = MOCKUP_UI.read_text(encoding="utf-8")
        builders = set(re.findall(r"^    def build_(\w+)\(", text, flags=re.M))
        self.assertTrue(builders, "从 mockups/_ui.py 里一个 build_* 都没数出来 —— 正则过时了")
        # build_menubar 不是「摆放件」：菜单条挂在 Tk 根窗口上，不参与 frame 的布局，
        # 所以它不在 SECTIONS 里。除它之外，两边必须一个不多一个不少。
        self.assertEqual(builders - {"menubar"}, set(EXPECTED_SECTIONS))
        self.assertEqual(len(EXPECTED_SECTIONS), 8)

    def test_every_layout_exposes_the_same_sections(self) -> None:
        compared = 0
        for module_name, module in LAYOUTS.items():
            sections = getattr(module, "SECTIONS", None)
            if sections is None:
                # split 由并行 agent 写，SECTIONS 还没进冻结面（已写进
                # interface_change_requests）。缺了就说清楚，不当成一致。
                self.assertNotIn(
                    module_name, OWNED_LAYOUTS, "%s 少了 SECTIONS" % module_name
                )
                continue
            self.assertEqual(
                section_gap(tuple(sections), EXPECTED_SECTIONS),
                ((), ()),
                "%s.SECTIONS 和草图那八件对不上" % module_name,
            )
            self.assertEqual(tuple(sections), EXPECTED_SECTIONS, "%s 的顺序也得一致" % module_name)
            compared += 1
        # 计数断言：至少把我这两版都比过了（空集合的 diff 永远好看）。
        self.assertEqual(compared, len([n for n in LAYOUTS if hasattr(LAYOUTS[n], "SECTIONS")]))
        self.assertGreaterEqual(compared, len(OWNED_LAYOUTS))

    def test_split_exposes_sections_too(self) -> None:
        """三版一致的判据要三版都在场才成立 —— split 缺 `SECTIONS` 时把话说明白。

        `SECTIONS` 还没进冻结面（`docs/INTERFACES.md` 只冻了 `LAYOUT_NAME` /
        `build_frame` / `main`），所以 split 没有它**不是它的错**。
        但那意味着「三版暴露同一组构件」目前只被验了 2/3 ——
        这条已写进本阶段的 `interface_change_requests`，别让它悄悄过去。
        """
        module = LAYOUTS.get("split")
        if module is None:
            self.skipTest("gui/frames/split.py 还没有（并行 agent 在写）")
        sections = getattr(module, "SECTIONS", None)
        if sections is None:
            self.skipTest(
                "gui.frames.split 没有 SECTIONS —— 它没进冻结面，所以不算 split 的错；"
                "但三版一致的判据因此只覆盖 stacked/tabbed。见 interface_change_requests。"
            )
        self.assertEqual(tuple(sections), EXPECTED_SECTIONS)

    def test_section_gap_reports_a_missing_section_negative(self) -> None:
        """反向：拿同一条比较路径，故意删掉一个 section，必须被指名报出来。"""
        broken = tuple(s for s in EXPECTED_SECTIONS if s != "runs")
        self.assertEqual(section_gap(broken, EXPECTED_SECTIONS), (("runs",), ()))
        extra = EXPECTED_SECTIONS + ("sidebar",)
        self.assertEqual(section_gap(extra, EXPECTED_SECTIONS), ((), ("sidebar",)))


# --------------------------------------------------------------------------
# 2. 坏参数当场报错
# --------------------------------------------------------------------------


class ArgumentChecks(unittest.TestCase):
    """`build_frame` 的入参检查 —— 报错要明确，且不许连好的一起拒。"""

    def modules(self) -> list[tuple[str, object]]:
        return [(name, LAYOUTS[name]) for name in OWNED_LAYOUTS if name in LAYOUTS]

    def test_bridge_method_list_matches_the_frozen_protocol(self) -> None:
        """写死的 `BRIDGE_METHODS` 必须和冻结面上的 Protocol 逐字相等。"""
        frozen = getattr(model.GuiBridgeProtocol, "__protocol_attrs__", None)
        expected = set(frozen) if frozen else set(_protocol_members(model.GuiBridgeProtocol))
        self.assertEqual(len(expected), 10, "GuiBridgeProtocol 的方法数变了：%r" % sorted(expected))
        for name, module in self.modules():
            self.assertEqual(set(module.BRIDGE_METHODS), expected, "%s 的 bridge 方法表漂了" % name)
            self.assertEqual(len(module.BRIDGE_METHODS), len(expected))

    def test_good_arguments_are_not_rejected(self) -> None:
        """先证明检查器不是「见谁拒谁」—— 否则下面的反向测试全是空绿。"""
        for name, module in self.modules():
            self.assertEqual(
                module.describe_argument_problems(FakeParent(), StubBridge()),
                [],
                "%s 把一个合法的 (parent, bridge) 拒了" % name,
            )

    def test_each_missing_bridge_method_is_named_negative(self) -> None:
        """反向：逐个抽掉 bridge 的一个方法，必须**指名**报出来，且只报这一条。"""
        for name, module in self.modules():
            checked = 0
            for method in module.BRIDGE_METHODS:
                bridge = StubBridge()
                setattr(bridge, method, None)  # 实例属性盖住类方法 = 这个方法没了
                problems = module.describe_argument_problems(FakeParent(), bridge)
                self.assertEqual(len(problems), 1, "%s 少了 %s 却报了 %r" % (name, method, problems))
                self.assertIn(method, problems[0])
                self.assertIn("GuiBridgeProtocol", problems[0])
                checked += 1
            # 计数断言：10 个方法一个不落地试过了。
            self.assertEqual(checked, len(module.BRIDGE_METHODS))
            self.assertEqual(checked, 10)

    def test_bad_parent_is_named(self) -> None:
        for name, module in self.modules():
            for bad in (None, object(), "not a widget"):
                problems = module.describe_argument_problems(bad, StubBridge())
                self.assertEqual(len(problems), 1, "%s 对 %r 的报告不对：%r" % (name, bad, problems))
                self.assertIn("parent", problems[0])

    def test_build_frame_raises_type_error_on_bad_bridge(self) -> None:
        for name, module in self.modules():
            with self.assertRaises(TypeError) as caught:
                module.build_frame(FakeParent(), object())
            message = str(caught.exception)
            self.assertIn("build_frame", message)
            self.assertIn("GuiBridgeProtocol", message)
            self.assertIn(name, message)

    def test_build_frame_raises_type_error_on_bad_parent(self) -> None:
        for name, module in self.modules():
            with self.assertRaises(TypeError) as caught:
                module.build_frame(None, StubBridge())
            self.assertIn("parent", str(caught.exception))


# --------------------------------------------------------------------------
# 3. headless 构建
# --------------------------------------------------------------------------


@unittest.skipIf(TK_SKIP, TK_SKIP)
class HeadlessBuild(unittest.TestCase):
    """控件树真的建起来了 —— 不是「函数没抛异常」而已。"""

    def setUp(self) -> None:
        import tkinter

        self.root = tkinter.Tk()
        self.root.withdraw()

    def tearDown(self) -> None:
        self.root.destroy()

    def test_stacked_builds_every_section(self) -> None:
        module = LAYOUTS["stacked"]
        frame = module.build_frame(self.root, StubBridge())
        self.assertEqual(frame.sections_built, EXPECTED_SECTIONS)
        self.assertEqual(len(frame.sections_built), 8)
        self.assertEqual(frame.layout_name, "stacked")
        # StubBridge 只满足冻结面，喂不饱 gui._ui.BaseApp ⇒ 这里走的是占位版那条路。
        # 明写出来，免得哪天路径变了而测试还绿着（走哪条路是被测行为的一部分）。
        self.assertEqual(frame.widget_kit_name, "")
        # 每个 section 至少落一个 widget，外加布局自己的容器和分隔线。
        self.assertGreater(module.count_widgets(frame), len(EXPECTED_SECTIONS))

    def test_tabbed_builds_every_section_and_four_tabs(self) -> None:
        module = LAYOUTS["tabbed"]
        frame = module.build_frame(self.root, StubBridge())
        self.assertEqual(frame.sections_built, EXPECTED_SECTIONS)
        self.assertEqual(frame.layout_name, "tabbed")
        self.assertEqual(len(frame.notebook.tabs()), 4)
        titles = [frame.notebook.tab(i, "text").strip() for i in range(4)]
        # tab 标题带计数（"Designs (0)"），所以比开头而不是全等。
        for expected, actual in zip(module.TAB_NAMES, titles):
            self.assertTrue(actual.startswith(expected), "tab %r 不是 %r 开头" % (actual, expected))
        self.assertGreater(module.count_widgets(frame), len(EXPECTED_SECTIONS))

    def test_two_layouts_place_the_same_sections_differently(self) -> None:
        """同一组构件、不同的树形 —— 这就是「三版只在布局上分岔」的直接证据。"""
        stacked = LAYOUTS["stacked"].build_frame(self.root, StubBridge())
        tabbed = LAYOUTS["tabbed"].build_frame(self.root, StubBridge())
        self.assertEqual(stacked.sections_built, tabbed.sections_built)
        # Tabbed 多一个 notebook + 四个 page，树必然更大；一样大说明有一版没真摆。
        self.assertNotEqual(
            LAYOUTS["stacked"].count_widgets(stacked),
            LAYOUTS["tabbed"].count_widgets(tabbed),
        )


@unittest.skipIf(TK_SKIP, TK_SKIP)
class WidgetKitWiring(unittest.TestCase):
    """布局到底给共用层递了什么 —— 「三版只在布局上分岔」的直接证据就在这些数字里。

    期望值全部手抄自界面草图：`mockups/stacked.py`（Runs 9 行）、
    `mockups/tabbed.py`（Runs 20 行、Designs 12 行、动作栏带乘法公式）。
    草图是设计成果、由人写的，拿它当 fixture 才不是自己证明自己。
    """

    def setUp(self) -> None:
        import tkinter

        self.root = tkinter.Tk()
        self.root.withdraw()

    def tearDown(self) -> None:
        self.root.destroy()

    def make_kit(self, accept_hints: bool = True):
        """假共用层：记下每个 section 收到的布局提示，返回真 widget 好让 pack 生效。"""
        import types
        from tkinter import ttk

        kit = types.SimpleNamespace()
        kit.__name__ = "tests.recording_kit"
        kit.calls = {}

        def make_builder(section: str):
            if accept_hints:

                def builder(parent, bridge=None, **hints):
                    kit.calls[section] = dict(hints)
                    return ttk.Frame(parent)

            else:

                def builder(parent):  # 只认 parent —— 用来验「提示落空要出声」
                    kit.calls[section] = {}
                    return ttk.Frame(parent)

            return builder

        for section in EXPECTED_SECTIONS:
            setattr(kit, "build_" + section, make_builder(section))
        return kit

    def place(self, layout: str, kit):
        """直接调 `place_sections` —— 布局的全部内容就在这一个函数里，不用起整个 app。"""
        from tkinter import ttk

        root = ttk.Frame(self.root)
        return LAYOUTS[layout].place_sections(kit, root, StubBridge())

    def test_stacked_asks_the_kit_for_the_sketch_1a_layout(self) -> None:
        kit = self.make_kit()
        placed = self.place("stacked", kit)
        # 计数断言：八个 section 一个不落地经过了构件层（空 dict 的比对永远好看）。
        self.assertEqual(len(kit.calls), 8)
        self.assertEqual(sorted(kit.calls), sorted(EXPECTED_SECTIONS))
        self.assertEqual(placed["built"], EXPECTED_SECTIONS)
        self.assertEqual(kit.calls["designs"], {"widths": (250, 250, 210), "rows": 2})
        self.assertEqual(kit.calls["settings"], {"compact": False, "show_formula": False})
        self.assertEqual(kit.calls["runs"], {"rows": 9})
        self.assertEqual(kit.calls["batchbar"], {})
        self.assertEqual(placed["dropped"], ())

    def test_tabbed_asks_the_kit_for_the_sketch_1b_layout(self) -> None:
        kit = self.make_kit()
        placed = self.place("tabbed", kit)
        self.assertEqual(len(kit.calls), 8)
        self.assertEqual(placed["built"], EXPECTED_SECTIONS)
        self.assertEqual(
            kit.calls["designs"],
            {"widths": (300, 300, 260), "rows": 12, "buttons": "three", "titled": False},
        )
        self.assertEqual(
            kit.calls["settings"],
            {"compact": False, "title": " Extraction settings ", "show_formula": False},
        )
        self.assertEqual(
            kit.calls["runs"], {"rows": 20, "titled": False, "header_in_title": False}
        )
        # 批次栏和动作栏在 notebook 外面常驻 —— 所以这一版要 show_dir / 乘法公式。
        self.assertEqual(kit.calls["batchbar"], {"show_dir": True})
        self.assertEqual(kit.calls["actionbar"], {"show_formula": True, "show_dir": False})
        self.assertEqual(placed["dropped"], ())

    def test_the_two_layouts_really_differ(self) -> None:
        """同一套构件、不同的数字。一样就说明有人照抄了另一版，界面白分三版。"""
        stacked_kit = self.make_kit()
        self.place("stacked", stacked_kit)
        tabbed_kit = self.make_kit()
        self.place("tabbed", tabbed_kit)
        self.assertEqual(sorted(stacked_kit.calls), sorted(tabbed_kit.calls))
        self.assertNotEqual(stacked_kit.calls["runs"], tabbed_kit.calls["runs"])
        self.assertEqual(stacked_kit.calls["runs"]["rows"], 9)
        self.assertEqual(tabbed_kit.calls["runs"]["rows"], 20)

    def test_ignored_layout_hints_are_reported_negative(self) -> None:
        """反向：构件层不认这些提示时必须**出声**，不许静默吞掉。

        同一条构造路径，只把 builder 换成「只收 parent」的那种。
        静默吞掉的后果是三版长得一模一样而没人发现 —— 那正是本阶段要防的事。
        """
        kit = self.make_kit(accept_hints=False)
        placed = self.place("stacked", kit)
        self.assertEqual(len(kit.calls), 8)
        dropped = placed["dropped"]
        self.assertIn("runs.rows", dropped)
        self.assertIn("designs.widths", dropped)
        # 只有真给过提示的 section 才该出现在报告里。
        self.assertNotIn("batchbar", "".join(dropped))
        self.assertEqual(len(dropped), 5)

    def test_the_real_shared_layer_accepts_every_hint(self) -> None:
        """整条产品路径：真共用层 + 真 `GuiState` —— 一个布局提示都不许落空。

        这是「recording kit 的期望值抄对了没有」的反查：假 kit 收 `**hints` 来者不拒，
        真 `gui._ui.BaseApp` 的 builder 是具名参数，名字对不上就会进 `dropped_hints`。
        """
        shared, bridge = real_shared_layer_and_bridge()
        if shared is None:
            self.skipTest(BRIDGE_SKIP)
        for layout in OWNED_LAYOUTS:
            module = LAYOUTS[layout]
            self.assertTrue(
                module.shared_layer_usable(shared, bridge),
                "%s 认不出真共用层 —— 产品路径会静默退成占位版" % layout,
            )
            frame = module.build_frame(self.root, bridge)
            self.assertEqual(frame.widget_kit_name, module.SHARED_LAYER_MODULE)
            self.assertEqual(frame.sections_built, EXPECTED_SECTIONS)
            self.assertEqual(
                frame.dropped_hints, (), "%s 有布局提示落空了 —— 那一版会长得跟别版一样" % layout
            )

    def test_a_frozen_face_only_bridge_falls_back_negative(self) -> None:
        """反向：只满足冻结面的 bridge 喂不饱共用层 ⇒ 必须走占位版，而不是崩。

        与上一条同一条构造路径，只换 bridge。两条一起证明「判据真的在判」——
        永远走共用层或永远走占位版，都会让上一条空绿。
        """
        shared, _ = real_shared_layer_and_bridge()
        if shared is None:
            self.skipTest(BRIDGE_SKIP)
        for layout in OWNED_LAYOUTS:
            module = LAYOUTS[layout]
            self.assertFalse(module.shared_layer_usable(shared, StubBridge()))
            frame = module.build_frame(self.root, StubBridge())
            self.assertEqual(frame.widget_kit_name, "")
            self.assertEqual(frame.sections_built, EXPECTED_SECTIONS)


@unittest.skipIf(TK_SKIP, TK_SKIP)
class TabbedRunCount(unittest.TestCase):
    """Run count 面板：草图 1b 用它换掉「设定和 run 不同屏」的代价。"""

    def setUp(self) -> None:
        import tkinter

        self.root = tkinter.Tk()
        self.root.withdraw()

    def tearDown(self) -> None:
        self.root.destroy()

    def test_panel_shows_counts_from_the_bridge(self) -> None:
        bridge = StubBridge(
            designs=(make_design("CELL_A"), make_design("CELL_B")),
            axes=(make_axis("corner", ("a", "b", "c")), make_axis("temperature", ("x", "y"))),
            runs=tuple(make_run(i) for i in range(12)),
        )
        frame = LAYOUTS["tabbed"].build_frame(self.root, bridge)
        rows = frame.refresh_counts()
        # 期望值是手算的：2 个 design、corner 3 个取值、temperature 2 个取值。
        self.assertEqual(rows, (("designs", 2), ("corner", 3), ("temperature", 2)))
        self.assertEqual(len(rows), 1 + len(bridge.axes()))
        self.assertEqual(frame.total_label.cget("text"), "12 runs")
        self.assertEqual(frame.notebook.tab(0, "text").strip(), "Designs (2)")
        self.assertEqual(frame.notebook.tab(3, "text").strip(), "Runs (12)")

    def test_panel_rereads_the_bridge_negative(self) -> None:
        """反向：换掉 bridge 里的数据再 refresh，面板必须跟着变。

        走的是同一条构造路径（同一个 StubBridge、同一个 frame）——
        只改数据。不变就说明面板显示的是建界面那一刻的快照，那种「一致」是假的。
        """
        bridge = StubBridge(
            designs=(make_design("CELL_A"), make_design("CELL_B")),
            axes=(make_axis("corner", ("a", "b", "c")), make_axis("temperature", ("x", "y"))),
            runs=tuple(make_run(i) for i in range(12)),
        )
        frame = LAYOUTS["tabbed"].build_frame(self.root, bridge)
        before = frame.refresh_counts()

        bridge._designs = (make_design("CELL_A"),)
        bridge._axes = (make_axis("corner", ("a", "b", "c", "d", "e")),)
        bridge._runs = tuple(make_run(i) for i in range(5))
        after = frame.refresh_counts()

        self.assertNotEqual(before, after)
        self.assertEqual(after, (("designs", 1), ("corner", 5)))
        self.assertEqual(frame.total_label.cget("text"), "5 runs")
        self.assertEqual(frame.notebook.tab(3, "text").strip(), "Runs (5)")


class AxisCounts(unittest.TestCase):
    """`axis_counts` 是纯函数，不需要 Tk —— 数值判据放这里，界面判据放上面。"""

    def module(self):
        return LAYOUTS["tabbed"]

    def test_counts_one_row_per_axis_plus_designs(self) -> None:
        bridge = StubBridge(
            designs=(make_design("CELL_A"), make_design("CELL_B")),
            axes=(make_axis("corner", ("a", "b", "c")), make_axis("temperature", ("x", "y"))),
        )
        rows = self.module().axis_counts(bridge)
        self.assertEqual(rows, [("designs", 2), ("corner", 3), ("temperature", 2)])
        self.assertEqual(len(rows), 1 + len(bridge.axes()))

    def test_counts_follow_the_data_negative(self) -> None:
        """反向：同一条构造路径，只把 corner 的取值从 3 个改成 5 个 —— 必须报 5。"""
        bridge = StubBridge(
            designs=(make_design("CELL_A"), make_design("CELL_B")),
            axes=(make_axis("corner", ("a", "b", "c")), make_axis("temperature", ("x", "y"))),
        )
        before = self.module().axis_counts(bridge)
        bridge._axes = (
            make_axis("corner", ("a", "b", "c", "d", "e")),
            make_axis("temperature", ("x", "y")),
        )
        after = self.module().axis_counts(bridge)
        self.assertNotEqual(before, after)
        self.assertEqual(after, [("designs", 2), ("corner", 5), ("temperature", 2)])

    def test_no_axes_still_reports_designs(self) -> None:
        rows = self.module().axis_counts(StubBridge(designs=(make_design("CELL_A"),)))
        self.assertEqual(rows, [("designs", 1)])


# --------------------------------------------------------------------------
# 4. check.sh 第 5 步跑的那条命令
# --------------------------------------------------------------------------


class SmokeEntryPoints(unittest.TestCase):
    """两条 headless 入口：闸门跑的那条（走整条产品路径）+ 本模块自带的独立那条。"""

    def run_module(self, layout: str, args: tuple[str, ...] = (), smoke_env: bool = False):
        import os

        env = dict(os.environ)
        if smoke_env:
            env["EWB_SMOKE"] = "1"
        else:
            env.pop("EWB_SMOKE", None)
        return subprocess.run(
            [sys.executable, "-m", "gui.frames." + layout, *args],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
        )

    def test_gate_command_exits_zero(self) -> None:
        """`EWB_SMOKE=1 python -m gui.frames.<v>` —— `scripts/check.sh` 第 5 步的原样复现。

        这条走的是**整条产品路径**（`gui._ui.frame_main` → `gui.app.launch` → 本布局的
        `build_frame`），所以它同时验了「我摆的东西真共用层收得下」。
        """
        for layout in OWNED_LAYOUTS:
            proc = self.run_module(layout, smoke_env=True)
            self.assertEqual(
                proc.returncode,
                0,
                "%s 退了 %d\nstdout:\n%s\nstderr:\n%s"
                % (
                    layout,
                    proc.returncode,
                    proc.stdout.decode("utf-8", "replace"),
                    proc.stderr.decode("utf-8", "replace"),
                ),
            )

    def test_standalone_smoke_reports_all_eight_sections(self) -> None:
        """`--smoke` 是本模块自带的那条：不经共用层、不经 gui.app，只证明本布局自己好。"""
        for layout in OWNED_LAYOUTS:
            proc = self.run_module(layout, args=("--smoke",))
            out = proc.stdout.decode("utf-8", "replace")
            err = proc.stderr.decode("utf-8", "replace")
            self.assertEqual(
                proc.returncode, 0, "%s 退了 %d\n%s\n%s" % (layout, proc.returncode, out, err)
            )
            self.assertTrue(out.startswith("smoke %s:" % layout), "冒烟没说话：%r" % out)
            if "skipped" in out:
                # 平台降级（没 tkinter / 没显示）。本机有显示时不该走到这。
                self.assertTrue(TK_SKIP, "本机起得了 Tk，冒烟却报 skipped：%r" % out)
                continue
            self.assertIn("8/8 sections", out)
            # 独立那条必须走占位版 —— 它的全部价值就是「不依赖别人的模块」。
            self.assertIn("kit=none", out)


class LazyTkinterImport(unittest.TestCase):
    """CLAUDE.md 硬约束 5：模块顶层不许 import tkinter，也不许在 import 时建 Tk()。"""

    def test_no_module_level_tkinter_import(self) -> None:
        import ast

        for name in OWNED_LAYOUTS:
            path = ROOT / "gui" / "frames" / (name + ".py")
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:  # 只看**顶层**语句：函数体里的 import 正是我们要的
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(
                            alias.name.split(".")[0] == "tkinter",
                            "%s 顶层 import 了 tkinter" % name,
                        )
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(
                        (node.module or "").split(".")[0],
                        "tkinter",
                        "%s 顶层 from tkinter import ..." % name,
                    )

    def test_importing_the_module_does_not_load_tkinter(self) -> None:
        """真跑一遍：一个干净的子进程里 import 布局模块，`sys.modules` 里不该出现 tkinter。"""
        code = (
            "import sys, importlib\n"
            "assert 'tkinter' not in sys.modules\n"
            "importlib.import_module('gui.frames.stacked')\n"
            "importlib.import_module('gui.frames.tabbed')\n"
            "assert 'tkinter' not in sys.modules, 'import 时就把 tkinter 拉进来了'\n"
            "print('lazy ok')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(ROOT), capture_output=True
        )
        self.assertEqual(
            proc.returncode,
            0,
            proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace"),
        )


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(TK_SKIP, TK_SKIP)
class SplitDividerIsDraggable(unittest.TestCase):
    """1c 的左右分隔必须是**可拖的 sash**，左栏宽度必须由**内容**决定。

    ## 出处：2026-08-19 用户实测 + 截图

    原来 split 的写法是 `Frame(width=452)` + `pack_propagate(False)` + `ttk.Separator`：

    * `Separator` 是画上去的一条线，**拖不动** —— 用户明确反馈了这一条；
    * `pack_propagate(False)` 把左栏钉死在 452px，内容超出就**被裁掉**。
      裁掉的东西里包括 **Corner 那行的第 5 个勾选框（`typical`）** ——
      最常用的工艺角**点不到**。那不是观感问题，是功能缺陷。

    452 是照设计稿抄的，而设计稿是拿 Windows 默认字体画的；红区是 Linux，
    字体度量不同 ⇒ 同样的内容更宽、裁得更狠。**任何写死的像素宽度都会在气隙对面
    变成另一个 bug**，所以这里钉的不是"宽度等于多少"，而是"宽度不许被写死"。
    """

    def setUp(self) -> None:
        import tkinter

        self.root = tkinter.Tk()
        self.root.withdraw()

    def tearDown(self) -> None:
        self.root.destroy()

    def _build_split(self):
        """用**真的** `GuiState` 而不是 StubBridge。

        这几条测的是「真实内容有多宽」——替身给的是空数据，撑不出真实宽度，
        测出来的结论就不是用户看到的那个界面（而这几条正是为一个"用户看到了、
        我们没看到"的 bug 加的）。
        """
        from gui.state import GuiState

        module = LAYOUTS["split"]
        return module.build_frame(self.root, GuiState())

    def _walk(self, widget):
        yield widget
        for child in widget.winfo_children():
            yield from self._walk(child)

    def test_split_uses_a_paned_window(self) -> None:
        """分隔条得是 `ttk.Panedwindow` 的 sash —— 那才是能拖的东西。"""
        from tkinter import ttk

        frame = self._build_split()
        paned = [w for w in self._walk(frame) if isinstance(w, ttk.Panedwindow)]
        self.assertEqual(
            len(paned), 1,
            "split 里应当恰好有一个 PanedWindow（左右两栏之间那个可拖的分隔）",
        )
        # 两个 pane：左配置、右 Runs。计数断言 —— 少一个就说明有一栏没被放进去，
        # 那时 sash 无处可拖，等于退回了不可拖的老样子。
        self.assertEqual(len(paned[0].panes()), 2)

    def test_no_pane_has_propagation_disabled(self) -> None:
        """`pack_propagate(False)` / `grid_propagate(False)` 是内容被裁的直接原因。

        它们的语义是"我不管孩子要多大，我就这么大"。在一个字体度量会变的目标平台上，
        这等于"到了那边就裁给你看"。整棵树里都不许有。
        """
        frame = self._build_split()
        offenders = []
        for widget in self._walk(frame):
            try:
                if widget.pack_propagate() is False or widget.grid_propagate() is False:
                    offenders.append(str(widget))
            except Exception:  # pragma: no cover - 个别控件不支持这两个查询
                continue
        self.assertEqual(
            offenders, [],
            "这些控件关掉了尺寸传播，内容超出时会被静默裁掉：%s" % offenders,
        )

    def test_left_pane_is_wide_enough_for_its_content(self) -> None:
        """左栏**请求**的宽度要能装下它最宽的那个子控件。

        这条是「typical 勾选框看不见」那个 bug 的直接判据：内容要求 N 像素，
        左栏却只有 452，差额就是被裁掉的部分。
        """
        from tkinter import ttk

        frame = self._build_split()
        frame.update_idletasks()
        paned = [w for w in self._walk(frame) if isinstance(w, ttk.Panedwindow)][0]
        left = frame.nametowidget(paned.panes()[0])
        need = max((c.winfo_reqwidth() for c in left.winfo_children()), default=0)
        self.assertGreater(need, 0, "左栏一个子控件都没有？前提变了，这条测试要重写")
        self.assertGreaterEqual(
            left.winfo_reqwidth(), need,
            "左栏请求的宽度装不下它自己的内容 —— 超出的部分会被裁掉，"
            "而被裁掉的东西是点不到的",
        )

    def test_split_window_is_wider_than_the_shared_default_negative(self) -> None:
        """反向的一半：1c 的默认窗口必须比共用默认宽。

        左栏由内容决定宽度之后，共用的 1180 减掉它，右边 Runs 表只剩四百来像素 ——
        而"勾选和 run 表同屏"是选这一版的**全部理由**。表被挤瘦 = 理由没了。
        """
        module = LAYOUTS["split"]
        shared = int(_ui_module().BaseApp.GEOMETRY.split("x")[0])
        mine = int(module.SplitApp.GEOMETRY.split("x")[0])
        self.assertGreater(mine, shared, "1c 的默认窗口应当比共用默认更宽")


def _ui_module():
    from gui import _ui

    return _ui
