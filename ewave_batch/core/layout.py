"""目录布局 / `cmd.sh` / 归档规则 / 产物验收 / `batch.json` 原子读写 / `runs.csv`。

权威是 `PROJECT_BRIEF.md` §5「归档布局」那棵树：

```
<batch_root>/<batch_name>/          batch_dir
  batch.json                        resume 只认这一个文件
  runs.csv                          汇总表
  gds/<design>.gds                  阶段 1 的产物，整个设定矩阵共用（D1a）
  gdsout/<design>.gdsout_setup      渲染出来的 strmout 模板，留档可追溯（D1c）
  sparam/<design>__<slug>__<corner>_<temp>.s4p    成功 run 的参数文件扁平汇聚
  runs/<design>/<axes-slug>/        ← ★ --workDir 指到这里
    cmd.sh                          该 run 的完整命令（可单独手工重跑）
    <corner>_<temp>/                ← ★ eWave 自己建的那层，我们控制不了名字
```

三条本模块必须自己守住的规矩：

1. **绝不写进设计师的 spine。** `<workarea>/ewave_simulation/` 只读（CLAUDE.md 硬约束 4）。
   落点路径里出现 `ewave_simulation` 这一层 → `StateError`。守卫抄的是 `mvp/redzone/cfg.sh`
   里那个 shell 版（`case "$MVP" in */ewave_simulation|*/ewave_simulation/*)`）。
   **唯一例外**是显式触发的 `set_run_as_current`：那一处要覆盖前备份 + 记日志。
2. **`done` 的判据是产物验过，不是退出码**（§12）。MVP 实测：eWave 崩了也 `exit=0`、
   还会留 0 字节文件报 "done"、写失败被吞。所以验收 = 存在 + 非空 + 端口数对。
3. **`batch.json` 原子写**：同目录临时文件 + `os.replace`。跑到一半断电不能留半份 JSON。
   临时文件必须建在**目标同目录** —— `os.replace` 跨卷会失败。

只用 stdlib，路径一律用 `/` 拼（最终跑在 Linux 上；Windows 上单测比字符串也一致）。
"""

from __future__ import annotations

import csv
import datetime
import fnmatch
import io
import json
import os
import re
import shlex
import shutil
import tempfile
from collections.abc import Mapping, Sequence

from ..model import (
    AXIS_SLUG_SEP,
    BASE_GROUP,
    BATCH_JSON_NAME,
    CMD_SH_NAME,
    CMD_SH_TEMPLATE,
    GDS_DIRNAME,
    GDSOUT_DIRNAME,
    GDSOUT_SETUP_SUFFIX,
    LOGS_DIRNAME,
    RUNS_CSV_COLUMNS,
    RUNS_CSV_NAME,
    RUNS_DIRNAME,
    RUN_LOG_NAME,
    RUN_LOG_TEMPLATE,
    SCHEMA_VERSION,
    SPARAM_DIRNAME,
    TIMESTAMP_FORMAT,
    ArchiveReport,
    Axis,
    AxisKind,
    AxisValue,
    BatchOptions,
    BatchState,
    CommandPlan,
    Design,
    Job,
    JobState,
    LogFacts,
    PortMode,
    PortSpec,
    Provenance,
    Run,
    RunGroup,
    RunPaths,
    RunStatus,
    StateError,
    StreamoutTask,
    VerifyReport,
)

__all__ = [
    "compute_run_paths",
    "ensure_run_dirs",
    "write_cmd_sh",
    "verify_run_outputs",
    "archive_run",
    "check_port_consistency",
    "port_count_from_suffix",
    "state_to_dict",
    "state_from_dict",
    "read_batch_state",
    "write_batch_state",
    "write_runs_csv",
    "set_run_as_current",
]


# --------------------------------------------------------------------------
# 本模块自己的常量（BRIEF §5 里有名字、但 model 没冻的那几个）
# --------------------------------------------------------------------------

SPINE_DIRNAME = "ewave_simulation"
"""设计师 spine 的那一层目录名。落点路径里出现它 = 拒绝（CLAUDE.md 硬约束 4）。
与 `mvp/redzone/cfg.sh` 的守卫**同一条规则**，只是这里是 Python 版。"""

GDS_SUFFIX = ".gds"

# `RUN_LOG_NAME` / `CMD_SH_NAME` 是**退路**名字：只在 `<corner>_<temp>` 预测不出来、
# 且 run_id 也给不出词根时才用。正常路径走 `RUN_LOG_TEMPLATE` / `CMD_SH_TEMPLATE`
# （每个 run 一份，理由见 model.CMD_SH_TEMPLATE）。两者都从 model 来，别在这儿重定义。

SET_CURRENT_LOG_NAME = "set_current.log"
"""`set_run_as_current` 的审计日志。写进批次的 `logs/`，**不写进 spine**。"""

_TOUCHSTONE_SUFFIX = re.compile(r"\.([A-Za-z])(\d+)[pP]$")
"""Touchstone 后缀：`.s4p` / `.y3p` / `.z2p`。**锚在字符串末尾** ——
`x.s4p.bak` 不算，那是备份不是产物。"""

_SAMPLE_MARK = "_sample"
"""eWave 顺带产的「求解器真算过的频点」那份（BRIEF §5 官方布局）。
扁平区里要保住这个标记，否则两份文件会撞名。"""


# --------------------------------------------------------------------------
# 内部小工具
# --------------------------------------------------------------------------


def _posix(path: str) -> str:
    """路径归一成 `/` 分隔、无尾斜杠。Windows 上写测试也能逐字符比对。"""
    text = str(path).replace("\\", "/")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def _join(*parts: str) -> str:
    """用 `/` 拼路径。空片段直接跳过，别拼出 `a//b`。"""
    cleaned = [_posix(p) for p in parts if p not in ("", None)]
    if not cleaned:
        return ""
    head = cleaned[0]
    tail = [p.strip("/") for p in cleaned[1:]]
    return "/".join([head] + [t for t in tail if t])


def _assert_outside_spine(path: str, what: str) -> None:
    """落点不许在设计师的 spine 里（CLAUDE.md 硬约束 4）。

    抄 `mvp/redzone/cfg.sh`：`case "$MVP" in */ewave_simulation|*/ewave_simulation/*)` ——
    也就是「路径里任何一层叫 ewave_simulation」都拒绝，不管是最后一层还是中间层。
    """
    for part in _posix(path).split("/"):
        if part == SPINE_DIRNAME:
            raise StateError(
                f"{what} lands inside {SPINE_DIRNAME}/: {path}\n"
                f"  That is the official GUI's turf (the designer's spine); this tool only reads it.\n"
                f"  Next: point batch_root somewhere else, e.g. <workarea>/ewave_batches.\n"
                f"  The only exception is the explicit 'set this run as current' (set_run_as_current)."
            )


def _validate_component(name: str, what: str) -> str:
    """目录名片段的体检：不许空、不许带路径分隔符、不许是 `..`。

    这些片段来自 `design_key` / `axes_slug` / `ewave_dir`，全是别的模块算出来的 ——
    真漏进来一个 `../..`，我们就把文件写到批次外面去了。
    """
    text = str(name).strip()
    if not text:
        raise StateError(f"{what} is empty - cannot build a directory name from it")
    if "/" in text or "\\" in text:
        raise StateError(f"{what} contains a path separator: {name!r} - it may only be one directory level")
    if text in (".", ".."):
        raise StateError(f"{what} is {name!r} - that would push the target outside the batch")
    return text


def _design_dir_name(design: Design, run: Run) -> str:
    """这个 design 在批次里的目录名。

    优先用 `run.design_key`（`core.matrix.expand_runs` 算好塞进去的，`run_id` 也是拿它拼的），
    其次 `design.key`；两个都空才兜底。兜底规则照 BRIEF §5：
    「design 目录名 = `<library>_<topCell>_<view>`」。

    **故意不 import `core.matrix`**：这里只需要一个稳定的目录名，而 `Run.design_key`
    是冻结面上的必填字段 —— 让路径依赖另一个模块能否 import，只会让落点变得不可预测。
    """
    for candidate in (run.design_key, design.key):
        if candidate and str(candidate).strip():
            return _validate_component(candidate, "design_key")
    fallback = "_".join(p for p in (design.library, design.cell, design.view) if p)
    return _validate_component(fallback, "design dir name (library_cell_view fallback)")


def _utcnow() -> str:
    """UTC、秒精度、ISO-8601（`model.TIMESTAMP_FORMAT`）。别在 batch.json 里写本地时间。"""
    return datetime.datetime.now(datetime.timezone.utc).strftime(TIMESTAMP_FORMAT)


def _atomic_write_text(path: str, text: str, *, newline: str = "\n") -> None:
    """原子写文本：**同目录**临时文件 → fsync → `os.replace`。

    ⚠️ 临时文件必须和目标同目录：`os.replace` 在 Windows 上跨卷会失败，而"跨卷"这件事
    只会在别人的机器上发作。失败时把临时文件删掉，绝不留残骸 —— 留下的 `.tmp` 会让
    下一次 resume 的人以为批次坏了。
    """
    target = _posix(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(target)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _is_sparam_name(name: str) -> bool:
    """是不是 S 参数产物（`.s4p` / `.S17P`）。`.y4p` 不算 —— 用户只要 S 参数（D5）。"""
    match = _TOUCHSTONE_SUFFIX.search(name)
    return bool(match) and match.group(1).lower() == "s"


def _flat_suffix(name: str) -> str | None:
    """扁平区文件名里接在 `RunPaths.sparam_prefix` 后面的那一截。

    `<Cell>_<corner>_<temp>.s4p`        → `.s4p`
    `<Cell>_<corner>_<temp>_sample.s4p` → `_sample.s4p`

    端口数来自产物本身（`.s4p` 的 17），所以 prefix 不带后缀 —— 见 `RunPaths.sparam_prefix`。
    认不出 Touchstone 后缀的文件返回 None（= 不进扁平区，只留在 run 目录里）。
    """
    match = _TOUCHSTONE_SUFFIX.search(name)
    if match is None:
        return None
    suffix = name[match.start() :]
    stem = name[: match.start()]
    if stem.endswith(_SAMPLE_MARK):
        return _SAMPLE_MARK + suffix
    return suffix


def _inside(path: str, base: str) -> bool:
    """`path` 是不是真的在 `base` 底下（解完符号链接之后）。删文件前的最后一道闸。"""
    try:
        real_base = os.path.realpath(base)
        real_path = os.path.realpath(path)
        # ⚠️ commonpath 在 Windows 上跨盘符会抛 ValueError —— 那种情况就是"不在里面"。
        return os.path.commonpath([real_base, real_path]) == real_base and real_path != real_base
    except (OSError, ValueError):  # pragma: no cover - 解析不了就一律当作"不在里面"
        return False


# --------------------------------------------------------------------------
# 布局
# --------------------------------------------------------------------------


def _per_run_name(template: str, fallback: str, ewave_dir_name: str) -> str:
    """每个 run 一份的留档文件名。`<corner>_<temp>` 预测不出来时退回固定名。

    为什么必须每个 run 一份：`<axes-slug>` 按定义**不含** corner/temp，而
    `<corner>_<temp>/` 是 eWave 在 `--workDir` 里自己建的 —— 同一个 axes-slug 下的
    N 个 corner/temp 组合**共用同一个 `run_dir`**。用固定的 `cmd.sh`，这 N 条命令行会
    互相覆盖，只剩最后跑的那条；而它们的 `--corner`/`--temperature`/`--emssTechFile`
    各不相同 ⇒「这个结果是拿什么命令跑出来的」再也答不上来。
    静默覆盖正是本工具要消灭的东西，不能在归档层自己再造一个。

    退路分支（`ewave_dir_name` 为空）出现在 corner/temperature 没都当轴扫的时候。
    此时该 axes-slug 下只会有一个 run，固定名不会撞 —— `test_layout_paths` 里有
    计数断言盯着「N 个 run ⇒ N 个互不相同的 cmd_sh」，撞了会当场红。
    """
    if not ewave_dir_name:
        return fallback
    return template.format(stem=ewave_dir_name)


def compute_run_paths(batch_dir: str, design: Design, run: Run) -> RunPaths:
    """算出 BRIEF §5「归档布局」那棵树上这个 run 相关的全部路径。

    **纯函数，一个目录都不建**（`ensure_run_dirs` 才建）。路径全用 `/` 拼，
    因为最终跑在 Linux 上；Windows 上单测比对字符串也一致。
    """
    root = _posix(batch_dir)
    if not root:
        raise StateError("batch_dir is empty - no target means no layout")
    _assert_outside_spine(root, "batch_dir")

    design_name = _design_dir_name(design, run)
    axes_slug = _validate_component(run.axes_slug, "axes_slug")
    # ★ `ewave_dir` 允许是空的 —— 那是「预测不出来」的诚实表示，不是遗漏。
    # 两种合法情形：①corner/temperature 没都当轴扫；②D12 原生多值把温度折叠成
    # `--temperature=a,b,c`（一条命令跑出好几层目录，名字本来就不止一个）。
    # 硬拒绝会把 D12 直接砍掉，所以这里放行，改由 `verify_run_outputs` 在跑完之后
    # **现场发现**那层目录 —— 那时候它已经存在了，不需要预测。
    ewave_dir_name = (
        _validate_component(run.ewave_dir, "ewave_dir (<corner>_<temp>)") if run.ewave_dir else ""
    )
    # 留档文件名的词根：优先用 eWave 那层目录名；预测不出来时退回 run_id 的最后一段
    # （`expand_runs` 保证它在同一个 run_dir 下唯一）；都没有再退回固定名。
    # 这一步是承重的：`<axes-slug>` 按定义不含 corner/temp ⇒ 同一个 run_dir 下会有 N 个 run，
    # 用固定名它们的命令行会互相覆盖，「这结果是拿什么命令跑的」就永远答不上来了。
    # 见 model.CMD_SH_TEMPLATE 与 tests/test_layout_no_collision.py。
    stem = ewave_dir_name or run.run_id.rsplit("/", 1)[-1].strip()

    gds_dir = _join(root, GDS_DIRNAME)
    gdsout_dir = _join(root, GDSOUT_DIRNAME)
    sparam_dir = _join(root, SPARAM_DIRNAME)
    logs_dir = _join(root, LOGS_DIRNAME)
    run_dir = _join(root, RUNS_DIRNAME, design_name, axes_slug)

    # 扁平区文件名：<design>__<axes-slug>__<corner>_<temp>（BRIEF §5）。
    # 分隔符复用 AXIS_SLUG_SEP（双下划线）—— 单下划线已经被温度占用（`-40.0` → `-40_0`）。
    flat_stem = AXIS_SLUG_SEP.join(
        part for part in (design_name, axes_slug, ewave_dir_name or stem) if part
    )

    return RunPaths(
        batch_dir=root,
        batch_json=_join(root, BATCH_JSON_NAME),
        runs_csv=_join(root, RUNS_CSV_NAME),
        gds_dir=gds_dir,
        gdsout_dir=gdsout_dir,
        sparam_dir=sparam_dir,
        logs_dir=logs_dir,
        design_gds=_join(gds_dir, design_name + GDS_SUFFIX),
        design_gdsout=_join(gdsout_dir, design_name + GDSOUT_SETUP_SUFFIX),
        run_dir=run_dir,
        cmd_sh=_join(run_dir, _per_run_name(CMD_SH_TEMPLATE, CMD_SH_NAME, stem)),
        # 预测不出来时留空串（**不是** run_dir）—— 空串是给 verify_run_outputs 的信号：
        # "去 run_dir 里现场找 eWave 建的那层"。填成 run_dir 会让它误以为产物就在外层。
        ewave_dir=_join(run_dir, ewave_dir_name) if ewave_dir_name else "",
        run_log=_join(run_dir, _per_run_name(RUN_LOG_TEMPLATE, RUN_LOG_NAME, stem)),
        sparam_prefix=_join(sparam_dir, flat_stem),
    )


def ensure_run_dirs(paths: RunPaths, *, dry_run: bool = False) -> None:
    """建好 batch/gds/gdsout/sparam/runs 这些目录。`dry_run=True` 时什么都不做。

    ⚠️ **绝不建到 `<workarea>/ewave_simulation/` 里去**（CLAUDE.md 硬约束 4，那是设计师的 spine）。
    路径落在 spine 内 → `StateError`。
    """
    # 再查一遍 spine —— RunPaths 也可能是别人手工拼的，守卫不能只在 compute_run_paths 里。
    _assert_outside_spine(paths.batch_dir, "batch_dir")
    _assert_outside_spine(paths.run_dir, "run_dir")
    if dry_run:
        return
    for directory in (
        paths.batch_dir,
        paths.gds_dir,
        paths.gdsout_dir,
        paths.sparam_dir,
        paths.logs_dir,
        paths.run_dir,
    ):
        if directory:
            os.makedirs(directory, exist_ok=True)
    # `paths.ewave_dir`（<corner>_<temp>）**故意不建** —— 那层是 eWave 自己建的（BRIEF §5）。
    # 我们提前建出来只会掩盖"eWave 根本没跑起来"这件事。


def write_cmd_sh(paths: RunPaths, plan: CommandPlan, *, dry_run: bool = False) -> str:
    """把这个 run 的完整命令写成 `cmd.sh`（可单独手工重跑），返回写到的路径。

    * 行尾必须是 **LF**（红区 bash 死在 CRLF 上，而那是最没法调试的地方）。
    * 参数逐个 `shlex.quote`，一行一个 flag 加续行 `\\`，人要能读。
    * `dry_run=True` 时只返回路径、不写文件。
    """
    target = _posix(paths.cmd_sh)
    if dry_run:
        return target
    _assert_outside_spine(target, "cmd.sh")
    if not plan.argv:
        raise StateError(f"CommandPlan has no argv - cannot write cmd.sh: {target}")

    lines: list[str] = ["#!/bin/sh"]
    lines.append("# Generated by ewave_batch: the full command for this run, replayable by hand.")
    lines.append("# Editing this file does not change the batch state (batch.json is the authority).")
    if plan.run_id:
        lines.append(f"# run_id : {plan.run_id}")
    if plan.design_key:
        lines.append(f"# design : {plan.design_key}")
    lines.append(f"# stage  : {plan.stage.value}")
    if plan.work_dir:
        lines.append(f"# workDir: {plan.work_dir}")
    lines.append("set -e")
    if plan.cwd:
        lines.append(f"cd {shlex.quote(_posix(plan.cwd))}")
    for key in sorted(plan.env):
        lines.append(f"{key}={shlex.quote(str(plan.env[key]))}; export {key}")
    argv = [str(item) for item in plan.argv]
    lines.append(shlex.quote(argv[0]) + (" \\" if len(argv) > 1 else ""))
    for index, item in enumerate(argv[1:], start=1):
        last = index == len(argv) - 1
        lines.append("    " + shlex.quote(item) + ("" if last else " \\"))

    _atomic_write_text(target, "\n".join(lines) + "\n", newline="\n")
    try:
        os.chmod(target, 0o755)
    except OSError:  # pragma: no cover - Windows / 只读文件系统上无所谓
        pass
    return target


# --------------------------------------------------------------------------
# 产物验收 —— done 的判据
# --------------------------------------------------------------------------


def port_count_from_suffix(path: str) -> int | None:
    """从 `.s4p` / `.y3p` 这种后缀里取端口数；认不出返回 None。不读文件内容。"""
    match = _TOUCHSTONE_SUFFIX.search(_posix(str(path)))
    if match is None:
        return None
    count = int(match.group(2))
    return count if count > 0 else None


def _discover_ewave_dirs(run_dir: str) -> list[str]:
    """跑完之后，在 `run_dir` 里现场找 eWave 自己建的那层 `<corner>_<temp>/`。

    只在规划阶段预测不出目录名时用（`RunPaths.ewave_dir` 为空串）。
    判据故意保守：**必须是直接子目录，且里面至少有一个 Touchstone 产物或 eWave 日志**。
    光看名字形状（`<something>_<something>`）会把我们自己写的东西也算进去。

    返回排序后的绝对路径列表。找不到返回空列表 —— 不抛异常，验收失败是正常结果之一。
    """
    root = _posix(run_dir)
    if not os.path.isdir(root):
        return []
    out: list[str] = []
    for name in sorted(os.listdir(root)):
        candidate = _join(root, name)
        if not os.path.isdir(candidate):
            continue
        try:
            inner = os.listdir(candidate)
        except OSError:
            continue
        looks_like_ewave_output = any(
            _is_sparam_name(n) or n in ("ewave.log", "emsolver.log", "mesh.log") for n in inner
        )
        if looks_like_ewave_output:
            out.append(candidate)
    return out


def verify_run_outputs(
    paths: RunPaths,
    run: Run,
    *,
    expected_port_count: int | None = None,
) -> VerifyReport:
    """产物验收 —— **`done` 的判据**（不是 job 退出码，§12）。

    三条实测出来的失败信号不可靠（BRIEF §10）：`exit=0` 但崩了、0 字节文件也报 "done"、
    写失败被吞。所以验收契约是：**存在 + 非空 + 端口数对**。
    只读，不改文件。找不到产物不抛异常，返回 `ok=False` + 原因。
    """
    reasons: list[str] = []
    ewave_dir = _posix(paths.ewave_dir)
    ports = tuple(run.ports)

    if not ewave_dir:
        # 「预测不出来怎么办」：`ewave_dir` 是空串 = 规划时算不出 `<corner>_<temp>`
        # （corner/temp 没都当轴扫，或 D12 原生多值把温度折叠了）。
        # 但**现在**它已经存在了 —— eWave 跑完就把目录建好了 —— 所以不用预测，直接看。
        # 这正是"规划要预测、验收只需要看"的分工：把不确定性推迟到它自然消失的时刻。
        found = _discover_ewave_dirs(paths.run_dir)
        if not found:
            return VerifyReport(
                ok=False,
                reasons=(
                    f"{_posix(paths.run_dir)} has no <corner>_<temp>/ subdir created by eWave - "
                    "this run probably never started",
                ),
                ports=ports,
            )
        if len(found) > 1:
            # 原生多值（D12）会一次跑出好几层。v1 不猜哪层是"这个 run 的"，
            # 老实报出来让人看见 —— 猜错会把别的组合的产物算到这个 run 头上。
            return VerifyReport(
                ok=False,
                reasons=(
                    f"found {len(found)} <corner>_<temp>/ dirs under {_posix(paths.run_dir)}: "
                    + ", ".join(os.path.basename(d) for d in found)
                    + ". Cannot predict which one belongs to this run, so no guessing.\n"
                    "  Next: make corner and temperature both axes (single-valued is fine) "
                    "so every run maps to exactly one such dir.",
                ),
                ports=ports,
            )
        ewave_dir = found[0]

    if not os.path.isdir(ewave_dir):
        return VerifyReport(
            ok=False,
            reasons=(
                f"output dir does not exist: {ewave_dir} "
                "(eWave never created that <corner>_<temp> level)",
            ),
            ports=ports,
        )

    names = sorted(n for n in os.listdir(ewave_dir) if os.path.isfile(_join(ewave_dir, n)))
    sparam_names = [n for n in names if _is_sparam_name(n)]
    if not sparam_names:
        return VerifyReport(
            ok=False,
            reasons=(f"{ewave_dir} has no .sNp output ({len(names)} other files are there)",),
            ports=ports,
        )

    files: list[str] = []
    total_bytes = 0
    counts: list[int | None] = []
    for name in sparam_names:
        full = _join(ewave_dir, name)
        try:
            size = os.path.getsize(full)
        except OSError as exc:  # pragma: no cover - 竞态：验收途中文件被删
            reasons.append(f"cannot read the size of {name}: {exc}")
            continue
        files.append(full)
        total_bytes += size
        if size == 0:
            # ★ 这不是假想：MVP 实测过 eWave 崩了也留 0 字节文件、还报 "done"（BRIEF §10）。
            reasons.append(f"{name} is 0 bytes - eWave saying done does not count (BRIEF sec. 10, measured)")
        counts.append(port_count_from_suffix(name))

    distinct = sorted({c for c in counts if c is not None})
    if len(distinct) > 1:
        reasons.append(
            f"port counts disagree among the .sNp files of one run: {distinct} - "
            "the outputs are mixed, stop comparing numbers here"
        )

    port_count = distinct[0] if len(distinct) == 1 else None

    want = expected_port_count
    if want is None and ports:
        want = len(ports)
    if want is not None:
        if port_count is None:
            reasons.append(
                f"cannot tell the port count of the output "
                f"(files: {', '.join(sparam_names)}), expected {want}"
            )
        elif port_count != want:
            # `--all` 的代价：pin 集合一变所有端口编号平移，而且**静默**（BRIEF §5）。
            reasons.append(f"port count mismatch: output has {port_count} ports, expected {want}")

    return VerifyReport(
        ok=not reasons,
        reasons=tuple(reasons),
        sparam_files=tuple(files),
        port_count=port_count,
        ports=ports,
        total_bytes=total_bytes,
    )


# --------------------------------------------------------------------------
# 归档（D5）
# --------------------------------------------------------------------------


def archive_run(
    paths: RunPaths,
    run: Run,
    *,
    keep: Sequence[str] = (),
    keep_logs_on_failure: bool = True,
    dry_run: bool = False,
) -> ArchiveReport:
    """按 D5 归档：参数文件收进 `sparam/` 扁平区，mesh/pmrg/pmsh/resist 中间件删掉。

    * `keep` 是 fnmatch 模式（默认用 `BatchOptions.archive_keep`）。
    * run 失败且 `keep_logs_on_failure` → 保留 `ewave.log` / `emsolver.log` 做诊断。
    * 扁平区的文件名 = `RunPaths.sparam_prefix` + 原后缀（`.s4p` 这种，端口数来自产物本身）。
    * **删除只在 `paths.ewave_dir` 里发生**，别的地方一个文件都不许删。
    写盘（`dry_run=True` 时只报告不动手）。

    ⚠️ **先验后删**：删之前先跑一遍 `verify_run_outputs`，没过就一个文件都不删 ——
    mesh/日志正是诊断材料，删完了这个 run 就没法查了。

    `kept` / `removed` 里是**相对 `paths.ewave_dir` 的文件名**（= 这个 run 目录里发生了什么）。
    扁平区那几份副本的路径 = `paths.sparam_prefix` + 后缀，调用方自己拼得出来
    （报告里没有单独的 `copied` 字段，见交接报告的 interface_change_requests）。
    """
    patterns = tuple(keep) or BatchOptions().archive_keep
    ewave_dir = _posix(paths.ewave_dir)
    _assert_outside_spine(ewave_dir, "archive dir")

    if not os.path.isdir(ewave_dir):
        return ArchiveReport(
            missing=patterns,
            errors=(f"output dir does not exist, nothing to archive: {ewave_dir}",),
        )

    entries = sorted(os.listdir(ewave_dir))
    files = [n for n in entries if os.path.isfile(_join(ewave_dir, n))]
    errors: list[str] = [
        f"skipping subdir (archiving only deletes files, it never recurses into dirs): {n}"
        for n in entries
        if os.path.isdir(_join(ewave_dir, n))
    ]

    matched = {n for n in files if any(fnmatch.fnmatch(n, pat) for pat in patterns)}
    missing = tuple(pat for pat in patterns if not any(fnmatch.fnmatch(n, pat) for n in files))

    keep_names = set(matched)
    if keep_logs_on_failure and run.status is RunStatus.FAILED:
        # D5：失败时留日志做诊断。成功时它们和 mesh 一起走。
        keep_names |= {n for n in files if n.endswith(".log")}

    # ---- 先验：产物没验过就一个都不删 -----------------------------------
    verdict = verify_run_outputs(paths, run)
    if not verdict.ok:
        errors.append(
            "output verification failed - deleting nothing (verify first, delete after): "
            + "; ".join(verdict.reasons)
        )
        return ArchiveReport(
            kept=tuple(files),
            removed=(),
            missing=missing,
            errors=tuple(errors),
            bytes_freed=0,
        )
    if not matched:
        # keep 模式一个都没命中 = 要么 spec 里的模式打错了，要么这个 run 根本没出参数文件。
        # 此时"照常归档"意味着**把全部产物删光**，那是不可逆的。宁可留一堆中间件。
        errors.append(
            f"keep patterns {list(patterns)} matched no file at all - deleting nothing "
            "(deleting anyway would wipe every output of this run, and that is irreversible)"
        )
        return ArchiveReport(
            kept=tuple(files),
            removed=(),
            missing=missing,
            errors=tuple(errors),
            bytes_freed=0,
        )

    # ---- 收进扁平区（只收 Touchstone 产物，别把日志倒进 sparam/）--------
    copy_failed = False
    for name in sorted(matched):
        suffix = _flat_suffix(name)
        if suffix is None:
            continue
        src = _join(ewave_dir, name)
        dst = paths.sparam_prefix + suffix
        if dry_run:
            continue
        try:
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            shutil.copy2(src, dst)
            if os.path.getsize(dst) != os.path.getsize(src):
                raise OSError("the copy's size does not match the original")
        except OSError as exc:
            copy_failed = True
            errors.append(f"failed to copy into the flat area {name} -> {dst}: {exc}")

    if copy_failed:
        errors.append("some copies did not land - deleting nothing (verify first, delete after)")
        return ArchiveReport(
            kept=tuple(files),
            removed=(),
            missing=missing,
            errors=tuple(errors),
            bytes_freed=0,
        )

    # ---- 后删：只在 ewave_dir 里，只删文件 --------------------------------
    kept: list[str] = []
    removed: list[str] = []
    bytes_freed = 0
    for name in files:
        if name in keep_names:
            kept.append(name)
            continue
        full = _join(ewave_dir, name)
        if not _inside(full, ewave_dir):  # pragma: no cover - 防御：符号链接指到外面去
            errors.append(f"skipping {name}: it points outside {ewave_dir}, deletion stays inside this run dir")
            kept.append(name)
            continue
        try:
            size = os.path.getsize(full)
        except OSError:  # pragma: no cover
            size = 0
        if dry_run:
            removed.append(name)
            bytes_freed += size
            continue
        try:
            os.remove(full)
        except OSError as exc:
            errors.append(f"cannot delete {name}: {exc}")
            kept.append(name)
            continue
        removed.append(name)
        bytes_freed += size

    return ArchiveReport(
        kept=tuple(kept),
        removed=tuple(removed),
        missing=missing,
        errors=tuple(errors),
        bytes_freed=bytes_freed,
    )


def check_port_consistency(state: BatchState) -> list[str]:
    """批次内互相比对每个 run 的端口列表，返回问题描述（空 list = 一致）。

    ⚠️ 这不是可选的（BRIEF §5「`--all` 的代价」）：设计师加/删/改名一个 pin ⇒ 所有编号平移 ⇒
    之前建的 nport 和归档的 `.sNp` 全部错位，**而且静默**。同一个 design 的多个 run
    端口列表不一致时必须报出来，让调用方停下而不是继续。只读。

    **按 design 分组比**：不同 design 端口数本来就不同（官方样本一个 17 端口一个 16 端口），
    跨 design 比会报出一堆假问题，然后没人再看这份报告。
    """
    problems: list[str] = []
    by_design: dict[str, list[Run]] = {}
    for run in state.runs:
        if not run.ports:
            continue
        by_design.setdefault(run.design_key, []).append(run)

    for design_key in sorted(by_design):
        runs = by_design[design_key]
        reference = runs[0]
        ref_ports = tuple(reference.ports)
        for run in runs[1:]:
            ports = tuple(run.ports)
            if ports == ref_ports:
                continue
            detail = f"{len(ports)} vs {len(ref_ports)} ports"
            for index, (got, want) in enumerate(zip(ports, ref_ports)):
                if got != want:
                    detail = f"position {index}: {got!r} vs {want!r}"
                    break
            problems.append(
                f"design {design_key}: the port list of run {run.run_id} differs from {reference.run_id} "
                f"({detail}) - shifted port numbering silently misaligns .sNp against nport, "
                "so stop comparing numbers"
            )
    return problems


# --------------------------------------------------------------------------
# batch.json：序列化
# --------------------------------------------------------------------------


def _enum_from(cls, value: object, what: str):
    try:
        return cls(value)
    except ValueError as exc:
        raise StateError(f"{what}: unknown value {value!r} (was batch.json written by another tool?)") from exc


def _req(data: Mapping[str, object], key: str, what: str) -> object:
    if key not in data:
        raise StateError(f"{what} is missing field {key!r}")
    return data[key]


def _as_str_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(str(item) for item in value)


def _as_flags(value: object) -> dict:
    if not value:
        return {}
    return {str(k): (v if isinstance(v, bool) else str(v)) for k, v in dict(value).items()}


def _portspec_to_dict(port_spec: PortSpec) -> dict[str, object]:
    return {
        "mode": port_spec.mode.value,
        "mapping": [[pid, pin] for pid, pin in port_spec.mapping],
        "signal_ports": list(port_spec.signal_ports),
    }


def _portspec_from_dict(data: object) -> PortSpec:
    if not isinstance(data, Mapping):
        return PortSpec()
    return PortSpec(
        mode=_enum_from(PortMode, data.get("mode", PortMode.ALL.value), "PortSpec.mode"),
        mapping=tuple((str(pair[0]), str(pair[1])) for pair in data.get("mapping", ()) or ()),
        signal_ports=_as_str_tuple(data.get("signal_ports")),
    )


def _axis_value_to_dict(value: AxisValue) -> dict[str, object]:
    return {"value": value.value, "flags": dict(value.flags), "slug": value.slug, "label": value.label}


def _axis_value_from_dict(data: Mapping[str, object]) -> AxisValue:
    return AxisValue(
        value=str(_req(data, "value", "AxisValue")),
        flags=_as_flags(data.get("flags")),
        slug=str(data.get("slug", "")),
        label=str(data.get("label", "")),
    )


def _axis_to_dict(axis: Axis) -> dict[str, object]:
    return {
        "name": axis.name,
        "values": [_axis_value_to_dict(v) for v in axis.values],
        "kind": axis.kind.value,
        "flags": list(axis.flags),
        "short": axis.short,
        "slug_template": axis.slug_template,
        "encoded_in_ewave_dir": axis.encoded_in_ewave_dir,
        "description": axis.description,
    }


def _axis_from_dict(data: Mapping[str, object]) -> Axis:
    return Axis(
        name=str(_req(data, "name", "Axis")),
        values=tuple(_axis_value_from_dict(v) for v in _req(data, "values", "Axis")),
        kind=_enum_from(AxisKind, data.get("kind", AxisKind.VALUE.value), "Axis.kind"),
        flags=_as_str_tuple(data.get("flags")),
        short=str(data.get("short", "")),
        slug_template=str(data.get("slug_template", "{short}-{slug}")),
        encoded_in_ewave_dir=bool(data.get("encoded_in_ewave_dir", False)),
        description=str(data.get("description", "")),
    )


def _design_to_dict(design: Design) -> dict[str, object]:
    return {
        "library": design.library,
        "cell": design.cell,
        "view": design.view,
        "official_run_dir": design.official_run_dir,
        "key": design.key,
        "resources": design.resources,
        "axis_overrides": {k: list(v) for k, v in design.axis_overrides.items()},
        "extra_flags": dict(design.extra_flags),
        "port_spec": None if design.port_spec is None else _portspec_to_dict(design.port_spec),
        "gds_path": design.gds_path,
        "label": design.label,
    }


def _design_from_dict(data: Mapping[str, object]) -> Design:
    port_spec = data.get("port_spec")
    return Design(
        library=str(_req(data, "library", "Design")),
        cell=str(_req(data, "cell", "Design")),
        view=str(_req(data, "view", "Design")),
        official_run_dir=str(data.get("official_run_dir", "")),
        key=str(data.get("key", "")),
        resources=str(data.get("resources", "")),
        axis_overrides={str(k): _as_str_tuple(v) for k, v in dict(data.get("axis_overrides") or {}).items()},
        extra_flags=_as_flags(data.get("extra_flags")),
        port_spec=None if port_spec is None else _portspec_from_dict(port_spec),
        gds_path=str(data.get("gds_path", "")),
        label=str(data.get("label", "")),
    )


def _options_to_dict(options: BatchOptions) -> dict[str, object]:
    return {
        "dry_run": options.dry_run,
        "max_parallel": options.max_parallel,
        "poll_interval": options.poll_interval,
        "scheduler": options.scheduler,
        "native_multi_value": options.native_multi_value,
        "stop_design_on_streamout_failure": options.stop_design_on_streamout_failure,
        "keep_logs_on_failure": options.keep_logs_on_failure,
        "archive_keep": list(options.archive_keep),
        "parallel_multiplier": options.parallel_multiplier,
        "include_port_order": options.include_port_order,
        "verify_port_count": options.verify_port_count,
        "timeout_seconds": options.timeout_seconds,
    }


def _options_from_dict(data: object) -> BatchOptions:
    if not isinstance(data, Mapping):
        return BatchOptions()
    fallback = BatchOptions()
    timeout = data.get("timeout_seconds", fallback.timeout_seconds)
    return BatchOptions(
        dry_run=bool(data.get("dry_run", fallback.dry_run)),
        max_parallel=int(data.get("max_parallel", fallback.max_parallel)),
        poll_interval=float(data.get("poll_interval", fallback.poll_interval)),
        scheduler=str(data.get("scheduler", fallback.scheduler)),
        native_multi_value=bool(data.get("native_multi_value", fallback.native_multi_value)),
        stop_design_on_streamout_failure=bool(
            data.get("stop_design_on_streamout_failure", fallback.stop_design_on_streamout_failure)
        ),
        keep_logs_on_failure=bool(data.get("keep_logs_on_failure", fallback.keep_logs_on_failure)),
        archive_keep=_as_str_tuple(data.get("archive_keep", fallback.archive_keep)),
        parallel_multiplier=float(data.get("parallel_multiplier", fallback.parallel_multiplier)),
        include_port_order=bool(data.get("include_port_order", fallback.include_port_order)),
        verify_port_count=bool(data.get("verify_port_count", fallback.verify_port_count)),
        timeout_seconds=None if timeout is None else float(timeout),
    )


def _provenance_to_dict(prov: Provenance) -> dict[str, object]:
    return {
        "tool_version": prov.tool_version,
        "interface_version": prov.interface_version,
        "schema_version": prov.schema_version,
        "python_version": prov.python_version,
        "created_at": prov.created_at,
        "updated_at": prov.updated_at,
        "spec_path": prov.spec_path,
        "spec_sha256": prov.spec_sha256,
        "official_run_dirs": list(prov.official_run_dirs),
        "notes": list(prov.notes),
    }


def _provenance_from_dict(data: object) -> Provenance:
    if not isinstance(data, Mapping):
        return Provenance()
    fallback = Provenance()
    return Provenance(
        tool_version=str(data.get("tool_version", "")),
        interface_version=int(data.get("interface_version", fallback.interface_version)),
        schema_version=int(data.get("schema_version", fallback.schema_version)),
        python_version=str(data.get("python_version", "")),
        created_at=str(data.get("created_at", "")),
        updated_at=str(data.get("updated_at", "")),
        spec_path=str(data.get("spec_path", "")),
        spec_sha256=str(data.get("spec_sha256", "")),
        official_run_dirs=_as_str_tuple(data.get("official_run_dirs")),
        notes=_as_str_tuple(data.get("notes")),
    )


def _job_to_dict(job: Job | None) -> dict[str, object] | None:
    if job is None:
        return None
    return {
        "job_id": job.job_id,
        "scheduler": job.scheduler,
        "state": job.state.value,
        "name": job.name,
        "submitted_at": job.submitted_at,
        "started_at": job.started_at,
        "ended_at": job.ended_at,
        "exit_code": job.exit_code,
        "account": job.account,
        "queue": job.queue,
        "resources": job.resources,
        "stdout_path": job.stdout_path,
        "stderr_path": job.stderr_path,
        "raw": job.raw,
    }


def _job_from_dict(data: object) -> Job | None:
    if data is None:
        return None
    if not isinstance(data, Mapping):
        raise StateError(f"Job is not an object: {data!r}")
    exit_code = data.get("exit_code")
    return Job(
        job_id=str(data.get("job_id", "")),
        scheduler=str(data.get("scheduler", "")),
        state=_enum_from(JobState, data.get("state", JobState.UNKNOWN.value), "Job.state"),
        name=str(data.get("name", "")),
        submitted_at=str(data.get("submitted_at", "")),
        started_at=str(data.get("started_at", "")),
        ended_at=str(data.get("ended_at", "")),
        exit_code=None if exit_code is None else int(exit_code),
        account=str(data.get("account", "")),
        queue=str(data.get("queue", "")),
        resources=str(data.get("resources", "")),
        stdout_path=str(data.get("stdout_path", "")),
        stderr_path=str(data.get("stderr_path", "")),
        raw=str(data.get("raw", "")),
    )


def _log_facts_to_dict(facts: LogFacts | None) -> dict[str, object] | None:
    if facts is None:
        return None
    return {
        "ok": facts.ok,
        "converged": facts.converged,
        "wall_seconds": facts.wall_seconds,
        "peak_memory_mb": facts.peak_memory_mb,
        "port_count": facts.port_count,
        "freq_points_calculated": facts.freq_points_calculated,
        "freq_points_requested": facts.freq_points_requested,
        "cpu_percent_avg": facts.cpu_percent_avg,
        "ewave_version": facts.ewave_version,
        "errors": list(facts.errors),
        "warnings": list(facts.warnings),
        "source_files": list(facts.source_files),
    }


def _opt(value: object, cast):
    """`None` 是「没测到」，别用 0 冒充（`LogFacts` 的 docstring）。"""
    return None if value is None else cast(value)


def _log_facts_from_dict(data: object) -> LogFacts | None:
    if data is None:
        return None
    if not isinstance(data, Mapping):
        raise StateError(f"LogFacts is not an object: {data!r}")
    return LogFacts(
        ok=_opt(data.get("ok"), bool),
        converged=_opt(data.get("converged"), bool),
        wall_seconds=_opt(data.get("wall_seconds"), float),
        peak_memory_mb=_opt(data.get("peak_memory_mb"), float),
        port_count=_opt(data.get("port_count"), int),
        freq_points_calculated=_opt(data.get("freq_points_calculated"), int),
        freq_points_requested=_opt(data.get("freq_points_requested"), int),
        cpu_percent_avg=_opt(data.get("cpu_percent_avg"), float),
        ewave_version=str(data.get("ewave_version", "")),
        errors=_as_str_tuple(data.get("errors")),
        warnings=_as_str_tuple(data.get("warnings")),
        source_files=_as_str_tuple(data.get("source_files")),
    )


def _group_to_dict(group: RunGroup) -> dict[str, object]:
    """`RunGroup` → dict。存的是 **base 之外**的那些组（base 的轴就是顶层 `axes:`）。

    不存的后果不是"少一行信息"：`gui/_ui.py` 的 Runs 表有一列 Group，resume 之后它
    读的是 `Run.group`；组归属丢了，那一列会对**每一条** run 都显示 `base`，
    而界面上没有任何提示说这列已经不可信（2026-08-19 复核实测）。
    """
    return {
        "name": group.name,
        "axes": {name: list(values) for name, values in group.axis_overrides.items()},
        "label": group.label,
    }


def _group_from_dict(data: Mapping[str, object]) -> RunGroup:
    return RunGroup(
        name=str(_req(data, "name", "RunGroup")),
        axis_overrides={
            str(k): tuple(str(v) for v in _as_list_of(value))
            for k, value in dict(data.get("axes") or {}).items()
        },
        label=str(data.get("label", "")),
    )


def _as_list_of(value: object) -> list:
    """一个值 → list（单个标量当成一元素列表）。轴取值在 JSON 里应当已经是 list。"""
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _run_to_dict(run: Run) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "design_key": run.design_key,
        "axis_values": dict(run.axis_values),
        "axes_slug": run.axes_slug,
        "ewave_dir": run.ewave_dir,
        "group": run.group,
        "work_dir": run.work_dir,
        "status": run.status.value,
        "job": _job_to_dict(run.job),
        "attempts": run.attempts,
        "submitted_at": run.submitted_at,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "wall_seconds": run.wall_seconds,
        "argv": list(run.argv),
        "artifacts": list(run.artifacts),
        "ports": list(run.ports),
        "log_facts": _log_facts_to_dict(run.log_facts),
        "message": run.message,
    }


def _run_from_dict(data: Mapping[str, object]) -> Run:
    return Run(
        run_id=str(_req(data, "run_id", "Run")),
        design_key=str(_req(data, "design_key", "Run")),
        axis_values={str(k): str(v) for k, v in dict(data.get("axis_values") or {}).items()},
        axes_slug=str(data.get("axes_slug", "")),
        ewave_dir=str(data.get("ewave_dir", "")),
        # 可选字段：老的 batch.json 里没有它 => 落回 `BASE_GROUP`，也就是"整批只有 base"，
        # 与加组之前逐字相同。所以 `SCHEMA_VERSION` 不用 +1（双向兼容）。
        group=str(data.get("group", "") or BASE_GROUP),
        work_dir=str(data.get("work_dir", "")),
        status=_enum_from(RunStatus, data.get("status", RunStatus.READY.value), "Run.status"),
        job=_job_from_dict(data.get("job")),
        attempts=int(data.get("attempts", 0)),
        submitted_at=str(data.get("submitted_at", "")),
        started_at=str(data.get("started_at", "")),
        ended_at=str(data.get("ended_at", "")),
        wall_seconds=_opt(data.get("wall_seconds"), float),
        argv=_as_str_tuple(data.get("argv")),
        artifacts=_as_str_tuple(data.get("artifacts")),
        ports=_as_str_tuple(data.get("ports")),
        log_facts=_log_facts_from_dict(data.get("log_facts")),
        message=str(data.get("message", "")),
    )


def _streamout_to_dict(task: StreamoutTask) -> dict[str, object]:
    return {
        "design_key": task.design_key,
        "status": task.status.value,
        "job": _job_to_dict(task.job),
        "gds_path": task.gds_path,
        "gdsout_setup_path": task.gdsout_setup_path,
        "log_path": task.log_path,
        "started_at": task.started_at,
        "ended_at": task.ended_at,
        "argv": list(task.argv),
        "message": task.message,
    }


def _streamout_from_dict(data: Mapping[str, object]) -> StreamoutTask:
    return StreamoutTask(
        design_key=str(_req(data, "design_key", "StreamoutTask")),
        status=_enum_from(RunStatus, data.get("status", RunStatus.READY.value), "StreamoutTask.status"),
        job=_job_from_dict(data.get("job")),
        gds_path=str(data.get("gds_path", "")),
        gdsout_setup_path=str(data.get("gdsout_setup_path", "")),
        log_path=str(data.get("log_path", "")),
        started_at=str(data.get("started_at", "")),
        ended_at=str(data.get("ended_at", "")),
        argv=_as_str_tuple(data.get("argv")),
        message=str(data.get("message", "")),
    )


def state_to_dict(state: BatchState) -> dict[str, object]:
    """`BatchState` → 可 JSON 序列化的 dict（枚举落 `.value`，元组落 list）。纯函数。"""
    return {
        "schema_version": state.schema_version,
        "batch_name": state.batch_name,
        "batch_dir": state.batch_dir,
        "designs": [_design_to_dict(d) for d in state.designs],
        "axes": [_axis_to_dict(a) for a in state.axes],
        "groups": [_group_to_dict(g) for g in state.groups],
        "runs": [_run_to_dict(r) for r in state.runs],
        "streamout": [_streamout_to_dict(t) for t in state.streamout],
        "options": _options_to_dict(state.options),
        "defaults": dict(state.defaults),
        "extra_flags": dict(state.extra_flags),
        "provenance": _provenance_to_dict(state.provenance),
    }


def state_from_dict(data: Mapping[str, object]) -> BatchState:
    """反过来。`schema_version` 比 `SCHEMA_VERSION` 大 → `StateError`（**拒绝而不是猜**）。"""
    if not isinstance(data, Mapping):
        raise StateError(f"the top level of batch.json is not an object: {type(data).__name__}")
    raw_version = data.get("schema_version", SCHEMA_VERSION)
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise StateError(f"schema_version is not an integer: {raw_version!r}") from exc
    if version > SCHEMA_VERSION:
        raise StateError(
            f"batch.json has schema_version={version}, this tool only knows up to {SCHEMA_VERSION} - "
            "refusing instead of guessing (a wrong guess would corrupt someone else's batch).\n"
            "  Next: upgrade the tool."
        )

    try:
        return BatchState(
            batch_name=str(data.get("batch_name", "")),
            batch_dir=str(data.get("batch_dir", "")),
            designs=[_design_from_dict(d) for d in data.get("designs", ()) or ()],
            axes=[_axis_from_dict(a) for a in data.get("axes", ()) or ()],
            groups=[_group_from_dict(g) for g in data.get("groups", ()) or ()],
            runs=[_run_from_dict(r) for r in data.get("runs", ()) or ()],
            streamout=[_streamout_from_dict(t) for t in data.get("streamout", ()) or ()],
            options=_options_from_dict(data.get("options")),
            defaults=_as_flags(data.get("defaults")),
            extra_flags=_as_flags(data.get("extra_flags")),
            provenance=_provenance_from_dict(data.get("provenance")),
            schema_version=version,
        )
    except StateError:
        raise
    except Exception as exc:  # 结构坏了就是坏了 —— 报成 StateError，别让 resume 拿半份状态往下跑
        raise StateError(f"batch.json has the wrong structure: {exc}") from exc


# --------------------------------------------------------------------------
# batch.json / runs.csv：落盘
# --------------------------------------------------------------------------


def read_batch_state(path: str) -> BatchState:
    """读 `batch.json`。文件不存在 / 解析失败 / 版本不认识 → `StateError`。"""
    target = _posix(path)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise StateError(
            f"no {target} - this directory is not a batch (resume only knows batch.json)"
        ) from exc
    except OSError as exc:
        raise StateError(f"cannot read {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise StateError(f"{target} is not valid JSON (power lost mid-write?): {exc}") from exc
    return state_from_dict(data)


def write_batch_state(path: str, state: BatchState) -> None:
    """**原子**写 `batch.json`：同目录临时文件 + `os.replace`。

    每拍都要写，跑到一半断电也不能留半份 JSON —— resume 只认这一个文件。
    顺手刷新 `provenance.updated_at`。
    """
    target = _posix(path)
    _assert_outside_spine(target, "batch.json")
    now = _utcnow()
    if not state.provenance.created_at:
        state.provenance.created_at = now
    state.provenance.updated_at = now
    payload = json.dumps(state_to_dict(state), ensure_ascii=False, indent=2) + "\n"
    try:
        _atomic_write_text(target, payload, newline="\n")
    except OSError as exc:
        raise StateError(f"cannot write {target}: {exc}") from exc


def _csv_cell(value: object) -> str:
    """CSV 单元格：`None` → 空串（"没测到"不是 0），bool → true/false，其余 str()。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def write_runs_csv(path: str, state: BatchState) -> None:
    """写汇总表。表头 = `RUNS_CSV_COLUMNS`（冻结），`newline=""` + LF，UTF-8。"""
    target = _posix(path)
    _assert_outside_spine(target, "runs.csv")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(RUNS_CSV_COLUMNS))
    for run in state.runs:
        facts = run.log_facts
        port_count: object = None
        if facts is not None and facts.port_count is not None:
            port_count = facts.port_count
        elif run.ports:
            port_count = len(run.ports)
        row = {
            "design": run.design_key,
            "run_id": run.run_id,
            "axes_slug": run.axes_slug,
            "ewave_dir": run.ewave_dir,
            "status": run.status.value,
            "job_id": run.job.job_id if run.job is not None else "",
            "submitted_at": run.submitted_at,
            "ended_at": run.ended_at,
            "wall_seconds": run.wall_seconds,
            "peak_memory_mb": None if facts is None else facts.peak_memory_mb,
            "port_count": port_count,
            "converged": None if facts is None else facts.converged,
            "sparam": ";".join(run.artifacts),
            "message": run.message,
            # 事后追溯「这条结果是哪个变体」的唯一机器可读来源。没有它只能靠人肉
            # 反推目录名，而目录名只编码"在变的轴"、编码不出组的身份。
            "group": run.group or "",
        }
        writer.writerow([_csv_cell(row[column]) for column in RUNS_CSV_COLUMNS])
    # newline="" 交给底层：文本里只有 \n，落盘就是 LF（红区 bash/awk 吃不了 CRLF）。
    _atomic_write_text(target, buffer.getvalue(), newline="")


# --------------------------------------------------------------------------
# 「把这个 run 设为当前」—— 唯一一处会写进 spine 的操作
# --------------------------------------------------------------------------


def _primary_sparam(ewave_dir: str, design: Design) -> str:
    """挑出这个 run 的主 `.sNp`（不是 `_sample` 那份）。挑不出来 → `StateError`。"""
    if not os.path.isdir(ewave_dir):
        raise StateError(f"output dir does not exist: {ewave_dir}")
    candidates = [
        n
        for n in sorted(os.listdir(ewave_dir))
        if _is_sparam_name(n) and os.path.isfile(_join(ewave_dir, n))
    ]
    if not candidates:
        raise StateError(f"{ewave_dir} has no .sNp - this run has nothing that could be set as current")
    non_empty = [n for n in candidates if os.path.getsize(_join(ewave_dir, n)) > 0]
    if not non_empty:
        raise StateError(
            f"every .sNp in {ewave_dir} is 0 bytes - refusing to copy empty files onto the "
            "designer's spine (BRIEF sec. 10, measured)"
        )
    primary = [n for n in non_empty if _SAMPLE_MARK not in n] or non_empty
    preferred = [n for n in primary if design.cell and n.startswith(design.cell)]
    return _join(ewave_dir, (preferred or primary)[0])


def set_run_as_current(
    paths: RunPaths,
    run: Run,
    design: Design,
    *,
    target_dir: str,
    dry_run: bool = False,
) -> list[str]:
    """把某个 run 的 `.sNp` 落到官方那个路径上，让现成的 nport 零编辑生效（BRIEF §5）。

    🚨 **这是唯一一处会写进设计师 spine 的操作**，三道约束一条都不许省：
    ① 必须显式触发（**绝不**在批量跑完时自动执行）；② 覆盖前备份原文件；③ 记进日志。
    返回做过的动作列表（给日志和界面看）。`target_dir` 不存在 → `StateError`。

    `target_dir` 给官方的 **design 目录**（`<...>/<library>_<topCell>_<view>/`），
    我们自己往下接 `<corner>_<temp>` 那层；已经指到 `<corner>_<temp>` 时原样用。
    """
    target = _posix(target_dir)
    if not target or not os.path.isdir(target):
        raise StateError(
            f"target dir does not exist: {target_dir}\n"
            "  This step writes into the designer's spine - if the dir is not even there, "
            "we certainly must not create it for him."
        )
    ewave_dir_name = _validate_component(run.ewave_dir, "ewave_dir (<corner>_<temp>)")
    source = _primary_sparam(_posix(paths.ewave_dir), design)

    dest_dir = target if os.path.basename(target) == ewave_dir_name else _join(target, ewave_dir_name)
    dest = _join(dest_dir, os.path.basename(source))
    stamp = _utcnow()
    actions: list[str] = []

    if not os.path.isdir(dest_dir):
        actions.append(f"mkdir {dest_dir}")
        if not dry_run:
            os.makedirs(dest_dir, exist_ok=True)

    if os.path.exists(dest):
        # 覆盖前备份 —— 冒号在文件名里到处惹麻烦，时间戳去掉它。
        backup = f"{dest}.bak.{stamp.replace(':', '').replace('-', '')}"
        actions.append(f"backup {dest} -> {backup}")
        if not dry_run:
            shutil.copy2(dest, backup)

    actions.append(f"copy {source} -> {dest}")
    if not dry_run:
        shutil.copy2(source, dest)

    log_path = _join(paths.logs_dir, SET_CURRENT_LOG_NAME)
    actions.append(f"log {log_path}")
    if not dry_run:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(log_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{stamp}\trun={run.run_id}\tsrc={source}\tdst={dest}\n")
            for line in actions:
                handle.write(f"{stamp}\t  {line}\n")

    if dry_run:
        return [f"[dry-run] {line}" for line in actions]
    return actions
