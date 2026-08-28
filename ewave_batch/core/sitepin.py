# -*- coding: utf-8 -*-
"""`ewave_batch.core.sitepin` —— 把**站点级**坐标钉在这台机器上，让官方 run 目录变可选。

## 这个模块存在的理由

用户 2026-08-28 在别人机器上部署时实测：`Official run dir` 那一格是第一道门，
而它「非常卡手」—— 新机器上没人猜得到该填哪个目录。方案 A（当天拍板）：
**装机时 load 一次官方目录，把站点级坐标钉下来；此后 official 降级成可选。**

## 三级梯子（照 `C:\\code\\Auto_ext` 的 `core/env.py`）

    官方 run 目录（给了就赢）  >  钉住的值  >  $环境变量  >  missing

`missing` 是**显式的第四态**，不是"取个默认值糊过去"。调用方据此画不同的图标 ——
这是本方案对「钉住的值会过期」那个老问题的回答：不承诺检测过期，
承诺**让人一眼看出这个值是钉的还是现读的**（`resolve_pinned` 返回的 `sources`）。

## 钉什么、绝不钉什么

分类依据是 08-24 就定下的那条线：**站点级 vs per-design**（见项目记忆
`what-official-rundir-gives-and-what-may-be-cached`）。这条线正好也是"能不能钉"的线，
不是巧合 —— 站点级的东西换个 design 不变，per-design 的换个 Cell 全变。

🚨 **`official_port_spec` 永远不钉，这是本模块唯一的红线。**
端口映射不在 `.sNp` 里，在命令行 `-p` 的顺序里。设计师加一个 pin ⇒ 缓存里还是老表 ⇒
`.sNp` 每一位的含义都错了，**而且跑得出来、数字也像**。它是 per-design 的，
所以本模块连存都不存它 —— 不是"存了但不用"，是 `PIN_FIELDS` 里没有它。
缺了端口表的后果只是少一层端口数校验，`sched.driver._warn_port_guard_once` 已经
处理了那种情况（说一次，然后跳过）。

`PIN_FIELDS` + `NEVER_PIN_FIELDS` **必须穷尽** `SiteFacts` 的每个字段，
`tests/test_sitepin.py::Classification` 盯着这条。将来给 `SiteFacts` 加字段的人
会被那条测试拦下来，逼他做一次显式决定 —— 少了它，新字段会**默认**漏进缓存。

## 环境变量：存引用，不存值

钉下来的路径先过一遍 `contract_env`：把任何一个环境变量的值换成 `${那个变量名}`。

    /some/pdk/apps/ewave/ewaveinterface/...   ->   ${EWAVE_ROOT}/ewaveinterface/...

好处有两个，第二个才是重点：

1. PDK 根一挪，钉住的值自己跟着走；
2. **钉文件里剩下的真实坐标更少** —— 08-18 的 step0 实测（BRIEF §10 / P9）已经
   确认 `EWAVE_ROOT` 与 `PDK_LAYER_MAP_FILE` 在红区 shell 里存在，
   于是 ptxt 那条路径里只剩「版本目录」一段是真的躲不掉的。

**变量名进源码是允许的**（硬约束 1b：「通用的东西可以进源码…那些是工具语义，
不是站点身份」）—— 但本模块连名字都不写死：`contract_env` 对着**当时 env 里的每一个**
变量找最长前缀匹配，一个候选名单都不用维护。

## 拿不到的那一个（08-18 已实测，别再去查一遍）

`$EWAVE_ROOT` 只给到 ptxt 的**根**。中间的**PDK 版本目录**那一段、和
**文件名模板** 没有任何环境变量给得出来，而且 PDK 的 layermap 与 ptxt 版本串
长期不一致（BRIEF P5），**不能从一个推另一个**。⇒ 这两段只能钉，这正是本模块的主要负载。

只读磁盘，除 `save_pin` 外不写任何文件。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping

from ..model import SiteFacts, StateError

__all__ = [
    "PIN_FILE_NAME",
    "PIN_SCHEMA_VERSION",
    "PIN_FIELDS",
    "NEVER_PIN_FIELDS",
    "SOURCE_PINNED",
    "SOURCE_ENV",
    "SOURCE_MISSING",
    "MIN_ENV_VALUE_CHARS",
    "contract_env",
    "expand_env",
    "pin_from_facts",
    "resolve_pinned",
    "save_pin",
    "load_pin",
    "pin_path",
    "merge_facts",
]


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

PIN_FILE_NAME = "site_facts.local.json"
"""钉住的坐标存哪。**装机目录下**，与 `site.local.sh` / `session.local.json` 同一层。

`.local.` 那一段是给 `.gitignore` 的（`*.local.*` 那条规则），名字里带 `site_facts`
是为了让人一眼知道里面是什么。`deploy.sh` 的 `PRESERVE` 要保它 ——
不保 = 一次升级把整台机器的配置吃掉，而症状是"official 又变成必填的了"。"""

PIN_SCHEMA_VERSION = 1
"""钉文件的版本。读到更大的版本 ⇒ 拒绝（`StateError`），不猜。"""

SOURCE_PINNED = "pinned"
SOURCE_ENV = "env"
SOURCE_MISSING = "missing"
"""一个字段的来源三态。**`missing` 是显式的一态**，不是空值的同义词 ——
调用方要能把"这个值是钉的"和"这个值根本没有"画成两种图标。"""

MIN_ENV_VALUE_CHARS = 8
"""`contract_env` 只拿长度到这个数的环境变量值去做前缀替换。

下限存在的理由不是效率，是**别把命令改坏**：`/` 或 `/tmp` 这种短值是几乎所有路径的
前缀，换进去之后钉文件里全是 `${SOMEVAR}/...`，而那个变量在别的机器上是别的东西 ——
一次静默的路径改写，比漏换一个变量坏得多。"""

PIN_FIELDS: tuple[str, ...] = (
    # ── 那个"拿不到"的（BRIEF P9 / P5）：env 只给到 $EWAVE_ROOT，版本目录和文件名模板只能钉
    "ptxt",
    "ptxt_dir",
    "ptxt_name_template",
    "pdk_root",
    # ── PDK 资产。layer_map 在红区是 $PDK_LAYER_MAP_FILE（P7 确认），钉下来时会被
    #    contract_env 换成引用，于是它实际上走的是 env 那一级。
    "layer_map",
    # ── D1c：gdsout_setup 模板。7 个随 design 变的字段已经是占位符了 ⇒ 站点级。
    #    非路径字段必须逐字复现（`maxVertices 200` 错一个字 GDS 内容就变，且跑得出来）。
    "gdsout_template",
    # ── §11 规则 1 的「默认表」。钉住它 = 换 PDK 版本不再自动跟上，
    #    这是方案 A 明知的代价，用 sources 标注让它可见（模块 docstring 第二节）。
    "production_flags",
    # ── license key：从 run_ewave_*.sh 解析，环境变量里没有 ⇒ 必须钉
    "key",
    # ── 工具路径。`command -v` 一般够，钉住是为了 PATH 没配好的机器。
    "ewave_bin",
    "strmout_bin",
    "ewave_version",
    # ── Donau。账号/队列 2026-08-28 起有内置默认值了，资源仍然值得钉。
    "dsub_account",
    "dsub_queue",
    "dsub_resources",
)
"""站点级 = 换个 design 不变 = **可以钉**。"""

NEVER_PIN_FIELDS: tuple[str, ...] = (
    # 🚨 红线。理由写在模块 docstring 里，改它之前先把那段读完。
    "official_port_spec",
    # per-design：换个 Cell 全变，而且用户本来就在界面上自己填 library/cell/view。
    "library",
    "top_cell",
    "view",
    # 官方**那一次** run 的取值 —— 它们是 run 的身份，不是站点的属性。
    # 钉住 corner 就等于把"官方那次跑的是 typical"变成"这台机器只会跑 typical"。
    "corner",
    "temperature",
    "ewave_dir_name",
    "official_command_line",
    "official_flags",
    # 描述这一次解析本身的东西，钉下来只会误导（说的是当时那个目录）。
    "official_run_dir",
    "source_files",
    "warnings",
)
"""per-design / run 身份 / 解析元数据 = **绝不钉**。"""


# --------------------------------------------------------------------------
# 环境变量：值 <-> 引用
# --------------------------------------------------------------------------

_RE_ENV_BRACE = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_RE_ENV_BARE = re.compile(r"(?<!\$)\$([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_])")
"""`${VAR}` 与 `$VAR` 两种写法。负向后顾 `(?<!\\$)` 让 `$$VAR` 保持字面量
（与 shell 同义），照抄 Auto_ext `core/env.py` 的口径。"""


def _env_of(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _substitutable(env: Mapping[str, str]) -> list[tuple[str, str]]:
    """能拿来做前缀替换的 `(值, 变量名)`，**长的排前面**。

    只收绝对路径且够长的（见 `MIN_ENV_VALUE_CHARS`）。长的先换是必须的：
    `<root>` 与 `<root>/apps/ewave` 同时存在时，短的先换会把长的切碎，
    剩下半截既不是引用也不是完整路径。
    """
    pairs: list[tuple[str, str]] = []
    for name, value in env.items():
        cleaned = str(value or "").rstrip("/")
        if len(cleaned) < MIN_ENV_VALUE_CHARS or not cleaned.startswith("/"):
            continue
        pairs.append((cleaned, name))
    pairs.sort(key=lambda item: (-len(item[0]), item[1]))
    return pairs


def contract_env(text: str, env: Mapping[str, str] | None = None) -> str:
    """路径里的环境变量**值** → `${变量名}`。换不动就原样返回。

    这是 `expand_env` 的逆。存进钉文件之前过一遍它，好处见模块 docstring
    「环境变量：存引用，不存值」那一节。

    **一条路径最多只换最前面那一段**：换第二处几乎必然是巧合（两个变量的值互相
    包含），而巧合的替换是静默的路径改写。
    """
    body = str(text or "")
    if not body or not body.startswith("/"):
        return body
    for value, name in _substitutable(_env_of(env)):
        if body == value:
            return "${%s}" % name
        if body.startswith(value + "/"):
            return "${%s}%s" % (name, body[len(value) :])
    return body


def expand_env(text: str, env: Mapping[str, str] | None = None) -> tuple[str, tuple[str, ...]]:
    """`${VAR}` / `$VAR` → 值。返回 `(展开后的文本, 没解析出来的变量名)`。

    **解不出来的变量原样留在文本里**，并且它的名字进第二个返回值 —— 不是换成空串。
    换成空串会把 `${EWAVE_ROOT}/x` 变成 `/x`，那是一条**看起来合法的错路径**，
    比一条明显没展开的路径难查得多。
    """
    body = str(text or "")
    if "$" not in body:
        return body, ()
    values = _env_of(env)
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in values and str(values[name]):
            return str(values[name])
        if name not in missing:
            missing.append(name)
        return match.group(0)

    body = _RE_ENV_BRACE.sub(replace, body)
    body = _RE_ENV_BARE.sub(replace, body)
    return body, tuple(missing)


# --------------------------------------------------------------------------
# 钉 / 读
# --------------------------------------------------------------------------


def pin_from_facts(
    facts: SiteFacts, *, env: Mapping[str, str] | None = None
) -> dict[str, object]:
    """`SiteFacts` → 可以落盘的字典。**只取 `PIN_FIELDS`**，路径过 `contract_env`。

    空值不写进去：一个 `"key": ""` 和"这个字段没钉"在读回来时长得一样，
    但前者会让 `resolve_pinned` 报 `pinned` 而不是 `missing` —— 那正好是
    本方案最不该说谎的地方。
    """
    data: dict[str, object] = {"schema_version": PIN_SCHEMA_VERSION}
    for name in PIN_FIELDS:
        value = getattr(facts, name, None)
        if isinstance(value, dict):
            if value:
                data[name] = {str(k): str(v) for k, v in value.items()}
            continue
        text = str(value or "")
        if not text:
            continue
        data[name] = contract_env(text, env) if text.startswith("/") else text
    return data


def resolve_pinned(
    data: Mapping[str, object], *, env: Mapping[str, str] | None = None
) -> tuple[SiteFacts, dict[str, str], tuple[str, ...]]:
    """钉住的字典 → `(SiteFacts, 每个字段的来源, 没解析出来的变量名)`。

    来源三态见 `SOURCE_*`。判据是**展开前后变没变**：变了说明这个值真的来自环境变量
    （于是标 `env`，界面该显示"这台机器现读的"），没变就是纯钉住的。
    """
    facts = SiteFacts()
    sources: dict[str, str] = {}
    missing: list[str] = []
    for name in PIN_FIELDS:
        raw = data.get(name)
        if raw is None or raw == "" or raw == {}:
            sources[name] = SOURCE_MISSING
            continue
        if isinstance(raw, Mapping):
            setattr(facts, name, {str(k): str(v) for k, v in raw.items()})
            sources[name] = SOURCE_PINNED
            continue
        text = str(raw)
        expanded, unresolved = expand_env(text, env)
        for var in unresolved:
            if var not in missing:
                missing.append(var)
        setattr(facts, name, expanded)
        sources[name] = SOURCE_ENV if expanded != text else SOURCE_PINNED
    return facts, sources, tuple(missing)


def pin_path(install_dir: str) -> str:
    """钉文件在哪。`install_dir` 是装机目录（放着 `deploy.sh` / `site.local.sh` 那一层）。"""
    if not install_dir:
        return ""
    return os.path.join(install_dir, PIN_FILE_NAME).replace("\\", "/")


def save_pin(path: str, facts: SiteFacts, *, env: Mapping[str, str] | None = None) -> str:
    """把站点级坐标钉到 `path`。**原子写**（同目录临时文件 + `os.replace`）。

    ⚠️ 这是本模块唯一会写盘的函数，而且**不该被自动调用** —— 照 Auto_ext
    `core/env_import.py` 的那条自我约束：「一个环境值是站点事实，采纳它是一个决定」。
    界面的流程是「load official → 给人看一张对照表 → 他点采纳 → 才调这里」。
    """
    target = str(path).replace("\\", "/")
    if not target:
        raise StateError("save_pin: no path given")
    payload = json.dumps(pin_from_facts(facts, env=env), indent=2, sort_keys=True, ensure_ascii=False)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload + "\n")
    os.replace(tmp, target)
    return target


def load_pin(path: str) -> dict[str, object]:
    """读钉文件。**不存在 → 空字典**（那是全新机器的正常状态，不是错误）。

    读坏了 / 版本不认识 → `StateError`：一份读不懂的配置比没有配置危险，
    因为它会让人以为坐标已经配好了。
    """
    target = str(path or "").replace("\\", "/")
    if not target or not os.path.isfile(target):
        return {}
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise StateError(f"cannot read {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError(f"{target}: the top level is not an object")
    version = data.get("schema_version", PIN_SCHEMA_VERSION)
    try:
        version = int(version)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise StateError(f"{target}: schema_version is not a number") from exc
    if version > PIN_SCHEMA_VERSION:
        raise StateError(
            f"{target} has schema_version={version}, this build only knows up to "
            f"{PIN_SCHEMA_VERSION}.\n"
            "  Next: upgrade the tool, or delete the file and adopt the official run dir again."
        )
    return data


def merge_facts(pinned: SiteFacts, live: SiteFacts) -> SiteFacts:
    """`live`（官方 run 目录现读的）压过 `pinned`，**逐字段**、只在 live 非空时。

    逐字段而不是整个对象二选一：官方目录**可以是残缺的**
    （`discover_site_facts` 的软失败契约 —— 比如只在本地跑过、没有 `remote_run_ewave.sh`），
    整份顶掉会让一个残缺的目录把钉好的坐标打回原形，而那正是方案 A 要消灭的状态。
    """
    merged = SiteFacts()
    for field_name in list(PIN_FIELDS) + list(NEVER_PIN_FIELDS):
        live_value = getattr(live, field_name, None)
        pinned_value = getattr(pinned, field_name, None)
        chosen = live_value if _has_value(live_value) else pinned_value
        if chosen is not None:
            setattr(merged, field_name, chosen)
    return merged


def _has_value(value: object) -> bool:
    """"这个字段算给了吗"。`0` / `False` 不是 `SiteFacts` 里会出现的取值，
    所以这里用真值判断就够，不必区分"空"和"未设置"。"""
    if value is None:
        return False
    if isinstance(value, (str, dict, tuple, list)):
        return bool(value)
    return bool(getattr(value, "mapping", None) or value)
