"""`python -m ewave_batch` —— 目前只做一件事：**接口漂移检测**。

```
python -m ewave_batch dry-run --self-test
```

`scripts/check.sh` 第 4 步跑的就是这条，**从 Phase 0 起它必须退 0**。

它检查什么：把 `ewave_batch.model.FROZEN` 里的模块逐个 import，
* 模块还不存在 → `pending: P<n>`（P1–P5 还没写，**这不是错**）；
* 模块存在 → 逐个符号核对存在性、定义位置（`__module__`）、以及签名是否与 model 里的桩子一致；
* `FROZEN_PROTOCOL_IMPLS` 里的类还要逐方法比签名（`@runtime_checkable` 的 isinstance
  只看方法名在不在，挡不住参数漂移）。

退出码：**有漂移 → 1，否则 0**。于是后面每个阶段的 `check.sh` 都会自动检查
"有没有人偷偷改了冻结接口"。改冻结接口的正当流程见 `docs/INTERFACES.md`。

⚠️ 本文件里**没有业务实现**，只有这台检测器。真正的 CLI 在 `ewave_batch.cli`（P5），
这里在它出现之前会礼貌地报 pending。
"""

from __future__ import annotations

import importlib
import inspect
import sys
from dataclasses import dataclass, field

from ewave_batch import model

# 归一化时要抹掉的模块前缀（长的排前面）。`tkinter.` 之类**不**在列 —— 那是真实信息。
_STRIP_PREFIXES = (
    "ewave_batch.model.",
    "ewave_batch.core.",
    "ewave_batch.tools.",
    "ewave_batch.sched.",
    "ewave_batch.",
    "collections.abc.",
    "builtins.",
    "typing.",
    "model.",
)

_REWRITES = (
    ("List[", "list["),
    ("Dict[", "dict["),
    ("Tuple[", "tuple["),
    ("Set[", "set["),
    ("FrozenSet[", "frozenset["),
    ("Type[", "type["),
    ("NoneType", "None"),
)


def annotation_text(ann: object) -> str:
    """把一个注解对象变成字符串（`from __future__ import annotations` 之下它本来就是字符串）。"""
    if ann is inspect.Signature.empty:
        return ""
    if isinstance(ann, str):
        return ann
    if isinstance(ann, type):
        return f"{ann.__module__}.{ann.__qualname__}"
    return str(ann)


def normalize_annotation(ann: object) -> str:
    """归一化返回注解，好让"写法不同但意思相同"不产生假阳性。

    做四件事：去空白与引号 → 抹掉自家/typing/builtins 的模块前缀 →
    `Optional[X]` 换成 `X|None` → `List[`/`Dict[` 之类换成小写内建写法。
    **不做**语义等价推断（`Sequence` 和 `list` 仍算不同）—— 实现方照抄冻结签名即可。
    """
    text = annotation_text(ann)
    if not text:
        return ""
    text = "".join(text.split()).replace("'", "").replace('"', "")
    while True:
        head = text.find("Optional[")
        if head < 0:
            break
        depth = 0
        for i in range(head + len("Optional[") - 1, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    inner = text[head + len("Optional[") : i]
                    text = text[:head] + inner + "|None" + text[i + 1 :]
                    break
        else:  # pragma: no cover - 括号不配对的注解，原样留着让人看见
            break
    for old, new in _REWRITES:
        text = text.replace(old, new)
    for prefix in _STRIP_PREFIXES:
        text = text.replace(prefix, "")
    return text


def normalize_signature(obj: object) -> str:
    """把一个可调用对象归一成签名字符串：`(a, b=, *args, *, kw=, **kw2) -> ret`。

    比对**参数名 + 顺序 + 参数种类 + 有没有默认值 + 返回注解**。
    故意**不**比参数注解、也**不**比默认值的具体对象 —— 那两样假阳性太多
    （`Sequence[str]` vs `list[str]`、`field(default_factory=...)`），而它们不改调用方式。
    """
    sig = inspect.signature(obj)  # type: ignore[arg-type]
    parts: list[str] = []
    seen_kw_only = False
    seen_var_positional = False
    positional_only: list[str] = []
    for param in sig.parameters.values():
        if param.name == "self":
            continue
        suffix = "=" if param.default is not inspect.Parameter.empty else ""
        if param.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional_only.append(param.name + suffix)
            continue
        if positional_only:
            parts.extend(positional_only)
            parts.append("/")
            positional_only = []
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            seen_var_positional = True
            parts.append("*" + param.name)
        elif param.kind is inspect.Parameter.KEYWORD_ONLY:
            if not seen_var_positional and not seen_kw_only:
                parts.append("*")
            seen_kw_only = True
            parts.append(param.name + suffix)
        elif param.kind is inspect.Parameter.VAR_KEYWORD:
            parts.append("**" + param.name)
        else:
            parts.append(param.name + suffix)
    if positional_only:
        parts.extend(positional_only)
        parts.append("/")
    rendered = "(" + ", ".join(parts) + ")"
    ret = normalize_annotation(sig.return_annotation)
    return f"{rendered} -> {ret}" if ret else rendered


def compare_signatures(expected: str, actual: str) -> str | None:
    """一致返回 None，不一致返回一句人能看懂的原因。"""
    if expected == actual:
        return None
    return f"签名漂移: 冻结的是 {expected}，实际是 {actual}"


@dataclass
class ModuleReport:
    """一个模块的检查结果。"""

    name: str
    phase: str
    status: str
    symbols_ok: int = 0
    symbols_total: int = 0
    drifts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _import_status(name: str, exc: ImportError) -> tuple[str, str]:
    """把 import 失败分成两类：模块自己还没写（pending） vs 依赖缺失（blocked）。

    区分的意义：`gui.frames.split` 在没装 tkinter 的机器上 import 不了，
    那是**平台降级**（tier 3 缺失是降级不是失败），不该报成"P5 还没写"，更不该算漂移。
    """
    missing = getattr(exc, "name", "") or ""
    if missing and (missing == name or name.startswith(missing + ".")):
        return "pending", ""
    if missing:
        return "blocked", missing
    return "pending", ""


def check_protocol(module_name: str, cls: object, protocol_name: str) -> list[str]:
    """逐方法比一个类和它该满足的 Protocol。返回漂移描述列表。"""
    drifts: list[str] = []
    proto = getattr(model, protocol_name, None)
    if proto is None:  # pragma: no cover - FROZEN_PROTOCOL_IMPLS 写错了才会到这
        return [f"{module_name}: model 里没有 Protocol {protocol_name}"]
    for attr in sorted(getattr(proto, "__protocol_attrs__", None) or _protocol_members(proto)):
        want = getattr(proto, attr, None)
        got = getattr(cls, attr, None)
        if got is None:
            drifts.append(f"{cls.__name__} 缺 {protocol_name}.{attr}")
            continue
        if isinstance(want, property):
            if not isinstance(got, property):
                drifts.append(f"{cls.__name__}.{attr} 该是 property（{protocol_name} 里是）")
            continue
        if not callable(want) or not callable(got):
            continue
        reason = compare_signatures(normalize_signature(want), normalize_signature(got))
        if reason:
            drifts.append(f"{cls.__name__}.{attr}: {reason}")
    return drifts


def _protocol_members(proto: object) -> list[str]:
    """Protocol 声明的成员名（不含 dunder 和 typing 自己塞进去的东西）。"""
    skip = {"__init__", "__subclasshook__", "__class_getitem__", "__init_subclass__"}
    return [
        name
        for name in vars(proto)
        if not (name.startswith("_") and name not in skip) and name not in skip
    ]


def check_module(module_name: str, symbols: tuple[str, ...]) -> ModuleReport:
    """检查一个冻结模块。"""
    phase = model.FROZEN_PHASE.get(module_name, "?")
    report = ModuleReport(name=module_name, phase=phase, status="?", symbols_total=len(symbols))
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        kind, missing = _import_status(module_name, exc)
        if kind == "blocked":
            report.status = f"blocked: {missing}"
            report.notes.append(f"依赖 {missing} 缺失 —— 平台降级，不算漂移（见 doctor 的 tier 划分）")
        else:
            report.status = f"pending: {phase}"
        return report

    for symbol in symbols:
        obj = getattr(module, symbol, None)
        if obj is None and not hasattr(module, symbol):
            report.drifts.append(f"缺符号 {symbol}")
            continue
        if module_name != "ewave_batch.model" and callable(obj) and not isinstance(obj, type(sys)):
            defined_in = getattr(obj, "__module__", module_name)
            if defined_in != module_name:
                report.drifts.append(
                    f"{symbol} 定义在 {defined_in}，不在 {module_name} —— "
                    "从 model re-export 一个桩子不算实现"
                )
                continue
        frozen_ref = getattr(model, symbol, None) if module_name != "ewave_batch.model" else None
        expected: str | None = None
        if callable(frozen_ref) and not isinstance(frozen_ref, type):
            expected = normalize_signature(frozen_ref)
        else:
            expected = model.FROZEN_SIGNATURES.get(f"{module_name}:{symbol}")
        if expected is not None and callable(obj):
            reason = compare_signatures(expected, normalize_signature(obj))
            if reason:
                report.drifts.append(f"{symbol}: {reason}")
                continue
        report.symbols_ok += 1

    for key, protocol_name in model.FROZEN_PROTOCOL_IMPLS.items():
        owner, _, cls_name = key.partition(":")
        if owner != module_name:
            continue
        cls = getattr(module, cls_name, None)
        if cls is None:
            continue
        report.drifts.extend(check_protocol(module_name, cls, protocol_name))

    if report.drifts:
        report.status = "DRIFT"
    elif phase == "P0":
        report.status = "frozen-only"
    else:
        report.status = "implemented"
    return report


def selftest(stream: object = None) -> int:
    """跑一遍漂移检测，打印表格，返回退出码（有漂移 → 1）。"""
    out = stream if stream is not None else sys.stdout
    reports = [check_module(name, syms) for name, syms in sorted(model.FROZEN.items())]
    width = max(len(r.name) for r in reports)

    print(f"ewave_batch 接口自检 —— INTERFACE_VERSION={model.INTERFACE_VERSION}", file=out)
    print(f"{'模块'.ljust(width)}  {'状态':<14} 符号", file=out)
    print("-" * (width + 26), file=out)
    for r in reports:
        print(f"{r.name.ljust(width)}  {r.status:<14} {r.symbols_ok}/{r.symbols_total}", file=out)
        for note in r.notes:
            print(f"{' ' * width}    note: {note}", file=out)
        for drift in r.drifts:
            print(f"{' ' * width}    DRIFT: {drift}", file=out)

    drifted = [r for r in reports if r.drifts]
    pending = [r for r in reports if r.status.startswith("pending")]
    blocked = [r for r in reports if r.status.startswith("blocked")]
    print("-" * (width + 26), file=out)
    print(
        f"模块 {len(reports)} 个：漂移 {len(drifted)} · 待实现 {len(pending)} · "
        f"平台跳过 {len(blocked)} · 就绪 {len(reports) - len(drifted) - len(pending) - len(blocked)}",
        file=out,
    )
    if drifted:
        print("", file=out)
        print("接口漂移 —— 冻结的签名和实际代码对不上。", file=out)
        print("要改冻结接口就走流程：同一个 commit 改 model.py + docs/INTERFACES.md + 全部调用方，", file=out)
        print("commit message 标 [interface-change]。静默漂移是禁止的，改一个错的冻结不是。", file=out)
        return 1
    print("self-test: OK（无漂移）", file=out)
    return 0


def main(argv: object = None) -> int:
    """入口。目前只认 `dry-run --self-test`，其余转给 `ewave_batch.cli`（P5 才有）。"""
    # 第一件事：把 stdout/stderr 变成 ASCII-locale 下不会崩的。红区 LANG 常是 C，
    # 而本文件的输出带中文 —— 不做这一步，闸门会在红区因为 locale 而红。见 ewave_batch.ascii_safe_stdio。
    from ewave_batch._stdio import ascii_safe_stdio

    ascii_safe_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in args:
        return selftest()
    try:
        from ewave_batch import cli as _cli
    except ImportError:
        print("ewave_batch.cli 还没写（P5）。现在只有：", file=sys.stderr)
        print("  python -m ewave_batch dry-run --self-test", file=sys.stderr)
        return 2
    return int(_cli.main(args))


if __name__ == "__main__":
    raise SystemExit(main())
