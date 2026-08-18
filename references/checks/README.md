# references/checks — 支撑设计决定的可复现验证

这里的脚本不是本项目的单元测试，而是**对外部事实的验证**。每个都对应
`PROJECT_BRIEF.md` 里一条设计决定，跑一下就能重新确认那条决定的依据还成立。
纯 stdlib，Windows 上直接 `python <脚本>` 即可。

| 脚本 | 验证什么 | 支撑 |
|---|---|---|
| `check_port_order.py` | 官方 GUI 的 `-p` 端口顺序**就是 pin 名的 case-sensitive ASCII 排序**（17/17 吻合，大小写不敏感排序被排除） | **D1b** —— 这是"`--all` 能逐位复现官方映射、于是 `.sNp` 全程不依赖 GUI"的全部依据 |
| `diff_ewave_help.py` | 2025.09.sp1 的 `ewave --help` 与 kit 里 2026-05-07 那份的差异：101 flag 零增删改，唯一差异是 `--cpw` 措辞 | **§7 P1** —— 生产默认 flag 组合无需返工 |

## 什么时候重跑

- **红区 eWave 升版后** → 重跑 `diff_ewave_help.py`（先把新的 `--help` 抓到
  `references/probes/`，改一下 `NEW` 指向），确认 flag 表没变。
- **换一个新 design、且它的 pin 名风格不同时**（例如出现下划线开头、数字开头、
  含 `<>` 总线位的 pin 名）→ 把那个 design 的 `-p` 列表填进 `check_port_order.py`
  重跑。ASCII 排序的结论目前只在**一个** 17 端口样本上验证过，
  边界字符（`_` 相对字母、数字相对字母）在那个样本里没有出现能区分的组合。
