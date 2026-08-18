"""闸门自检：证明 check.sh 真的能因为一条红测试而否决。

这条测试平时是绿的。想验证闸门本身还有没有牙，把 SHOULD_FAIL 改成 True 跑一次
check.sh —— 它必须报 RED。2026-08-18 就是这么抓到管道退出码那个洞的。
"""
import unittest

SHOULD_FAIL = False


class GateSelfCheck(unittest.TestCase):
    def test_gate_can_still_fail(self):
        self.assertFalse(SHOULD_FAIL, "闸门自检：这条红了说明 check.sh 的否决权是活的")
