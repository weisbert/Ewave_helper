"""Phase 0 的测试：冻结面本身是不是自洽的、漂移检测器有没有牙。

这里**不测业务逻辑**（Phase 0 一行实现都没有），只测契约：
`--self-test` 退 0、`FROZEN` 清单合法、`RunStatus` 恰好 6 个、签名比对真的能报差异。
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

from ewave_batch import model
from ewave_batch.__main__ import compare_signatures, normalize_annotation, normalize_signature

ROOT = Path(__file__).resolve().parents[1]

_MODULE_NAME = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$")


class SelfTestGate(unittest.TestCase):
    """check.sh 第 4 步跑的就是这条命令。"""

    def test_self_test_exits_zero(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "ewave_batch", "dry-run", "--self-test"],
            cwd=str(ROOT),
            capture_output=True,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"self-test 退了 {proc.returncode}\n"
            f"stdout:\n{proc.stdout.decode('utf-8', 'replace')}\n"
            f"stderr:\n{proc.stderr.decode('utf-8', 'replace')}",
        )


class FrozenManifest(unittest.TestCase):
    def test_module_names_are_legal(self) -> None:
        self.assertTrue(model.FROZEN, "FROZEN 空了 —— 冻结面没了")
        for name, symbols in model.FROZEN.items():
            self.assertRegex(name, _MODULE_NAME, f"{name} 不是合法的模块名")
            self.assertIsInstance(symbols, tuple, f"{name} 的符号清单必须是 tuple（不可变）")
            self.assertTrue(symbols, f"{name} 一个符号都没冻结")
            self.assertEqual(len(symbols), len(set(symbols)), f"{name} 的符号清单有重复")
            for symbol in symbols:
                self.assertTrue(symbol.isidentifier(), f"{name}.{symbol} 不是合法标识符")

    def test_every_module_has_a_phase(self) -> None:
        self.assertEqual(
            sorted(model.FROZEN), sorted(model.FROZEN_PHASE), "FROZEN 和 FROZEN_PHASE 对不上"
        )

    def test_model_exports_everything_it_declares(self) -> None:
        for symbol in model.FROZEN["ewave_batch.model"]:
            self.assertTrue(hasattr(model, symbol), f"model 自己都没有 {symbol}")

    def test_no_stub_is_orphaned(self) -> None:
        """model 里每个公开函数桩子都必须被某个模块认领 —— 否则没人会去实现它。"""
        owned = {s for name, syms in model.FROZEN.items() if name != "ewave_batch.model" for s in syms}
        for name, obj in vars(model).items():
            if name.startswith("_") or not callable(obj) or isinstance(obj, type):
                continue
            if getattr(obj, "__module__", "") != "ewave_batch.model":
                continue
            self.assertIn(name, owned, f"model.{name} 是个没人认领的桩子，FROZEN 里加上它的归属")

    def test_protocol_bindings_are_resolvable(self) -> None:
        for key, protocol_name in model.FROZEN_PROTOCOL_IMPLS.items():
            module_name, sep, cls_name = key.partition(":")
            self.assertEqual(sep, ":", f"{key} 该写成 模块:类名")
            self.assertIn(module_name, model.FROZEN, f"{key} 的模块不在 FROZEN 里")
            self.assertIn(cls_name, model.FROZEN[module_name], f"{key} 的类名没进 FROZEN")
            self.assertTrue(hasattr(model, protocol_name), f"model 里没有 {protocol_name}")


class DataContracts(unittest.TestCase):
    def test_run_status_has_exactly_six_states(self) -> None:
        # 计数断言：状态集是用户 2026-08-18 拍板的 6 个，多一个少一个都要重新过设计。
        self.assertEqual(len(model.RunStatus), 6)
        self.assertEqual(
            [s.value for s in model.RunStatus],
            ["ready", "pending", "running", "done", "failed", "skipped"],
        )

    def test_pending_means_queued_not_unsubmitted(self) -> None:
        """`ready` = 还没提交，`pending` = 已 dsub 在排队（Donau 自己的词）。别合并这两个。"""
        self.assertIs(model.RunStatus.READY, model.RunStatus("ready"))
        self.assertIs(model.RunStatus.PENDING, model.RunStatus("pending"))

    def test_merge_order_is_the_documented_one(self) -> None:
        self.assertEqual(
            model.FlagLayers.MERGE_ORDER,
            (
                model.FlagLayer.BUILTIN,
                model.FlagLayer.DEFAULTS,
                model.FlagLayer.EXTRA,
                model.FlagLayer.AXIS,
                model.FlagLayer.LOCKED,
            ),
            "四层合并顺序是契约：内置默认 < 默认表 < Extra flags < 轴 < 机制",
        )

    def test_user_forbidden_covers_mechanism_flags(self) -> None:
        self.assertTrue(model.MECHANISM_FLAGS <= model.USER_FORBIDDEN_FLAGS)
        # --emssTechFile 用户不许给，但 corner 轴要改它（§7 corner 轴要同时改两处）
        self.assertIn("--emssTechFile", model.USER_FORBIDDEN_FLAGS)
        self.assertNotIn("--emssTechFile", model.MECHANISM_FLAGS)

    def test_runs_csv_columns_are_unique(self) -> None:
        self.assertEqual(len(model.RUNS_CSV_COLUMNS), len(set(model.RUNS_CSV_COLUMNS)))

    def test_axis_with_no_values_is_rejected(self) -> None:
        with self.assertRaises(model.SpecError):
            model.Axis(name="corner", values=())

    def test_design_requires_the_full_triple(self) -> None:
        with self.assertRaises(model.SpecError):
            model.Design(library="L", cell="C", view="")


class SignatureChecker(unittest.TestCase):
    """漂移检测器自己的测试 —— 它是后面每个阶段的闸门，不能是聋的。"""

    def test_normalize_signature_format(self) -> None:
        def sample(a, b=1, *args, c=2, **kw) -> "list[str]":
            raise NotImplementedError

        self.assertEqual(normalize_signature(sample), "(a, b=, *args, c=, **kw) -> list[str]")

    def test_normalize_annotation_smooths_writing_styles(self) -> None:
        self.assertEqual(normalize_annotation("Optional[str]"), "str|None")
        self.assertEqual(normalize_annotation("ewave_batch.model.Run"), "Run")
        self.assertEqual(normalize_annotation("typing.List[ int ]"), "list[int]")
        self.assertEqual(normalize_annotation(inspect_empty()), "")

    def test_identical_signatures_pass(self) -> None:
        def frozen(run, ctx) -> "int":
            raise NotImplementedError

        def actual(run, ctx) -> "int":
            return 0

        self.assertIsNone(
            compare_signatures(normalize_signature(frozen), normalize_signature(actual))
        )

    def test_renamed_parameter_is_caught_negative(self) -> None:
        def frozen(corner, temperature) -> "str":
            raise NotImplementedError

        def actual(corner, temp) -> "str":
            return ""

        reason = compare_signatures(normalize_signature(frozen), normalize_signature(actual))
        self.assertIsNotNone(reason, "改了参数名居然没被抓到 —— 检测器是聋的")
        self.assertIn("temperature", str(reason))

    def test_dropped_default_is_caught_negative(self) -> None:
        def frozen(path, *, dry_run=False) -> "None":
            raise NotImplementedError

        def actual(path, *, dry_run) -> "None":
            return None

        self.assertIsNotNone(
            compare_signatures(normalize_signature(frozen), normalize_signature(actual))
        )

    def test_changed_return_annotation_is_caught_negative(self) -> None:
        def frozen(x) -> "list[Run]":
            raise NotImplementedError

        def actual(x) -> "dict[str, Run]":
            return {}

        self.assertIsNotNone(
            compare_signatures(normalize_signature(frozen), normalize_signature(actual))
        )

    def test_frozen_signatures_escape_hatch_is_wired(self) -> None:
        """`FROZEN_SIGNATURES` 现在是空的，但那条代码路径必须是活的。"""
        self.assertIsInstance(model.FROZEN_SIGNATURES, dict)

        def actual(parent, bridge) -> "object":
            return bridge

        self.assertIsNone(compare_signatures("(parent, bridge) -> object", normalize_signature(actual)))
        self.assertIsNotNone(compare_signatures("(parent, state) -> object", normalize_signature(actual)))


class LazyImportDiscipline(unittest.TestCase):
    """CLAUDE.md 硬约束 5：包初始化文件不许 import 子模块，否则惰性 import 静默失效。"""

    INITS = (
        "ewave_batch/__init__.py",
        "ewave_batch/core/__init__.py",
        "ewave_batch/tools/__init__.py",
        "ewave_batch/sched/__init__.py",
        "gui/__init__.py",
        "gui/frames/__init__.py",
    )

    def test_package_inits_import_nothing(self) -> None:
        for rel in self.INITS:
            path = ROOT / rel
            self.assertTrue(path.is_file(), f"{rel} 不见了")
            lines = [
                ln.strip()
                for ln in path.read_text(encoding="utf-8").splitlines()
                if re.match(r"^\s*(import|from)\s", ln)
            ]
            self.assertEqual(lines, [], f"{rel} 里有 import：{lines}")

    def test_model_does_not_import_tkinter_or_yaml(self) -> None:
        text = (ROOT / "ewave_batch" / "model.py").read_text(encoding="utf-8")
        for banned in ("import tkinter", "import yaml"):
            self.assertNotIn(f"\n{banned}", text, f"model.py 不许 {banned}")


def inspect_empty() -> object:
    """`inspect.Signature.empty` 的占位，放在这里免得测试文件顶上再 import 一次 inspect。"""
    import inspect

    return inspect.Signature.empty


if __name__ == "__main__":
    unittest.main()
