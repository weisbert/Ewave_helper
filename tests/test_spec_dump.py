"""`spec_to_mapping` / `dump_spec` / `save_spec` —— GUI「Save spec as…」的落笔处。

## 这组测试在防什么

spec 文件是本工具的**工程文件**：designs、要扫哪些轴、默认表覆盖、extra flags、
批次名和落点。`batch.json` 存不了这些（它存的是跑起来**之后**的状态，给 resume 用）。
所以「在界面上配好 → 保存 → 下次打开还在」这条路只有 spec 一条。

序列化少写一个字段的后果是：**用户设了、保存了、下次打开没了，而且无声**。
所以这里的核心判据是**往返不动点**：dump → load → dump 必须逐字节相同，
而且中间那个 load 出来的 `BatchSpec` 要和原来的逐字段相等。

## 防自证

往返测试有个天然的陷阱：如果 `spec_to_mapping` 和 `parse_spec_mapping` **同时**漏掉
同一个字段，往返照样是不动点 —— 空集合的 diff 永远好看。
所以另配一条**字段覆盖**测试：拿 `BatchSpec` 的 dataclass 字段清单逐个点名，
要求每个字段要么被序列化、要么在豁免名单里（豁免要写理由）。漏一个当场红。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import fields as dataclass_fields

from ewave_batch import model
from ewave_batch.core import matrix
from ewave_batch.core import spec as spec_module

# 不需要序列化的字段 + 理由。加进这个名单是要写理由的 —— 这就是那道摩擦。
EXEMPT_SPEC_FIELDS = {
    # 这两个记的是「这份 spec 是从哪个文件读来的」，属于**来源**而不是**内容**。
    # 写回去只会让「另存为」出来的文件声称自己来自旧路径。
    "source_path": "来源元数据，不是内容",
    "source_sha256": "来源元数据，不是内容",
}


def _demo_spec() -> model.BatchSpec:
    """一份把各类字段都填上的 spec。正反两向共用这一条构造路径。"""
    catalog = matrix.builtin_axis_catalog()
    return model.BatchSpec(
        batch_name="demo_batch",
        batch_root="/tmp/ewb",
        designs=[
            model.Design(
                library="MY_LIB",
                cell="MY_CELL",
                view="layout_em",
                official_run_dir="/fake/off",
                resources="cpu=20;mem=100000",
                label="the inductor",
            ),
            model.Design(
                library="MY_LIB",
                cell="OTHER_CELL",
                view="layout",
                official_run_dir="/fake/off2",
                axis_overrides={"temperature": ["25.0"]},
                extra_flags={"--printDouble": True},
            ),
        ],
        axes=[
            matrix.axis_with_values(catalog["corner"], ["typical", "rcworst"]),
            matrix.axis_with_values(catalog["temperature"], ["-40.0", "125.0"]),
            matrix.axis_with_values(catalog["equalCurrent"], ["on", "off"]),
        ],
        groups=[
            # 组是 base 之上的 delta：只列它覆盖的轴。这里同时覆盖两根轴，
            # 是为了让「组的 axes 整段没被序列化」和「只写了一根」两种漏法都能被抓到。
            model.RunGroup(
                name="eqcur-off",
                axis_overrides={"temperature": ["125.0"], "equalCurrent": ["off"]},
                label="one-off: equalCurrent off at 125C",
            ),
        ],
        defaults={"--viaMode": "1"},
        extra_flags={"--surface": True, "--someOff": False},
        options=model.BatchOptions(max_parallel=8),
    )


class FieldCoverage(unittest.TestCase):
    """**每个 `BatchSpec` 字段都要被序列化**，否则用户的设定会静默丢失。"""

    def test_every_spec_field_is_serialised_or_exempt(self) -> None:
        spec = _demo_spec()
        data = spec_module.spec_to_mapping(spec)
        missing = []
        for field_info in dataclass_fields(model.BatchSpec):
            name = field_info.name
            if name in EXEMPT_SPEC_FIELDS:
                continue
            if name not in data:
                missing.append(name)
        self.assertEqual(
            missing, [],
            "这些字段没被写进 spec —— 用户设了、保存了、下次打开会没有，而且无声：%s" % missing,
        )
        # 防空过：豁免名单不许把所有字段都吃掉
        self.assertLess(
            len(EXEMPT_SPEC_FIELDS), len(dataclass_fields(model.BatchSpec)) // 2,
            "豁免名单太长了 —— 这条覆盖测试正在失去意义",
        )

    def test_exemptions_all_have_a_reason(self) -> None:
        for name, why in EXEMPT_SPEC_FIELDS.items():
            self.assertTrue(str(why).strip(), f"{name} 的豁免没写理由")


class RoundTrip(unittest.TestCase):
    """dump → load → dump 是不动点，而且中间那份逐字段等于原来的。"""

    def _round_trip(self, spec):
        """唯一的一条往返路径。反向测试只改输入。"""
        with tempfile.TemporaryDirectory() as tmp:
            written = spec_module.save_spec(spec, os.path.join(tmp, "s.yaml"))
            self.assertTrue(os.path.isfile(written), "save_spec 说写了，但文件不在")
            back = spec_module.load_spec(written)
            return back, written

    def test_round_trip_is_a_fixed_point(self) -> None:
        spec = _demo_spec()
        back, _ = self._round_trip(spec)
        self.assertEqual(
            spec_module.dump_spec(back), spec_module.dump_spec(spec),
            "往返之后文本变了 —— 有字段在序列化或解析的某一侧丢了",
        )

    def test_round_trip_keeps_every_value(self) -> None:
        """逐字段比，不是只比文本 —— 文本相同但语义漂了同样是坏的。"""
        spec = _demo_spec()
        back, _ = self._round_trip(spec)

        self.assertEqual(back.batch_name, spec.batch_name)
        self.assertEqual(back.batch_root, spec.batch_root)
        self.assertEqual(back.defaults, spec.defaults)
        self.assertEqual(back.extra_flags, spec.extra_flags)
        self.assertEqual(back.options.max_parallel, spec.options.max_parallel)

        # ★ 计数断言：design 数、轴数、组数一个不少
        self.assertEqual(len(back.designs), len(spec.designs))
        self.assertEqual(len(back.axes), len(spec.axes))
        self.assertEqual(len(back.groups), len(spec.groups))

        self.assertEqual(
            [(d.library, d.cell, d.view, d.official_run_dir, d.resources, d.label)
             for d in back.designs],
            [(d.library, d.cell, d.view, d.official_run_dir, d.resources, d.label)
             for d in spec.designs],
        )
        # 归一成 list 再比：解析侧把取值统一成 tuple（不可变，防调用方就地改），
        # 构造侧写的是 list —— 表示不同、语义相同，比的是语义。
        def _norm(overrides):
            return {k: list(v) for k, v in overrides.items()}

        self.assertEqual(
            [_norm(d.axis_overrides) for d in back.designs],
            [_norm(d.axis_overrides) for d in spec.designs],
        )
        self.assertEqual(
            [dict(d.extra_flags) for d in back.designs],
            [dict(d.extra_flags) for d in spec.designs],
        )
        self.assertEqual(
            [(a.name, [v.value for v in a.values]) for a in back.axes],
            [(a.name, [v.value for v in a.values]) for a in spec.axes],
        )
        # 组：名字、label、以及**每根被覆盖的轴的取值**都要原样回来。
        # 只比名字的话，「组的 axes 整段丢了」会静默通过 —— 那正是最坏的漏法：
        # 组还在界面上列着，但它展开出来的 run 和 base 一模一样。
        self.assertEqual(
            [(g.name, g.label, _norm(g.axis_overrides)) for g in back.groups],
            [(g.name, g.label, _norm(g.axis_overrides)) for g in spec.groups],
        )

    def test_a_changed_value_really_shows_up_negative(self) -> None:
        """反向：改一个取值 → 往返出来的文本必须不同。

        少了这条，上面两条可能只是因为 dump 出来的东西**根本不含**那些值
        （两边都空 ⇒ 永远相等）。
        """
        spec = _demo_spec()
        base_text = spec_module.dump_spec(spec)

        mutated = _demo_spec()
        mutated.axes[1] = matrix.axis_with_values(
            matrix.builtin_axis_catalog()["temperature"], ["-40.0", "85.0"]
        )
        self.assertNotEqual(
            spec_module.dump_spec(mutated), base_text,
            "改了温度取值，dump 出来却一模一样 —— 说明轴取值压根没被序列化",
        )
        back, _ = self._round_trip(mutated)
        self.assertEqual(
            [v.value for v in back.axes[1].values], ["-40.0", "85.0"],
            "改过的取值没能原样读回来",
        )

    def test_false_flag_survives_the_round_trip(self) -> None:
        """`False` = 「显式关掉」，有语义，**不能**在序列化时被当成"没有"丢掉。

        丢了的后果：下次打开时那个 flag 变成"由低层默认说了算"，
        而低层可能正好是打开的 —— 用户以为自己关了，其实开着。
        """
        spec = _demo_spec()
        self.assertIs(spec.extra_flags["--someOff"], False, "前提：构造里有一个 False")
        back, _ = self._round_trip(spec)
        self.assertIn("--someOff", back.extra_flags)
        self.assertIs(back.extra_flags["--someOff"], False)


class FallbackFormat(unittest.TestCase):
    """没有 PyYAML 时的退路：出 JSON，**并且把扩展名也换成 `.json`**。"""

    def test_json_fallback_has_no_comment_header(self) -> None:
        """JSON 没有注释语法 —— 加了 `#` 头就不是合法 JSON，而 `load_spec` 在没有
        PyYAML 的机器上正是靠 `json.loads` 读它。这条防的是"自己写的文件自己读不回来"。
        """
        text = spec_module.dump_spec(_demo_spec(), as_json=True)
        self.assertFalse(text.lstrip().startswith("#"))
        json.loads(text)  # 解析不了会直接抛，不需要额外断言

    def test_yaml_output_has_the_header(self) -> None:
        if not spec_module.have_yaml():
            self.skipTest("平台性 skip：本机没装 PyYAML（红区装了 6.0.1）")
        text = spec_module.dump_spec(_demo_spec(), as_json=False)
        self.assertTrue(text.lstrip().startswith("#"), "YAML 分支该带注释头")

    def test_extension_switches_when_yaml_is_unavailable(self) -> None:
        """没有 PyYAML 时存 `.yaml` → 实际落成 `.json`。

        `load_spec` 是**按扩展名**选解析器的，所以内容是 JSON、名字是 .yaml 的文件
        读的时候会走 YAML 分支然后报错 —— 用户会拿到一个自己打不开的文件。
        """
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "s.yaml")
            written = spec_module.save_spec(_demo_spec(), target)
            expected_ext = ".yaml" if spec_module.have_yaml() else ".json"
            self.assertEqual(os.path.splitext(written)[1], expected_ext)
            # 不管走哪条路，写出来的东西都必须读得回来 —— 这才是真正要的性质
            spec_module.load_spec(written)

    def test_json_extension_is_never_rewritten_negative(self) -> None:
        """反向：显式存 `.json` 时扩展名一个字都不许动（有 PyYAML 也一样）。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "s.json")
            written = spec_module.save_spec(_demo_spec(), target)
            self.assertEqual(written, target)
            spec_module.load_spec(written)


class AtomicWrite(unittest.TestCase):
    def test_no_temp_file_is_left_behind(self) -> None:
        """原子写不许留残骸 —— 留下的 `.tmp` 会让下一个人以为目录坏了。"""
        with tempfile.TemporaryDirectory() as tmp:
            spec_module.save_spec(_demo_spec(), os.path.join(tmp, "s.yaml"))
            leftovers = [n for n in os.listdir(tmp) if n.endswith(".tmp")]
            self.assertEqual(leftovers, [], f"留了临时文件: {leftovers}")

    def test_overwrite_keeps_the_file_readable(self) -> None:
        """覆盖已有文件之后仍然读得回来（`os.replace` 是原子的）。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "s.yaml")
            first = spec_module.save_spec(_demo_spec(), target)
            second = spec_module.save_spec(_demo_spec(), target)
            self.assertEqual(first, second)
            spec_module.load_spec(second)


if __name__ == "__main__":
    unittest.main()
