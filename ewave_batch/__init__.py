"""ewave_batch —— eWave 批量驱动的包根。

⚠️ **这个文件必须保持空/极薄：不许 import 任何子模块。**
理由是 CLAUDE.md 硬约束 5：GUI 的 import 必须惰性，无 `$DISPLAY` 的纯 ssh 会话里 CLI 要能用。
包根一旦 `from . import cli`，惰性就没了 —— 而且这种破坏是静默的，只有到红区才发作。

接口冻结面在 `ewave_batch.model`，冻结清单是 `ewave_batch.model.FROZEN`。
漂移检测：`python -m ewave_batch dry-run --self-test`
"""

__version__ = "0.1.0.dev0"
"""工具版本，进 `Provenance.tool_version`。
仓库根的 `VERSION` 文件是 `git archive` 时打的 commit 戳，两者不是一回事。"""
