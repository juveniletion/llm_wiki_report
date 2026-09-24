# -*- coding: utf-8 -*-
"""
graph_build.py — **因果知识图谱（派生视图）**

定位
----
图谱是**派生视图**，与 `state/`、`.index/`、SQLite 镜像同级：

    raw/ + wiki/  ← 唯一事实源
          ↓ 脚本抽取（每条边带坐标）
        graph/    ← 派生、可再生、**绝不手改**

**与 wiki 冲突时以 wiki 为准。**

为什么不手写图谱
----------------
本项目的 `demo/graph_kg.py` 是手写的，里面有一条因果链：

    金银花 → 水煎醇沉 → TQ-01提取罐 → 5月热电偶漂移 → 金银花消耗超标

逐项核对 raw 后确认**整条链都是编的**：`TQ-01` 编号不存在（真实 `EQ-TQ-001`）、
维修记录里没有提取罐故障、`+3.88%` 也算不出来、连出处文件名都是虚构的。

⚠️ **图谱比普通文档更危险**：普通文档编一个数还容易被 grep 抓到；
图谱编一条边看起来像"结构化知识"，自带权威感，再套上 provenance
就成了"有出处的因果链"，反而更难质疑。

所以本模块从 raw/wiki **抽取**而非手写，并在抽取后做**结构校验**：
引用了不存在的设备编号/实体 → 直接报错。
**一个真正从 raw 派生的图谱，会自动抓出上面那个 TQ-01 错误。**

节点类型
--------
    product     产品       银黄口服液 …
    material    原材料     金银花 …
    process     工序       水提取、浓缩 …
    equipment   设备       EQ-TQ-001 …
    incident    设备故障    4 条维修记录
    anomaly     成本异动    官方异动清单
    peer        对标单位    中药二厂

边类型
------
    product  --处方含(占比)--> material
    product  --生产工序------> process
    process  --关键设备------> equipment
    equipment --发生故障-----> incident
    anomaly  --涉及产品------> product
    incident --导致停机------> anomaly      （仅当有证据时）

用法
----
    python scripts/graph_build.py                 # 构建并校验
    python scripts/graph_build.py --json          # 打印全图
    python scripts/graph_build.py --trace 银黄口服液   # 多跳因果穿透
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
_CODE_ROOT = HERE.parent
# ⚠️ 与 ingest_tools / state 同一约定：`LLM_WIKI_WS` 可指向个人工作区。
#    个人区的图谱只抽个人 wiki/raw——**不读共享基线**，否则个人图谱会
#    把公司的词条也算进去，产出一个"混合体"。
WIKI_ROOT = Path(os.getenv("LLM_WIKI_WS") or _CODE_ROOT)
WIKI = WIKI_ROOT / "wiki"
RAW = WIKI_ROOT / "raw"
GRAPH_DIR = WIKI_ROOT / "graph"

EQUIP_CODE = re.compile(r"`?(EQ-[A-Z]{2}-\d{3})`?")
PEER = "中药二厂"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Node:
    id: str
    type: str
    label: str
    attrs: Dict[str, Any] = field(default_factory=dict)
    coord: str = ""          # 溯源坐标：这个节点是从哪抽出来的


@dataclass
class Edge:
    src: str
    dst: str
    relation: str
    attrs: Dict[str, Any] = field(default_factory=dict)
    coord: str = ""          # 溯源坐标：这条边凭什么成立


class Graph:
    def __init__(self) -> None:
        self.nodes: Dict[str, Node] = {}
        self.edges: List[Edge] = []
        self.warnings: List[str] = []
        self.errors: List[str] = []

    def add_node(self, n: Node) -> None:
        if n.id in self.nodes:
            return
        self.nodes[n.id] = n

    def add_edge(self, e: Edge) -> None:
        self.edges.append(e)

    # ---- 出/入度查询 ----
    def out(self, node_id: str) -> List[Edge]:
        return [e for e in self.edges if e.src == node_id]

    def inc(self, node_id: str) -> List[Edge]:
        return [e for e in self.edges if e.dst == node_id]

    def by_type(self, t: str) -> List[Node]:
        return [n for n in self.nodes.values() if n.type == t]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_from": "wiki/ + raw/（派生视图，可再生）",
            "nodes": [asdict(n) for n in self.nodes.values()],
            "edges": [asdict(e) for e in self.edges],
            "stats": {
                "nodes": len(self.nodes),
                "edges": len(self.edges),
                "by_type": {t: len(self.by_type(t)) for t in
                            sorted({n.type for n in self.nodes.values()})},
                "by_relation": {r: sum(1 for e in self.edges if e.relation == r)
                                for r in sorted({e.relation for e in self.edges})},
            },
            "warnings": self.warnings,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# 抽取器
# ---------------------------------------------------------------------------

def _rows(md_path: Path) -> List[List[str]]:
    """把 markdown 里的所有表格行抽出来（`| a | b |` → ['a','b']）。"""
    out = []
    for line in md_path.read_text(encoding="utf-8", errors="replace").split("\n"):
        s = line.strip()
        if s.startswith("|") and not re.match(r"^\|[\s:|-]+\|$", s):
            cells = [c.strip() for c in s.strip("|").split("|")]
            out.append(cells)
    return out


def extract_equipment(g: Graph) -> None:
    """
    从设备台账表抽设备节点。
    真实表头：| 设备编号 | 设备名称 | 规格型号 | 数量 | 制造商 | 投产年份 | 原值(万元) | 月折旧(元) |
    """
    p = WIKI / "equipment/设备台账与维修历史.md"
    if not p.exists():
        g.errors.append("找不到设备台账词条")
        return
    coord_p = "wiki/equipment/设备台账与维修历史.md"
    for r in _rows(p):
        if len(r) < 3 or not EQUIP_CODE.fullmatch(r[0].strip("`")):
            continue
        code = r[0].strip("`")
        g.add_node(Node(
            id=code, type="equipment", label=r[1],
            attrs={"规格型号": r[2], "数量": r[3] if len(r) > 3 else "",
                   "制造商": r[4] if len(r) > 4 else "",
                   "投产年份": r[5] if len(r) > 5 else "",
                   "月折旧(元)": r[7] if len(r) > 7 else ""},
            coord=f"[{coord_p}:设备台账:{code}]"))


def extract_materials(g: Graph) -> None:
    """
    从原材料消耗明细抽 产品→原料 边。
    占比用 5 月（信息截点月）的「占总材料成本比例」。
    """
    f = RAW / "csv/cost_data/中药一厂_原材料消耗明细_2026年1-6月.csv"
    with open(f, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        if (r.get("月份") or "").strip() != "2026-05":
            continue
        prod = (r.get("产品名称") or "").strip()
        mat = (r.get("原材料名称") or "").strip()
        if not prod or not mat:
            continue
        g.add_node(Node(id=prod, type="product", label=prod,
                        coord=f"[中药一厂_成本汇总_2026年1-6月.csv:产品名称:{prod}]"))
        g.add_node(Node(id=mat, type="material", label=mat,
                        coord=f"[中药一厂_原材料消耗明细_2026年1-6月.csv:原材料名称:{mat}]"))
        g.add_edge(Edge(
            src=prod, dst=mat, relation="处方含",
            attrs={"占总材料成本": (r.get("占总材料成本比例") or "").strip(),
                   "单位消耗成本(元/盒)": (r.get("单位消耗成本(元/盒)") or "").strip()},
            coord=(f"[中药一厂_原材料消耗明细_2026年1-6月.csv:"
                   f"占总材料成本比例:{prod}&{mat}&2026-05]")))


def extract_processes(g: Graph) -> None:
    """
    从各产品词条的**工艺表**抽 产品→工序 与 工序→设备。

    表头形如：| 工序 | 关键工艺参数(CPP) | 收率/消耗指标 | 关键设备 | 对成本影响 |
    关键设备列里写的是真实编号（如 `EQ-TQ-001`）——这正是图谱最该利用的现成结构。
    """
    for f in sorted(WIKI.glob("products/*.md")):
        prod = f.stem
        coord_p = f"wiki/products/{prod}.md"
        for r in _rows(f):
            if len(r) < 5 or r[0] in ("工序", "月份"):
                continue
            name, cpp, yield_, equip_cell, impact = r[0], r[1], r[2], r[3], r[4]
            # 工序名要像工序（避免把数据表的行误当工序）
            if not name or len(name) > 14:
                continue

            # 判据：**工艺表里凡是写了关键设备的行，就是一道工序**。
            # 有些工序的 CPP 是 `—`（拣选+清洗、抛光、外包装、包装），
            # 但**仍指定了设备**——它们同样是工序，不能因为"没参数"就丢掉。
            #
            # ⚠️ 与「找不到编号」的区别，是这里的核心难点：
            #      · `equip_cell` 里**根本没写编号** → 这行不是工艺工序，跳过（正常）
            #      · `equip_cell` 里写了编号、但**编号不存在** → 这是**错误**，
            #        必须让边建出来，交给 validate 抓。
            #   第一版把两者都 `continue` 掉了 → 悬空引用被静默吞掉 →
            #   校验报"✅ 通过"（实测：塞入 `TQ-01` 后照样绿灯，边数却少了 1）。
            #
            # 所以这里用**宽松的编号形态**（任意 `XX-NN` 样式）来判断"这行有没有写设备"，
            # 而**严格格式校验交给 validate**。宽进严出。
            looks_like_equip = bool(re.search(r"[A-Z]{2,3}-\d{2,3}", equip_cell))
            if not looks_like_equip:
                continue

            proc_id = f"{prod}·{name}"
            g.add_node(Node(id=proc_id, type="process", label=name,
                            attrs={"产品": prod, "关键工艺参数": cpp, "收率指标": yield_},
                            coord=f"[{coord_p}:工艺表:{name}]"))
            g.add_edge(Edge(src=prod, dst=proc_id, relation="生产工序",
                            coord=f"[{coord_p}:工艺表:{name}]"))

            # 严格编号（`EQ-XX-###`）正常成边；形态可疑的（如 `TQ-01`）也成边，
            # 但额外记一条错误——**绝不静默吞掉**。
            codes = EQUIP_CODE.findall(equip_cell)
            if not codes:
                for bad in set(re.findall(r"[A-Z]{2,3}-\d{2,3}", equip_cell)):
                    g.errors.append(
                        f"工艺表「{name}」({prod}) 引用的设备编号 `{bad}` "
                        f"不符合 EQ-<两位字母>-<三位数字> 格式"
                        f"（坐标 {coord_p}）。**不存在 `TQ-01` 这类简写。**")
                continue
            for code in codes:
                g.add_edge(Edge(
                    src=proc_id, dst=code, relation="关键设备",
                    attrs={"对成本影响": impact},
                    coord=f"[{coord_p}:工艺表:关键设备列:{name}→{code}]"))


def extract_incidents(g: Graph) -> None:
    """
    从维修历史表抽 设备→故障事件。
    ⚠️ 这是**抓 TQ-01 类错误的关键环节**：
       若维修记录里提到的设备在台账中不存在，校验阶段会报错。
    """
    p = WIKI / "equipment/设备台账与维修历史.md"
    coord_p = "wiki/equipment/设备台账与维修历史.md"
    name2code = {n.label: n.id for n in g.by_type("equipment")}

    def _strip(s: str) -> str:
        """去 markdown 强调与行内代码记号。"""
        return re.sub(r"[*`]", "", s).strip()

    for r in _rows(p):
        # 维修表：| 日期 | 设备 | 故障描述 | 维修费(元) | 停工(h) | 影响 |
        # ⚠️ 日期可能带粗体（`**2026-03**`）——那是 wiki 用来标"最值得注意"的方式。
        #    实测踩过：第一版正则 `^\d{4}-\d{2}$` 只吃裸日期，
        #    结果**恰好漏掉唯一一条 2026 年的记录**（3 月胶囊填充机故障），
        #    而那条正是六味地黄成本异动的官方根因。漏最要紧的那条。
        raw0 = r[0].strip()
        m = re.match(r"^\*{0,2}(\d{4}-\d{2})\*{0,2}$", raw0)
        if len(r) < 4 or not m:
            continue
        date = m.group(1)
        dev = _strip(r[1])
        fault = _strip(r[2])
        inc_id = f"{date}·{fault}"
        if inc_id in g.nodes:
            continue
        g.add_node(Node(id=inc_id, type="incident", label=f"{date} {dev}：{fault}",
                        attrs={"日期": date, "设备": dev, "故障": fault,
                               "维修费(元)": _strip(r[3]) if len(r) > 3 else "",
                               "停工(h)": _strip(r[4]) if len(r) > 4 else "",
                               "影响": _strip(r[5]) if len(r) > 5 else ""},
                        coord=f"[{coord_p}:维修历史:{date}]"))

        # 设备 → 故障 的边：设备单元格里常直接写着编号（`EQ-JN-006`），优先用它，
        # 比按名称反查可靠（名称可能带规格后缀，如「颗粒分装机（DXDK-40VI）」）。
        codes = EQUIP_CODE.findall(r[1])
        if codes:
            code = codes[0]
            if code not in g.nodes:
                g.errors.append(f"维修记录 {date} 引用了未登记的设备编号「{code}」")
            else:
                g.add_edge(Edge(src=code, dst=inc_id, relation="发生故障",
                                coord=f"[{coord_p}:维修历史:{date}]"))
            continue

        # 回退：按名称反查
        code = next((c for nm, c in name2code.items()
                     if dev and (dev in nm or nm in dev)), None)
        if code:
            g.add_edge(Edge(src=code, dst=inc_id, relation="发生故障",
                            coord=f"[{coord_p}:维修历史:{date}]"))
        else:
            # ⚠️ 维修记录引用了台账里没有的设备 → 校验阶段报错。
            #    这正是 demo/graph_kg.py 里 `TQ-01` 那类错误的产生方式。
            g.errors.append(
                f"维修记录 {date} 提到的设备「{dev}」在设备台账中找不到对应编号。"
                f"这正是 `TQ-01` 那类错误的产生方式。")


def extract_anomalies(g: Graph) -> None:
    """
    从成本异动登记册的「官方异动清单」抽异动节点与 异动→产品 边。
    表头：| # | 波动事件 | 涉及产品 | 发生月份 | 官方根因 |
    """
    p = WIKI / "costs/成本异动登记册.md"
    coord_p = "wiki/costs/成本异动登记册.md"
    if not p.exists():
        g.warnings.append("找不到成本异动登记册，跳过异动清单")
        return
    for r in _rows(p):
        if len(r) < 5 or not r[0].isdigit():
            continue
        _, event, prod, month, cause = r[0], r[1], r[2], r[3], r[4]
        an_id = f"异动{event}"
        g.add_node(Node(id=an_id, type="anomaly", label=event,
                        attrs={"涉及产品": prod, "发生月份": month, "官方根因": cause},
                        coord=f"[{coord_p}:1 官方异动清单:{event}]"))
        # 产品节点可能已在 extract_materials 建过；这里不覆盖
        if prod and not prod.startswith("全部"):
            g.add_node(Node(id=prod, type="product", label=prod,
                            coord=f"[{coord_p}:1 官方异动清单:{prod}]"))
            g.add_edge(Edge(src=an_id, dst=prod, relation="涉及产品",
                            attrs={"月份": month},
                            coord=f"[{coord_p}:1 官方异动清单:{event}→{prod}]"))


def extract_peers(g: Graph) -> None:
    """对标单位节点 + 一厂/二厂 的同产品对比关系。"""
    g.add_node(Node(id=PEER, type="peer", label=PEER,
                    coord="[中药二厂_成本汇总_2026年1-6月.csv:工厂:中药二厂]"))
    f = RAW / "csv/cost_data/中药二厂_成本汇总_2026年1-6月.csv"
    if not f.exists():
        return
    with open(f, encoding="utf-8-sig", newline="") as fh:
        prods = {(r.get("产品名称") or "").strip() for r in csv.DictReader(fh)}
    for prod in sorted(p for p in prods if p):
        if prod in g.nodes:
            g.add_edge(Edge(
                src=prod, dst=PEER, relation="对标对比",
                coord=(f"[中药一厂_成本汇总_2026年1-6月.csv] + "
                       f"[中药二厂_成本汇总_2026年1-6月.csv:{prod}]")))


# ---------------------------------------------------------------------------
# 结构校验 ⭐
# ---------------------------------------------------------------------------

def validate(g: Graph) -> None:
    """
    结构校验：图的**引用完整性**。

    这是图谱相对普通文档的**最大增量价值**——文档里写错一个编号要靠人眼发现，
    图里引用一个不存在的节点是**可机械检测**的。
    """
    before = len(g.errors)

    # 1. 所有边的 src/dst 必须存在
    for e in g.edges:
        for end, tag in ((e.src, "src"), (e.dst, "dst")):
            if end not in g.nodes:
                g.errors.append(
                    f"边 {e.relation} 的 {tag} 节点「{end}」不存在"
                    f"（坐标 {e.coord or '无'}）")

    # 2. 设备编号格式校验：必须是 EQ-<两位大写字母>-<三位数字>
    for n in g.by_type("equipment"):
        if not re.fullmatch(r"EQ-[A-Z]{2}-\d{3}", n.id):
            g.errors.append(
                f"设备编号「{n.id}」不符合 EQ-<类别>-<三位流水> 格式"
                f"（不能是 TQ-01 这类简写）")

    # 3. 工序→设备 的引用必须是已登记设备
    for e in g.edges:
        if e.relation == "关键设备" and e.dst not in g.nodes:
            g.errors.append(
                f"工艺表引用了未登记的设备「{e.dst}」（坐标 {e.coord}）")

    # 4. 产品必须至少有一条处方原料边与一条工序边（否则抽取漏了）
    for n in g.by_type("product"):
        if not g.out(n.id):
            g.warnings.append(f"产品「{n.id}」没有任何出边——抽取可能不全")

    # 5. 孤立节点（既无出边也无入边）
    for n in g.nodes.values():
        if not g.out(n.id) and not g.inc(n.id):
            g.warnings.append(f"孤立节点：{n.type} / {n.id}")

    # 6. ⭐ **覆盖度断言**：抽取量不得低于已知基线。
    #
    #    为什么需要这条：第 1 条只能查"边的端点不存在"，
    #    **查不出"边根本没被创建"**——而后者才是本模块最容易犯的错
    #    （实测踩过：一条 continue 把带错误编号的工序整行吞掉，
    #      校验器看不到任何悬空引用，于是**假通过**、还报绿灯）。
    #    数量断言是这类静默丢失的兜底：**丢东西就会跌破基线。**
    BASELINE = {"equipment": 29, "incident": 4, "process": 20, "material": 15,
                "product": 3}
    for t, low in BASELINE.items():
        got = len(g.by_type(t))
        if got < low:
            g.errors.append(
                f"覆盖度不足：{t} 只抽到 {got} 个，低于基线 {low}。"
                f"**有内容被静默丢弃**，请检查抽取逻辑（不要用 continue 吞掉异常行）。")

    # ⚠️ 只有**本轮校验**（不是整个抽取过程）没新增错误，才算通过。
    #    早期版本用 `len(g.errors) == before` —— 而 `before` 取的是进入 validate
    #    时的快照，抽取阶段记录的错误已计入，所以"通过"与"有错"会**同时出现**，
    #    看起来像在自我矛盾。改为看本轮增量。
    if len(g.errors) == before:
        g.warnings.insert(0, "✅ 结构校验通过：无悬空引用、设备编号格式合规、"
                             "各类节点数不低于基线")
    else:
        g.warnings.insert(0, f"❌ 结构校验**未通过**：本轮新增 "
                             f"{len(g.errors) - before} 处问题，见下方 errors")


# ---------------------------------------------------------------------------
# 因果穿透
# ---------------------------------------------------------------------------

def trace(g: Graph, start: str, max_hops: int = 6) -> List[List[Edge]]:
    """
    从起点做**多跳正向前穿透**，返回若干条链路。

    这就是图谱相对「段落检索」的核心增量：
    检索只能找到「提到设备故障的那段文字」，
    穿透能回答「成本上涨**经过哪些环节**」——一条路径，而不只是一段话。
    """
    paths: List[List[Edge]] = []

    def dfs(node: str, path: List[Edge], seen: Set[str]) -> None:
        if len(path) >= max_hops or len(paths) >= 30:
            return
        outs = g.out(node)
        if not outs:
            if path:
                paths.append(list(path))
            return
        for e in outs:
            if e.dst in seen:
                continue
            path.append(e)
            dfs(e.dst, path, seen | {e.dst})
            path.pop()

    if start in g.nodes:
        dfs(start, [], {start})
    return paths


# ---------------------------------------------------------------------------

def build(verbose: bool = True) -> Dict[str, Any]:
    g = Graph()
    extract_equipment(g)
    extract_materials(g)
    extract_processes(g)
    extract_incidents(g)
    extract_anomalies(g)
    extract_peers(g)
    validate(g)

    doc = g.to_dict()
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    (GRAPH_DIR / "graph.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1),
        encoding="utf-8", newline="\n")

    if verbose:
        st = doc["stats"]
        print(f"图谱已建立：{st['nodes']} 节点 / {st['edges']} 边")
        print(f"  节点类型: {st['by_type']}")
        print(f"  边类型  : {st['by_relation']}")
        for w in doc["warnings"][:6]:
            print(f"  ⚠️  {w}")
        for e in doc["errors"][:8]:
            print(f"  ❌ {e}")
    return doc


def _print_trace(g: Graph, start: str) -> None:
    paths = trace(g, start)
    node = g.nodes.get(start)
    print("=" * 76)
    print(f"因果穿透：{start}" + (f"（{node.type} · {node.label}）" if node else ""))
    print("=" * 76)
    if not paths:
        print("  无可穿透路径")
        return
    # 只显示最长的几条（最短的通常是"产品→原料"这类单跳）
    paths.sort(key=len, reverse=True)
    for i, p in enumerate(paths[:6], 1):
        chain = [start]
        for e in p:
            chain.append(f"-[{e.relation}]→")
            chain.append(e.dst)
        print(f"\n  路径 {i}（{len(p)} 跳）")
        print("    " + " ".join(chain))
        for e in p:
            if e.coord:
                print(f"      ↳ {e.relation}: {e.coord}")


def main() -> int:
    ap = argparse.ArgumentParser(description="因果知识图谱（派生视图）")
    ap.add_argument("--json", action="store_true", help="打印全图 JSON")
    ap.add_argument("--trace", help="从某节点做多跳因果穿透")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    doc = build()
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    elif a.trace:
        g = Graph()
        extract_equipment(g); extract_materials(g); extract_processes(g)
        extract_incidents(g); extract_anomalies(g); extract_peers(g)
        _print_trace(g, a.trace)
    return 1 if doc["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
