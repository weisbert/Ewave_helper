"""`core.template`：把一条现成的 ewave 命令行解析回 flag dict + 端口表。

两层测试：

* **golden**（跟着 `tests/test_cmd_golden.py` 的规矩走）：拿**真实的官方 run 脚本**当输入，
  期望值全部来自 `tests/fixtures/production_cmd.local.json`（人抽的那份）。
  解析器多认一个 flag、少认一个 flag、把端口顺序弄反了，都会当场红。
  这两份输入是红区资料（`references/` 不进 git），**缺文件时优雅 skip 并打印原因**。
* **单元**：管道/续行/引号/重定向这些 shell 形态，期望值是手写字面量 ——
  它们是 shell 的通用语法，不是站点坐标，注释里写清依据。

🚨 本文件源码里**零站点标识符**：pin 名 / ptxt 路径 / cell 名一个都不出现，
要用的时候从 fixture 或真实脚本里读出来（CLAUDE.md 硬约束 1b）。
"""

from __future__ import annotations

import json
import shlex
import unittest
from pathlib import Path

from ewave_batch.core import cmd, template
from ewave_batch.model import PortMode, PortSpec, SpecError

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "production_cmd.local.json"

# 与 fixture 同源的那条真实命令（kit 里的样本，`--temperature=125.0`）。
GOLDEN_SCRIPT = ROOT / "references" / "ewave_donau_kit" / "ewave" / "run_examples" / "run_ewave_typical_125_0.sh"
# 红区当场取回的另一条，已验证与上面那份**逐字节相同、只差温度**（见文件头的注释）。
PROBE_SCRIPT = ROOT / "references" / "probes" / "run_ewave_typical_-40_0.sh"

FIXTURE_SKIP = (
    "本机没有 tests/fixtures/production_cmd.local.json —— 人从真实生产命令抽出来的 golden "
    "基准，含站点坐标所以不进 git（公开克隆者看到这条 skip 是正常的）"
)
SCRIPT_SKIP = (
    "本机没有 references/ 下的真实 run 脚本 —— 红区资料，永不进 git（公开克隆者正常）"
)


def _load_fixture() -> dict | None:
    if not FIXTURE_PATH.exists():
        return None
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


FIXTURE = _load_fixture()


def _expected_port_spec(fixture: dict) -> PortSpec:
    mapping = []
    for item in fixture["port_order"]:
        port_id, _, pin = str(item).partition("=")
        mapping.append((port_id, pin))
    return PortSpec(
        mode=PortMode.EXPLICIT,
        mapping=tuple(mapping),
        signal_ports=tuple(str(p) for p in fixture["signal_ports"]),
    )


@unittest.skipIf(FIXTURE is None, FIXTURE_SKIP)
@unittest.skipUnless(GOLDEN_SCRIPT.exists(), SCRIPT_SKIP)
class GoldenParseOfProductionScript(unittest.TestCase):
    """★ 解析真实的官方 run 脚本 → 必须**逐条**等于人抽出来的那份 fixture。

    这条测试同时钉死三件事：flag 的解析（等号长 flag / 空格短 flag / 裸 flag 混用）、
    端口表的**顺序**（顺序就是映射，BRIEF §5）、以及末尾那段剥色管道被丢掉。
    """

    def script_text(self) -> str:
        return GOLDEN_SCRIPT.read_text(encoding="utf-8")

    def parse(self, text: str | None = None):
        """正反两向共用的唯一一条输入路径：文本 → 抽出 ewave 那行 → 解析。"""
        source = self.script_text() if text is None else text
        line = template.extract_command_line(source)
        self.assertIsNotNone(line, "在真实脚本里没找到 ewave 那一行 —— extract_command_line 瞎了")
        return template.parse_command_line(str(line))

    def test_parsed_flags_match_fixture(self) -> None:
        parsed = self.parse()
        diff = cmd.diff_flags(parsed.flags, dict(FIXTURE["flags"]))
        self.assertTrue(
            diff.clean,
            f"解析结果和 fixture 对不上：多 {diff.only_actual}，少 {diff.only_expected}，"
            f"值不同 {[(d.flag, d.actual, d.expected) for d in diff.differing]}",
        )
        # 计数断言（配方 4）：真的比了 22 条，不是"两边都空所以很好看"。
        self.assertEqual(diff.compared_count, len(FIXTURE["flags"]))
        self.assertEqual(diff.ignored, ())
        self.assertEqual(len(parsed.flags), len(FIXTURE["flags"]))
        self.assertEqual(parsed.program, "ewave")

    def test_parsed_flags_match_fixture_negative(self) -> None:
        """反向：把脚本文本里的 `-e 0.4` 改成 `-e 0.5` → 必须且只报这一处。

        （`-e 0.4` 是 eWave 的工具语义，不是站点坐标，可以写进源码 —— CLAUDE.md 硬约束 1b。）
        """
        text = self.script_text()
        self.assertIn("-e 0.4", text, "真实脚本里没有 `-e 0.4`，这条反向测试会变成空过")
        parsed = self.parse(text.replace("-e 0.4", "-e 0.5"))
        diff = cmd.diff_flags(parsed.flags, dict(FIXTURE["flags"]))
        self.assertFalse(diff.clean)
        self.assertEqual([d.flag for d in diff.differing], ["-e"])
        self.assertEqual(diff.differing[0].actual, "0.5")
        self.assertEqual(diff.compared_count, len(FIXTURE["flags"]))

    def test_parsed_port_order_matches_fixture(self) -> None:
        parsed = self.parse()
        expected = _expected_port_spec(FIXTURE)
        diff = cmd.diff_ports(parsed.port_spec, expected)
        self.assertTrue(diff.matched, f"端口顺序对不上，第 {diff.first_mismatch_index} 位起分叉")
        self.assertEqual(diff.compared_count, FIXTURE["port_count"])
        self.assertEqual(parsed.port_spec.mode, PortMode.EXPLICIT)
        self.assertEqual(parsed.port_spec.signal_ports, expected.signal_ports)

    def test_parsed_port_order_matches_fixture_negative(self) -> None:
        """反向：把脚本里前两个 `-p` 的**次序**对调 → 必须报第 0 位错位。

        pin 集合没变、命令照样跑得出来、数字还挺像 —— 这正是 `--all` 那个"静默平移"
        失效模式的形状（BRIEF §5），所以必须靠**位置**抓，不能靠集合。
        """
        text = self.script_text()
        first, second = (str(x) for x in FIXTURE["port_order"][:2])
        token_first, token_second = f"-p '{first}'", f"-p '{second}'"
        self.assertIn(token_first, text)
        self.assertIn(token_second, text)
        swapped = (
            text.replace(token_first, "\x00").replace(token_second, token_first).replace("\x00", token_second)
        )
        parsed = self.parse(swapped)
        expected = _expected_port_spec(FIXTURE)
        diff = cmd.diff_ports(parsed.port_spec, expected)
        self.assertFalse(diff.matched, "端口次序对调了却报一致 —— 比对没在看顺序")
        self.assertEqual(diff.first_mismatch_index, 0)
        # 集合没变：只有顺序错了。这正是它危险的地方。
        self.assertEqual(diff.only_actual, ())
        self.assertEqual(diff.only_expected, ())
        self.assertEqual(diff.compared_count, FIXTURE["port_count"])

    def test_trailing_pipe_is_not_parsed_as_arguments(self) -> None:
        """生产那条命令末尾恒接 `| sed -r 's/…//g'` 剥 ANSI 色码 —— 它不是命令的一部分。"""
        text = self.script_text()
        self.assertIn("|sed", text.replace("| sed", "|sed"), "真实脚本里没有剥色管道，这条会空过")
        parsed = self.parse()
        self.assertEqual(parsed.positional, ())
        self.assertNotIn("-r", parsed.flags)

    def test_round_trip_through_render(self) -> None:
        """解析 → 渲染 → 再解析，两次结果相同。

        证明 `core.cmd.render_flags` / `_render_ports` 和这个解析器对同一条真实命令的
        理解是一致的 —— 红区 dry-run 的"自带比对"两头就是这两个函数。
        """
        parsed = self.parse()
        rebuilt = shlex.join(
            [parsed.program, *cmd.render_flags(parsed.flags), *cmd._render_ports(parsed.port_spec)]
        )
        again = template.parse_command_line(rebuilt)
        self.assertTrue(cmd.diff_flags(again.flags, parsed.flags).clean)
        self.assertTrue(cmd.diff_ports(again.port_spec, parsed.port_spec).matched)
        self.assertEqual(again.port_spec.signal_ports, parsed.port_spec.signal_ports)


@unittest.skipIf(FIXTURE is None, FIXTURE_SKIP)
@unittest.skipUnless(PROBE_SCRIPT.exists(), SCRIPT_SKIP)
class GoldenParseOfSecondCorner(unittest.TestCase):
    """红区取回的第二条真实命令：与 fixture **只差温度**（那份文件的头注释就是这么写的）。

    它是对上一条 golden 的独立复核 —— 同一个解析器、另一份真实输入、期望的差异恰好一处。
    """

    def test_only_temperature_differs(self) -> None:
        line = template.extract_command_line(PROBE_SCRIPT.read_text(encoding="utf-8"))
        self.assertIsNotNone(line)
        parsed = template.parse_command_line(str(line))
        diff = cmd.diff_flags(parsed.flags, dict(FIXTURE["flags"]))

        self.assertEqual([d.flag for d in diff.differing], ["--temperature"])
        self.assertEqual(diff.only_actual, ())
        self.assertEqual(diff.only_expected, ())
        self.assertEqual(diff.compared_count, len(FIXTURE["flags"]))
        # 防空过：两份输入的温度必须真的不同，否则这条测试什么都没说。
        self.assertNotEqual(diff.differing[0].actual, diff.differing[0].expected)
        # 端口表两边完全一样（同一个 design）。
        self.assertTrue(cmd.diff_ports(parsed.port_spec, _expected_port_spec(FIXTURE)).matched)


class SplitCommandLine(unittest.TestCase):
    """shell 形态。期望值是手写字面量，依据是 POSIX shell 的通用语法 + 生产脚本的形状
    （末尾恒接 `|sed …`，见 BRIEF §6）。"""

    def test_drops_trailing_pipeline(self) -> None:
        self.assertEqual(
            template.split_command_line("ewave --nogui |sed -r 's/x//g'"),
            ["ewave", "--nogui"],
        )

    def test_drops_csh_style_pipe_and_tee(self) -> None:
        self.assertEqual(
            template.split_command_line("ewave --nogui |& tee run.log"), ["ewave", "--nogui"]
        )

    def test_keeps_pipe_inside_quotes(self) -> None:
        """引号里的竖线是**值**，不是管道 —— 从这里截断会把命令切碎。"""
        self.assertEqual(
            template.split_command_line("ewave --x='a|b' --y=2"), ["ewave", "--x=a|b", "--y=2"]
        )

    def test_joins_line_continuations(self) -> None:
        self.assertEqual(
            template.split_command_line("ewave --x=1 \\\n    --y=2 \\\n    -e 0.4"),
            ["ewave", "--x=1", "--y=2", "-e", "0.4"],
        )

    def test_drops_redirections_and_background(self) -> None:
        self.assertEqual(
            template.split_command_line("ewave --x=1 > out.log 2>&1 &"), ["ewave", "--x=1"]
        )

    def test_unbalanced_quote_is_refused(self) -> None:
        with self.assertRaises(SpecError):
            template.split_command_line("ewave --x='unclosed")


class ParseCommandLine(unittest.TestCase):
    """token → 角色。依据写在 `parse_command_line` 的 docstring 里（官方那条命令的形状）。"""

    def test_flag_shapes(self) -> None:
        parsed = template.parse_command_line("ewave --nogui -m --corner=typical -e 0.4 --labelDepth=0")
        self.assertEqual(parsed.program, "ewave")
        self.assertEqual(
            parsed.flags,
            {"--nogui": True, "-m": True, "--corner": "typical", "-e": "0.4", "--labelDepth": "0"},
        )

    def test_value_containing_equals_is_kept_whole(self) -> None:
        """`--multiSweep=adaptive,0:0.1:40` 只在**第一个**等号处切。"""
        parsed = template.parse_command_line("ewave --x=a=b")
        self.assertEqual(parsed.flags, {"--x": "a=b"})

    def test_ports_are_collected_in_order(self) -> None:
        parsed = template.parse_command_line("ewave -p 'P000=aa' -p 'P001=bb' -i P000 -i P001")
        self.assertEqual(parsed.port_spec.mode, PortMode.EXPLICIT)
        self.assertEqual(parsed.port_spec.mapping, (("P000", "aa"), ("P001", "bb")))
        self.assertEqual(parsed.port_spec.signal_ports, ("P000", "P001"))
        self.assertNotIn("-p", parsed.flags)
        self.assertNotIn("-i", parsed.flags)

    def test_all_becomes_port_mode_all(self) -> None:
        parsed = template.parse_command_line("ewave --all --nogui")
        self.assertEqual(parsed.port_spec.mode, PortMode.ALL)
        self.assertEqual(parsed.port_spec.mapping, ())
        # 同时留在 flags 里，好让 diff_flags 看得见它（`--all` 在 DEFAULT_DIFF_IGNORE 里）。
        self.assertIs(parsed.flags["--all"], True)

    def test_unknown_tokens_go_to_positional_not_silently_dropped(self) -> None:
        parsed = template.parse_command_line("ewave --nogui leftover -- --notaflag")
        self.assertEqual(parsed.positional, ("leftover", "--notaflag"))

    def test_env_prefix_before_program_is_skipped(self) -> None:
        parsed = template.parse_command_line("EWAVE_ROOT=/x /some/where/ewave --nogui")
        self.assertEqual(parsed.program, "/some/where/ewave")
        self.assertEqual(parsed.flags, {"--nogui": True})

    def test_malformed_port_mapping_is_refused(self) -> None:
        """`-p` 后面不是 `P000=<pin>` 就必须报错 —— 端口猜错 = 整条 .sNp 错位。"""
        with self.assertRaises(SpecError):
            template.parse_command_line("ewave -p P000")

    def test_dangling_signal_port_is_refused(self) -> None:
        with self.assertRaises(SpecError):
            template.parse_command_line("ewave --nogui -i")

    def test_raw_is_preserved(self) -> None:
        line = "ewave --nogui |sed -r 's/x//g'"
        self.assertEqual(template.parse_command_line(line).raw, line)


class ExtractCommandLine(unittest.TestCase):
    """从脚本文本里找那一行。形状照 `references/probes/run_ewave_*.sh`（`#!` + 注释 + 一行命令）。"""

    def test_finds_the_line_after_comments(self) -> None:
        text = "#!/bin/sh\n# ewave is mentioned here in a comment\n\newave --nogui -m\n"
        self.assertEqual(template.extract_command_line(text), "ewave --nogui -m")

    def test_joins_continuations(self) -> None:
        text = "#!/bin/sh\newave --nogui \\\n  --corner=typical\n"
        # 只关心"续行被接起来了"；接缝处留几个空格不是契约，shlex 反正会吃掉。
        joined = str(template.extract_command_line(text))
        self.assertEqual(joined.split(), ["ewave", "--nogui", "--corner=typical"])
        self.assertEqual(template.split_command_line(joined), ["ewave", "--nogui", "--corner=typical"])

    def test_matches_absolute_path(self) -> None:
        """工具的绝对路径是站点坐标（不写进源码），运行时可能长成任何样子 —— 认 basename。"""
        text = "/opt/vendor/2025.09/bin/ewave --nogui\n"
        self.assertEqual(template.extract_command_line(text), text.strip())

    def test_returns_none_when_absent(self) -> None:
        self.assertIsNone(template.extract_command_line("#!/bin/sh\nstrmout -templateFile x\n"))

    def test_other_program_can_be_asked_for(self) -> None:
        text = "#!/bin/sh\nstrmout -templateFile ./gdsout_setup\n"
        self.assertEqual(
            template.extract_command_line(text, program="strmout"),
            "strmout -templateFile ./gdsout_setup",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
