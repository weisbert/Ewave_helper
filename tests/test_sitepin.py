# -*- coding: utf-8 -*-
"""`ewave_batch.core.sitepin` —— 「official 变成真正可选」那套机制的核心。

## 这份文件盯什么

用户 2026-08-28 拍板方案 A：装机时 load 一次官方目录，把**站点级**坐标钉下来，
此后 `Official run dir` 降级成可选。整件事的安全性压在两条判据上，本文件全部盯着：

1. 🚨 **端口表永远不进钉文件。** `-p` 的顺序就是 `.sNp` 每一位的含义 ——
   缓存一份过期的端口表，产物每一位都错，**而且跑得出来、数字也像**。
   `PortTableIsTheRedLine` 是这条的机器判据。
2. **分类必须穷尽。** `PIN_FIELDS + NEVER_PIN_FIELDS` 要覆盖 `SiteFacts` 的每个字段，
   `Classification` 盯着。少了它，将来给 `SiteFacts` 加字段的人什么都不用做，
   而那个新字段会**默认**漏进缓存 —— 默认方向错了的机制迟早会出事。

## 四条配方（`docs/OVERNIGHT.md`）在这里的落点

* **期望值来源** = 手写字面量。环境变量名和路径全是显式假值
  （`FAKE_ENV` / `FAKE_PDK_ROOT`），**一个红区取值都没有**；
  「哪些字段该钉」那张表也是手写的，不是从 `PIN_FIELDS` 抄回来的 ——
  抄回来就变成"代码等于代码"。
* **反向验证** = 每条正向配一条 `_negative`，共用同一条构造路径只改一个入参。
* **计数断言** = 字段条数、未分类字段数、missing 变量条数逐个等于手写值。
* ⏱ 全程只碰临时目录，不起任何工具（硬约束 3）。

🚨 本文件零站点标识符。
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest

from ewave_batch.core import sitepin
from ewave_batch.model import PortMode, PortSpec, SiteFacts, StateError

# --------------------------------------------------------------------------
# ★ 手写的假值（一个真实取值都没有）
# --------------------------------------------------------------------------

FAKE_PDK_ROOT = "/fake/pdk"
FAKE_EWAVE_ROOT = "/fake/pdk/apps/ewave"
FAKE_LAYER_MAP = "/fake/pdk/tech/fake.layermap"
FAKE_PTXT_DIR = "/fake/pdk/apps/ewave/ewaveinterface/process/Ver_X.Y/ptxt_enc"
FAKE_PTXT_NAME = "FAKEPROC_{corner}_encrypted_package.ptxt"
FAKE_KEY = "000000"

FAKE_ENV: dict[str, str] = {
    # 变量**名**是工具语义、可以进源码（硬约束 1b 的"通用的东西"那一条）；
    # 值全是假的。两个变量的值互相包含，正好用来验"长的先换"。
    "FAKE_PDK": FAKE_PDK_ROOT,
    "FAKE_EWAVE_ROOT": FAKE_EWAVE_ROOT,
    "FAKE_SHORT": "/x",  # 太短，`MIN_ENV_VALUE_CHARS` 该把它挡掉
    "FAKE_RELATIVE": "not/absolute",  # 不是绝对路径，同样挡掉
}

EXPECTED_PINNED_PTXT_DIR = "${FAKE_EWAVE_ROOT}/ewaveinterface/process/Ver_X.Y/ptxt_enc"
"""手写：`FAKE_EWAVE_ROOT` 比 `FAKE_PDK` 长 ⇒ 它赢。这一串是**手敲的**，
不是拿 `contract_env` 算出来的。"""

EXPECTED_UNCLASSIFIED = 0
"""`SiteFacts` 里没被两张表分类的字段数。**必须是 0**，理由见文件 docstring 第 2 条。"""

MUST_BE_PINNED: tuple[str, ...] = (
    # 手抄自 BRIEF：P9 说 env 只给到 ptxt 的根、版本目录和文件名模板拿不到；
    # D1c 说 gdsout 模板的非路径字段必须逐字复现；§11 规则 1 说的是默认表。
    "ptxt_dir",
    "ptxt_name_template",
    "gdsout_template",
    "production_flags",
    "key",
)
"""不钉住这几样，official 就降不了级 —— 它们没有第二个来源。"""

MUST_NEVER_BE_PINNED: tuple[str, ...] = (
    "official_port_spec",  # 🚨 红线
    "library",
    "top_cell",
    "view",
    "corner",
    "temperature",
)
"""钉住任何一样都会让工具**静默地**用错坐标。"""


def _facts(**overrides: object) -> SiteFacts:
    """一份填满了的假 `SiteFacts`。正反两向共用，只改传进来的那一个字段。"""
    base = SiteFacts(
        official_run_dir="/fake/wa/ewave_simulation/design",
        library="FAKELIB",
        top_cell="FAKECELL",
        view="fakeview",
        layer_map=FAKE_LAYER_MAP,
        gdsout_template="library {library}\nmaxVertices 200\n",
        ptxt=f"{FAKE_PTXT_DIR}/FAKEPROC_typical_encrypted_package.ptxt",
        ptxt_dir=FAKE_PTXT_DIR,
        ptxt_name_template=FAKE_PTXT_NAME,
        pdk_root=FAKE_PDK_ROOT,
        key=FAKE_KEY,
        corner="typical",
        temperature="-40.0",
        ewave_dir_name="typical_-40_0",
        production_flags={"--viaMode": "1", "--sweep": "3"},
        official_flags={"--corner": "typical"},
        dsub_account="fake_acct",
        dsub_queue="fakeq",
        dsub_resources="cpu=2;mem=100",
        ewave_bin="/fake/bin/ewave",
        strmout_bin="/fake/bin/strmout",
        official_port_spec=PortSpec(
            mode=PortMode.EXPLICIT, mapping=(("P000", "FAKEPINA"), ("P001", "FAKEPINB"))
        ),
    )
    for name, value in overrides.items():
        setattr(base, name, value)
    return base


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ewb_pin_")
        self.addCleanup(self._tmp.cleanup)

    def root(self) -> str:
        return self._tmp.name.replace("\\", "/")


# ==========================================================================
# 1. 分类必须穷尽
# ==========================================================================


class Classification(unittest.TestCase):
    """两张表加起来 == `SiteFacts` 的全部字段。**这是本方案的结构性防线。**"""

    def _all_fields(self) -> set[str]:
        return {f.name for f in dataclasses.fields(SiteFacts)}

    def test_every_field_is_classified(self) -> None:
        """★ 计数断言：未分类的字段恰好 0 个。

        将来谁给 `SiteFacts` 加字段，会在这里被拦下来，逼他回答一句
        「这个东西换个 design 变不变」—— 而那正是能不能钉的判据。
        """
        classified = set(sitepin.PIN_FIELDS) | set(sitepin.NEVER_PIN_FIELDS)
        unclassified = sorted(self._all_fields() - classified)
        self.assertEqual(len(unclassified), EXPECTED_UNCLASSIFIED, unclassified)

    def test_every_field_is_classified_negative(self) -> None:
        """反向：两张表里不许有 `SiteFacts` 上根本不存在的字段名。

        没有这条的话，把一个字段**改名**之后上面那条照样绿（旧名字还在表里凑数），
        而那个字段实际上已经没人分类了。
        """
        classified = set(sitepin.PIN_FIELDS) | set(sitepin.NEVER_PIN_FIELDS)
        stale = sorted(classified - self._all_fields())
        self.assertEqual(stale, [])

    def test_the_two_tables_do_not_overlap(self) -> None:
        """同一个字段不许既钉又不钉 —— 那种表读起来像有结论，其实没有。"""
        both = set(sitepin.PIN_FIELDS) & set(sitepin.NEVER_PIN_FIELDS)
        self.assertEqual(sorted(both), [])

    def test_the_fields_that_have_no_second_source_are_pinned(self) -> None:
        """手写清单：这几样没有第二个来源，不钉住 official 就降不了级。"""
        for name in MUST_BE_PINNED:
            with self.subTest(field=name):
                self.assertIn(name, sitepin.PIN_FIELDS)


# ==========================================================================
# 2. 🚨 端口表这条红线
# ==========================================================================


class PortTableIsTheRedLine(_Tmp):
    """端口表**连存都不存**。不是"存了但不用"，是钉文件里根本没有它。"""

    def test_the_port_table_never_reaches_the_pin_file(self) -> None:
        """★ 关键测试：钉一份填满了的 facts，端口名一个字节都不许出现在文件里。

        判据故意下沉到**文件文本**而不是字典的键：将来有人换个键名把它塞进去，
        看键名的断言会绿，看文本的不会。
        """
        path = sitepin.save_pin(
            sitepin.pin_path(self.root()), _facts(), env=dict(FAKE_ENV)
        )
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("FAKEPINA", text)
        self.assertNotIn("FAKEPINB", text)
        self.assertNotIn("official_port_spec", text)

    def test_the_port_table_never_reaches_the_pin_file_negative(self) -> None:
        """反向：同一条路，**该钉的**那几样必须真的在文件里。

        没有这条，一个"什么都不写"的实现也能让上面那条绿。
        """
        path = sitepin.save_pin(
            sitepin.pin_path(self.root()), _facts(), env=dict(FAKE_ENV)
        )
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn(FAKE_PTXT_NAME, text)
        self.assertIn(FAKE_KEY, text)
        self.assertIn("maxVertices 200", text)

    def test_resolving_a_pin_never_produces_a_port_table(self) -> None:
        """读回来的 facts 上，端口表必须是空的 —— 每次现读，绝不来自缓存。"""
        data = sitepin.pin_from_facts(_facts(), env=dict(FAKE_ENV))
        facts, _sources, _missing = sitepin.resolve_pinned(data, env=dict(FAKE_ENV))
        self.assertEqual(facts.official_port_spec.mapping, ())

    def test_nothing_per_design_is_pinned(self) -> None:
        """per-design 的那几样一律不进钉文件（换个 Cell 它们全变）。"""
        data = sitepin.pin_from_facts(_facts(), env=dict(FAKE_ENV))
        for name in MUST_NEVER_BE_PINNED:
            with self.subTest(field=name):
                self.assertNotIn(name, data)


# ==========================================================================
# 3. 环境变量：值 <-> 引用
# ==========================================================================


class EnvContraction(unittest.TestCase):
    """`contract_env` —— 存引用不存值，钉文件里的真实坐标因此更少。"""

    def test_the_longest_matching_variable_wins(self) -> None:
        """★ 两个变量的值互相包含时，长的赢。

        短的先换会把长的切碎，剩下半截既不是引用也不是完整路径 ——
        而那是一条**看起来合法的**错路径。
        """
        self.assertEqual(
            sitepin.contract_env(FAKE_PTXT_DIR, FAKE_ENV), EXPECTED_PINNED_PTXT_DIR
        )

    def test_the_longest_matching_variable_wins_negative(self) -> None:
        """反向：把长的那个变量拿掉 ⇒ 换成短的那个，**而且仍然是一条完整路径**。"""
        env = {k: v for k, v in FAKE_ENV.items() if k != "FAKE_EWAVE_ROOT"}
        contracted = sitepin.contract_env(FAKE_PTXT_DIR, env)
        self.assertTrue(contracted.startswith("${FAKE_PDK}/"), contracted)
        expanded, missing = sitepin.expand_env(contracted, env)
        self.assertEqual(expanded, FAKE_PTXT_DIR)
        self.assertEqual(missing, ())

    def test_short_and_relative_values_are_ignored(self) -> None:
        """`/x` 这种短值是几乎所有路径的前缀，换进去就是静默的路径改写。"""
        self.assertEqual(sitepin.contract_env("/x/y/z", FAKE_ENV), "/x/y/z")
        self.assertEqual(sitepin.contract_env("not/absolute/at/all", FAKE_ENV), "not/absolute/at/all")

    def test_round_trip(self) -> None:
        """收缩再展开 == 原文。这两个函数互为逆，测试里把这条钉住。"""
        contracted = sitepin.contract_env(FAKE_PTXT_DIR, FAKE_ENV)
        expanded, missing = sitepin.expand_env(contracted, FAKE_ENV)
        self.assertEqual(expanded, FAKE_PTXT_DIR)
        self.assertEqual(missing, ())


class EnvExpansion(unittest.TestCase):
    """`expand_env` —— 解不出来的变量**原样留着**，并且报出来。"""

    def test_an_unresolved_variable_stays_in_the_text_and_is_reported(self) -> None:
        """★ 关键：不是换成空串。

        换成空串会把 `${X}/ptxt` 变成 `/ptxt` —— 一条**看起来合法的错路径**，
        比一条明显没展开的路径难查得多（而且它会被真的传给 eWave）。
        """
        text, missing = sitepin.expand_env("${NOT_SET_ANYWHERE}/ptxt", {})
        self.assertEqual(text, "${NOT_SET_ANYWHERE}/ptxt")
        self.assertEqual(missing, ("NOT_SET_ANYWHERE",))

    def test_an_unresolved_variable_stays_in_the_text_and_is_reported_negative(self) -> None:
        """反向：同一条路，变量给了 ⇒ 换掉，且**不许**再报 missing。"""
        text, missing = sitepin.expand_env(
            "${FAKE_EWAVE_ROOT}/ptxt", {"FAKE_EWAVE_ROOT": FAKE_EWAVE_ROOT}
        )
        self.assertEqual(text, f"{FAKE_EWAVE_ROOT}/ptxt")
        self.assertEqual(missing, ())

    def test_both_spellings_work(self) -> None:
        """`$VAR` 和 `${VAR}` 都认 —— 人手写钉文件时两种都会出现。"""
        env = {"FAKE_EWAVE_ROOT": FAKE_EWAVE_ROOT}
        for spelling in ("$FAKE_EWAVE_ROOT/p", "${FAKE_EWAVE_ROOT}/p"):
            with self.subTest(spelling=spelling):
                text, _ = sitepin.expand_env(spelling, env)
                self.assertEqual(text, f"{FAKE_EWAVE_ROOT}/p")

    def test_a_doubled_dollar_stays_literal(self) -> None:
        """`$$FOO` 是字面量（与 shell 同义）—— 照抄 Auto_ext `core/env.py` 的口径。"""
        text, missing = sitepin.expand_env("$$FAKE_EWAVE_ROOT", FAKE_ENV)
        self.assertEqual(text, "$$FAKE_EWAVE_ROOT")
        self.assertEqual(missing, ())


# ==========================================================================
# 4. 来源三态
# ==========================================================================


class Sources(unittest.TestCase):
    """每个字段说得出自己是**钉的 / env 给的 / 根本没有**。

    这是本方案对「钉住的值会过期」那个老问题的全部回答：不承诺检测过期，
    承诺让人一眼看出这个值是钉的还是现读的。所以这三态必须分得开。
    """

    def test_the_three_states_are_told_apart(self) -> None:
        data = sitepin.pin_from_facts(
            _facts(gdsout_template=""), env=dict(FAKE_ENV)
        )
        _facts_out, sources, _missing = sitepin.resolve_pinned(data, env=dict(FAKE_ENV))
        # ptxt_dir 收缩成了 ${FAKE_EWAVE_ROOT}/... ⇒ 展开时变了 ⇒ env
        self.assertEqual(sources["ptxt_dir"], sitepin.SOURCE_ENV)
        # key 里没有 `$` ⇒ 纯钉住
        self.assertEqual(sources["key"], sitepin.SOURCE_PINNED)
        # 上面显式清空的那个 ⇒ missing
        self.assertEqual(sources["gdsout_template"], sitepin.SOURCE_MISSING)

    def test_the_three_states_are_told_apart_negative(self) -> None:
        """反向：同一条路，把那个字段填上 ⇒ 它不许再是 `missing`。"""
        data = sitepin.pin_from_facts(_facts(), env=dict(FAKE_ENV))
        _facts_out, sources, _missing = sitepin.resolve_pinned(data, env=dict(FAKE_ENV))
        self.assertNotEqual(sources["gdsout_template"], sitepin.SOURCE_MISSING)

    def test_an_env_that_lost_a_variable_reports_it_instead_of_going_quiet(self) -> None:
        """★ 钉在 A 机器、拿到 B 机器上跑（B 没有那个变量）⇒ **报名字**，不静默。

        这是钉文件跨机器时唯一会出事的地方，也是它必须出声的地方。
        """
        data = sitepin.pin_from_facts(_facts(), env=dict(FAKE_ENV))
        _facts_out, _sources, missing = sitepin.resolve_pinned(data, env={})
        self.assertIn("FAKE_EWAVE_ROOT", missing)


# ==========================================================================
# 5. 落盘 / 读回
# ==========================================================================


class SaveAndLoad(_Tmp):
    def test_round_trip_through_the_file(self) -> None:
        path = sitepin.save_pin(sitepin.pin_path(self.root()), _facts(), env=dict(FAKE_ENV))
        facts, _sources, _missing = sitepin.resolve_pinned(
            sitepin.load_pin(path), env=dict(FAKE_ENV)
        )
        self.assertEqual(facts.ptxt_dir, FAKE_PTXT_DIR)
        self.assertEqual(facts.ptxt_name_template, FAKE_PTXT_NAME)
        self.assertEqual(facts.production_flags, {"--viaMode": "1", "--sweep": "3"})

    def test_a_missing_file_is_a_normal_state_not_an_error(self) -> None:
        """全新机器上还没钉过 —— 空字典，**不抛**。"""
        self.assertEqual(sitepin.load_pin(sitepin.pin_path(self.root())), {})

    def test_a_broken_file_is_an_error(self) -> None:
        """读不懂的配置比没有配置危险：它让人以为坐标已经配好了。"""
        path = sitepin.pin_path(self.root())
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("{ this is not json")
        with self.assertRaises(StateError):
            sitepin.load_pin(path)

    def test_a_future_schema_is_refused_rather_than_guessed(self) -> None:
        path = sitepin.pin_path(self.root())
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"schema_version": sitepin.PIN_SCHEMA_VERSION + 1}, handle)
        with self.assertRaises(StateError) as caught:
            sitepin.load_pin(path)
        self.assertIn("schema_version", str(caught.exception))

    def test_the_write_is_atomic(self) -> None:
        """写完不留临时文件 —— 跑到一半断电不能留半份 JSON（同 `layout.write_batch_state`）。"""
        sitepin.save_pin(sitepin.pin_path(self.root()), _facts(), env=dict(FAKE_ENV))
        leftovers = [n for n in os.listdir(self.root()) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_empty_values_are_left_out_rather_than_written_as_empty(self) -> None:
        """空值不写进去：`"key": ""` 读回来会被报成 `pinned` 而不是 `missing`，
        而那正好是本方案最不该说谎的地方。"""
        data = sitepin.pin_from_facts(_facts(key=""), env=dict(FAKE_ENV))
        self.assertNotIn("key", data)


# ==========================================================================
# 6. 官方目录赢过钉住的 —— 但只在它真给了值的字段上
# ==========================================================================


class MergePrecedence(unittest.TestCase):
    def test_the_official_dir_wins_field_by_field(self) -> None:
        pinned = SiteFacts(ptxt_dir=FAKE_PTXT_DIR, key=FAKE_KEY)
        live = SiteFacts(key="999999")
        merged = sitepin.merge_facts(pinned, live)
        self.assertEqual(merged.key, "999999")

    def test_a_partial_official_dir_does_not_wipe_the_pinned_values(self) -> None:
        """★ 关键：官方目录**可以是残缺的**（`discover_site_facts` 的软失败契约）。

        整份顶掉的话，一个只在本地跑过、没有 `remote_run_ewave.sh` 的目录
        会把钉好的坐标打回原形 —— 而"official 又变成必填的了"正是方案 A 要消灭的状态。
        """
        pinned = SiteFacts(ptxt_dir=FAKE_PTXT_DIR, key=FAKE_KEY, dsub_queue="fakeq")
        live = SiteFacts(key="999999")  # 只解析出了 key，其余全空
        merged = sitepin.merge_facts(pinned, live)
        self.assertEqual(merged.ptxt_dir, FAKE_PTXT_DIR)
        self.assertEqual(merged.dsub_queue, "fakeq")

    def test_the_port_table_comes_only_from_the_live_side(self) -> None:
        """端口表只可能来自现读的那一份（钉住的那份按定义没有它）。"""
        live_spec = PortSpec(mode=PortMode.EXPLICIT, mapping=(("P000", "FAKEPINA"),))
        merged = sitepin.merge_facts(SiteFacts(), SiteFacts(official_port_spec=live_spec))
        self.assertEqual(merged.official_port_spec.mapping, (("P000", "FAKEPINA"),))


# ==========================================================================
# 7. 端到端：钉过之后，official 真的可以不填
# ==========================================================================


class OfficialBecomesOptional(_Tmp):
    """★ 方案 A 的**验收判据**。前面六节都是零件，这一节是那句承诺本身。

    用户 2026-08-28 的话：「official 地址…感觉完全可以不填了呀」。
    机器判据只有一条：**同一份坐标，钉过之后，一个空的 official 也能过 preflight。**
    """

    def _bridge(self, *, official: str, pin_path: str) -> object:
        """走界面那条路造一个 bridge。正反两向共用，只改 `official` 一个入参。

        `discover` 注入 —— 本机没有官方 run 目录（硬约束 3），
        而本节要证明的正是"没有它也能跑"，所以这份假 facts 就是全部输入。
        """
        from gui.state import GuiState

        return GuiState(
            batch_root=self.root() + "/batches",
            batch_name="b",
            official_run_dir=official,
            env={"EWB_SITE_FACTS": pin_path},
            discover=lambda _path: _facts(),
        )

    def _official_problem(self, bridge: object) -> str:
        """preflight 里那条抱怨 official 的（没有就空串）。

        只挑这一条：preflight 还会抱怨"没有 design"之类，拿整个列表断言会把
        本节的判据和别的规则绑在一起。
        """
        for problem in bridge.preflight(dry_run=True):  # type: ignore[attr-defined]
            if "Official run dir" in problem:
                return problem
        return ""

    def test_a_fresh_box_with_no_official_dir_is_blocked(self) -> None:
        """对照组：全新机器、没填 official、没钉过 ⇒ preflight **必须**拦。

        没有这条，下面那条"钉过就放行"可能只是因为 preflight 根本不检查这件事。
        """
        bridge = self._bridge(official="", pin_path=sitepin.pin_path(self.root()))
        self.assertIn("no site coordinates pinned", self._official_problem(bridge))

    def test_after_adopting_an_empty_official_dir_no_longer_blocks(self) -> None:
        """★ 关键测试：钉一次 → **换一个没有 official 的新 bridge** → 不再拦。

        必须换一个新 bridge：同一个对象上问，答案可能来自内存里那份，
        而这套东西的全部意义在于**下次开界面**（甚至下次开机）时它还在。
        """
        pin_path = sitepin.pin_path(self.root())
        first = self._bridge(official="/fake/wa/ewave_simulation/design", pin_path=pin_path)
        first.adopt_site_facts()  # type: ignore[attr-defined]

        later = self._bridge(official="", pin_path=pin_path)
        self.assertTrue(later.site_facts_are_pinned())  # type: ignore[attr-defined]
        self.assertEqual(self._official_problem(later), "")

    def test_a_pin_file_written_through_the_gui_still_has_no_port_table(self) -> None:
        """🚨 红线在**真实那条路**上也成立。

        第 2 节验的是 `save_pin` 这个函数，这条验的是"界面按下 Adopt"这条路 ——
        中间隔着 `_live_facts_for_adopt` / `pin_from_facts`，任何一环把端口表捞进去
        都会在这里红。
        """
        pin_path = sitepin.pin_path(self.root())
        bridge = self._bridge(official="/fake/wa/ewave_simulation/design", pin_path=pin_path)
        written = bridge.adopt_site_facts()  # type: ignore[attr-defined]
        with open(written, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("FAKEPINA", text)
        self.assertNotIn("FAKEPINB", text)

    def test_the_official_dir_still_wins_when_it_is_given(self) -> None:
        """钉住的是**垫底的**，不是锁：官方目录一给，它的值照样赢。

        这条守的是方案 A 最容易走偏的方向 —— 钉完之后再也读不进新坐标，
        那就成了"永远用着一份旧快照"，而症状是静默的。
        """
        pin_path = sitepin.pin_path(self.root())
        first = self._bridge(official="/fake/wa/ewave_simulation/design", pin_path=pin_path)
        first.adopt_site_facts()  # type: ignore[attr-defined]

        from gui.state import GuiState

        newer = SiteFacts(key="999999", ptxt_dir="/fake/pdk/newer/ptxt_enc")
        bridge = GuiState(
            batch_root=self.root() + "/batches",
            batch_name="b",
            official_run_dir="/fake/wa/ewave_simulation/design",
            env={"EWB_SITE_FACTS": pin_path},
            discover=lambda _path: newer,
        )
        bridge.add_design("FAKELIB", "FAKECELL", "fakeview")
        facts = bridge._facts_for(bridge._designs[0])
        self.assertEqual(facts.key, "999999")
        self.assertEqual(facts.ptxt_dir, "/fake/pdk/newer/ptxt_enc")

    def test_a_partial_official_dir_does_not_wipe_the_pin(self) -> None:
        """反向：官方目录**残缺**（只解析出 key）⇒ 其余字段仍由钉住的补上。

        `discover_site_facts` 的软失败契约允许残缺（比如只在本地跑过、
        没有 `remote_run_ewave.sh`）。整份顶掉的话，一个残缺目录会把钉好的坐标
        打回原形 —— 而"official 又变成必填的了"正是方案 A 要消灭的状态。
        """
        pin_path = sitepin.pin_path(self.root())
        first = self._bridge(official="/fake/wa/ewave_simulation/design", pin_path=pin_path)
        first.adopt_site_facts()  # type: ignore[attr-defined]

        from gui.state import GuiState

        bridge = GuiState(
            batch_root=self.root() + "/batches",
            batch_name="b",
            official_run_dir="/fake/wa/ewave_simulation/design",
            env={"EWB_SITE_FACTS": pin_path},
            discover=lambda _path: SiteFacts(key="999999"),
        )
        bridge.add_design("FAKELIB", "FAKECELL", "fakeview")
        facts = bridge._facts_for(bridge._designs[0])
        self.assertEqual(facts.key, "999999")
        self.assertEqual(facts.ptxt_name_template, FAKE_PTXT_NAME)
        self.assertEqual(facts.gdsout_template, "library {library}\nmaxVertices 200\n")

    def test_forget_puts_the_box_back_to_asking(self) -> None:
        """`Forget` 之后回到"要填 official"的状态 —— 这条让 Adopt 可逆。"""
        pin_path = sitepin.pin_path(self.root())
        bridge = self._bridge(official="/fake/wa/ewave_simulation/design", pin_path=pin_path)
        bridge.adopt_site_facts()  # type: ignore[attr-defined]
        self.assertTrue(bridge.site_facts_are_pinned())  # type: ignore[attr-defined]
        bridge.forget_site_facts()  # type: ignore[attr-defined]
        self.assertFalse(bridge.site_facts_are_pinned())  # type: ignore[attr-defined]

    def test_preview_lists_what_would_change_and_writes_nothing(self) -> None:
        """预览**不写盘**（抄 Auto_ext `core/env_import` 的自我约束）。

        判据是两半：列得出会变的行 + 文件仍然不存在。只验前一半的话，
        一个"顺手先写了再说"的实现照样绿，而那正是那条约束要防的。
        """
        pin_path = sitepin.pin_path(self.root())
        bridge = self._bridge(official="/fake/wa/ewave_simulation/design", pin_path=pin_path)
        rows = bridge.site_facts_preview()  # type: ignore[attr-defined]
        self.assertTrue(rows)
        self.assertFalse(os.path.exists(pin_path))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
