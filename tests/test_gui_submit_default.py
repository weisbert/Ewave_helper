"""「Donau 在界面上哪里设置」—— 用户 2026-08-20 当面提的界面缺陷。

那一格（从前叫 `Submit command`，现在叫 `Donau submit` / `dsub command`）开局是**空的**，
而空输入框不告诉任何人它想要什么形状 ⇒ 用户在界面上根本找不到「Donau 设置」这件事。
改法是给一条模板默认值（`gui.state.DEFAULT_SUBMIT_COMMAND`）。

一个**默认值**同时要满足三件互相拉扯的事，本文件给每件配一对正/反测试
（`docs/OVERNIGHT.md` 配方 2：只有正向测试的话，无法区分「防护起作用」和「根本没这个问题」）：

1. **形状是真的** —— 模板本身能过 `sched.donau.parse_dsub_prefix`，flag 名与那边同源。
   模板要是过不了自己的解析器，用户改完两个词照样提交不了，而报错离病根很远。
2. **值是假的，且换不掉就不许真提交** —— 账号 / 队列是站点身份，不进源码（硬约束 1b）；
   放过占位符的话，这个默认值只是把「空着」换成「一批必然被 dsub 拒掉的作业」。
3. **一有站点坐标就让位** —— 官方 run 目录里那条提交前缀整条顶掉模板，
   而模板里那条**例子**资源串**不许**顶掉官方那条真的。

第 3 条是这次最容易写错的地方：「用户还没给过命令」从前的判据是 `submit_command`
为空串，模板一进来那个判据就永远为假 —— 于是站点前缀再也灌不进界面，
而模板里的 `cpu=20` 会悄悄决定 `--parallel`。承重的是 `GuiState._submit_is_template`。

第 4 节测的是**另一半**（用户 2026-08-20 选定的方案）：占位符模板只是兜底，装了
`site.local.sh` 的机器上开局那格直接就是本站点真实的那条 dsub 命令。
真值留在跑它的那台机器上，仓库里只有形状 —— 硬约束 1b 的两条路之一。
那一节的判据同样是「让位」：site.local 是**默认值**，不是锁，官方 run 目录赢过它，
用户手打的赢过所有人。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from ewave_batch.model import EwaveBatchError, PortMode, PortSpec, SiteFacts
from ewave_batch.sched import donau
from ewave_batch.sched.donau import parse_dsub_prefix
from ewave_batch.sched.fake import FakeRunner, FakeScheduler

import gui.state as gui_state
from gui.state import GuiState

# --------------------------------------------------------------------------
# ★ 手写的期望（防自证配方 2：期望值不许由被测代码算出来）
# --------------------------------------------------------------------------

EXPECTED_TEMPLATE_ARGV: list[str] = [
    "dsub",
    "-A",
    "ACCOUNT",
    "-q",
    "QUEUE",
    "-R",
    "cpu=20;mem=100000",
]
"""模板 shlex 分词之后必须逐字是这几个 token。

`-R` 的值抄自 `docs/spec_example.yaml` 那条**例子**串（不是红区实测值）；
账号 / 队列是占位符（硬约束 1b）。这张表手写出来是为了让「模板变了」这件事必须
有人显式改测试，而不是测试跟着代码一起漂。
"""

# --------------------------------------------------------------------------
# 假站点坐标（字段全是假路径 —— `SiteFacts` 里装的全是站点身份，硬约束 1b）
# --------------------------------------------------------------------------

FAKE_LIB = "TESTLIB"
FAKE_CELL = "CELLA"
FAKE_VIEW = "testview"
FAKE_PORT_NAMES = ("PIN_A", "PIN_B")
SITE_RESOURCES = "cpu=2;mem=100"
"""站点那条**真的**资源串。刻意与模板里的 `cpu=20` 不同 ——
两者相等的话，「谁顶掉谁」这条测试恒真。"""

# ⚠️ 本文件里的假账号一律**不带点**（`fake_acct` 而不是 `fake.acct`）。
# `scripts/redzone_scan.sh` 的通用规则之一是 `-A[ =][A-Za-z][A-Za-z0-9_]*\.[A-Za-z]`
# —— 「`-A` 后面跟一个带点的标识符」正是真实 Donau 账号的形状，闸门认形状、分不出真假。
# 换成带点的写法就会让整条提交闸红，而红的理由跟这个测试想证明的事毫无关系
# （这条注释本身踩过一次：写反例时把那个形状原样打了出来，闸门当场命中）。
# 真账号带不带点对被测代码没有任何影响（shlex 分词不看点）。


def _facts(
    official_run_dir: str, *, resources: str, account: str = "", queue: str = ""
) -> SiteFacts:
    return SiteFacts(
        official_run_dir=official_run_dir,
        ewave_bin="/tmp/fakebin/ewave",
        strmout_bin="/tmp/fakebin/strmout",
        layer_map="/tmp/fakepdk/layer.map",
        dsub_account=account,
        dsub_queue=queue,
        dsub_resources=resources,
        ptxt_dir="/tmp/fakepdk/ptxt",
        ptxt_name_template="fake_{corner}.ptxt",
        official_port_spec=PortSpec(
            mode=PortMode.EXPLICIT,
            mapping=tuple((f"P{i:03d}", name) for i, name in enumerate(FAKE_PORT_NAMES)),
        ),
    )


class _BridgeTest(unittest.TestCase):
    """每个测试一个干净的临时根目录 + 一个能 `plan()` 出东西的最小批次。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ewb_submit_")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name.replace("\\", "/")
        # 假 workarea：`strmout` 的 cwd 要能往上找到一份 `cds.lib`。
        area = f"{self.root}/wa"
        os.makedirs(area, exist_ok=True)
        with open(f"{area}/cds.lib", "w", encoding="utf-8", newline="\n") as handle:
            handle.write("DEFINE FAKE ./fake\n")
        self.offdir = f"{area}/ewave_simulation/design"

    def _bridge(
        self,
        *,
        resources: str,
        official: bool = True,
        fake_sched: bool = False,
        account: str = "",
        queue: str = "",
        env: dict[str, str] | None = None,
    ) -> GuiState:
        """一个只扫 corner x temperature 的最小批次。

        `official=False` ⇒ 完全没有站点坐标（模板留在原地，这是"新装的机器"那一档）。
        `fake_sched=False` ⇒ **不注入调度器**，`_make_scheduler()` 会走真的 donau 分支，
        占位符那道闸才在测试范围内（注入了假的就直接返回，闸门根本不执行）。
        """
        offdir = self.offdir if official else ""
        facts = _facts(offdir, resources=resources, account=account, queue=queue)
        runner = FakeRunner(port_count=len(FAKE_PORT_NAMES))
        bridge = GuiState(
            batch_root=self.root,
            batch_name="submit_batch",
            official_run_dir=offdir,
            scheduler=FakeScheduler(runner) if fake_sched else None,
            runner=runner,
            discover=lambda _path: facts,
            env=env,
        )
        bridge.set_axis_values("corner", ("typical",))
        bridge.set_axis_values("temperature", ("55.0",))
        for name in ("fullWave", "equalCurrent", "relativeTolerance", "relativeCurrentTolerance"):
            bridge.set_axis_values(name, ())
        bridge.add_design(FAKE_LIB, FAKE_CELL, FAKE_VIEW)
        return bridge


# ==========================================================================
# 1. 形状是真的 —— 模板过得了 donau 自己的解析器
# ==========================================================================


class TemplateShape(unittest.TestCase):
    def test_template_parses_as_a_dsub_prefix(self) -> None:
        """模板 = 一条**可执行命令的模板**，不是示意图。"""
        self.assertEqual(parse_dsub_prefix(gui_state.DEFAULT_SUBMIT_COMMAND), EXPECTED_TEMPLATE_ARGV)

    def test_template_parses_as_a_dsub_prefix_negative(self) -> None:
        """反向：同一个解析器必须**拒掉**一条形似而实非的模板。

        没有这条，上面那条测的可能只是「解析器什么都收」。这里用的两种写法各自都是
        真会发生的手滑：LSF 的习惯（`bsub` 开头）、以及把生产脚本那个 `-I` 粘进来。
        """
        for bad in ("bsub -A ACCOUNT -q QUEUE", "dsub -A ACCOUNT -q QUEUE -I ./run.sh"):
            with self.subTest(bad=bad):
                with self.assertRaises(EwaveBatchError):
                    parse_dsub_prefix(bad)

    def test_template_flags_come_from_donau(self) -> None:
        """flag 名不许在这边手抄一份 —— 抄了就会和 `sched.donau` 各走各的。"""
        argv = parse_dsub_prefix(gui_state.DEFAULT_SUBMIT_COMMAND)
        self.assertEqual(argv[0], donau.DSUB)
        self.assertEqual(
            [argv[1], argv[3], argv[5]],
            [donau.ACCOUNT_FLAG, donau.QUEUE_FLAG, donau.RESOURCE_FLAG],
        )

    def test_template_flags_come_from_donau_negative(self) -> None:
        """反向：改掉任何一个 flag 名，上面那条必须红。

        证明它盯的是「与 donau 同源」，不是「argv 有 7 个 token」。
        """
        wrong = gui_state.DEFAULT_SUBMIT_COMMAND.replace(donau.ACCOUNT_FLAG, "-P", 1)
        argv = parse_dsub_prefix(wrong)
        self.assertNotEqual(argv[1], donau.ACCOUNT_FLAG)


# ==========================================================================
# 2. 值是假的 —— 占位符不换掉就不许真提交（硬约束 1b 的另一半）
# ==========================================================================


class Placeholders(_BridgeTest):
    def test_untouched_template_is_reported_as_placeholders(self) -> None:
        bridge = self._bridge(resources="", official=False)
        self.assertEqual(bridge.submit_command_placeholders(), ("ACCOUNT", "QUEUE"))
        self.assertIn("placeholder", bridge.submit_command_error())

    def test_untouched_template_is_reported_as_placeholders_negative(self) -> None:
        """反向：两个都换成真值 ⇒ 一句红字都不许剩（否则界面永远在报警，等于没报）。"""
        bridge = self._bridge(resources="", official=False)
        bridge.set_submit_command("dsub -A my_acct -q my_queue -R cpu=4;mem=100")
        self.assertEqual(bridge.submit_command_placeholders(), ())
        self.assertEqual(bridge.submit_command_error(), "")

    def test_half_edited_command_still_counts_as_a_placeholder(self) -> None:
        """账号填了、队列忘了 —— 这条命令和模板已经不同，却照样一个 job 都提交不成。

        判据必须是逐 token 比对，不是「整条命令 == 模板」。
        """
        bridge = self._bridge(resources="", official=False)
        bridge.set_submit_command("dsub -A my_acct -q QUEUE -R cpu=4")
        self.assertEqual(bridge.submit_command_placeholders(), ("QUEUE",))

    def test_submit_is_refused_while_placeholders_remain(self) -> None:
        """真提交那条路必须**在发出任何东西之前**抛。"""
        bridge = self._bridge(resources="", official=True)
        with self.assertRaises(EwaveBatchError) as caught:
            bridge.start(dry_run=False)
        self.assertIn("ACCOUNT", str(caught.exception))
        self.assertFalse(bridge.is_running(), "抛了却还把自己置成 running")

    def test_submit_is_refused_while_placeholders_remain_negative(self) -> None:
        """反向：换成真账号 / 真队列之后，同一条路**不许**再抛。

        没有这条的话，「永远抛」和「按占位符抛」看起来一样。
        """
        bridge = self._bridge(resources="", official=True)
        bridge.set_submit_command("dsub -A my_acct -q my_queue -R cpu=4;mem=100")
        bridge.start(dry_run=False)
        self.assertTrue(bridge.is_running())

    def test_dry_run_is_allowed_with_the_template(self) -> None:
        """dry-run 一个字节都不发出去，而「先看看命令长什么样」正是模板的用途。"""
        bridge = self._bridge(resources="", official=True)
        bridge.start(dry_run=True)
        self.assertTrue(bridge.is_running())


# ==========================================================================
# 3. 一有站点坐标就让位（承重的是 `_submit_is_template`）
# ==========================================================================


class SiteWinsOverTemplate(_BridgeTest):
    def test_site_prefix_replaces_the_template(self) -> None:
        """官方 run 目录里解析出来的提交前缀比模板准 ⇒ 整条顶掉，让用户接着改。"""
        bridge = self._bridge(resources=SITE_RESOURCES, fake_sched=True)
        bridge.plan()
        self.assertNotIn("ACCOUNT", bridge.submit_command)
        self.assertIn(SITE_RESOURCES, bridge.submit_command)
        self.assertEqual(bridge.submit_command_placeholders(), ())

    def test_site_prefix_replaces_the_template_negative(self) -> None:
        """反向：用户先手写过一条 ⇒ 站点前缀**不许**把它盖掉。

        盖掉的话，用户每按一次 Dry-run，自己刚改的资源就被悄悄改回去。
        """
        bridge = self._bridge(resources=SITE_RESOURCES, fake_sched=True)
        bridge.set_submit_command("dsub -A my_acct -q my_queue -R cpu=6;mem=7")
        bridge.plan()
        self.assertIn("cpu=6;mem=7", bridge.submit_command)
        self.assertNotIn(SITE_RESOURCES, bridge.submit_command)

    def test_template_resources_do_not_override_the_site(self) -> None:
        """模板里的 `cpu=20` 是**例子**，不许决定 `--parallel`。

        这是引入默认值最贵的那个副作用：`--parallel` 会跟着 `-R` 的 `cpu=` 走
        （1:1），于是一条没人动过的模板能悄悄把 20 个核写进每一条 ewave 命令，
        而界面上「显示的」和「跑的」看起来完全一致。
        """
        bridge = self._bridge(resources=SITE_RESOURCES, fake_sched=True)
        bridge.plan()
        self.assertEqual(bridge.parallel(), 2)

    def test_template_resources_do_not_override_the_site_negative(self) -> None:
        """反向：用户**真改过**那个框 ⇒ 命令里的 `cpu=` 必须赢过站点那份。

        「模板让位」不许连「用户说了算」一起让掉。
        """
        bridge = self._bridge(resources=SITE_RESOURCES, fake_sched=True)
        bridge.set_submit_command("dsub -A my_acct -q my_queue -R cpu=6;mem=7")
        bridge.plan()
        self.assertEqual(bridge.parallel(), 6)


class FillTrigger(_BridgeTest):
    """站点前缀「什么时候」顶掉模板 —— 判据是三元组里**任意一个**，不是只看 `-R`。"""

    def test_account_and_queue_alone_are_enough_to_fill(self) -> None:
        """站点只给 `-A` / `-q`（资源走队列默认）也必须填上。

        账号和队列正是用户手打最烦、也最容易打错的两个 —— 只看 `-R` 的话，
        这种站点会一直停在占位符上，看起来像「工具不认识我的集群」。
        """
        bridge = self._bridge(
            resources="", account="my_acct", queue="my_queue", fake_sched=True
        )
        bridge.plan()
        self.assertEqual(bridge.submit_command_placeholders(), ())
        self.assertIn("my_acct", bridge.submit_command)
        self.assertIn("my_queue", bridge.submit_command)

    def test_account_and_queue_alone_are_enough_to_fill_negative(self) -> None:
        """反向：三元组一个都没解析到 ⇒ 模板**留在原地**。

        没有这条，上面那条会诱使人把判据放宽成「有 facts 就填」——
        而那会把整格换成一条光秃秃的 `dsub`（`_dsub_command_from` 对空 facts 的输出），
        比模板更没用，且看起来像是工具"发现"了什么。
        """
        bridge = self._bridge(resources="", fake_sched=True)
        bridge.plan()
        self.assertEqual(bridge.submit_command, gui_state.DEFAULT_SUBMIT_COMMAND)


# ==========================================================================
# 4. site.local.sh —— 真值留在机器上，源码里只有形状
# ==========================================================================


class SiteLocal(unittest.TestCase):
    """硬约束 1b 的两条路之一：站点身份不进源码，但也不该让用户天天重打。

    ⚠️ 本节每个测试**自带 `env`**，不吃 `tests/__init__.py` 那道全局闸 ——
    那道闸挡的是"读到开发机上真的那份"，而这里要测的正是"读得到一份"。
    """

    def _env_with(self, body: str) -> dict[str, str]:
        tmp = tempfile.TemporaryDirectory(prefix="ewb_site_")
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "site.local.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
        return {"EWB_SITE_LOCAL": path}

    def test_site_local_supplies_the_opening_default(self) -> None:
        """装了 site.local.sh 的机器上，开局那格直接是站点真实的那条 —— 一个字不用打。"""
        env = self._env_with(
            "# this box\n"
            'EWB_SUBMIT_COMMAND=\'dsub -A fake_acct -q bigq -R "cpu=8;mem=99"\'\n'
        )
        bridge = GuiState(batch_root="./x", batch_name="b", env=env)
        self.assertEqual(bridge.submit_command, 'dsub -A fake_acct -q bigq -R "cpu=8;mem=99"')
        self.assertEqual(bridge.submit_command_placeholders(), ())
        self.assertEqual(bridge.submit_command_error(), "")
        self.assertEqual(bridge.parallel(), 8)

    def test_site_local_supplies_the_opening_default_negative(self) -> None:
        """反向：没有 site.local.sh ⇒ 退回占位符模板。

        盯着两件事：① 不是空串（那是这次要修的原病）；② 不是别处捡来的值 ——
        `$EWB_SITE_LOCAL` 指到不存在的文件时**不许**再去装机目录/CWD 兜底找一个，
        否则"指错路径"的症状会变成"用了另一台机器的坐标"。
        """
        env = {"EWB_SITE_LOCAL": "/no/such/dir/site.local.sh"}
        bridge = GuiState(batch_root="./x", batch_name="b", env=env)
        self.assertEqual(bridge.submit_command, gui_state.DEFAULT_SUBMIT_COMMAND)
        self.assertEqual(bridge.submit_command_placeholders(), ("ACCOUNT", "QUEUE"))

    def test_parser_tolerates_a_hand_written_file(self) -> None:
        """这文件是人手写的，读它的是 GUI 的构造函数 —— 多一行怪东西不该让界面起不来。"""
        values = gui_state.parse_site_local(
            "# comment\n"
            "\n"
            "export EWB_SUBMIT_COMMAND='dsub -A a_b -q q'\n"
            "OFFDIR=/some/path   # trailing note\n"
            "UNKNOWN_KEY=whatever\n"
            "a line that is not an assignment\n"
        )
        self.assertEqual(values["EWB_SUBMIT_COMMAND"], "dsub -A a_b -q q")
        self.assertEqual(values["OFFDIR"], "/some/path")
        self.assertEqual(values["UNKNOWN_KEY"], "whatever")

    def test_parser_tolerates_a_hand_written_file_negative(self) -> None:
        """反向：引号**里**的 `#` 必须活下来。

        「切掉 `#` 之后的东西」这一步最容易多吃一口。路径里带 `#` 不常见但真实，
        而它被切掉的症状是一条看起来完全正常、只是短了一截的路径。
        """
        values = gui_state.parse_site_local('OFFDIR="/some/path#2/design"\n')
        self.assertEqual(values["OFFDIR"], "/some/path#2/design")

    def test_unreadable_site_local_falls_back_instead_of_raising(self) -> None:
        """指向一个目录（不是文件）⇒ 退回模板，**不抛** —— 配置坏了不该让 GUI 起不来。"""
        tmp = tempfile.TemporaryDirectory(prefix="ewb_site_")
        self.addCleanup(tmp.cleanup)
        env = {"EWB_SITE_LOCAL": tmp.name}
        self.assertEqual(gui_state.default_submit_command(env), gui_state.DEFAULT_SUBMIT_COMMAND)


class SiteLocalYieldsToTheRunDir(_BridgeTest):
    """site.local.sh 是**默认值**，不是锁 —— 官方 run 目录仍然赢过它。"""

    def _site_local_env(self, line: str) -> dict[str, str]:
        path = os.path.join(self.root, "site.local.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("EWB_SUBMIT_COMMAND='%s'\n" % line)
        return {"EWB_SITE_LOCAL": path}

    def test_official_run_dir_still_wins_over_site_local(self) -> None:
        """真正跑过的那条脚本比任何默认值都准 —— 包括 site.local.sh 里那条。

        承重点：「用户动过没有」的判据必须跟**这台机器的默认值**比，不是跟模板比。
        跟模板比的话，装了 site.local 的机器上开局就算"用户改过"，站点前缀再也进不来。
        """
        env = self._site_local_env('dsub -A stale_acct -q staleq -R "cpu=99;mem=1"')
        bridge = self._bridge(resources=SITE_RESOURCES, fake_sched=True, env=env)
        self.assertIn("stale_acct", bridge.submit_command)
        bridge.plan()
        self.assertNotIn("stale_acct", bridge.submit_command)
        self.assertEqual(bridge.parallel(), 2)

    def test_official_run_dir_still_wins_over_site_local_negative(self) -> None:
        """反向：用户手打过之后，站点前缀**不许**再盖 —— 让位的是默认值，不是用户。"""
        env = self._site_local_env('dsub -A stale_acct -q staleq -R "cpu=99;mem=1"')
        bridge = self._bridge(resources=SITE_RESOURCES, fake_sched=True, env=env)
        bridge.set_submit_command('dsub -A my_acct -q my_queue -R "cpu=6;mem=7"')
        bridge.plan()
        self.assertIn("my_acct", bridge.submit_command)
        self.assertEqual(bridge.parallel(), 6)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
