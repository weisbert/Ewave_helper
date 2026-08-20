"""`ewave_batch.core.template` —— 解析一条现成的 ewave 命令行 → `ParsedCommand`。

两个用途（D3）：

1. **可选入口**：用户手上已经有一条配好的命令（特殊 case），导进来当模板；
2. **基准素材**：golden 测试和红区 dry-run 的"自带比对"都要先把参考命令解析成 flag dict，
   才能交给 `core.cmd.diff_flags` 逐 flag 比。

要能吃官方 `run_ewave_<corner>_<temp>.sh` 那种形态：行尾续行 `\\`、单双引号、
`-e 0.4` 这种空格分隔的短 flag 与 `--corner=typical` 这种等号长 flag 混用、
以及**末尾恒接的那段 `| sed -r 's/…//g'` 剥色管道**。

🚨 硬约束 1b：本文件解析出来的东西全是站点坐标（ptxt 路径 / cell 名 / 端口名），
所以源码里**一个真实取值都没有**，测试的期望值也只从 fixture 里读，不写进源码。

纯字符串函数，不碰文件系统。
"""

from __future__ import annotations

import posixpath
import re
import shlex

from ..model import FlagDict, ParsedCommand, PortMode, PortSpec, SpecError

_CONTINUATION = re.compile(r"\\[ \t]*\r?\n")
"""行尾续行：反斜杠 + （可能的空白）+ 换行。`shlex` 自己处理不了这个形态，先抹平。"""

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
"""`VAR=value cmd …` 里那种前缀赋值。只在**程序名之前**才当它是赋值 ——
`-p 'P000=<pin>'` 也长这样，但它不在行首，别搞混。"""

_REDIRECT_FD = re.compile(r"^\d*>&?\d*$|^&>>?$")
"""`2>&1` / `>` / `>>` / `&>` 这类 shell 重定向算子。"""


def split_command_line(line: str) -> list[str]:
    """把一行 shell 命令拆成 token（`shlex`，`posix=True`）。

    要能吃掉行尾续行 `\\`、单双引号、以及末尾的管道段（`| sed -r 's/…//g'` 要被丢掉）。
    拆不动 → `SpecError`。

    丢掉的是 **shell 的装饰**，不是命令的内容：

    * 第一个**不在引号里**的 `|` 之后的一切（`|sed …` / `|& tee …`）。
      生产那条命令末尾恒接剥 ANSI 色码的 sed —— 我们不靠管道做这件事
      （改在 `logparse.strip_ansi` 里），所以 argv 要干净（见 `CommandPlan.log_path`）。
    * 重定向算子及其目标（`> log`、`2>&1`）和结尾的 `&`。

    ⚠️ 引号状态是自己扫的，不是找 `line.index("|")` —— flag 的值里完全可能有竖线
    （`sed -r 's/\\x1B\\[[0-9;]*m//g'` 里就有一堆特殊字符），从引号里面截断会把命令切碎。
    """
    flattened = _CONTINUATION.sub(" ", line)
    head = _cut_at_pipeline(flattened)
    try:
        tokens = shlex.split(head, posix=True)
    except ValueError as exc:  # 引号不配对之类
        raise SpecError(f"cannot tokenize this command line ({exc}): {head.strip()[:120]}") from exc
    return _drop_redirections(tokens)


def _cut_at_pipeline(line: str) -> str:
    """截到第一个**不在引号里**的 `|` 为止。自己扫是因为要认引号和反斜杠转义。"""
    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote is not None:
            if char == "\\" and quote == '"' and index + 1 < len(line):
                out.append(char)
                out.append(line[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            out.append(char)
            index += 1
            continue
        if char in "'\"":
            quote = char
            out.append(char)
        elif char == "\\" and index + 1 < len(line):
            out.append(char)
            out.append(line[index + 1])
            index += 2
            continue
        elif char == "|":
            break
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _drop_redirections(tokens: list[str]) -> list[str]:
    """去掉 shell 的重定向与后台符号。`> file` 要连目标一起去掉。"""
    kept: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token == "&":
            continue
        if _REDIRECT_FD.match(token):
            # `>` / `>>` / `1>` 后面跟文件名；`2>&1` / `&>` 自带目标。
            skip_next = not token.endswith(("&1", "&2", "&0"))
            continue
        kept.append(token)
    return kept


def parse_command_line(line: str) -> ParsedCommand:
    """一条 ewave 命令行 → `ParsedCommand`（program + flags + 端口 + 位置参数）。

    * `--x=y` → `{"--x": "y"}`；`--x` → `{"--x": True}`；`-e 0.4` → `{"-e": "0.4"}`。
    * `-p 'P000=<pin>'` 收进 `port_spec.mapping`（**保序**），`-i <pin>` 收进 `signal_ports`，
      `--all` → `PortMode.ALL`。
    * 认不出的 token 进 `positional`，**不许静默丢弃**。

    这是 golden 测试和红区 dry-run 自带比对的入口（D3 的"可选模板入口"也是它）。纯函数。

    三条判断规则和它们的依据：

    1. **裸长 flag 不吃下一个 token**（`--equalCurrent` → `True`）。依据：官方那条生产命令里
       每个带值的长 flag 都写成 `--x=y`，一个例外都没有（`references/probes/run_ewave_*.sh`）。
       长 flag 若真的吃了后面那个 token，`--equalCurrent --viaMode=1` 会被解析成
       `--equalCurrent="--viaMode=1"` —— 与其猜，不如按证据来。
    2. **短 flag 吃下一个 token**，除非下一个 token 以 `-` 开头或者没有下一个了
       （`-e 0.4` 吃、`-m --workDir=.` 不吃）。
    3. 行首的 `VAR=value` 前缀赋值跳过，之后第一个 token 才是 program。
    """
    tokens = split_command_line(line)
    flags: FlagDict = {}
    mapping: list[tuple[str, str]] = []
    signal_ports: list[str] = []
    positional: list[str] = []

    index = 0
    while index < len(tokens) and _ENV_ASSIGN.match(tokens[index]):
        index += 1
    program = tokens[index] if index < len(tokens) else ""
    index += 1

    only_positional = False
    while index < len(tokens):
        token = tokens[index]
        following = tokens[index + 1] if index + 1 < len(tokens) else None

        if only_positional:
            positional.append(token)
            index += 1
            continue
        if token == "--":
            only_positional = True
            index += 1
            continue

        if token == "-p":
            if following is None or "=" not in following:
                raise SpecError(
                    f"`-p` must be followed by a `P000=<pin>` pair, got {following!r} - "
                    "a misparsed port map shifts the whole .sNp, so guessing is not allowed"
                )
            port_id, _, pin = following.partition("=")
            mapping.append((port_id, pin))
            index += 2
            continue
        if token == "-i":
            if following is None:
                raise SpecError("`-i` is followed by nothing - this command line is incomplete")
            signal_ports.append(following)
            index += 2
            continue
        if token == "--all":
            flags["--all"] = True
            index += 1
            continue

        if token.startswith("--"):
            name, sep, value = token.partition("=")
            flags[name] = value if sep else True
            index += 1
            continue
        if token.startswith("-") and len(token) > 1:
            if following is not None and not following.startswith("-"):
                flags[token] = following
                index += 2
            else:
                flags[token] = True
                index += 1
            continue

        positional.append(token)
        index += 1

    # 有 `-p` 就是 EXPLICIT（顺序即映射，信息全在这儿）；否则 ALL —— `--all` 写没写都一样，
    # 它已经作为 flag 记在 `flags` 里了，两种表达同时出现时两边的信息都不丢。
    mode = PortMode.EXPLICIT if mapping else PortMode.ALL
    port_spec = PortSpec(mode=mode, mapping=tuple(mapping), signal_ports=tuple(signal_ports))

    return ParsedCommand(
        program=program,
        flags=flags,
        port_spec=port_spec,
        positional=tuple(positional),
        raw=line.strip(),
    )


def extract_command_line(text: str, *, program: str = "ewave") -> str | None:
    """从一份 shell 脚本文本里把调用 `program` 的那一行（含续行）抠出来，找不到返回 None。

    官方 `run_ewave_<corner>_<temp>.sh` 就是这么被解析的。纯函数。

    判据：把续行接起来之后，某个**逻辑行**里第一个非赋值 token 的 basename 等于 `program`
    （所以 `/some/where/ewave --nogui …` 也认得出来 —— 工具的绝对路径是站点坐标，
    不写进源码，只能在运行时长成任何样子）。`#` 开头的注释行跳过。

    返回的是**接好续行、去掉首尾空白**的那一行，管道段还留着 ——
    `split_command_line` / `parse_command_line` 负责丢它。
    """
    for logical in _logical_lines(text):
        stripped = logical.strip()
        if not stripped or stripped.startswith("#"):
            continue
        words = stripped.split()
        position = 0
        while position < len(words) and _ENV_ASSIGN.match(words[position]):
            position += 1
        if position >= len(words):
            continue
        candidate = words[position]
        if candidate == program or posixpath.basename(candidate) == program:
            return stripped
    return None


def _logical_lines(text: str) -> list[str]:
    """把物理行按行尾 `\\` 接成逻辑行。"""
    joined = _CONTINUATION.sub(" ", text)
    return joined.splitlines()
