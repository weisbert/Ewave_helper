# -*- coding: utf-8 -*-
"""三版布局共用的假数据 + run 展开逻辑。

界面草图专用：不接后端，没有 ewave / dsub，不写任何文件。
所有数据都是编的，不含任何站点标识符（见 CLAUDE.md 硬约束 1b）。
"""

# ---------------------------------------------------------------- fake data

DESIGN_ROWS = [
    ("MY_RF_LIB", "IND_TOP_A", "layout_em_sim"),
    ("MY_RF_LIB", "IND_TOP_B", "layout_em_sim"),
]

DEFAULT_DESIGNS = DESIGN_ROWS      # 旧名，v3_spec_file.py 还在用

CORNERS = ["typical", "cbest", "cworst", "rcbest", "rcworst"]
MODES = ["Quasi-static", "Full wave"]
SWEEP_MODES = ["adaptive", "linear", "logarithmic", "discrete"]

DEFAULT_DSUB = 'dsub -A <account> -q <queue> -R "cpu=20;mem=100000"'
BATCH_ROOT = "/proj/<area>/ewave_batches"

# 第 2 层：有默认值、不上主界面、可在 Tools → Extraction defaults… 里改。
# 真实实现里这张表不写死在源码，而是第一次运行时从官方 run 目录学来的。
SITE_DEFAULTS = [
    ("--viaMode", "1", "learned from official run dir"),
    ("--sparamImpedance", "50", "learned from official run dir"),
    ("--labelDepth", "0", "learned from official run dir"),
    ("--sweep", "3", "eWave default (KMOR fitting)"),
    ("--simulationMode", "1", "eWave default (normal)"),
    ("--maxIterNum", "1000", "eWave default"),
]

# 第 0 层：锁死，界面上根本不出现 —— 改了工具自身的机制就失效
LOCKED_FLAGS = ["--nogui", "-m", "--workDir", "--gds", "--top", "--cadencePins",
                "--all", "--includePortOrder", "--sparam", "--emssTechFile"]

# 界面上的轴 —— 它们参与 run 的身份（进目录名），所以 extra flags 里不许再出现
AXIS_FLAGS = ["--corner", "--temperature", "--fullWave", "--multiSweep",
              "--logarithmicSweep", "--discreteFreq", "-e", "-d",
              "--viaMergeSpace", "--equalCurrent", "--relativeTolerance",
              "--relativeCurrentTolerance", "--parallel"]


# ------------------------------------------------------------ small helpers

def parse_list(text):
    """'-40, 55 125' -> ['-40', '55', '125']"""
    return [t for t in text.replace(",", " ").split() if t]


def temp_dirname(t):
    """eWave 自己建的目录名：-40 -> -40_0"""
    try:
        return ("%.1f" % float(t)).replace(".", "_")
    except ValueError:
        return str(t)


def parse_cpu(dsub_line):
    """从 dsub 那一整行里抠出 cpu=N —— --parallel 要跟着它走。"""
    i = dsub_line.find("cpu=")
    if i < 0:
        return None
    num = ""
    for ch in dsub_line[i + 4:]:
        if ch.isdigit():
            num += ch
        else:
            break
    return int(num) if num else None


def sweep_flag(mode, start, stop, step, points):
    """四个格子 -> --multiSweep / --logarithmicSweep / --discreteFreq"""
    if mode == "logarithmic":
        return "--logarithmicSweep=%s" % (stop or "40")
    if mode == "discrete":
        return "--discreteFreq='%s'" % (start or "5")
    kind = "adaptive" if mode == "adaptive" else "lin"
    if points:
        return "--multiSweep=%s,%s-%s-%s" % (kind, start, points, stop)
    return "--multiSweep=%s,%s:%s:%s" % (kind, start, step, stop)


def conflicting_flags(extra):
    """extra flags 里撞上轴的那些 —— 撞了目录名就会和实际值对不上。"""
    hits = []
    for tok in extra.split():
        name = tok.split("=")[0]
        if name in AXIS_FLAGS and name not in hits:
            hits.append(name)
    return hits


def wall_to_secs(w):
    if not w or ":" not in w:
        return 0
    m, s = w.split(":")
    return int(m) * 60 + int(s)


def secs_to_wall(secs):
    return "%d:%02d" % (secs // 60, secs % 60)


# --------------------------------------------------------------- expansion

class Run(object):
    def __init__(self, design, corner, temp, mode, mesh, tol, eq):
        self.design = design          # (lib, cell, view)
        self.corner = corner
        self.temp = temp
        self.mode = mode              # 'Quasi-static' / 'Full wave'
        self.mesh = mesh              # (edge, vert, viaMerge)
        self.tol = tol                # (relTol, relCurTol)
        self.eq = eq                  # bool
        self.status = "ready"
        self.wall = ""
        self.jobid = ""

    def mode_short(self):
        return "full wave" if self.mode == "Full wave" else "quasi-static"

    def axes_slug(self, varying):
        """除 corner/temp 之外真的在变的轴 —— 没有就是 base。"""
        parts = []
        if "mode" in varying:
            parts.append("fw-on" if self.mode == "Full wave" else "fw-off")
        if "mesh" in varying:
            parts.append("mesh-%s-%s-%s" % self.mesh)
        if "tol" in varying:
            parts.append("tol-%s" % self.tol[0])
        if "eq" in varying:
            parts.append("eqI-on" if self.eq else "eqI-off")
        return "__".join(parts) if parts else "base"

    def leaf(self):
        """★ 这一层是 eWave 自己建的，名字我们控制不了（BRIEF §7 P4b）。"""
        return "%s_%s" % (self.corner, temp_dirname(self.temp))

    def outdir(self, batch, varying):
        return "%s/%s/runs/%s/%s/%s/" % (
            BATCH_ROOT, batch, self.design[1], self.axes_slug(varying), self.leaf())

    def sparam_name(self):
        return "%s_%s.s17p" % (self.design[1], self.leaf())

    def command(self, sweep, parallel, extra=""):
        cell = self.design[1]
        f = ["ewave --nogui -m --workDir=.",
             "--gds=%s.gds --top=%s" % (cell, cell),
             "--cadencePins=1 --all --includePortOrder=1 --labelDepth=0",
             "--emssTechFile=<ptxt:%s>" % self.corner,
             sweep,
             "-e %s -d %s --viaMergeSpace=%s" % self.mesh,
             "--viaMode=1 --sparamImpedance=50",
             "--relativeTolerance=%s --relativeCurrentTolerance=%s" % self.tol]
        if self.eq:
            f.append("--equalCurrent")
        if self.mode == "Full wave":
            f.append("--fullWave")
        f.append("--parallel=%s" % parallel)
        f.append("--sparam=%s --corner=%s --temperature=%s"
                 % (cell, self.corner, self.temp))
        if extra.strip():
            f.append(extra.strip())
        return " ".join(f)


def expand(designs, corners, temps, modes, meshes, tols, eqs):
    """填几个值就跑几个 run —— 这就是全部的矩阵规则。"""
    runs = []
    for d in designs:
        for c in corners:
            for t in temps:
                for m in modes:
                    for mesh in meshes:
                        for tol in tols:
                            for eq in eqs:
                                runs.append(Run(d, c, t, m, mesh, tol, eq))
    return runs


def varying_axes(corners, temps, modes, meshes, tols, eqs):
    """哪些轴真的有多个值 —— 只有它们才进目录名。"""
    v = set()
    if len(modes) > 1:
        v.add("mode")
    if len(meshes) > 1:
        v.add("mesh")
    if len(tols) > 1:
        v.add("tol")
    if len(eqs) > 1:
        v.add("eq")
    return v
