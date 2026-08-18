"""`ewave_batch.core.discover` —— 从**官方 run 目录**运行时解析全部站点坐标。

这是 CLAUDE.md 硬约束 1b「运行时发现优于配置项」的落地点，也是整个「源码零站点标识符」
策略的支点：官方 GUI 跑过的那个 design 目录里本来就躺着全部坐标，
**解析它 = 既没有标识符进仓库，也没有手抄错的可能**（尤其那一长串 `-p`）。

shell 版样板是 `mvp/redzone/cfg.sh`（红区 MVP 用的那套），本文件是它的 Python 翻译 + 单测。

| 来源 | 解析出什么 |
|---|---|
| `gdsout_setup` | library / topCell / view / layerMap + 整份模板 |
| `run_ewave_*.sh` | ptxt / key / corner / temperature / 整串 `-p`/`-i` / 生产 flag |
| `remote_run_ewave.sh` | dsub 的 `-A` / `-q` / `-R` |
| `--emssTechFile` 路径倒推 | PDK 根 / ptxt 目录 / corner 文件名模板 |
| `shutil.which` | ewave / strmout 的实际路径（版本目录不写死） |

🚨 本文件**只读**，一个字节都不写。它解析出来的东西全是站点身份 →
源码里一个真实取值都没有，测试的期望值只来自 `tests/fixtures/offdir_synthetic/`
（合成的、全占位符的假官方目录）或红区 fixture（缺文件时优雅 skip）。

⚠️ 路径一律走 `posixpath`：解析出来的是红区 Linux 的绝对路径，
本机是 Windows，用 `os.path` 会把 `/` 拼成 `\` 然后静默生成一条跑不通的路径。
**只有本地文件系统的遍历（`suggest_official_dirs`）才用 `os.path`。**
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
from collections.abc import Mapping

from ..model import (
    MECHANISM_FLAGS,
    DiscoveryError,
    FlagDict,
    SiteFacts,
)
from . import matrix, template

# --------------------------------------------------------------------------
# 常量（全部私有 —— 冻结面 `model.FROZEN` 只认下面那 8 个函数，
#       新增公开符号要走 `[interface-change]` 流程，并行阶段不许改 model.py）
# --------------------------------------------------------------------------

_SETUP_NAME = "gdsout_setup"
"""官方 design 目录的**判据**：有这个文件就是（cfg.sh 的 `suggest_offdir` 同款）。"""

_RUNSH_GLOB_PREFIX = "run_ewave_"
_RUNSH_SUFFIX = ".sh"
"""官方内层脚本 `run_ewave_<corner>_<temp>.sh`。
⚠️ 前缀匹配而不是 `*run_ewave*`：`remote_run_ewave.sh` 是**外层**提交脚本，
两者内容完全不同，认错了 ptxt / 端口表全解析不出来。"""

_REMOTE_NAME = "remote_run_ewave.sh"

_PTXT_CORNER_KEY = "{corner}"
"""`SiteFacts.ptxt_name_template` 里的占位符。

⚠️ 这个字面量在 `core.cmd._ptxt_path_for_corner` 的兜底分支里也出现了一次
（P1 写的，那时本模块还不存在）。**两处必须一致**，见交接报告的
`interface_change_requests`：建议把它提到 `model.py` 里当公开常量。"""

_GDSOUT_VARYING_FIELDS: tuple[tuple[str, str], ...] = (
    ("runDir", "@@RUNDIR@@"),
    ("library", "@@LIBRARY@@"),
    ("topCell", "@@TOPCELL@@"),
    ("view", "@@VIEW@@"),
    ("strmFile", "@@STRMFILE@@"),
    ("logFile", "@@LOGFILE@@"),
    ("layerMap", "@@LAYERMAP@@"),
)
"""`gdsout_setup` 里**随 design 变**的 7 个字段 → 模板占位符（D1c）。

占位符名字抄自 `mvp/redzone/gdsout_setup.tmpl`（红区 MVP 实跑过的那份），
`ewave_batch.tools.strmout.GDSOUT_PLACEHOLDERS` 必须与之一致 ——
`tests/test_discover.py::TemplatePlaceholderContract` 在 strmout 落地后会盯着这条。

🚨 **其余字段一律逐字保留**，一个都不许"顺手改成更合理的值"：
`convertPin "geometry"` + `pinAttNum 1` 决定 pin 是不是几何图形，
`case "preserve"` 决定大小写混用的端口名匹不匹配得上，
`maxVertices 200` 决定顶点怎么切 → mesh 不同 → L/Q 不同，而且跑得出来、数字也像。
"""

_SITE_IDENTITY_FLAGS = frozenset(
    {
        "--emssTechFile",
        "--gds",
        "--top",
        "--workDir",
        "--sparam",
        "--key",
    }
)
"""从官方命令里学默认表时要剔掉的**站点身份 / per-run**项。

🚨 **精确名匹配，绝不前缀匹配。** MVP 踩过：排除规则写 `--sparam` 前缀，
把 `--sparamImpedance` 一起吃掉了，两边同时被跳过 → diff 空得非常好看但根本没比
（BRIEF §10）。这里用 `frozenset` 的成员判断，前缀问题在结构上就不存在；
回归测试见 `tests/test_discover.py::LearnDefaultFlags::test_sparam_does_not_eat_sparamimpedance`。
"""

_GDSOUT_LINE = re.compile(r"^([ \t]*)([A-Za-z_][A-Za-z0-9_]*)([ \t]+)(.*)$")
"""`gdsout_setup` 的一行：缩进 + key + 分隔空白 + value。
value 可能带引号、可能为空（裸 flag `arrayInstToScalar` 连分隔空白都没有 → 不匹配，
由调用方单独处理）。"""

_DSUB_A = re.compile(r"(?:^|\s)-A[ \t]+(\S+)")
_DSUB_Q = re.compile(r"(?:^|\s)-q[ \t]+(\S+)")
_DSUB_R_QUOTED = re.compile(r"(?:^|\s)-R[ \t]+([\"'])(.*?)\1")
_DSUB_R_BARE = re.compile(r"(?:^|\s)-R[ \t]+(\S+)")
"""dsub 三元组。`-R` 的值几乎总是带引号（里面有 `;`），但裸值也认。"""

_PDK_ROOT_CUT = re.compile(r"/apps/ewave/.*$")
"""ptxt 路径 → PDK 根的倒推：切掉 `/apps/ewave/…` 以后那一截（cfg.sh 同款）。"""

_TOOL_ENV_SUFFIXES = ("_BIN", "_ABS")
"""`find_tool` 的环境变量兜底：`EWAVE_BIN` / `EWAVE_ABS`（后者是 cfg.sh 用的名字）。"""

_SUGGEST_PRUNE = frozenset({".git", "__pycache__", ".venv", "venv", "node_modules", ".tox"})
_SUGGEST_LIMIT = 50
"""`suggest_official_dirs` 的剪枝与上限 —— 它跑在**报错路径**上，不许把 `$HOME` 走穿。"""


# --------------------------------------------------------------------------
# gdsout_setup
# --------------------------------------------------------------------------


def parse_gdsout_setup(text: str) -> dict[str, str]:
    """`gdsout_setup` 文本 → 字段 dict。

    格式是 `key<tab>value`，value 可能带引号，也可能是裸 flag（`arrayInstToScalar` 没有值 → `""`）。
    纯字符串函数，单测全走它。

    细则（都照 `cfg.sh` 的 `gs_field`）：

    * 行首缩进（真实文件每行都以 tab 起头）无所谓；
    * key 和 value 之间可以是 tab 也可以是空格，几个都行（真实文件里两种都有）；
    * value 两端**成对**的 `"` 或 `'` 剥掉；`""` → 空串；
    * `#` 开头的注释行和空行跳过；
    * **同名 key 首次出现的那条胜出**（cfg.sh 的 awk 是 `print; exit`）。
    """
    fields: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _GDSOUT_LINE.match(raw_line)
        if match is not None:
            key = match.group(2)
            value = _strip_quotes(match.group(4).strip())
        else:
            # 裸 flag：整行就是一个 key，没有 value（`arrayInstToScalar`）。
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", line):
                continue
            key, value = line, ""
        fields.setdefault(key, value)
    return fields


def _quote_char(value: str) -> str:
    """value 两端**成对**的引号是哪个（`"` / `'`）。没有成对引号 → 空串。

    只认成对的：单边引号（`"unbalanced`）原样留着 —— 宁可留着也别切错。
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[0]
    return ""


def _strip_quotes(value: str) -> str:
    """剥掉成对的首尾引号。"""
    return value[1:-1] if _quote_char(value) else value


def templatize_gdsout_setup(text: str) -> str:
    """官方 `gdsout_setup` → 模板：只把 7 个随 design 变的字段换成 `@@…@@` 占位符
    （`GDSOUT_PLACEHOLDERS`），其余**逐字保留**。

    D1c 的全部要害在"其余逐字保留"上：`convertPin "geometry"` + `pinAttNum 1` 决定 pin 是不是
    几何图形，`case "preserve"` 决定大小写混用的端口名匹不匹配得上，`maxVertices 200`
    决定顶点怎么切 → mesh 不同 → L/Q 结果不同，而且跑得出来、数字也像。

    实现上"逐字保留"是**结构性**的，不是靠自觉：按行走，只重写命中那 7 个 key 的行，
    连该行的缩进、key 与 value 之间的原始空白、原来的引号形态、以及行尾（`\\n` / `\\r\\n`）
    都照抄。没命中的行**原样 append**，一个字符都不碰。
    """
    varying = dict(_GDSOUT_VARYING_FIELDS)
    out: list[str] = []
    for raw_line in text.splitlines(keepends=True):
        body, newline = _split_newline(raw_line)
        match = _GDSOUT_LINE.match(body)
        if match is None:
            out.append(raw_line)
            continue
        indent, key, gap, value = match.groups()
        placeholder = varying.get(key)
        if placeholder is None:
            out.append(raw_line)
            continue
        quote = _quote_char(value.strip())
        out.append(f"{indent}{key}{gap}{quote}{placeholder}{quote}{newline}")
    return "".join(out)


def _split_newline(raw_line: str) -> tuple[str, str]:
    """把一行拆成 (正文, 行尾)。行尾可能是 `\\n` / `\\r\\n` / 空（最后一行没换行）。"""
    for ending in ("\r\n", "\n", "\r"):
        if raw_line.endswith(ending):
            return raw_line[: -len(ending)], ending
    return raw_line, ""


# --------------------------------------------------------------------------
# remote_run_ewave.sh —— dsub 三元组
# --------------------------------------------------------------------------


def parse_dsub_options(text: str) -> dict[str, str]:
    """`remote_run_ewave.sh` 文本 → `{"account": …, "queue": …, "resources": …}`（缺的键就不给）。

    **缺的键就不给**（而不是给空串）：调用方靠 `in` 判断"官方到底有没有指定队列"，
    空串会让"没写"和"写了个空的"看起来一样。

    ⚠️ 只在**含 `dsub` 的行**里找（比 cfg.sh 的整文件 grep 严一点）：官方脚本里常留着
    注释掉的旧参数（`# 上次用的是 -q <别的队列>`），整文件 grep 会把注释当真 ——
    然后我们拿一个已经作废的队列去提交，作业排在那儿不动，而命令看起来完全正常。
    一行 `dsub` 都没有时退回整文件扫（片段也要能解析，单测就是喂片段的）。
    """
    scope = _dsub_scope(text)
    options: dict[str, str] = {}
    account = _DSUB_A.search(scope)
    if account is not None:
        options["account"] = account.group(1)
    queue = _DSUB_Q.search(scope)
    if queue is not None:
        options["queue"] = queue.group(1)
    quoted = _DSUB_R_QUOTED.search(scope)
    if quoted is not None:
        options["resources"] = quoted.group(2)
    else:
        bare = _DSUB_R_BARE.search(scope)
        if bare is not None:
            options["resources"] = bare.group(1)
    return options


def _dsub_scope(text: str) -> str:
    """把要扫的范围收窄到含 `dsub` 的行（注释行排除）。一行都没有就返回原文。"""
    lines = [
        line
        for line in text.splitlines()
        if "dsub" in line and not line.lstrip().startswith("#")
    ]
    return "\n".join(lines) if lines else text


# --------------------------------------------------------------------------
# ptxt —— corner 轴要同时改两处（BRIEF §7）
# --------------------------------------------------------------------------


def ptxt_path_for_corner(facts: SiteFacts, corner: str) -> str:
    """算某个 corner 对应的 ptxt 绝对路径。

    ⚠️ corner 轴要同时改两处（§7）：`--corner=` 的值 **和** `--emssTechFile=` 的文件名 ——
    ptxt 是 per-corner 一个文件，文件名里带 corner。这个函数负责后者。
    `facts.ptxt_name_template` 为空（没解析出模板）→ `DiscoveryError`。不检查文件是否存在
    （那是 `check-env` 的活，dry-run 在没有 PDK 的机器上也要能跑）。

    危险在于这是**字符串替换**：corner 名（`typical`）完全可能同时出现在目录段、
    出现在别的词里面（`atypical`）、出现在版本目录里（`typical_v2`）。
    换错一处 = 「目录名说 typical、实际用了别的工艺角」，而且跑得出来、数字也像。
    所以真正的替换发生在 **discovery 时**（`_ptxt_name_template`，只动 basename、
    只认词边界），这里只是把已经算好的模板填上值 —— 危险动作只做一次、只在一个地方。
    """
    if not facts.ptxt_name_template:
        raise DiscoveryError(
            "SiteFacts.ptxt_name_template 是空的 —— 没能从官方 ptxt 路径里认出 corner 那一段，\n"
            "  于是换 corner 时 --emssTechFile 会**不跟着变**：目录名说一个工艺角、\n"
            "  实际算的是另一个，而且跑得出来、数字也像（BRIEF §7）。\n"
            "  下一步（三选一）：\n"
            "    1) 确认官方 run 目录里 run_ewave_*.sh 的 --emssTechFile 文件名里确实带 corner 名；\n"
            "    2) corner 名与文件名里那一段拼写不同时，在 spec 的 corner 轴上直接写死\n"
            "       --emssTechFile 的取值（轴的 flags 支持多 flag）；\n"
            "    3) 这个批次本来就不扫 corner 轴 —— 那就别让 corner 轴进 spec。"
        )
    if _PTXT_CORNER_KEY not in facts.ptxt_name_template:
        raise DiscoveryError(
            f"SiteFacts.ptxt_name_template 里没有 {_PTXT_CORNER_KEY} 占位符"
            f"（拿到的是 {facts.ptxt_name_template!r}）—— 换 corner 会得到一条**没变**的 ptxt 路径。\n"
            "  下一步：让 discover_site_facts 重新解析官方 run 目录，别手工拼这个字段。"
        )
    name = facts.ptxt_name_template.replace(_PTXT_CORNER_KEY, corner)
    directory = facts.ptxt_dir or posixpath.dirname(facts.ptxt)
    return posixpath.join(directory, name) if directory else name


def _ptxt_name_template(ptxt: str, corner: str) -> tuple[str, int]:
    """ptxt 路径 + 官方 corner → (文件名模板, 命中次数)。认不出返回 `("", 0)`。

    两道过滤器，每一道都对应一类真实的误伤：

    1. **只动 basename。** 目录段里出现同一个 corner 名是常态
       （`…/process/typical/…`），换掉它就指到了根本不存在的目录。
    2. **词边界**（前后不是字母/数字）。`atypical` 里的 `typical` 不是 corner，
       换掉它得到的是 `acworst` 这种没人能一眼看出错的字符串。
       `_` / `.` / `-` **算**边界 —— ptxt 文件名就是用 `_` 分段的
       （`…_AL28K_<corner>_V1.0_encrypted_package.ptxt`）。

    命中 > 1 次时照样全换，但调用方会记一条 warning：文件名里出现两处 corner
    本身就是歧义，静默挑一处才是更坏的选择。
    """
    name = posixpath.basename(ptxt)
    if not name or not corner:
        return "", 0
    pattern = re.compile(r"(?<![0-9A-Za-z])" + re.escape(corner) + r"(?![0-9A-Za-z])")
    templated, count = pattern.subn(_PTXT_CORNER_KEY, name)
    if count == 0:
        return "", 0
    return templated, count


# --------------------------------------------------------------------------
# 默认表：不写死在源码，从官方 run 目录学（§11 规则 1）
# --------------------------------------------------------------------------


def learn_default_flags(facts: SiteFacts) -> FlagDict:
    """从官方实际在用的 flag 里学出「默认表」（§11 规则 1：**默认表的值不写死在源码**）。

    剔掉：站点相关（`--emssTechFile` / `--gds` / `--top` / `--workDir` / `--sparam` / `--key`）、
    机制层（`MECHANISM_FLAGS`）、以及**任何一根轴掌管的 flag**（那些由轴给）。
    剔除按 flag 名精确匹配 —— 又是 `--sparam` / `--sparamImpedance` 那个坑，别用前缀。

    留下来的正好是 §11「默认表」那一层：影响结果、但基本不动、也不进目录名的那些
    （`--labelDepth` / `--viaMode` / `--sparamImpedance` / …）。

    输入优先取 `facts.production_flags`（已经去过站点项），空则退回 `facts.official_flags`。
    两条路的结果必须一致 —— 剔除是幂等的集合运算。
    """
    source = facts.production_flags or facts.official_flags
    drop = set(_SITE_IDENTITY_FLAGS) | set(MECHANISM_FLAGS) | _axis_owned_flags()
    return {name: value for name, value in source.items() if name not in drop}


def _axis_owned_flags() -> frozenset[str]:
    """内置轴目录里所有被轴掌管的 flag 名（`--corner` / `--temperature` / `-e` / …）。

    这些由轴层给（它们是 run 的身份、会进目录名），学进默认表就会出现
    「默认表说 25.0、轴说 125.0」这种两个层同时写同一个 flag 的局面 ——
    合并顺序保证轴赢，但默认表里留着一个永远不生效的值本身就是误导。
    """
    owned: set[str] = set()
    for axis in matrix.builtin_axis_catalog().values():
        owned.update(axis.flags)
    return frozenset(owned)


# --------------------------------------------------------------------------
# 工具路径 —— 绝不写死绝对路径
# --------------------------------------------------------------------------


def find_tool(name: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """`command -v` 的等价物（`shutil.which`），找不到返回 None。

    **工具绝对路径不许进源码**（CLAUDE.md 硬约束 1b）→ 版本目录一律靠这条路发现。

    两级：

    1. `shutil.which(name)` —— PATH 上有就用 PATH 上的（红区 `ma ewave/…` 之后就有）；
    2. 环境变量兜底 `<NAME>_BIN` / `<NAME>_ABS`（`EWAVE_BIN` / `EWAVE_ABS`，
       后者是 `mvp/redzone/cfg.sh` 用的名字）—— 给"装在 PATH 外面"的站点留的口子，
       值由用户的环境提供，**不是源码里的常量**。

    传了 `env` 就**只**看 `env`（连 PATH 也从它取；没给 PATH 就当 PATH 是空的）——
    测试要能在一台根本没有 ewave 的机器上把两条分支都走一遍。
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    search_path = None if env is None else environ.get("PATH", "")
    found = shutil.which(name, path=search_path)
    if found:
        return found
    stem = re.sub(r"[^A-Za-z0-9]", "_", name).upper()
    for suffix in _TOOL_ENV_SUFFIXES:
        value = environ.get(stem + suffix, "").strip()
        if value:
            return value
    return None


# --------------------------------------------------------------------------
# 找官方 run 目录 —— 用户打错一个字符后面全废，让机器找
# --------------------------------------------------------------------------


def suggest_official_dirs(root: str, *, max_depth: int = 3) -> list[str]:
    """在 `root` 底下找含 `gdsout_setup` 的目录，当作官方 design 目录的候选返回（已排序去重）。

    抄 `cfg.sh` 的 `suggest_offdir`：用户打错一个字符后面全废，不如让机器找。只读。

    `max_depth` 的语义与 `find -maxdepth` 一致：**`gdsout_setup` 这个文件**的深度上限
    （`root` 自己算 0）。所以 `max_depth=3` 找的是 `root/*/*/gdsout_setup` 为止。

    它跑在**报错路径**上，所以要便宜：跳过 `.git` / `__pycache__` / `.venv` 之类，
    跳过点开头的目录，命中 `_SUGGEST_LIMIT` 条就停。找不到返回空 list，**不抛异常** ——
    "帮忙找候选"失败不该盖掉真正的那条错误。
    """
    if not root:
        return []
    base = os.path.abspath(root)
    if not os.path.isdir(base):
        return []

    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base, onerror=lambda _exc: None):
        relative = os.path.relpath(dirpath, base)
        depth = 0 if relative in (os.curdir, "") else relative.count(os.sep) + 1
        if depth + 1 >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [
                d for d in dirnames if d not in _SUGGEST_PRUNE and not d.startswith(".")
            ]
            dirnames.sort()
        if _SETUP_NAME in filenames:
            found.append(os.path.abspath(dirpath))
            if len(found) >= _SUGGEST_LIMIT:
                break
    return sorted(set(found))


# --------------------------------------------------------------------------
# 主路径
# --------------------------------------------------------------------------


def discover_site_facts(official_run_dir: str, *, env: Mapping[str, str] | None = None) -> SiteFacts:
    """解析一个官方 GUI 跑过的 design 目录 → `SiteFacts`。

    这是 CLAUDE.md 硬约束 1b 的主路径：**坐标不手抄，现场解析**，既没有标识符进仓库，
    也没有抄错的可能（尤其那 34 个 `-p` flag）。解析路径与 `mvp/redzone/cfg.sh` 一致，
    改之前先读那个文件。

    目录不存在 / 没有 `gdsout_setup` → `DiscoveryError`（消息里要提示"这不像官方 design 目录"）。
    只读，一个字节都不写。

    **硬失败只有三种**（都在最前面，且每条都带"下一步怎么办"）：没给目录、不是目录、
    里面没有 `gdsout_setup`。其余一律是**软失败** —— 记进 `SiteFacts.warnings`，
    字段留空。理由：官方目录的形态在不同版本之间会变（比如只在本地跑、没有
    `remote_run_ewave.sh`），为一个缺失的可选文件炸掉整个批次的规划不划算；
    而留空 + warning 让调用方能自己决定"这个字段我用不用得着"。
    """
    setup_path = _validate_official_dir(official_run_dir)
    directory = os.path.dirname(setup_path)

    warnings: list[str] = []
    source_files: dict[str, str] = {}
    facts = SiteFacts(official_run_dir=directory)

    _read_gdsout(facts, setup_path, source_files)
    _read_run_script(facts, directory, source_files, warnings)
    _read_remote_script(facts, directory, source_files, warnings)
    _resolve_tools(facts, source_files, warnings, env=env)

    facts.source_files = source_files
    facts.warnings = tuple(warnings)
    return facts


def _validate_official_dir(official_run_dir: str) -> str:
    """三道闸，每条错误都带一条能照着做的下一步。返回 `gdsout_setup` 的路径。"""
    if not official_run_dir or not official_run_dir.strip():
        raise DiscoveryError(
            "没给官方 run 目录 —— 不知道去哪儿解析站点坐标。\n"
            "  它是官方 GUI（eWave on Virtuoso）跑过的那个 design 目录，长这样：\n"
            "    <workarea>/ewave_simulation/<library>_<topCell>_<view>/   （里面有 gdsout_setup）\n"
            "  下一步：spec 里写 designs[].official_run_dir，或 CLI 传 --official-run-dir。\n"
            + _candidates_help(os.getcwd())
        )
    path = official_run_dir.strip()
    if not os.path.isdir(path):
        what = "是个文件，不是目录" if os.path.exists(path) else "不存在"
        raise DiscoveryError(
            f"官方 run 目录{what}: {path}\n"
            "  下一步：确认路径拼写（红区路径很长，打错一个字符后面全废）。\n"
            + _candidates_help(os.path.dirname(os.path.abspath(path)) or os.getcwd())
        )
    setup_path = os.path.join(path, _SETUP_NAME)
    if not os.path.isfile(setup_path):
        raise DiscoveryError(
            f"{path} 里没有 {_SETUP_NAME} —— 这不像是官方 GUI 的 design 目录。\n"
            f"  判据就是这个文件：官方 stream out 时会把它留在 design 目录里。\n"
            "  下一步：多半是指到了上一级（ewave_simulation/）或指到了 <corner>_<temp>/ 子目录，\n"
            "  往下/往上挪一层再试。\n" + _candidates_help(path)
        )
    return setup_path


def _candidates_help(root: str) -> str:
    """报错时顺手把附近的候选列出来（cfg.sh 的 `offdir_help`）。找不到就说找不到。"""
    candidates: list[str] = []
    seen: set[str] = set()
    for probe in (root, os.path.join(root, ".."), os.getcwd()):
        for found in suggest_official_dirs(probe):
            if found not in seen:
                seen.add(found)
                candidates.append(found)
        if len(candidates) >= 10:
            break
    if not candidates:
        return "  -- 在附近没找到含 gdsout_setup 的目录（请手动指定）--"
    listed = "\n".join("    " + c for c in candidates[:10])
    return "  -- 在附近找到的候选（直接抄一行）--\n" + listed


def _read_gdsout(facts: SiteFacts, setup_path: str, source_files: dict[str, str]) -> None:
    """`gdsout_setup` → library / topCell / view / layerMap + 整份模板。"""
    text = _read_text(setup_path)
    fields = parse_gdsout_setup(text)
    facts.library = fields.get("library", "")
    facts.top_cell = fields.get("topCell", "")
    facts.view = fields.get("view", "")
    facts.layer_map = fields.get("layerMap", "")
    facts.gdsout_template = templatize_gdsout_setup(text)
    for name in ("library", "top_cell", "view", "layer_map", "gdsout_template"):
        source_files[name] = setup_path


def _read_run_script(
    facts: SiteFacts,
    directory: str,
    source_files: dict[str, str],
    warnings: list[str],
) -> None:
    """`run_ewave_<corner>_<temp>.sh` → ptxt / key / corner / temperature / 端口表 / 生产 flag。"""
    scripts = _find_run_scripts(directory)
    if not scripts:
        warnings.append(
            f"{directory} 里没有 {_RUNSH_GLOB_PREFIX}*{_RUNSH_SUFFIX} —— "
            "ptxt / key / corner / 端口表全解析不出来。"
            "下一步：在官方 GUI 里对这个 design 跑一次（哪怕只是生成脚本），再回来。"
        )
        return
    if len(scripts) > 1:
        warnings.append(
            f"官方 run 脚本有 {len(scripts)} 份，取排序后的第一份 "
            f"({os.path.basename(scripts[0])})。它们之间通常只差 corner/temperature，"
            "但 flag 若真有分歧，学到的默认表就取决于文件名排序 —— 值得看一眼。"
        )
    script_path = scripts[0]
    text = _read_text(script_path)
    line = template.extract_command_line(text, program="ewave")
    if line is None:
        warnings.append(
            f"{script_path} 里没找到 ewave 那一行 —— 官方脚本的形态可能变了。"
            "下一步：打开它确认第一条非注释命令确实是 ewave（或它的绝对路径）。"
        )
        return

    parsed = template.parse_command_line(line)
    facts.official_command_line = line
    facts.official_flags = dict(parsed.flags)
    facts.official_port_spec = parsed.port_spec
    facts.production_flags = {
        name: value for name, value in parsed.flags.items() if name not in _SITE_IDENTITY_FLAGS
    }
    facts.ptxt = str(parsed.flags.get("--emssTechFile", "") or "")
    facts.key = str(parsed.flags.get("--key", "") or "")
    facts.corner = str(parsed.flags.get("--corner", "") or "")
    facts.temperature = str(parsed.flags.get("--temperature", "") or "")
    facts.ewave_dir_name = matrix.ewave_dir_name(facts.corner, facts.temperature)

    if facts.ptxt:
        facts.ptxt_dir = posixpath.dirname(facts.ptxt)
        pdk_root = _PDK_ROOT_CUT.sub("", facts.ptxt)
        facts.pdk_root = pdk_root if pdk_root != facts.ptxt else ""
        name_template, hits = _ptxt_name_template(facts.ptxt, facts.corner)
        facts.ptxt_name_template = name_template
        if hits == 0:
            warnings.append(
                f"ptxt 文件名里没认出 corner ({facts.corner!r})，"
                "换 corner 时 --emssTechFile 不会跟着变（BRIEF §7 要求同时改两处）。"
                "下一步：corner 轴上直接写死 --emssTechFile 的取值，或别扫 corner 轴。"
            )
        elif hits > 1:
            warnings.append(
                f"ptxt 文件名里 corner ({facts.corner!r}) 出现了 {hits} 次，全部换掉了。"
                "下一步：换 corner 之后核对一眼 --emssTechFile 是不是你要的那个文件。"
            )
    else:
        warnings.append(
            "官方命令里没有 --emssTechFile —— ptxt 路径解析不出来，corner 轴会跑不起来。"
        )

    for name in (
        "ptxt",
        "ptxt_dir",
        "ptxt_name_template",
        "pdk_root",
        "key",
        "corner",
        "temperature",
        "official_command_line",
        "official_flags",
        "official_port_spec",
        "production_flags",
    ):
        source_files[name] = script_path


def _find_run_scripts(directory: str) -> list[str]:
    """`run_ewave_*.sh`，按文件名排序。**前缀匹配**，`remote_run_ewave.sh` 不算。"""
    try:
        names = os.listdir(directory)
    except OSError:  # pragma: no cover - 目录刚被验过存在，这里只是防竞态
        return []
    hits = [
        n
        for n in names
        if n.startswith(_RUNSH_GLOB_PREFIX)
        and n.endswith(_RUNSH_SUFFIX)
        and os.path.isfile(os.path.join(directory, n))
    ]
    return [os.path.join(directory, n) for n in sorted(hits)]


def _read_remote_script(
    facts: SiteFacts,
    directory: str,
    source_files: dict[str, str],
    warnings: list[str],
) -> None:
    """`remote_run_ewave.sh` → dsub 的 `-A` / `-q` / `-R`。缺文件是软失败。"""
    remote_path = os.path.join(directory, _REMOTE_NAME)
    if not os.path.isfile(remote_path):
        warnings.append(
            f"{directory} 里没有 {_REMOTE_NAME} —— dsub 的账号/队列/资源没学到。"
            "下一步：在 spec 的 scheduler 段里显式写 account / queue / resources。"
        )
        return
    options = parse_dsub_options(_read_text(remote_path))
    facts.dsub_account = options.get("account", "")
    facts.dsub_queue = options.get("queue", "")
    facts.dsub_resources = options.get("resources", "")
    for key, name in (
        ("account", "dsub_account"),
        ("queue", "dsub_queue"),
        ("resources", "dsub_resources"),
    ):
        if key in options:
            source_files[name] = remote_path
        else:
            warnings.append(
                f"{_REMOTE_NAME} 里没解析出 dsub 的 {key} —— 该字段留空，"
                "下一步：在 spec 的 scheduler 段里显式给它。"
            )


def _resolve_tools(
    facts: SiteFacts,
    source_files: dict[str, str],
    warnings: list[str],
    *,
    env: Mapping[str, str] | None,
) -> None:
    """`ewave` / `strmout` 的实际路径。**绝不写死** —— 版本目录随 `ma` 模块变。

    ⚠️ `ewave_version` 这里**不填**：唯一可靠的取法是执行 `ewave --version`，
    而 discovery 只读、不执行任何工具（本机根本没有 ewave，执行就是不可测的）。
    它由 P6 的 `doctor.sh` / `check-env` 填。
    """
    for name, attr in (("ewave", "ewave_bin"), ("strmout", "strmout_bin")):
        found = find_tool(name, env=env)
        setattr(facts, attr, found or "")
        if found:
            source_files[attr] = f"which:{name}"
        else:
            warnings.append(
                f"PATH 上没有 {name}（也没有 {name.upper()}_BIN / {name.upper()}_ABS 环境变量）——"
                f" 本机没装是正常的，dry-run 照跑；真提交前需要 `ma` 出 {name} 所在的模块。"
            )


def _read_text(path: str) -> str:
    """读文本。红区文件基本是 ASCII，但注释里偶有中文 → UTF-8 + `errors="replace"`
    （**解析失败也不许炸**：一个坏字节不该让整个批次规划不出来）。"""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()
