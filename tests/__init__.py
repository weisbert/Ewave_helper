"""测试包。这里只做一件事：**把本机的 `site.local.sh` 挡在测试之外。**

`gui.state.default_submit_command()` 会去装机目录根找 `site.local.sh`，好让界面开局
那格直接是本站点真实的 dsub 命令（用户 2026-08-20）。而仓库根**就是**开发机的装机
目录根 —— 于是开发机上一装这个文件，整套单测的默认值就跟着变了：

* 断言默认值的测试当场红，而失败信息里会把**真实的账号/队列**打进 CI 日志；
* 更坏的一种：测试仍然绿，只是绿的理由变了（默认值被站点前缀顶掉，看不出来），
  于是"没装 site.local 的机器上还对不对"这件事再也没人验。

两条都属于「测试结果取决于开发机上有没有某个未跟踪文件」，那是不可复现。
所以在**包被 import 的那一刻**就把查找路径钉死到一个不可能存在的文件上。

⚠️ 必须放在 `tests/__init__.py`，不能放 `conftest.py`：本项目的闸门跑的是
`python -m unittest discover`（`scripts/check.sh` 第 2 步、`deploy/doctor.sh --test`），
不是 pytest —— `conftest.py` 在那两条路上根本不会被加载。

要**测** site-local 那条路的测试，自己传一份 `env` 指向临时文件
（`GuiState(env=...)` / `default_submit_command(env)`），不受这里影响。
"""

from __future__ import annotations

import os

from gui.state import SITE_LOCAL_ENV

os.environ[SITE_LOCAL_ENV] = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "no_such_site.local.sh"
)
"""指到一个**存在的目录下的不存在文件**：`site_local_path()` 对显式路径的规则是
「找不到就是找不到，不再往下猜」，所以这一句同时也把「装机目录根」和「当前工作目录」
两个兜底位置关掉了 —— 这正是我们要的，而且它顺带就是那条规则的活样例。
"""
