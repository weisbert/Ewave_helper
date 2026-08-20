"""`ewave_batch.tools.strmout` —— 阶段 1：渲染 `gdsout_setup` + 拼 `strmout` 命令（D1c）。

🚨 **这个模块的全部价值在于"不做事"**：把官方 `gdsout_setup` 当模板，
**只**替换随 design 变的那几个字段，其余**逐字节**复现。

为什么这么偏执（BRIEF §4 D1c）：Auto_ext 的 `strmout.py` 走的是裸 argv，只传 5 个 flag
（`-library -topCell -view -strmFile -layerMap`），**漏掉了官方 setup 里 8 个会改变 GDS
内容的字段**。三个最致命：

* `convertPin "geometry"` + `pinAttNum 1` —— 默认成 text 的话 pin 就不是几何图形，
  `--cadencePins=1` **一个端口都找不到**；
* `case "preserve"` —— 端口名混用大小写，变 upper 就匹配不上；
* `maxVertices 200` —— 顶点切分方式不同 → mesh 不同 → **L/Q 结果不同，
  而且跑得出来、数字也像**（这一类才是真正危险的：它不报错）。

D1c 已由 MVP 实测证完（BRIEF §10「D1c 已证完」）：我们 `strmout -templateFile` 导出的 GDS
与官方 GUI 导出的 GDS **产出逐字节相同的网格**（`pmrg.gtxt` / `pmrg.gtxt.mrg` /
`pmsh.gtxt.msh` 三件套 md5 全同）。⇒ **模板的保真度是有实测背书的，别"顺手优化"它的格式。**
`DEFAULT_GDSOUT_TEMPLATE` 里那些看着不整齐的空白（`propMap` 后面 5 个空格、
`viaMap` 后面 2 个空格…）是官方文件的原貌，不是笔误。

--------------------------------------------------------------------------
占位符分两类 —— 这个区分是承重的（CLAUDE.md 硬约束 1b）
--------------------------------------------------------------------------

| 占位符 | 是什么 | 从哪来 |
|---|---|---|
| `@@RUNDIR@@` `@@STRMFILE@@` `@@LOGFILE@@` | **我们自己的落点**（批次目录里的路径） | `core.layout.compute_run_paths` 算的，不是站点身份 |
| `@@LIBRARY@@` `@@TOPCELL@@` `@@VIEW@@` | **用户输入的 design 三元组** | `Design`（用户在界面里打的），源码里零默认值 |
| `@@LAYERMAP@@` | 🚨 **站点坐标** —— 一条指进 PDK 的绝对路径 | `core.discover` 从官方 `gdsout_setup` 解析出来（退路：`$PDK_LAYER_MAP_FILE`），**经 `SiteFacts.layer_map` 传进来**。它随 PDK/站点变，**不随 design 变** —— 所以它既不许写死在源码里（那是把站点身份提交进公开仓库），也不该由用户手抄（抄错一个字符，导出的 GDS 层号全错，而且照样跑得出来） |

⇒ 于是本文件里出现的一切具体取值都是 **Cadence 的工具语义**（`maxVertices 200`、
`convertPin "geometry"`…），**没有一个是站点身份**。

--------------------------------------------------------------------------
不做什么
--------------------------------------------------------------------------

* **不写盘**。`build_strmout_plan` 只拼命令；setup 文件由调用方（driver）写 ——
  dry-run 要能"只打印不落地"（D8）。
* **不加 `-checkPolygon`**。BRIEF §7 P7a 把它列为"建议我们开"（只加日志不改 GDS），
  但 D1c 那条 mesh 逐字节相同的实测证据是在**不带它**的 argv 形状下取得的。
  在没有第二次实测之前，argv 保持与被证过的那条完全一致；要开它是一个显式的产品决定，
  不是实现细节。
* **不猜 `layerMap`**。传空 → `SpecError`，绝不"默默用个合理的默认值"。
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from types import MappingProxyType

from ..model import (
    GDS_DIRNAME,
    LOGS_DIRNAME,
    CommandPlan,
    Design,
    FlagDict,
    FlagDiff,
    GdsoutFields,
    PlanContext,
    SpecError,
    Stage,
    ToolMissingError,
)

# --------------------------------------------------------------------------
# 模板本体
# --------------------------------------------------------------------------

DEFAULT_GDSOUT_TEMPLATE: str = (
    '\trunDir\t\t\t"@@RUNDIR@@"\n'
    '\tlibrary\t\t"@@LIBRARY@@"\n'
    '\ttopCell\t\t"@@TOPCELL@@"\n'
    '\tview\t\t"@@VIEW@@"\n'
    '\tstrmFile\t\t"@@STRMFILE@@"\n'
    "\thierDepth\t\t32\n"
    "\tmaxVertices\t\t200\n"
    '\trefLibList\t\t""\n'
    '\tstrmVersion\t\t"5"\n'
    "\tarrayInstToScalar\n"
    '\tcase\t"preserve"\n'
    '\tconvertDot\t"ignore"\n'
    '\tlogFile\t\t"@@LOGFILE@@"\n'
    '\tcellMap\t\t""\n'
    '\tlayerMap\t\t"@@LAYERMAP@@"\n'
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
"""兜底模板 —— 与 `mvp/redzone/gdsout_setup.tmpl` **逐字节相同**（`tests/test_strmout.py` 守着）。

⚠️ **必须是源码里的字符串常量，不许改成读 `.tmpl` 文件**（docs/INTERFACES.md 明写）。
两条独立理由：
1. `.gitattributes` 把 `mvp/` 标了 `export-ignore` ⇒ `git archive` 打出来的红区包里**没有**
   这个文件；读文件的写法在本机全绿、到红区静默失效。
2. `.gitignore` 里 `gdsout_setup*` 是被忽略的（那是真 setup 文件的名字）——
   放成文件容易被下一个人误伤。

**优先用运行时解析出来的那份**（`SiteFacts.gdsout_template`，`core.discover` 从用户自己的
官方 run 目录 templatize 出来的）。本常量只是"用户没给官方目录"时的兜底：
字段名和数值是 Cadence 的工具语义，通用；而真实站点的 `layerMap` 只以占位符形式出现。
"""

GDSOUT_PLACEHOLDERS: Mapping[str, str] = MappingProxyType(
    {
        "runDir": "@@RUNDIR@@",
        "library": "@@LIBRARY@@",
        "topCell": "@@TOPCELL@@",
        "view": "@@VIEW@@",
        "strmFile": "@@STRMFILE@@",
        "logFile": "@@LOGFILE@@",
        "layerMap": "@@LAYERMAP@@",
    }
)
"""`gdsout_setup` 字段名 → 它在模板里的占位符。**恰好 7 个**（docs/INTERFACES.md）。

做成 mapping 而不是裸 tuple：`core.discover.templatize_gdsout_setup` 要的是"哪个字段换成
哪个 token"，两边必须用同一份表，否则一个写 `@@VIEW@@` 一个写 `@@CELLVIEW@@`，
渲染时谁都换不掉 —— 而没换掉的占位符会被 `render_gdsout_setup` 当场拒绝（这是好事）。

`tuple(GDSOUT_PLACEHOLDERS)` 就是那 7 个字段名，`len()` 就是 7。
"""

GDSOUT_CRITICAL_FIELDS: tuple[str, ...] = (
    "hierDepth",
    "maxVertices",
    "strmVersion",
    "case",
    "convertPin",
    "pinAttNum",
    "convertDot",
    "arrayInstToScalar",
)
"""D1c 点名的 8 个**会改变 GDS 内容**的字段（BRIEF §4 D1c）。

它们**不是**占位符 —— 恰恰相反，它们必须逐字复现。列在这里是为了能被断言：
渲染丢了其中任何一行，GDS 照样导得出来、eWave 照样跑得完、数字还挺像，
只有 mesh 悄悄变了。`tests/test_strmout.py` 逐个断言它们原样出现在渲染结果里。
"""

CDSWORK_DIRNAME = "cdswork"
"""`strmout` 的 cwd 目录名（批次目录下）。

P7a-1（BRIEF §10 step1 实测）：`strmout` 要能解析 `-library`，cwd 里必须有一份能看见
目标 library 的 `cds.lib`。实测可行的做法是在**我们自己的目录**里放一行
`INCLUDE <workarea>/cds.lib`，然后 cd 过去 —— 于是 Cadence 的散落写入（`CDS.log`、
`libManager.log`…）全留在批次目录里，**不必 cd 进 workarea**，硬约束 4（不写设计师的
spine）得以保持。

单开一层子目录而不是直接用 `batch_dir`：那些散落文件会和 `batch.json` / `runs.csv` 混在
一起，归档时分不清哪些是产物。写 `cds.lib` 是调用方（driver）的事，本模块只给位置。
"""

_TOKEN_RE = re.compile(r"@@[A-Za-z0-9_]+@@")
"""占位符的词法：`@@` + 大写/数字/下划线 + `@@`。

🚨 **整份文本只扫这一遍**，逐 token 替换 —— 不是"对每个占位符 `str.replace` 一次"。
理由是 MVP 那个真 bug 的同构物：排除规则写 `--sparam` 前缀，把 `--sparamImpedance`
一起吃掉了。逐 token 匹配从根上消灭这类前缀误伤：`@@VIEW@@` 与 `@@VIEWPORT@@`
是两个不同的 token，谁先谁后都不影响结果，替换进去的值也**不会被再扫一遍**。
"""

_GDS_OUT_LOG_SUFFIX = ".gds_out.log"
"""strmout 自己的日志名后缀（MVP 里叫 `gds_out.log`）。加上 design 词根，因为
`gds/` 是整个批次共用的一层，每个 design 一份日志才不会互相覆盖。"""


# --------------------------------------------------------------------------
# 内部 helper
# --------------------------------------------------------------------------


def _field_values(fields: GdsoutFields) -> dict[str, str]:
    """`GdsoutFields` → `{gdsout 字段名: 取值}`，键与 `GDSOUT_PLACEHOLDERS` 对齐。"""
    return {
        "runDir": fields.run_dir,
        "library": fields.library,
        "topCell": fields.top_cell,
        "view": fields.view,
        "strmFile": fields.strm_file,
        "logFile": fields.log_file,
        "layerMap": fields.layer_map,
    }


def _check_values(values: Mapping[str, str]) -> None:
    """把"渲染出来能跑、但语义是错的"那几种输入挡在门外。

    三条，全部是"宁可炸也别默默生成一份错的 setup"：

    * **空值** —— `layerMap ""` 意味着不做层映射，GDS 层号会全错；`view ""` 意味着
      strmout 去解析一个不存在的 cellview。两者都不会在这一步报错。
    * **带 `"`** —— setup 是 `key<tab>"value"` 格式，值里再来一个引号就把字段截断了。
    * **带换行 / 带 `@@`** —— 前者会凭空多出一个字段（后面的字段全部错位），
      后者会让渲染结果里出现一个看起来没换完的占位符。
    """
    for name, value in sorted(values.items()):
        if not value or not value.strip():
            raise SpecError(
                f"gdsout_setup field {name!r} is empty. "
                "An empty value produces a setup that is syntactically fine and semantically wrong "
                "(e.g. an empty layerMap means no layer mapping at all, so every GDS layer number is "
                "wrong while strmout still exits 0) - refusing to render."
                + (
                    "\n  Next: layerMap is a site coordinate - core.discover parses it out of the "
                    "official gdsout_setup (fallback $PDK_LAYER_MAP_FILE). Never hardcode it, "
                    "never copy it by hand."
                    if name == "layerMap"
                    else ""
                )
            )
        if '"' in value:
            raise SpecError(
                f"gdsout_setup field {name!r} has a double quote in its value, "
                f"which truncates the setup field: {value!r}"
            )
        if "\n" in value or "\r" in value:
            raise SpecError(
                f"gdsout_setup field {name!r} has a newline in its value - "
                f"that conjures up an extra field: {value!r}"
            )
        if "@@" in value:
            raise SpecError(
                f"gdsout_setup field {name!r} has `@@` in its value; the rendered result "
                f"would look like it still carries an unsubstituted placeholder: {value!r}"
            )


def _strmout_program(ctx: PlanContext) -> str:
    """要执行的 `strmout`。**绝不在源码里写死绝对路径**（CLAUDE.md 硬约束 1b）。

    运行时发现在 `core.discover.find_tool`（`command -v` 的等价物），结果落在
    `SiteFacts.strmout_bin`；这里只负责取用和守卫。
    """
    program = ctx.facts.strmout_bin
    if not program:
        raise ToolMissingError(
            "SiteFacts.strmout_bin is empty - no idea which strmout to execute.\n"
            "  Next: run core.discover.discover_site_facts(<official run dir>), "
            "or make sure `strmout` is on PATH (hard constraint 1b: absolute tool paths never go into the source)"
        )
    return program


# --------------------------------------------------------------------------
# 公开函数
# --------------------------------------------------------------------------


def substitute_placeholders(text: str, values: Mapping[str, str]) -> tuple[str, tuple[str, ...]]:
    """单遍替换 `@@TOKEN@@`，返回 `(结果文本, 没认出来的 token)`。

    `values` 的键是 **gdsout 字段名**（`"view"`），token 由 `GDSOUT_PLACEHOLDERS` 查出来。

    🚨 **前缀不许互相误伤**：`@@VIEW@@` 和 `@@VIEWPORT@@` 是两个独立 token，
    替换 `view` 绝不能把 `viewPort` 那行改成 `<替换值>PORT@@`。这是 MVP 那个
    `--sparam` 吃掉 `--sparamImpedance` 的同构物 —— 那次两边同时被跳过、diff 空得非常
    好看，但根本没比。逐 token 扫一遍从根上消灭它，顺带也让替换进去的值**不被再扫一遍**
    （值里真有 `@@FOO@@` 也不会被当成占位符 —— 虽然 `_check_values` 已经先拒了它）。

    分开成一个函数（而不是塞进 `render_gdsout_setup`）是为了让"没认出来的 token"能被
    单独测到：`render_gdsout_setup` 遇到它们要抛异常，异常路径上没法断言"另一个 token
    完好无损"。
    """
    token_to_value = {
        GDSOUT_PLACEHOLDERS[name]: value
        for name, value in values.items()
        if name in GDSOUT_PLACEHOLDERS
    }
    unknown: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token in token_to_value:
            return token_to_value[token]
        unknown.append(token)
        return token

    rendered = _TOKEN_RE.sub(_replace, text)
    # 去重但保序 —— 报错信息里同一个 token 出现三遍没有意义。
    seen: list[str] = []
    for token in unknown:
        if token not in seen:
            seen.append(token)
    return rendered, tuple(seen)


def parse_gdsout_fields(text: str) -> dict[str, str]:
    """`gdsout_setup` 文本 → `{字段名: 取值}`（外层引号剥掉；没有值的字段给空串）。

    只在**本模块内部 + 测试**用，用途是"数字段"和"逐字段比对"：
    渲染时丢了一整行，GDS 照样导得出来、mesh 却变了 —— 只有计数能抓住它。

    ⚠️ 与 `core.discover.parse_gdsout_setup` 是同一件事的两个实现。没有合并成一个，
    是因为 P2 的两个模块是并行写的，互相 import 会让一边的进度卡住另一边；
    `tests/test_strmout.py` 有一条**交叉校验**：两边在同一份模板上必须解析出同一组字段名
    （值不比 —— 引号剥不剥属于各自的表示约定，不是事实分歧）。

    重复字段 → `SpecError`：`case` 写两遍时"最后一个赢"是 Cadence 的事，
    我们这边悄悄丢掉一个才是灾难（BRIEF §4 D1c 那三条哪一条被覆盖都不报错）。
    """
    fields: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(";") or stripped.startswith("#"):
            continue
        parts = stripped.split(None, 1)
        key = parts[0]
        value = parts[1].strip() if len(parts) > 1 else ""
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if key in fields:
            raise SpecError(
                f"gdsout_setup line {lineno}: field {key!r} appears twice. "
                "Which duplicate wins is up to the tool, and if any of the 8 fields named by D1c "
                "gets silently overridden the mesh changes, L/Q change, and nothing complains - "
                "refusing to parse"
            )
        fields[key] = value
    return fields


def diff_gdsout_setup(actual_text: str, expected_text: str, *, ignore: tuple[str, ...] = ()) -> FlagDiff:
    """两份 `gdsout_setup` 逐字段 diff。复用 `core.cmd.diff_flags` —— **一份比较逻辑，两个用户。**

    两个用户：
    * 本机单测（"我们渲染的 setup vs 手写的 golden"）；
    * 红区 dry-run 的自带比对（"我们渲染的 setup vs 官方那份"，MVP step1 里那条
      `diff "$OFF_SETUP" "$SETUP"`，预期只差 `runDir` / `logFile` 两处路径）。

    `ignore` 的语义**继承自 `diff_flags`：按字段名精确匹配，绝不做前缀匹配**
    （`ignore=("view",)` 不许把 `viewPort` 一起吃掉）。
    `FlagDiff.compared_count` 是真正参与比较的字段条数 —— **断言它**，
    否则"两边同时被跳过"的空 diff 永远是绿的。
    """
    from ..core import cmd as _cmd  # 惰性：避免 tools ↔ core 的 import 环，也让本模块 import 更轻

    actual: FlagDict = dict(parse_gdsout_fields(actual_text))
    expected: FlagDict = dict(parse_gdsout_fields(expected_text))
    return _cmd.diff_flags(actual, expected, ignore=ignore)


def render_gdsout_setup(template_text: str, fields: GdsoutFields) -> str:
    """把 `GDSOUT_PLACEHOLDERS` 换成 `fields` 的值，**其余逐字不动**（D1c）。

    🚨 绝不用 Auto_ext 那种裸 argv 调 strmout —— 它只传 5 个 flag，漏掉 8 个会改变 GDS 内容的字段。
    模板里还有没换完的占位符 → `SpecError`（宁可炸，也别默默生成一份错的 setup）。纯字符串函数。

    四道闸（每一道都对应一种"跑得出来但结果是错的"）：

    1. **值本身合法**（非空、无引号、无换行、无 `@@`）—— 见 `_check_values`；
    2. **7 个占位符必须全部出现在模板里**。少一个意味着那个字段保留了**来源 design**
       的取值 —— 拿 A 的 library 去导 B 的 GDS，strmout 会很高兴地照做；
    3. **单遍逐 token 替换**，前缀不互相误伤（`@@VIEW@@` ≠ `@@VIEWPORT@@`）；
    4. **字段条数必须与模板一致**。渲染是纯替换，理论上不可能改变行数 ——
       这一道是给"以后有人把实现换成正则/format"准备的：丢一行不报错，但 mesh 会变。
    """
    values = _field_values(fields)
    _check_values(values)

    missing = [
        f"{name}({token})"
        for name, token in GDSOUT_PLACEHOLDERS.items()
        if token not in template_text
    ]
    if missing:
        raise SpecError(
            "the template is missing placeholders: " + ", ".join(missing) + ". "
            "Whichever is missing keeps the value of the design the template came from - "
            "streaming out GDS with somebody else's library/topCell/view, and strmout will not complain.\n"
            "  Next: let core.discover.templatize_gdsout_setup build the template from the official "
            f"gdsout_setup, or use {__name__}.DEFAULT_GDSOUT_TEMPLATE directly"
        )

    rendered, unknown = substitute_placeholders(template_text, values)
    if unknown:
        raise SpecError(
            "the template still carries unsubstituted placeholders: "
            + ", ".join(unknown)
            + f". This module only knows these {len(GDSOUT_PLACEHOLDERS)}: "
            + ", ".join(GDSOUT_PLACEHOLDERS.values())
        )

    before = parse_gdsout_fields(template_text)
    after = parse_gdsout_fields(rendered)
    if len(after) != len(before):
        raise SpecError(
            f"the field count changed while rendering ({len(before)} -> {len(after)}) - rendering "
            "may only substitute placeholders, never add or drop fields. A dropped line still streams "
            "out a GDS, but the mesh changes, L/Q change, and nothing complains (D1c)"
        )
    return rendered


def gdsout_fields_for_design(
    design: Design,
    ctx: PlanContext,
    *,
    gds_path: str,
    layer_map: str = "",
) -> GdsoutFields:
    """按归档布局算出这个 design 的 7 个字段。

    * `library` / `topCell` / `view` —— **来自 `Design`**（用户输入的三元组，D1）。
      不从 `SiteFacts` 取：facts 是"官方那次跑的是谁"，而我们要导的是用户点名的这个 design。
    * `runDir` / `strmFile` —— 从 `gds_path` 拆出来。`gds_path` 的权威是
      `core.layout.compute_run_paths(...).design_gds`（D1a：整个设定矩阵共用一份 GDS）。
    * `logFile` —— 与 GDS 同目录、同词根 + `.gds_out.log`。`gds/` 是批次共用的一层，
      不带 design 词根的话多个 design 的 strmout 日志会互相覆盖。
    * `layerMap` —— **站点坐标**：显式参数优先，否则 `SiteFacts.layer_map`
      （`core.discover` 从官方 `gdsout_setup` 解析）。两个都空 → `SpecError`，绝不猜。
    """
    if not gds_path:
        raise SpecError(
            "gds_path is empty - no idea where the GDS should land "
            "(the authority is layout.compute_run_paths().design_gds)"
        )
    run_dir = posixpath.dirname(gds_path)
    strm_file = posixpath.basename(gds_path)
    if not run_dir or not strm_file:
        raise SpecError(f"gds_path is not a <dir>/<filename> shaped path: {gds_path!r}")
    resolved_layer_map = layer_map or ctx.facts.layer_map
    if not resolved_layer_map:
        raise SpecError(
            "layerMap is not available. It is a site coordinate (an absolute path into the PDK), "
            "so the source has no default for it and must not have one.\n"
            "  Next: run core.discover.discover_site_facts(<official run dir>) to parse it out of the "
            "official gdsout_setup (fallback: $PDK_LAYER_MAP_FILE)"
        )
    stem = posixpath.splitext(strm_file)[0] or strm_file
    return GdsoutFields(
        run_dir=run_dir,
        library=design.library,
        top_cell=design.cell,
        view=design.view,
        strm_file=strm_file,
        log_file=posixpath.join(run_dir, stem + _GDS_OUT_LOG_SUFFIX),
        layer_map=resolved_layer_map,
    )


def cdswork_dir(batch_dir: str) -> str:
    """`strmout` 该在哪个目录跑（见 `CDSWORK_DIRNAME`）。`batch_dir` 为空 → 空串（= 继承调用方 cwd）。"""
    return posixpath.join(batch_dir, CDSWORK_DIRNAME) if batch_dir else ""


def build_strmout_plan(design: Design, ctx: PlanContext, *, setup_path: str) -> CommandPlan:
    """拼阶段 1 的 `strmout` 命令（`-templateFile <setup_path>` 形式）。

    `stage=Stage.STREAMOUT`。**不**写 setup 文件（那是调用方的事，dry-run 要能只打印）。
    `facts.strmout_bin` 为空 → `ToolMissingError`。

    argv 恰好是 MVP 实测跑通的那一条形状（BRIEF §10 step1）：`strmout -templateFile <file>`，
    没有第二个 flag。**别往里加东西** —— D1c 的"mesh 逐字节相同"是在这条形状下取得的。

    `cwd` 落在 `<batch_dir>/cdswork`（P7a-1：那里要有一份能看见目标 library 的 `cds.lib`，
    由调用方写）。`work_dir` 是 GDS 的落点 `<batch_dir>/gds`，与 `RunPaths.design_gds` 同一层。
    """
    if not setup_path:
        raise SpecError(
            "setup_path is empty - the -templateFile of strmout must point at the gdsout_setup we "
            "rendered. Without it we fall back to bare argv (the Auto_ext style) and lose all 8 fields "
            "named by D1c (BRIEF sec. 4 D1c)"
        )
    from ..core import cmd as _cmd  # 惰性：与 diff_gdsout_setup 同一个理由
    from ..core.matrix import design_key as _design_key

    flags: FlagDict = {"-templateFile": setup_path}
    key = _design_key(design)
    batch_dir = ctx.batch_dir
    return CommandPlan(
        argv=(_strmout_program(ctx), *_cmd.render_flags(flags)),
        cwd=cdswork_dir(batch_dir),
        work_dir=posixpath.join(batch_dir, GDS_DIRNAME) if batch_dir else "",
        stage=Stage.STREAMOUT,
        run_id="",
        design_key=key,
        flags=flags,
        log_path=posixpath.join(batch_dir, LOGS_DIRNAME, f"strmout_{key}.log") if batch_dir else "",
    )
