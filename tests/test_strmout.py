"""`tools.strmout` 的测试 —— **D1c 的机器判据**。

D1c 说的是：把官方 `gdsout_setup` 当模板，只替换随 design 变的那几个字段，**其余逐字复现**。
它已由 MVP 实测证完（我们导的 GDS 与官方 GUI 导的 GDS 产出 md5 相同的 mesh，
BRIEF §10「D1c 已证完」）。但那次实测在红区、由人跑，**这里要留一份机器每次都能跑的**。

渲染是纯字符串函数 ⇒ 最容易做 golden 测试，也**最必须**做：丢掉一行照样导得出 GDS、
eWave 照样跑得完、数字还挺像，只有 mesh 悄悄变了。

四条防自证配方（`docs/OVERNIGHT.md`）在这份文件里的落点：

1. **关键测试** = `test_render_golden_*`（渲染结果逐字节等于期望文本）、
   `test_build_strmout_plan_argv`（argv 等于期望值）；
2. **期望值全部手写**，来源写在每条常量的注释里 —— `GOLDEN_RENDERED` 是从
   `mvp/redzone/gdsout_setup.tmpl`（已进 git 的占位符版）逐行抄下来、把 7 个占位符换成
   本文件里的假值；argv 形状抄自 `mvp/redzone/step1_strmout.sh` 实跑过的那一条。
   **没有一处用被测函数的输出当期望值。**
3. **反向验证**：每条关键测试配一条 `_negative`，与正向走同一条输入构造路径
   （`_fields()` / `_ctx()`），只改坏一个东西，断言 `diff_gdsout_setup` **报告了**它；
4. **计数断言 + 过滤器测试**：字段总数 `GOLDEN_FIELD_COUNT`（人数出来的 24）、
   `FlagDiff.compared_count`、以及 `@@VIEW@@` 不许吃掉 `@@VIEWPORT@@` /
   `ignore=("view",)` 不许吃掉 `viewPort`（MVP 那个 `--sparam` 误伤 `--sparamImpedance`
   的同构物，写一条防将来）。

🚨 本文件零站点标识符：library/cell/view/路径全是显式假值（`TESTLIB` / `/tmp/...`）。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from ewave_batch.model import (
    Design,
    GdsoutFields,
    PlanContext,
    SiteFacts,
    SpecError,
    Stage,
    ToolMissingError,
)
from ewave_batch.tools import strmout

ROOT = Path(__file__).resolve().parents[1]
TMPL_PATH = ROOT / "mvp" / "redzone" / "gdsout_setup.tmpl"

TMPL_SKIP_REASON = (
    "本机没有 mvp/redzone/gdsout_setup.tmpl —— `.gitattributes` 把 mvp/ 标了 export-ignore，"
    "所以 `git archive` 打出来的红区包里本来就没有它。这条对照测试只在开发仓里跑"
    "（源码里的 DEFAULT_GDSOUT_TEMPLATE 才是产品用的那份，其余测试全都在测它）"
)

# --------------------------------------------------------------------------
# 手写的假值 —— 全部是本文件自造的，与任何真实站点无关
# --------------------------------------------------------------------------

FAKE_BATCH_DIR = "/tmp/ewb"
FAKE_GDS_DIR = "/tmp/ewb/gds"
FAKE_LIBRARY = "TESTLIB"
FAKE_CELL = "TESTCELL"
FAKE_VIEW = "testview"
FAKE_STRM_FILE = "TESTCELL.gds"
FAKE_LOG_FILE = "/tmp/ewb/gds/TESTCELL.gds_out.log"
FAKE_LAYERMAP = "/tmp/fake_pdk/fake.layermap"
FAKE_STRMOUT_BIN = "/tmp/fakebin/strmout"
FAKE_SETUP_PATH = "/tmp/ewb/gdsout/d0.gdsout_setup"

GOLDEN_FIELD_COUNT = 24
"""**人从 `mvp/redzone/gdsout_setup.tmpl` 数出来的**：24 行 = 24 个字段，一行一个，无空行。

这个数字是计数断言的锚。渲染时丢掉一行不会有任何报错信号 —— GDS 照样导得出来，
只有 mesh 变了（D1c 那 8 个字段任何一个丢了都是这样）。所以"渲染前后字段数相同"
和"等于 24"必须被显式断言，不能靠"看起来对"。
"""

GOLDEN_RENDERED = (
    '\trunDir\t\t\t"/tmp/ewb/gds"\n'
    '\tlibrary\t\t"TESTLIB"\n'
    '\ttopCell\t\t"TESTCELL"\n'
    '\tview\t\t"testview"\n'
    '\tstrmFile\t\t"TESTCELL.gds"\n'
    "\thierDepth\t\t32\n"
    "\tmaxVertices\t\t200\n"
    '\trefLibList\t\t""\n'
    '\tstrmVersion\t\t"5"\n'
    "\tarrayInstToScalar\n"
    '\tcase\t"preserve"\n'
    '\tconvertDot\t"ignore"\n'
    '\tlogFile\t\t"/tmp/ewb/gds/TESTCELL.gds_out.log"\n'
    '\tcellMap\t\t""\n'
    '\tlayerMap\t\t"/tmp/fake_pdk/fake.layermap"\n'
    '\tfontMap\t\t""\n'
    '\tconvertPin\t\t"geometry"\n'
    "\tpinAttNum\t\t1\n"
    '\tpropMap     ""\n'
    '\tuserSkillFile   ""\n'
    '\tviaMap  ""\n'
    '\tsummaryFile     ""\n'
    '\ttechLib     ""\n'
    '\tobjectMap   ""\n'
)
"""**期望值：手写。** 从 `mvp/redzone/gdsout_setup.tmpl` 逐行抄下来，7 个占位符换成上面那些假值。

⚠️ 空白是**逐字节**抄的，不是随手排版的：`runDir` 后面 3 个 tab、`library` 后面 2 个、
`case` 后面 1 个、`propMap` 后面 5 个**空格**（不是 tab）、`viaMap` 后面 2 个空格……
官方文件本来就不整齐。D1c 的实测背书是针对这份原貌的，"顺手对齐一下"就等于换了个东西测。
写成 `\\t` 转义而不是真 tab，是为了让这件事在 diff 里看得见、也免得编辑器把 tab 转成空格。
"""

CRITICAL_LINES = (
    "\thierDepth\t\t32\n",
    "\tmaxVertices\t\t200\n",
    '\tstrmVersion\t\t"5"\n',
    '\tcase\t"preserve"\n',
    '\tconvertPin\t\t"geometry"\n',
    "\tpinAttNum\t\t1\n",
    '\tconvertDot\t"ignore"\n',
    "\tarrayInstToScalar\n",
)
"""D1c 点名的 8 个「会改变 GDS 内容」的字段，**连同它们的空白一起**手写在这里。

BRIEF §4 D1c 的原文列的就是这 8 个：`hierDepth` / `maxVertices` / `strmVersion` / `case` /
`convertPin` / `pinAttNum` / `convertDot` / `arrayInstToScalar`。三个最致命的理由：
`convertPin "geometry"` + `pinAttNum 1`（默认成 text ⇒ `--cadencePins=1` 一个端口都找不到）、
`case "preserve"`（端口名大小写混用，变 upper 就匹配不上）、
`maxVertices 200`（顶点切分不同 ⇒ mesh 不同 ⇒ L/Q 不同，**且跑得出来、数字也像**）。
"""


def _fields() -> GdsoutFields:
    """**正反两向共用的**唯一一条输入构造路径。反向测试只改其中一个入参。"""
    return GdsoutFields(
        run_dir=FAKE_GDS_DIR,
        library=FAKE_LIBRARY,
        top_cell=FAKE_CELL,
        view=FAKE_VIEW,
        strm_file=FAKE_STRM_FILE,
        log_file=FAKE_LOG_FILE,
        layer_map=FAKE_LAYERMAP,
    )


def _ctx(*, strmout_bin: str = FAKE_STRMOUT_BIN, batch_dir: str = FAKE_BATCH_DIR) -> PlanContext:
    """阶段 1 的上下文。正反两向共用。"""
    design = Design(library=FAKE_LIBRARY, cell=FAKE_CELL, view=FAKE_VIEW, key="d0")
    facts = SiteFacts(strmout_bin=strmout_bin, layer_map=FAKE_LAYERMAP)
    return PlanContext(design=design, facts=facts, batch_dir=batch_dir)


def _drop_field(text: str, field: str) -> str:
    """从模板文本里删掉某个字段那一整行（反向验证用）。"""
    kept = [line for line in text.splitlines(keepends=True) if line.strip().split(None, 1)[0] != field]
    return "".join(kept)


# --------------------------------------------------------------------------
# 模板本体
# --------------------------------------------------------------------------


class TemplateConstant(unittest.TestCase):
    """`DEFAULT_GDSOUT_TEMPLATE` 必须与仓库里那份占位符模板逐字节相同。"""

    @unittest.skipIf(not TMPL_PATH.exists(), TMPL_SKIP_REASON)
    def test_default_template_matches_repo_tmpl_byte_for_byte(self) -> None:
        # 二进制读 + 手动 decode：文本模式会做 universal newlines，Windows 上把 CRLF
        # 悄悄变成 LF，那正好会掩盖"包里行尾错了"这类问题。
        raw = TMPL_PATH.read_bytes()
        self.assertEqual(
            raw.decode("ascii"),
            strmout.DEFAULT_GDSOUT_TEMPLATE,
            "源码里的模板常量和 mvp/redzone/gdsout_setup.tmpl 不一致。"
            "两者必须逐字节相同：D1c 的 mesh 逐字节相同是针对那份原貌取得的",
        )
        self.assertNotIn(b"\r", raw, "模板里出现了 CR —— .gitattributes 把仓库钉成 LF，目标机是 Linux")

    def test_template_field_count_is_24(self) -> None:
        fields = strmout.parse_gdsout_fields(strmout.DEFAULT_GDSOUT_TEMPLATE)
        self.assertEqual(len(fields), GOLDEN_FIELD_COUNT)

    def test_template_has_all_seven_placeholders(self) -> None:
        self.assertEqual(len(strmout.GDSOUT_PLACEHOLDERS), 7)
        for name, token in strmout.GDSOUT_PLACEHOLDERS.items():
            with self.subTest(field=name):
                self.assertIn(token, strmout.DEFAULT_GDSOUT_TEMPLATE)
                self.assertIn(name, strmout.parse_gdsout_fields(strmout.DEFAULT_GDSOUT_TEMPLATE))
        # 模板里出现的 token 恰好就是这 7 个 —— 多一个就是"渲染时换不掉"，少一个就是
        # "那个字段保留了来源 design 的取值"。
        self.assertEqual(
            sorted(set(strmout._TOKEN_RE.findall(strmout.DEFAULT_GDSOUT_TEMPLATE))),
            sorted(strmout.GDSOUT_PLACEHOLDERS.values()),
        )

    def test_critical_fields_are_not_placeholders(self) -> None:
        """D1c 那 8 个字段**不许**被做成占位符 —— 它们要逐字复现，不是随 design 变的。"""
        self.assertEqual(len(strmout.GDSOUT_CRITICAL_FIELDS), 8)
        overlap = set(strmout.GDSOUT_CRITICAL_FIELDS) & set(strmout.GDSOUT_PLACEHOLDERS)
        self.assertEqual(overlap, set())
        for field in strmout.GDSOUT_CRITICAL_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, strmout.parse_gdsout_fields(strmout.DEFAULT_GDSOUT_TEMPLATE))

    def test_template_carries_no_site_coordinates(self) -> None:
        """模板常量里只许有 Cadence 的工具语义，站点坐标只能以占位符形式出现（硬约束 1b）。"""
        for name in ("library", "topCell", "view", "layerMap", "runDir", "strmFile", "logFile"):
            with self.subTest(field=name):
                value = strmout.parse_gdsout_fields(strmout.DEFAULT_GDSOUT_TEMPLATE)[name]
                self.assertEqual(value, strmout.GDSOUT_PLACEHOLDERS[name])


# --------------------------------------------------------------------------
# 关键测试：渲染 = golden（正向）
# --------------------------------------------------------------------------


class RenderGolden(unittest.TestCase):
    """正向 + 计数 + 逐字段，全部对着手写的 `GOLDEN_RENDERED`。"""

    def test_render_golden(self) -> None:
        rendered = strmout.render_gdsout_setup(strmout.DEFAULT_GDSOUT_TEMPLATE, _fields())
        self.assertEqual(rendered, GOLDEN_RENDERED)

    def test_render_golden_field_count(self) -> None:
        """配方 4：非占位符字段**一个不少**。"""
        rendered = strmout.render_gdsout_setup(strmout.DEFAULT_GDSOUT_TEMPLATE, _fields())
        self.assertEqual(len(strmout.parse_gdsout_fields(rendered)), GOLDEN_FIELD_COUNT)
        self.assertEqual(
            len(strmout.parse_gdsout_fields(rendered)),
            len(strmout.parse_gdsout_fields(strmout.DEFAULT_GDSOUT_TEMPLATE)),
            "渲染前后字段条数不一致 —— 丢一行不会报错，但 mesh 会变（D1c）",
        )

    def test_render_keeps_critical_lines_verbatim(self) -> None:
        """D1c 那 8 个字段**连空白一起**逐字出现在结果里。"""
        rendered = strmout.render_gdsout_setup(strmout.DEFAULT_GDSOUT_TEMPLATE, _fields())
        self.assertEqual(len(CRITICAL_LINES), len(strmout.GDSOUT_CRITICAL_FIELDS))
        for line in CRITICAL_LINES:
            with self.subTest(line=line.strip()):
                self.assertIn(line, rendered)

    def test_render_placed_every_value(self) -> None:
        """7 个字段的值都真的落进去了，且一个占位符都没剩下。"""
        rendered = strmout.render_gdsout_setup(strmout.DEFAULT_GDSOUT_TEMPLATE, _fields())
        parsed = strmout.parse_gdsout_fields(rendered)
        self.assertEqual(parsed["runDir"], FAKE_GDS_DIR)
        self.assertEqual(parsed["library"], FAKE_LIBRARY)
        self.assertEqual(parsed["topCell"], FAKE_CELL)
        self.assertEqual(parsed["view"], FAKE_VIEW)
        self.assertEqual(parsed["strmFile"], FAKE_STRM_FILE)
        self.assertEqual(parsed["logFile"], FAKE_LOG_FILE)
        self.assertEqual(parsed["layerMap"], FAKE_LAYERMAP)
        self.assertEqual(strmout._TOKEN_RE.findall(rendered), [])

    def test_diff_against_golden_is_clean_and_counts(self) -> None:
        """比较逻辑本身在正向上是干净的 —— 而且**真的比了 24 条**（防"空得非常好看"）。"""
        rendered = strmout.render_gdsout_setup(strmout.DEFAULT_GDSOUT_TEMPLATE, _fields())
        diff = strmout.diff_gdsout_setup(rendered, GOLDEN_RENDERED)
        self.assertTrue(diff.clean)
        self.assertEqual(diff.compared_count, GOLDEN_FIELD_COUNT)
        self.assertEqual(diff.ignored, ())


# --------------------------------------------------------------------------
# 反向验证：故意改坏，断言比较逻辑报告了它
# --------------------------------------------------------------------------


class RenderNegative(unittest.TestCase):
    """每一条都与 `RenderGolden` 走同一条输入构造路径（`_fields()` + `DEFAULT_GDSOUT_TEMPLATE`），
    只改坏一个东西 —— 排除"换了个东西测"。"""

    def _rendered_from(self, template: str) -> str:
        return strmout.render_gdsout_setup(template, _fields())

    def test_render_golden_negative_convert_pin_text(self) -> None:
        """`convertPin "geometry"` → `"text"`：pin 不再是几何图形，`--cadencePins=1`
        一个端口都找不到（D1c 三个最致命之一）。"""
        tampered = strmout.DEFAULT_GDSOUT_TEMPLATE.replace(
            '\tconvertPin\t\t"geometry"\n', '\tconvertPin\t\t"text"\n'
        )
        self.assertNotEqual(tampered, strmout.DEFAULT_GDSOUT_TEMPLATE, "改坏这一步本身没生效，测试会空过")
        rendered = self._rendered_from(tampered)
        self.assertNotEqual(rendered, GOLDEN_RENDERED)

        diff = strmout.diff_gdsout_setup(rendered, GOLDEN_RENDERED)
        self.assertFalse(diff.clean)
        self.assertEqual([d.flag for d in diff.differing], ["convertPin"])
        self.assertEqual(diff.differing[0].actual, "text")
        self.assertEqual(diff.differing[0].expected, "geometry")
        self.assertEqual(diff.compared_count, GOLDEN_FIELD_COUNT)

    def test_render_golden_negative_max_vertices_changed(self) -> None:
        """`maxVertices 200` 换成别的值：顶点切分不同 → mesh 不同 → L/Q 不同，**不报错**。

        （Cadence 不设这个字段时的内建默认值具体是多少，不在我们取回的证据里 ——
        所以这里测的是"**任何**改动都必须被报出来"，而"不设 = 用默认值"那一种由
        `test_render_golden_negative_dropping_any_critical_field` 覆盖。）
        """
        tampered = strmout.DEFAULT_GDSOUT_TEMPLATE.replace(
            "\tmaxVertices\t\t200\n", "\tmaxVertices\t\t8000\n"
        )
        self.assertNotEqual(tampered, strmout.DEFAULT_GDSOUT_TEMPLATE)
        diff = strmout.diff_gdsout_setup(self._rendered_from(tampered), GOLDEN_RENDERED)
        self.assertFalse(diff.clean)
        self.assertEqual([d.flag for d in diff.differing], ["maxVertices"])
        self.assertEqual(diff.differing[0].actual, "8000")
        self.assertEqual(diff.differing[0].expected, "200")
        self.assertEqual(diff.compared_count, GOLDEN_FIELD_COUNT)

    def test_render_golden_negative_case_line_deleted(self) -> None:
        """删掉整行 `case "preserve"`：字段计数当场对不上（23 ≠ 24），且 diff 点名 `case`。"""
        tampered = _drop_field(strmout.DEFAULT_GDSOUT_TEMPLATE, "case")
        self.assertEqual(len(strmout.parse_gdsout_fields(tampered)), GOLDEN_FIELD_COUNT - 1)

        rendered = self._rendered_from(tampered)
        self.assertEqual(len(strmout.parse_gdsout_fields(rendered)), GOLDEN_FIELD_COUNT - 1)
        self.assertNotIn('\tcase\t"preserve"\n', rendered)

        diff = strmout.diff_gdsout_setup(rendered, GOLDEN_RENDERED)
        self.assertFalse(diff.clean)
        self.assertEqual(diff.only_expected, ("case",))
        self.assertEqual(diff.only_actual, ())
        self.assertEqual(diff.compared_count, GOLDEN_FIELD_COUNT)

    def test_render_golden_negative_dropping_any_critical_field(self) -> None:
        """D1c 那 8 个字段，**逐个**删掉都必须被报出来（"不设 = 用工具默认值"这一路）。"""
        for field in strmout.GDSOUT_CRITICAL_FIELDS:
            with self.subTest(field=field):
                tampered = _drop_field(strmout.DEFAULT_GDSOUT_TEMPLATE, field)
                self.assertEqual(len(strmout.parse_gdsout_fields(tampered)), GOLDEN_FIELD_COUNT - 1)
                diff = strmout.diff_gdsout_setup(self._rendered_from(tampered), GOLDEN_RENDERED)
                self.assertFalse(diff.clean)
                self.assertEqual(diff.only_expected, (field,))
                self.assertEqual(diff.compared_count, GOLDEN_FIELD_COUNT)

    def test_render_golden_negative_wrong_view(self) -> None:
        """把入参（不是模板）改坏一个：view 换掉 → diff 只报 view 这一处。"""
        broken = GdsoutFields(
            run_dir=FAKE_GDS_DIR,
            library=FAKE_LIBRARY,
            top_cell=FAKE_CELL,
            view="anotherview",
            strm_file=FAKE_STRM_FILE,
            log_file=FAKE_LOG_FILE,
            layer_map=FAKE_LAYERMAP,
        )
        rendered = strmout.render_gdsout_setup(strmout.DEFAULT_GDSOUT_TEMPLATE, broken)
        diff = strmout.diff_gdsout_setup(rendered, GOLDEN_RENDERED)
        self.assertEqual([d.flag for d in diff.differing], ["view"])
        self.assertEqual(diff.differing[0].actual, "anotherview")
        self.assertEqual(diff.compared_count, GOLDEN_FIELD_COUNT)


# --------------------------------------------------------------------------
# 过滤器测试（配方 4）：前缀不许互相误伤
# --------------------------------------------------------------------------


class PlaceholderFilter(unittest.TestCase):
    """`@@VIEW@@` 不许吃掉 `@@VIEWPORT@@`。

    模板里现在**没有** `viewPort` 这种字段 —— 这是一条**防将来**的测试：
    MVP 里真踩过同构的 bug（排除规则写 `--sparam` 前缀，把 `--sparamImpedance` 一起吃掉，
    两边同时被跳过，diff 空得非常好看但根本没比）。占位符替换是同一类前缀陷阱。
    """

    PREFIX_TEMPLATE = '\tview\t\t"@@VIEW@@"\n\tviewPort\t"@@VIEWPORT@@"\n'

    def test_token_regex_treats_viewport_as_one_token(self) -> None:
        self.assertEqual(
            strmout._TOKEN_RE.findall(self.PREFIX_TEMPLATE),
            ["@@VIEW@@", "@@VIEWPORT@@"],
        )

    def test_substitute_does_not_damage_longer_token(self) -> None:
        rendered, unknown = strmout.substitute_placeholders(
            self.PREFIX_TEMPLATE, {"view": FAKE_VIEW}
        )
        self.assertEqual(rendered, '\tview\t\t"testview"\n\tviewPort\t"@@VIEWPORT@@"\n')
        # 长 token 原样留着（没有被截成 `testviewPORT@@` 之类），并且被如实报成"不认识"。
        self.assertIn("@@VIEWPORT@@", rendered)
        self.assertEqual(unknown, ("@@VIEWPORT@@",))

    def test_substitute_does_not_rescan_inserted_values(self) -> None:
        """替换进去的值不再被当成模板扫一遍（否则值里带 `@@X@@` 就会二次替换）。"""
        rendered, unknown = strmout.substitute_placeholders(
            '\tview\t\t"@@VIEW@@"\n', {"view": "@@TOPCELL@@"}
        )
        self.assertEqual(rendered, '\tview\t\t"@@TOPCELL@@"\n')
        self.assertEqual(unknown, ())

    def test_render_rejects_leftover_placeholder(self) -> None:
        """没换完的占位符 → `SpecError`（宁可炸，也别生成一份错的 setup）。"""
        template = strmout.DEFAULT_GDSOUT_TEMPLATE + '\tviewPort\t"@@VIEWPORT@@"\n'
        with self.assertRaises(SpecError) as caught:
            strmout.render_gdsout_setup(template, _fields())
        self.assertIn("@@VIEWPORT@@", str(caught.exception))

    def test_ignore_is_exact_match_not_prefix(self) -> None:
        """`diff_gdsout_setup(ignore=("view",))` 不许把 `viewPort` 一起吃掉。"""
        left = '\tview\t\t"a"\n\tviewPort\t"x"\n\tcase\t"preserve"\n'
        right = '\tview\t\t"b"\n\tviewPort\t"y"\n\tcase\t"preserve"\n'
        diff = strmout.diff_gdsout_setup(left, right, ignore=("view",))
        self.assertEqual(diff.ignored, ("view",))
        self.assertEqual([d.flag for d in diff.differing], ["viewPort"])
        # 计数断言：被忽略的不计入，剩下的两条（viewPort / case）都真的比了。
        self.assertEqual(diff.compared_count, 2)


# --------------------------------------------------------------------------
# 解析器 —— 计数断言的地基，所以它自己也要被测
# --------------------------------------------------------------------------


class ParseFields(unittest.TestCase):
    def test_parse_strips_quotes_and_keeps_valueless_field(self) -> None:
        parsed = strmout.parse_gdsout_fields(strmout.DEFAULT_GDSOUT_TEMPLATE)
        self.assertEqual(parsed["hierDepth"], "32")
        self.assertEqual(parsed["case"], "preserve")
        self.assertEqual(parsed["arrayInstToScalar"], "")
        self.assertEqual(parsed["refLibList"], "")

    def test_parse_rejects_duplicate_field(self) -> None:
        """同一个字段出现两次 → 报错。悄悄丢掉一个才是灾难（D1c）。"""
        with self.assertRaises(SpecError):
            strmout.parse_gdsout_fields('\tcase\t"preserve"\n\tcase\t"upper"\n')

    def test_parse_agrees_with_discover_on_field_names(self) -> None:
        """交叉校验：`core.discover.parse_gdsout_setup` 与本模块的解析器必须数出同一组字段。

        只比**字段名集合**，不比值 —— 引号剥不剥、没有值的字段给空串还是 `True`，
        那是各自的表示约定，不是事实分歧。会分歧的是"谁少数了一行"，而那正是要抓的。
        """
        # **故意不 try/except**：core.discover 已落地，兜底 skipTest 的语义过期了。
        # 留着它，将来谁把 core.discover 改出 ImportError，这条接缝测试会静默变 skip
        # 而不是变红 —— 接缝正是"各自单测全绿、接起来才炸"的地方，不能让它无声。
        from ewave_batch.core import discover  # noqa: PLC0415 - 接缝测试，就地 import
        theirs = discover.parse_gdsout_setup(strmout.DEFAULT_GDSOUT_TEMPLATE)
        mine = strmout.parse_gdsout_fields(strmout.DEFAULT_GDSOUT_TEMPLATE)
        self.assertEqual(sorted(theirs), sorted(mine))
        self.assertEqual(len(mine), GOLDEN_FIELD_COUNT)

    def test_discover_templatize_output_renders_here(self) -> None:
        """整条链路的对接契约：`core.discover.templatize_gdsout_setup` 吐出来的模板，
        本模块必须能渲染 —— **占位符 token 两边得是同一套**。

        这是 P2 内两个并行模块的接缝，也是 P3 真正会走的那条路（用户给官方 run 目录 →
        discover 解析出模板 → 我们渲染）。两边各写各的 token（`@@VIEW@@` vs `@@CELLVIEW@@`）
        在各自的单测里都是绿的，只在接起来的时候炸 —— 所以这条测试写在接缝上。
        """
        # **故意不 try/except**：core.discover 已落地，兜底 skipTest 的语义过期了。
        # 留着它，将来谁把 core.discover 改出 ImportError，这条接缝测试会静默变 skip
        # 而不是变红 —— 接缝正是"各自单测全绿、接起来才炸"的地方，不能让它无声。
        from ewave_batch.core import discover  # noqa: PLC0415 - 接缝测试，就地 import

        # 先造一份"官方 gdsout_setup"：把占位符填成另一个 design 的取值（同样是假值）。
        official, unknown = strmout.substitute_placeholders(
            strmout.DEFAULT_GDSOUT_TEMPLATE,
            {
                "runDir": "/tmp/off/run",
                "library": "OFFLIB",
                "topCell": "OFFCELL",
                "view": "offview",
                "strmFile": "OFFCELL.gds",
                "logFile": "/tmp/off/run/gds_out.log",
                "layerMap": FAKE_LAYERMAP,
            },
        )
        self.assertEqual(unknown, ())
        self.assertEqual(strmout._TOKEN_RE.findall(official), [], "这一步该把占位符全填掉")

        templatized = discover.templatize_gdsout_setup(official)
        self.assertEqual(
            sorted(set(strmout._TOKEN_RE.findall(templatized))),
            sorted(strmout.GDSOUT_PLACEHOLDERS.values()),
            "两个模块用的占位符 token 对不上 —— 接起来才会炸，各自单测都是绿的",
        )
        # 渲染出来必须与"直接用兜底模板渲染"逐字节相同：来源 design 的取值一点都不许漏进来。
        self.assertEqual(strmout.render_gdsout_setup(templatized, _fields()), GOLDEN_RENDERED)


# --------------------------------------------------------------------------
# 入参守卫：宁可炸，也别默默生成一份错的 setup
# --------------------------------------------------------------------------


class RenderGuards(unittest.TestCase):
    def test_missing_placeholder_in_template_raises(self) -> None:
        """模板里少一个占位符 → 那个字段会保留**来源 design** 的取值，strmout 不会报错。"""
        template = strmout.DEFAULT_GDSOUT_TEMPLATE.replace('"@@VIEW@@"', '"someoneelsesview"')
        with self.assertRaises(SpecError) as caught:
            strmout.render_gdsout_setup(template, _fields())
        self.assertIn("@@VIEW@@", str(caught.exception))

    def test_empty_layer_map_raises(self) -> None:
        """`layerMap ""` = 不做层映射，导出的 GDS 层号全错，而 strmout 照样退 0。"""
        fields = GdsoutFields(
            run_dir=FAKE_GDS_DIR,
            library=FAKE_LIBRARY,
            top_cell=FAKE_CELL,
            view=FAKE_VIEW,
            strm_file=FAKE_STRM_FILE,
            log_file=FAKE_LOG_FILE,
            layer_map="",
        )
        with self.assertRaises(SpecError) as caught:
            strmout.render_gdsout_setup(strmout.DEFAULT_GDSOUT_TEMPLATE, fields)
        self.assertIn("layerMap", str(caught.exception))

    def test_quote_in_value_raises(self) -> None:
        fields = GdsoutFields(
            run_dir=FAKE_GDS_DIR,
            library='TEST"LIB',
            top_cell=FAKE_CELL,
            view=FAKE_VIEW,
            strm_file=FAKE_STRM_FILE,
            log_file=FAKE_LOG_FILE,
            layer_map=FAKE_LAYERMAP,
        )
        with self.assertRaises(SpecError):
            strmout.render_gdsout_setup(strmout.DEFAULT_GDSOUT_TEMPLATE, fields)

    def test_newline_in_value_raises(self) -> None:
        """值里带换行 = 凭空多出一个字段，后面全部错位。"""
        fields = GdsoutFields(
            run_dir=FAKE_GDS_DIR,
            library=FAKE_LIBRARY,
            top_cell=FAKE_CELL,
            view='x"\n\tcase\t"upper',
            strm_file=FAKE_STRM_FILE,
            log_file=FAKE_LOG_FILE,
            layer_map=FAKE_LAYERMAP,
        )
        with self.assertRaises(SpecError):
            strmout.render_gdsout_setup(strmout.DEFAULT_GDSOUT_TEMPLATE, fields)


# --------------------------------------------------------------------------
# 字段推导
# --------------------------------------------------------------------------


class FieldsForDesign(unittest.TestCase):
    def test_fields_for_design(self) -> None:
        ctx = _ctx()
        fields = strmout.gdsout_fields_for_design(
            ctx.design, ctx, gds_path="/tmp/ewb/gds/TESTCELL.gds"
        )
        self.assertEqual(fields, _fields())

    def test_fields_for_design_negative_wrong_gds_path(self) -> None:
        """改坏 gds_path → runDir / strmFile / logFile 三处一起变（同一条构造路径，只改这一个）。"""
        ctx = _ctx()
        fields = strmout.gdsout_fields_for_design(
            ctx.design, ctx, gds_path="/tmp/ewb/gds/OTHER.gds"
        )
        self.assertNotEqual(fields, _fields())
        self.assertEqual(fields.strm_file, "OTHER.gds")
        self.assertEqual(fields.log_file, "/tmp/ewb/gds/OTHER.gds_out.log")
        self.assertEqual(fields.run_dir, FAKE_GDS_DIR)

    def test_fields_for_design_takes_triplet_from_design_not_facts(self) -> None:
        """三元组来自 `Design`（用户输入），不是 `SiteFacts`（官方那次跑的是谁）。"""
        design = Design(library="OTHERLIB", cell="OTHERCELL", view="otherview", key="d1")
        facts = SiteFacts(strmout_bin=FAKE_STRMOUT_BIN, layer_map=FAKE_LAYERMAP,
                          library=FAKE_LIBRARY, top_cell=FAKE_CELL, view=FAKE_VIEW)
        ctx = PlanContext(design=design, facts=facts, batch_dir=FAKE_BATCH_DIR)
        fields = strmout.gdsout_fields_for_design(design, ctx, gds_path="/tmp/ewb/gds/OTHERCELL.gds")
        self.assertEqual((fields.library, fields.top_cell, fields.view),
                         ("OTHERLIB", "OTHERCELL", "otherview"))

    def test_fields_for_design_layer_map_from_facts(self) -> None:
        ctx = _ctx()
        self.assertEqual(
            strmout.gdsout_fields_for_design(
                ctx.design, ctx, gds_path="/tmp/ewb/gds/TESTCELL.gds"
            ).layer_map,
            FAKE_LAYERMAP,
        )

    def test_fields_for_design_without_layer_map_raises(self) -> None:
        design = Design(library=FAKE_LIBRARY, cell=FAKE_CELL, view=FAKE_VIEW, key="d0")
        ctx = PlanContext(design=design, facts=SiteFacts(strmout_bin=FAKE_STRMOUT_BIN),
                          batch_dir=FAKE_BATCH_DIR)
        with self.assertRaises(SpecError) as caught:
            strmout.gdsout_fields_for_design(design, ctx, gds_path="/tmp/ewb/gds/TESTCELL.gds")
        self.assertIn("layerMap", str(caught.exception))

    def test_fields_for_design_without_gds_path_raises(self) -> None:
        ctx = _ctx()
        with self.assertRaises(SpecError):
            strmout.gdsout_fields_for_design(ctx.design, ctx, gds_path="")


# --------------------------------------------------------------------------
# 关键测试：strmout 的 argv
# --------------------------------------------------------------------------


class StrmoutPlan(unittest.TestCase):
    """期望 argv 手写自 `mvp/redzone/step1_strmout.sh` 里**实跑通过**的那一条：

    ```
    cd $CWD && "$STRMOUT" -templateFile "$SETUP"
    ```

    只有这两个 token，没有第三个 flag —— D1c 的"mesh 逐字节相同"就是在这条形状下取得的
    （BRIEF §10 step1 / 「D1c 已证完」）。
    """

    def test_build_strmout_plan_argv(self) -> None:
        plan = strmout.build_strmout_plan(_ctx().design, _ctx(), setup_path=FAKE_SETUP_PATH)
        self.assertEqual(
            plan.argv,
            (FAKE_STRMOUT_BIN, "-templateFile", FAKE_SETUP_PATH),
        )
        self.assertEqual(plan.stage, Stage.STREAMOUT)
        self.assertEqual(plan.design_key, "d0")
        self.assertEqual(plan.run_id, "")
        self.assertEqual(plan.flags, {"-templateFile": FAKE_SETUP_PATH})
        # cwd：P7a-1 —— strmout 要在一个能看见目标 library 的 cds.lib 的目录里跑，
        # 而那个目录必须是我们自己的（硬约束 4：不写设计师的 spine）。
        self.assertEqual(plan.cwd, "/tmp/ewb/cdswork")
        self.assertEqual(plan.work_dir, FAKE_GDS_DIR)
        self.assertEqual(plan.log_path, "/tmp/ewb/logs/strmout_d0.log")

    def test_build_strmout_plan_argv_negative_wrong_setup_path(self) -> None:
        """同一条构造路径，只改 setup_path → 比较逻辑必须报告这一处。"""
        ctx = _ctx()
        plan = strmout.build_strmout_plan(ctx.design, ctx, setup_path="/tmp/ewb/gdsout/WRONG.setup")
        self.assertNotEqual(plan.argv, (FAKE_STRMOUT_BIN, "-templateFile", FAKE_SETUP_PATH))

        from ewave_batch.core import cmd as _cmd

        diff = _cmd.diff_flags(plan.flags, {"-templateFile": FAKE_SETUP_PATH})
        self.assertFalse(diff.clean)
        self.assertEqual([d.flag for d in diff.differing], ["-templateFile"])
        self.assertEqual(diff.compared_count, 1)

    def test_build_strmout_plan_argv_has_exactly_one_template_flag(self) -> None:
        """计数断言：`-templateFile` 恰好一次 —— 多一次 strmout 会用后面那个，静默换掉 setup。"""
        plan = strmout.build_strmout_plan(_ctx().design, _ctx(), setup_path=FAKE_SETUP_PATH)
        self.assertEqual(list(plan.argv).count("-templateFile"), 1)
        self.assertEqual(len(plan.argv), 3, "argv 多出了 token —— D1c 的实测背书只覆盖这条形状")

    def test_missing_strmout_bin_raises(self) -> None:
        ctx = _ctx(strmout_bin="")
        with self.assertRaises(ToolMissingError):
            strmout.build_strmout_plan(ctx.design, ctx, setup_path=FAKE_SETUP_PATH)

    def test_empty_setup_path_raises(self) -> None:
        """没有 `-templateFile` 就退化成 Auto_ext 那种裸 argv —— D1c 的 8 个字段全丢。"""
        ctx = _ctx()
        with self.assertRaises(SpecError):
            strmout.build_strmout_plan(ctx.design, ctx, setup_path="")

    def test_plan_without_batch_dir_leaves_paths_empty(self) -> None:
        """没有 batch_dir 时不瞎猜落点：cwd/work_dir/log_path 全留空，由调用方决定。"""
        ctx = _ctx(batch_dir="")
        plan = strmout.build_strmout_plan(ctx.design, ctx, setup_path=FAKE_SETUP_PATH)
        self.assertEqual((plan.cwd, plan.work_dir, plan.log_path), ("", "", ""))
        self.assertEqual(plan.argv, (FAKE_STRMOUT_BIN, "-templateFile", FAKE_SETUP_PATH))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
