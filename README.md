# Ewave_helper

在 eWave（商用 3D EM 场求解器）官方 GUI **外面**加一层批量驱动：定义若干提取设定的
组合，一次性批量跑到集群上，结果自动归档，后续可批量对比同一参数在不同设定下的差异。

官方 GUI 一次只能配一个设定、输出落默认目录、要的文件得手动 copy —— 这三件事由本工具接掉。

**状态：设计阶段。** 界面草图已经能跑，后端尚未开写。

---

## 目录

```
mockups/            三版界面草图（tkinter，不接后端，可直接运行）
mvp/                一次性验证脚手架（已完成使命，保留作参考）
  redzone/            部署到目标机上跑的 shell 脚本；坐标全部运行时解析
references/checks/  支撑设计决定的可复现验证脚本（纯 stdlib）
scripts/            redzone_scan.sh —— 提交闸门；install_hooks.sh
CLAUDE.md           给 AI 助手的项目须知（也是最紧凑的一份人读简介）
```

设计文档（需求、决定表、架构、未决项）**不在本库内**，留在本机。

## 跑界面草图

```
python mockups\stacked.py     单窗口纵向堆叠
python mockups\tabbed.py      四个 tab
python mockups\split.py       左配置栏 + 右整高 Runs 表
```

三版内容相同，只在「run 数显示在哪」「Runs 表能留多少行」上分岔。
`Tools → Demo state` 可一键跳到四个状态。详见 `mockups/README.md`。

## 约束（写代码前必读）

1. **零站点标识符。** 主机名 / 工号 / 项目代号 / library / cell / view / 端口名 /
   PDK 版本串 / 集群账号与队列 / 工具绝对路径 —— 一律不进源码。
   两条替代路：site-local 配置（`*.example.sh` 进库，`*.local.sh` 不进），或
   **运行时发现**（从官方 run 目录解析、`command -v`、环境变量）。后者优先。
   工具语义（flag 名和数值，如 `-e 0.4`、`--viaMode=1`）不算标识符，可以进库。
2. **只用 stdlib。** 目标机无装包权限。GUI 用 tkinter。
   唯一例外：读用户手写 YAML 时用 PyYAML，惰性 import + JSON 退路。
3. **无法本地调试。** 开发机上没有 `ewave` / `dsub` / `strmout`。
   所以：纯逻辑核心 + 可注入 runner + 全程 `--dry-run` + golden 命令测试。
4. **GUI 的 import 必须惰性**，保证无 `$DISPLAY` 的纯 ssh 会话里 CLI 可用。

## 提交闸门

克隆后跑一次：

```
sh scripts/install_hooks.sh
```

装上 pre-commit hook，每次提交扫一遍站点标识符。扫描分两层：通用结构规则写在
`scripts/redzone_scan.sh` 里；站点词表放 `.redzone_patterns.local`，**不进本库**
（词表本身就是那份清单）。新机器上没有词表时闸门只跑通用规则，会打印提示。

手动全扫：`sh scripts/redzone_scan.sh`
