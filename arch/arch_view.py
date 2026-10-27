"""構成図を出す小さなウィンドウ（見るだけ）。ブラウザを使わず、tkinter で直接描く（メモリを食わないため）。

  python arch_view.py            herdr で今選んでいるペインのプロジェクトのタブで開く（開いていれば、そのタブを足す）
  python arch_view.py <project>  そのプロジェクトに固定（"all" で全部）
  herdr では prefix+o で開く（すでに開いていれば何もしない）

中身は board.py が書く ~/.herdr/kanban-state.json の "arch"（判断は arch.py）。ここは読んで描くだけ。
部品の並べ方は Graphviz（dot）に任せる。state が変われば数秒で描き直す（カンバンと同じ組み立てを見る）。
"""

import ctypes
import itertools
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
import json
import math
import shutil
import subprocess
import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

import arch
from datetime import datetime, timezone

STATE = Path.home() / ".herdr" / "kanban-state.json"
BOARD = Path(__file__).resolve().parent.parent / "board.py"
# 組み立て（board.py build）を頼む決まりは、カンバンの画面と同じ: 90 秒より古ければ、20 秒に 1 回まで、重ねない
REBUILD_EVERY, REBUILD_AT_LEAST = 90, 20
DOT = shutil.which("dot") or r"C:\Program Files\Graphviz\bin\dot.exe"
POLL_MS = 2000
# herdr や dot はコンソールのプログラムなので、そのまま呼ぶと呼ぶたびにコンソールの窓が開いて入力を奪う（2026-10-06）
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 色（白いノートに点の方眼。Miro のような見た目）
# 白・薄い灰色でも明るすぎた（2026-10-06）ので、中くらいの灰色の紙にする
C = {
    # 無彩色で組み、色は意味のある所だけ（2026-10-06: 色を均等にばらまくと「AI で作った感」が出た）
    "bg": "#eceef1", "dot": "#d5d9de", "panel": "#ffffff", "line": "#d5d9de", "card": "#ffffff", "shadow": "#e4e7eb",
    "text": "#1f2328", "dim": "#4b535d", "faint": "#636c76",  # 文字は 3 段。いちばん薄くても白地・枠の色地で 4.5:1 以上
    "edge": "#5f6772", "edge_quiet": "#d9dde2",  # 線は濃い灰色（流れの背骨が見えるように）。種類は形で分ける
    "card_line": "#bfc5cd",  # カードの枠は、紙・枠の背景より 1 段濃く
    # 1) 状態がひと目で分かるよう、例外の部品だけカードを淡く色付けする（本番は白のまま）
    "tint_wip": "#e6effc", "tint_stop": "#fbf1d6", "tint_none": "#eef0f2",
    "bone": "#1e3a8a",  # アクセントは紺 1 色だけ（選んだもの・リンク・骨格の番号）
    # 状態の丸の色（文字と組で出す）。慣習どおり 成功 = 緑・進行中 = 青・注意 = 黄。赤はエラーだけ（待ちはエラーではない）
    "live": "#1a7f37", "wip": "#2f6fd0", "none": "#8c959f", "stop": "#bf8700", "error": "#cf222e",
}
# 系の区別は、枠の名前の横の小さな色の四角だけ
# 系ごとに薄い背景色（線は引かない）。色は 3 つまで・トーンをそろえる（60:30:10 の 30 の側）:
# プロジェクトは青系と暖色系を交互に、外部・社内の仕組み・外部サービスは無彩色の淡い灰色
KIND_MARK = {"external": "#e2e5e9", "company": "#e2e5e9", "cloud": "#e2e5e9"}
PROJECT_MARKS = ["#dce6f4", "#e7dcf4"]  # 青と紫（明るさ・鮮やかさは同じで、色相だけ変える）
FONT = "BIZ UDPゴシック"  # Yu Gothic UI は Windows で細く薄く出る。BIZ UDP は太めで小さくても読める（標準で入っている）
NUM_FONT = "Consolas"  # 番号・本数は桁のそろう字体
R_NODE, R_ZONE = 6, 10  # 角丸は 2 つの値だけ（部品・枠）。チップは丸端
DOT_STEP = 22  # 方眼の点の間隔（画素）
# 「止まっている」「停止」は合わなかった（2026-10-06）。カンバンの「外部待ち」と同じ意味なので「待ち」
STATUS_TEXT = {"live": "本番で動いている", "wip": "作りかけ", "none": "まだ無い", "stop": "待ち"}
STATUS_SHORT = {"live": "本番", "wip": "作りかけ", "none": "まだ無い", "stop": "待ち"}
DASH = {"live": (), "wip": (7, 5), "none": (2, 5), "stop": (10, 4, 2, 4)}
NODE_W, NODE_H = 2.5, 0.78  # インチ（Graphviz の単位）。1 インチ = 72 点


_fonts = {}


def font(px, bold=False, family=None):
    """大きさは画素で指定する（負の値）。点で指定すると画面の拡大率で文字だけが大きくなり、四角からはみ出す。
    同じ大きさは作り直さない（毎回作ると、動かすたびの描き直しが目に見えて遅くなる）。"""
    family = family or (FONT if FONT in _families() else "Yu Gothic UI")
    key = (max(8, round(px * 1.33)), bold, family)
    if key not in _fonts:
        _fonts[key] = tkfont.Font(family=family, size=-key[0], weight="bold" if bold else "normal")
    return _fonts[key]


def _families():
    global _family_set
    if "_family_set" not in globals():
        _family_set = set(tkfont.families())
    return _family_set


# ---------------------------------------------------------------- 並べ方（Graphviz）

ZONE_GAP = 64      # 枠どうしの間（点）。枠をまたぐ線はこのすき間を通る
LANE = 10          # 同じすき間を通る線どうしのずらし幅（点）
ZONE_HEAD = 56     # 枠の上の、名前を書く場所（点）
ZONE_PAD = 18      # 枠の内側の余白（点）


def run_dot(lines):
    out = subprocess.run([DOT, "-Tjson"], input="\n".join(lines), capture_output=True, text=True, encoding="utf-8",
                         creationflags=NO_WINDOW)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    return json.loads(out.stdout)


def spline(e, flip):
    """Graphviz の線の形（pos）を、なめらかな折れ線にする。"""
    pts, end = [], None
    for tok in e["pos"].split():
        parts = tok.split(",")
        if parts[0] == "e":
            end = flip(parts[1], parts[2])
        elif parts[0] != "s":
            pts.append(flip(parts[0], parts[1]))
    return bezier(pts) + ([end] if end else [])


_zone_cache = {}


def zone_layout(zi, members, inner_edges, names, rankdir="LR"):
    """1 つの枠の中だけを Graphviz に並べさせる。座標は枠の中の左上を原点にした点。
    中身が同じなら前の結果を使う（Graphviz を 1 回立てるのに 0.3 秒ほどかかる）。"""
    key = (zi, rankdir, tuple((n["id"], n["blocked"]) for n in members),
           tuple((e["from"], e["to"], e.get("label", "")) for e in inner_edges), tuple(sorted(names.items())))
    if key not in _zone_cache:
        _zone_cache[key] = _zone_layout(zi, members, inner_edges, names, rankdir)
    return _zone_cache[key]


def _zone_layout(zi, members, inner_edges, names, rankdir):
    lines = ['digraph G {', f'rankdir={rankdir}; nodesep=0.35; ranksep=0.9; splines=spline;',
             'graph [fontname="Yu Gothic UI", fontsize=13, margin=0];',
             f'node [shape=box, fixedsize=true, width={NODE_W}, height={NODE_H}, label=""];',
             'edge [fontname="Yu Gothic UI", fontsize=10];']
    groups = {}
    for n in members:
        if n["blocked"]:
            groups.setdefault(n["blocked"], []).append(n)
    # 同じ理由で止まっている部品は、点線の枠にまとめる（Graphviz が寄せて並べる）
    for gi, ns in enumerate(groups.values()):
        lines.append(f'subgraph cluster_b{gi} {{ label=" "; fontsize=18; margin=14; '
                     + " ".join(names[n["id"]] + ";" for n in ns) + "}")
    lines.append(" ".join(names[n["id"]] + ";" for n in members if not n["blocked"]))
    for e in inner_edges:  # 本物の線を先に書く（結果の edges の先頭から本物として読む）
        label = f' [label="{e["label"]}"]' if e.get("label") else ""
        lines.append(f'{names[e["from"]]} -> {names[e["to"]]}{label};')
    # 枠の中の線でつながっていない部品は、1 列に縦長に並ばないよう、見えない線で数列の格子にする
    linked = {x for e in inner_edges for x in (e["from"], e["to"])}
    free = [n for n in members if n["id"] not in linked]
    if len(free) > 3:
        rows = math.ceil(len(free) / math.ceil(math.sqrt(len(free) / 2)))
        for i in range(len(free) - rows):
            lines.append(f'{names[free[i]["id"]]} -> {names[free[i + rows]["id"]]} [style=invis];')
    lines.append("}")
    g = run_dot(lines)
    x0, y0, x1, y1 = map(float, g["bb"].split(","))
    flip = lambda x, y: (float(x) - x0, (y1 - float(y)))
    by_name = {v: k for k, v in names.items()}
    pos, frames = {}, []
    for o in g.get("objects", []):
        if o.get("name", "").startswith("cluster_b") and "bb" in o:
            a, b, c, d = map(float, o["bb"].split(","))
            frames.append({"key": f"z{zi}_" + o["name"][len("cluster_"):], "box": (a - x0, y1 - d, c - x0, y1 - b)})
        elif o.get("name") in by_name:
            pos[by_name[o["name"]]] = flip(*o["pos"].split(","))
    paths = [{"points": spline(e, flip), "label_at": flip(*e["lp"].split(",")) if "lp" in e else None}
             for e in g.get("edges", [])[:len(inner_edges)]]
    return {"pos": pos, "frames": frames, "paths": paths, "size": (x1 - x0, y1 - y0)}


def safe_layout(view, aspect=1.45):
    try:
        return layout(view, aspect)
    except Exception as e:
        return e


def hits(p, q, rects, skip):
    """線分 p-q が、skip 以外の枠のどれかを横切るか。"""
    for i, (x1, y1, x2, y2) in enumerate(rects):
        if i in skip:
            continue
        if p[0] == q[0]:  # 縦の線分
            if x1 < p[0] < x2 and min(p[1], q[1]) < y2 and max(p[1], q[1]) > y1:
                return True
        elif p[1] == q[1]:  # 横の線分
            if y1 < p[1] < y2 and min(p[0], q[0]) < x2 and max(p[0], q[0]) > x1:
                return True
    return False


def rounded_path(pts, r=14):
    """折れ線の角を丸める（角ごとに短い曲線を入れる）。"""
    out = [pts[0]]
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        da, dc = math.dist(a, b), math.dist(b, c)
        k = min(r, da / 2, dc / 2)
        if k < 1:
            out.append(b)
            continue
        p = (b[0] + (a[0] - b[0]) * k / da, b[1] + (a[1] - b[1]) * k / da)
        q = (b[0] + (c[0] - b[0]) * k / dc, b[1] + (c[1] - b[1]) * k / dc)
        out.append(p)
        for t in (0.25, 0.5, 0.75):
            out.append(((1 - t) ** 2 * p[0] + 2 * t * (1 - t) * b[0] + t * t * q[0],
                        (1 - t) ** 2 * p[1] + 2 * t * (1 - t) * b[1] + t * t * q[1]))
        out.append(q)
    out.append(pts[-1])
    return out


def route_cross(a, b, za, zb, rects, total, lanes, w=NODE_W * 72 / 2):
    """枠をまたぐ線を、枠と枠のあいだの通り道だけを通して引く（別の枠の上を通らない）。
    a, b は部品の中心、za, zb は両端の枠の番号（rects の添字）、total は全体の大きさ。
    通り道: 枠の左右のすき間（縦）と、全体の上・下のすき間（横）。同じ通り道の線は少しずつずらす（lanes）。
    w は端の点から辺までの幅（部品なら部品の半分、枠の辺から出すなら 0）。"""
    ra, rb = rects[za], rects[zb]
    half = ZONE_GAP / 2
    right = b[0] >= a[0]
    same_column = ra[0] < rb[2] and rb[0] < ra[2]  # 上下に積んだ枠どうし: 右のすき間を通る
    if same_column:
        ex = nx = max(ra[2], rb[2]) + half
    else:
        ex = ra[2] + half if right else ra[0] - half  # 出口: 自分の枠の、相手の側の辺の外（すき間の真ん中）
        nx = rb[0] - half if right else rb[2] + half  # 入口: 相手の枠の、こちら側の辺の外
    start = (a[0] + w if ex > a[0] else a[0] - w, a[1])
    end = (b[0] - w if nx < b[0] else b[0] + w, b[1])
    top, bottom = -half, total[1] + half
    candidates = [[start, (ex, a[1]), (ex, b[1]), end]] if ex == nx else []
    candidates += [[start, (ex, a[1]), (ex, b[1]), (nx, b[1]), end],  # 縦に動いてから、相手の高さで横へ
                   [start, (ex, a[1]), (nx, a[1]), (nx, b[1]), end]]  # 横に動いてから縦へ
    candidates += [[start, (ex, a[1]), (ex, y), (nx, y), (nx, b[1]), end] for y in (top, bottom)]  # 全体の上か下を回る
    ok = lambda path: not any(hits(p, q, rects, {za, zb}) for p, q in zip(path[1:-2], path[2:-1]))
    length = lambda path: sum(math.dist(p, q) for p, q in zip(path, path[1:]))
    pts = min([c for c in candidates if ok(c)] or candidates[-2:], key=length)
    pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
    # 選んだ通り道にだけ、ずらし幅を割り当てる（同じすき間を通る線が重ならないように）
    def lane(key):
        lanes[key] = lanes.get(key, -1) + 1
        n = lanes[key]
        return (n + 1) // 2 * (LANE if n % 2 else -LANE)
    dx = {x: lane(("v", round(x))) for x in {ex, nx}}
    dy = {y: lane(("h", y)) for y in (top, bottom) if any(p[1] == y for p in pts[1:-1])}
    pts = [pts[0]] + [(p[0] + dx.get(p[0], 0), p[1] + dy.get(p[1], 0)) for p in pts[1:-1]] + [pts[-1]]
    mid = max(zip(pts, pts[1:]), key=lambda seg: math.dist(*seg))  # いちばん長い区間の真ん中に名札
    return {"points": rounded_path(pts), "label_at": ((mid[0][0] + mid[1][0]) / 2, (mid[0][1] + mid[1][1]) / 2 - 7)}


def arrange(sizes):
    """枠を左から順に置く。小さい枠は、いちばん高い枠の高さに収まるあいだ同じ列に縦に積む。返すのは各枠の左上と全体の大きさ。"""
    tallest = max(h for _, h in sizes)
    spots, x, y, col_w = [], 0.0, 0.0, 0.0
    for w, h in sizes:
        if y > 0 and y + h > tallest + 1:
            x, y, col_w = x + col_w + ZONE_GAP, 0.0, 0.0
        spots.append((x, y))
        y, col_w = y + h + ZONE_GAP, max(col_w, w)
    return spots, (x + col_w, tallest)


def layout(view, aspect=1.45):
    """枠ごとに中を Graphviz で並べ、枠どうしは左から決まった狭い間隔で並べる。座標は点（左上原点）。
    全体を 1 回で Graphviz に任せると、枠をまたぐ線を通すために枠を離して外側を大回りさせ、
    枠どうしの間が大きく空いた（2026-10-06）。
    枠の中を横に流すか縦に流すかは、全体が窓の形（aspect = 幅 / 高さ）にいちばん近くなる組み合わせを選ぶ
    （全部を横に流すと横に細長くなり、窓に収めると文字が小さくなった）。"""
    names = {n["id"]: f"n{i}" for i, n in enumerate(view["nodes"])}
    zone_of = {n["id"]: n["zone"] for n in view["nodes"]}
    zi_of = {z["id"]: i for i, z in enumerate(view["zones"])}
    order = zone_order(view)
    idx = {zid: [i for i, e in enumerate(view["edges"]) if zone_of[e["from"]] == zone_of[e["to"]] == zid] for zid in order}
    jobs = {}
    with ThreadPoolExecutor(max_workers=8) as pool:  # 枠ごと・向きごとの Graphviz を同時に走らせる
        for zid in order:
            members = [n for n in view["nodes"] if n["zone"] == zid]
            inner_edges = [view["edges"][i] for i in idx[zid]]
            jobs[zid] = [pool.submit(zone_layout, zi_of[zid], members, inner_edges, names, d) for d in ("LR", "TB")]
    options = {zid: [f.result() for f in fs] for zid, fs in jobs.items()}
    box = lambda inner: (inner["size"][0] + ZONE_PAD * 2, inner["size"][1] + ZONE_HEAD + ZONE_PAD)
    best = None
    for pick in itertools.product((0, 1), repeat=len(order)):
        chosen = [options[zid][p] for zid, p in zip(order, pick)]
        spots, (tw, th) = arrange([box(c) for c in chosen])
        score = abs(math.log((tw / th) / aspect)) + 0.15 * math.log(tw * th)  # 窓の形に近く、なるべく小さく
        if best is None or score < best[0]:
            best = (score, chosen, spots, (tw, th))
    _, chosen, spots, total = best
    pos, frames, paths = {}, [], [None] * len(view["edges"])
    for zid, inner, (x, y) in zip(order, chosen, spots):
        zi = zi_of[zid]
        ox, oy = x + ZONE_PAD, y + ZONE_HEAD
        move = lambda p, ox=ox, oy=oy: (p[0] + ox, p[1] + oy)
        for nid, p in inner["pos"].items():
            pos[nid] = move(p)
        for f in inner["frames"]:
            a, b, c, d = f["box"]
            frames.append({"key": f["key"], "box": (*move((a, b)), *move((c, d)))})
        for i, p in zip(idx[zid], inner["paths"]):
            paths[i] = {"points": [move(q) for q in p["points"]], "label_at": p["label_at"] and move(p["label_at"])}
        w, h = box(inner)
        frames.insert(0, {"key": f"z{zi}", "box": (x, y, x + w, y + h)})
    rects = {f["key"]: f["box"] for f in frames if "_" not in f["key"]}
    keys = list(rects)
    rect_list = [rects[k] for k in keys]
    zone_rect = {zid: keys.index(f"z{zi_of[zid]}") for zid in order}
    lanes = {}
    for i, e in enumerate(view["edges"]):
        if paths[i] is None:
            paths[i] = route_cross(pos[e["from"]], pos[e["to"]], zone_rect[zone_of[e["from"]]],
                                   zone_rect[zone_of[e["to"]]], rect_list, total, lanes)
    return {"pos": pos, "frames": frames, "paths": paths, "size": total,
            "bundles": bundles(view, pos, zone_of, zone_rect, rect_list, total)}


WORST = ["stop", "none", "wip", "live"]  # まとめた線の色は、中でいちばん悪い状態に合わせる


def bundles(view, pos, zone_of, zone_rect, rects, total):
    """枠をまたぐ線を、枠の組ごとに 1 本にまとめる。端は枠の、相手に向いた辺（高さは関わる部品の平均）。"""
    groups = {}
    for i, e in enumerate(view["edges"]):
        za, zb = zone_of[e["from"]], zone_of[e["to"]]
        if za != zb:
            groups.setdefault(tuple(sorted((za, zb))), []).append(i)
    out, lanes = [], {}

    def anchor(zone, rect, other, idx):
        ys = [pos[x][1] for i in idx for x in (view["edges"][i]["from"], view["edges"][i]["to"]) if zone_of[x] == zone]
        y = min(max(sum(ys) / len(ys), rect[1] + 30), rect[3] - 20)
        if rect[0] < other[2] and other[0] < rect[2]:  # 上下に積んだ枠どうし: 右の辺から
            return (rect[2], y)
        return (rect[2], y) if other[0] >= rect[2] else (rect[0], y)

    ends = {}
    for (za, zb), idx in groups.items():
        ra, rb = rects[zone_rect[za]], rects[zone_rect[zb]]
        ends[(za, zb)] = [anchor(za, ra, rb, idx), anchor(zb, rb, ra, idx)]
    # 同じ枠の同じ辺から出る端どうしは、高さの順を保ったまま 24 点以上あける（線が重ならないように）
    sides = {}
    for key, pair in ends.items():
        for k in (0, 1):
            sides.setdefault((key[k], pair[k][0]), []).append((pair[k][1], key, k))
    for (zone, _), items in sides.items():
        rect = rects[zone_rect[zone]]
        items.sort()
        ys = [y for y, _, _ in items]
        for i in range(1, len(ys)):
            ys[i] = max(ys[i], ys[i - 1] + 24)
        shift = max(0, ys[-1] - (rect[3] - 16))  # 下にはみ出たぶん、全体を上へ
        for (y0, key, k), y in zip(items, ys):
            ends[key][k] = (ends[key][k][0], max(rect[1] + 24, y - shift))
    for (za, zb), idx in groups.items():
        a, b = ends[(za, zb)]
        path = route_cross(a, b, zone_rect[za], zone_rect[zb], rects, total, lanes, w=0)
        kinds = ["stop" if view["edges"][i].get("blocked") else view["edges"][i]["status"] for i in idx]
        forward = any(zone_of[view["edges"][i]["from"]] == za for i in idx)
        backward = any(zone_of[view["edges"][i]["from"]] == zb for i in idx)
        out.append({"zones": (za, zb), "edges": idx, "kind": min(kinds, key=WORST.index),
                    "arrow": "both" if forward and backward else ("last" if forward else "first"), **path})
    return out


def clip(text, f, room):
    """四角の幅に収まらない文字は、末尾を「…」にして切る（はみ出すと隣の四角に重なる）。"""
    if f.measure(text) <= room:
        return text
    while text and f.measure(text + "…") > room:
        text = text[:-1]
    return text + "…"


def zone_order(view):
    """枠を左から並べる順。外部 → 社内の仕組み → プロジェクト → 外部サービス。
    プロジェクトどうしは、呼ぶ側を左・呼ばれる側（土台）を右に置く（線が左から右へ流れる）。"""
    used = [z for z in view["zones"] if any(n["zone"] == z["id"] for n in view["nodes"])]
    zone_of = {n["id"]: n["zone"] for n in view["nodes"]}
    called = {z["id"]: sum(1 for e in view["edges"] if zone_of[e["to"]] == z["id"] != zone_of[e["from"]]) for z in used}
    kind_rank = {"external": 0, "company": 1, "project": 2, "cloud": 3}
    return [z["id"] for z in sorted(used, key=lambda z: (kind_rank.get(z.get("kind"), 2), called[z["id"]]))]


def bezier(pts, steps=12):
    """Graphviz の 3 次ベジェの制御点を、なめらかな折れ線にする。"""
    out = [pts[0]] if pts else []
    for i in range(0, len(pts) - 3, 3):
        p0, p1, p2, p3 = pts[i:i + 4]
        for s in range(1, steps + 1):
            t = s / steps
            a, b, c, d = (1 - t) ** 3, 3 * t * (1 - t) ** 2, 3 * t * t * (1 - t), t ** 3
            out.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return out


# ---------------------------------------------------------------- ウィンドウ

class Viewer:
    def __init__(self, fixed):
        # タブは全部固定。1 枚目は開いたときに herdr で選んでいたペインのプロジェクト（リポジトリでなければ全体）。
        # 窓が開いているときに prefix+o を押すと、そのプロジェクトのタブを足す（あれば切り替える）。窓は増やさない。
        # （初めは 1 枚目を「選んでいるペインを追いかける自動のタブ」にしていたが、押す前に自動のタブが先に
        # 切り替わり、開いていたタブが置き換わったように見えたのでやめた・2026-10-06）
        if fixed is None:
            fixed = opening_scope()
        self.scope = "" if fixed == "all" else fixed
        self.tabs, self.cur = [{"follow": False, "scope": self.scope}], 0
        self.state_mtime, self.state, self.view, self.lay, self.shape = 0, None, None, None, None
        self.scale, self.ox, self.oy, self.drag, self.selected = 1.0, 0.0, 0.0, None, None
        self.moved = False  # 手で動かした・拡大したら、自動で収め直さない（f で戻す）
        self.pending = None  # ホイールのあとの描き直しの予約
        self.pending_view = None  # 裏で並べている最中の中身
        self.working = {}  # 作業中の部品（herdr のペインのブランチから）
        self.owner, self.owners, self.picked = None, [], None  # 出している骨格のプロジェクト・出せるもの・全体で選んだもの

        self.root = tk.Tk()
        self.root.title("構成図")
        self.root.geometry("1400x860")
        self.root.configure(bg=C["bg"])
        top = tk.Frame(self.root, bg=C["panel"], height=44, highlightthickness=1, highlightbackground=C["line"])
        top.pack(side="top", fill="x")
        self.title = tk.Label(top, text="構成図", bg=C["panel"], fg=C["text"], font=font(12, True))
        self.title.pack(side="left", padx=14, pady=8)
        self.meta = tk.Label(top, text="", bg=C["panel"], fg=C["dim"], font=font(9))
        self.meta.pack(side="right", padx=14)
        tk.Label(top, text="押すと詳しく　f 全体　s 骨格だけ　← → 骨格をたどる　r 更新", bg=C["panel"], fg=C["faint"],
                 font=font(9)).pack(side="right", padx=10)
        # 図と現物のずれ・前回からの変化。あるときだけ紺の文字で出し、押すと右に一覧
        self.drift_btn = tk.Label(top, text="", bg=C["panel"], fg=C["bone"], font=font(9, True), cursor="hand2")
        self.drift_btn.pack(side="right", padx=6)
        self.drift_btn.bind("<Button-1>", lambda e: self.show_drift())
        self.change_btn = tk.Label(top, text="", bg=C["panel"], fg=C["bone"], font=font(9, True), cursor="hand2")
        self.change_btn.pack(side="right", padx=6)
        self.change_btn.bind("<Button-1>", lambda e: self.show_changes())
        self.legend = tk.Canvas(top, bg=C["panel"], height=24, width=820, highlightthickness=0)
        self.legend.pack(side="right", padx=8)
        self.draw_legend()

        self.tabbar = tk.Canvas(self.root, bg=C["bg"], height=36, highlightthickness=0)
        self.tabbar.pack(side="top", fill="x")
        self.tabbar.bind("<ButtonRelease-1>", self.on_tab)
        self.root.bind("<Control-Tab>", lambda e: self.switch((self.cur + 1) % len(self.tabs)))
        self.root.bind("<Control-w>", lambda e: self.close_tab(self.cur))
        # 下の帯（窓の幅いっぱい）: ウォーキングスケルトンを通る順に部品を並べ、どこまで本番で通ったかを一目で見せる
        self.strip = tk.Canvas(self.root, bg=C["panel"], height=64, highlightthickness=1,
                               highlightbackground=C["line"], highlightcolor=C["line"])
        self.strip.pack(side="bottom", fill="x")
        self.strip.bind("<ButtonRelease-1>", self.on_strip)
        self.strip.bind("<Configure>", lambda e: self.draw_strip())
        body = tk.Frame(self.root, bg=C["bg"])
        body.pack(fill="both", expand=True)
        self.side = tk.Text(body, width=34, bg=C["panel"], fg=C["text"], bd=0, padx=16, pady=16, wrap="word",
                            tabs=("84",), spacing1=2, spacing3=2, highlightthickness=1, highlightbackground=C["line"],
                            font=font(10), cursor="arrow")
        self.side_shown = False  # 右の欄は、部品かまとめた線を選んだときだけ出す（図に使える面積を広げる）
        self.side.tag_configure("h", font=font(12, True), spacing3=6)
        self.side.tag_configure("k", foreground=C["faint"], font=font(9))
        self.side.tag_configure("key", foreground=C["faint"], font=font(9))
        for s in ("live", "wip", "none"):
            self.side.tag_configure(s, foreground=C[s])
        self.side.tag_configure("stop", foreground=C["stop"])
        self.side.tag_configure("error", foreground=C["error"])
        self.side.tag_configure("bone", foreground=C["bone"], font=font(9, True))
        self.side.tag_configure("label", foreground=C["faint"], font=font(9, True), spacing1=12)
        self.side.tag_configure("next", foreground=C["text"], font=font(10, True))
        self.side.tag_configure("done", foreground=C["faint"])
        self.side.tag_configure("section", foreground=C["text"], font=font(10, True), spacing1=10)
        self.side.tag_configure("link", foreground=C["bone"])
        for tag in ("link", "section"):  # 押せる所は、指の形にする
            self.side.tag_bind(tag, "<Enter>", lambda e: (self.side.configure(cursor="hand2"),
                                                          self.side.tag_configure("link", underline=True)))
            self.side.tag_bind(tag, "<Leave>", lambda e: (self.side.configure(cursor="arrow"),
                                                          self.side.tag_configure("link", underline=False)))
        self.opened, self._links = {}, {}  # 畳んだ見出しの開き具合（部品ごと）と、押せる文字の行き先
        self.canvas = tk.Canvas(body, bg=C["bg"], highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Leave>", self.on_leave)
        self.hover_node, self.hover_edge = None, None  # マウスを乗せている部品・線
        self.canvas.bind("<Configure>", self.on_resize)
        self.root.bind("f", lambda e: self.fit(reset=True))
        self.builder, self.asked_at = None, 0.0  # 裏で頼んだ組み立てと、頼んだ時刻
        self.root.bind("r", lambda e: self.rebuild_if_old(force=True))  # r: 今すぐ組み立て直す（20 秒に 1 回まで）
        self.bones_only = False  # s で、ウォーキングスケルトン以外を薄くする
        self.flow, self.playing = None, None  # 骨格をたどっているときの番号と、再生の予約
        self.root.bind("<Right>", lambda e: self.flow_go(+1))
        self.root.bind("<Left>", lambda e: self.flow_go(-1))
        self.root.bind("<space>", lambda e: self.flow_play())
        self.root.bind("<Escape>", lambda e: self.flow_stop())
        self.root.bind("s", lambda e: (setattr(self, "bones_only", not self.bones_only), self.redraw()))
        self.show_side(None)
        self.tick()
        self.root.after(300, self.bring_front)
        self.root.after(300, self.listen_show)
        self.root.after(1500, self.watch_work)

    def bring_front(self):
        """窓を一番前に出す。いったん「常に手前」にしてすぐ戻す（Windows は裏から立てた窓を前に出させないため）。"""
        r = self.root
        r.deiconify()
        r.lift()
        r.attributes("-topmost", True)
        r.after(500, lambda: r.attributes("-topmost", False))
        try:
            # Windows は、いま前にいない処理からの「前に出して」を断る。前にいる窓の入力にいったん相乗りしてから頼むと受け付ける。
            # （Alt を一瞬押した形にする回避は、窓の動きが止まることがあったのでやめた・2026-10-06）
            user32, kernel32, hwnd = ctypes.windll.user32, ctypes.windll.kernel32, int(r.wm_frame(), 16)
            fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            me = kernel32.GetCurrentThreadId()
            if fg_thread and fg_thread != me:
                user32.AttachThreadInput(me, fg_thread, True)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                user32.AttachThreadInput(me, fg_thread, False)
            else:
                user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        r.focus_force()

    def listen_show(self):
        """2 回目に押されたら（already_open からの知らせ）、窓を前に出す。「open:<範囲>」ならタブを足す（あれば切り替える）。"""
        try:
            conn, _ = _lock.accept()
            msg = conn.recv(256).decode("utf-8", "replace")
            conn.close()
            if msg.startswith("open:"):
                self.open_tab(msg[5:])
            self.bring_front()
        except (BlockingIOError, NameError, OSError):
            pass
        self.root.after(300, self.listen_show)

    # -- タブ --------------------------------------------------------------------
    KEEP = ("scope", "ox", "oy", "scale", "moved", "selected", "flow", "shape", "lay", "view", "pending_view", "picked")

    def save_tab(self):
        self.tabs[self.cur].update({k: getattr(self, k) for k in self.KEEP})

    def switch(self, i):
        """タブを切り替える。見ていた場所は覚えておき、戻ったら同じ場所から。"""
        if i == self.cur or not (0 <= i < len(self.tabs)):
            return
        self.save_tab()
        self.load(i)

    def load(self, i):
        """タブ i の見ていた場所を取り出して描く（今のタブを覚えるのは呼ぶ側）。"""
        self.cur, tab = i, self.tabs[i]
        for k in self.KEEP:
            setattr(self, k, tab.get(k, {"scale": 1.0, "ox": 0.0, "oy": 0.0, "moved": False}.get(k)))
        self.hover_node = self.hover_edge = None
        self.redraw()
        self.show_side(self.selected)
        self.rebuild()  # 中身は最新の state で（並べ方が変わっていなければ描き直すだけ）

    def open_tab(self, scope):
        """その範囲の固定のタブへ切り替える。無ければ足す（prefix+o を押すたびに、今のプロジェクトをタブとして残せる）。"""
        for i, t in enumerate(self.tabs):
            if not t["follow"] and t["scope"] == scope:
                return self.switch(i)
        # 自動のタブが同じ範囲を出していても足す（自動のタブは押した時点で同じペインを追いかけ済みなので、
        # 「出していれば増やさない」にすると、タブが一度も増えなかった・2026-10-06）
        self.save_tab()
        self.tabs.append({"follow": False, "scope": scope})
        self.cur = len(self.tabs) - 1
        self.scope, self.lay, self.view, self.shape, self.pending_view = scope, None, None, None, None
        self.selected, self.flow, self.moved = None, None, False
        self.show_side(None)
        self.rebuild()

    def close_tab(self, i):
        if len(self.tabs) == 1:
            return  # 最後の 1 枚は閉じない
        if i == self.cur:  # 今のタブを閉じたら、となりのタブへ
            self.tabs.pop(i)
            self.load(min(i, len(self.tabs) - 1))
        else:
            self.save_tab()
            self.tabs.pop(i)
            self.cur -= 1 if i < self.cur else 0
            self.draw_tabs()

    def draw_tabs(self):
        """タブの帯。今のタブは白く浮かせ、ほかは紙の色に沈める。固定のタブには ×。"""
        tb, f, fb = self.tabbar, font(9), font(9, True)
        tb.delete("all")
        x = 12
        for i, t in enumerate(self.tabs):
            name = (t["scope"] or "全体") if t.get("scope") is not None else "…"
            label = ("自動: " + name) if t["follow"] else name
            on = i == self.cur
            closable = len(self.tabs) > 1
            w = (fb if on else f).measure(label) + (40 if closable else 24)
            tb.create_rectangle(x, 6, x + w, 36, fill=C["panel"] if on else C["bg"], outline=C["line"] if on else "",
                                tags=("tab", f"tab#{i}"))
            if on:  # 下の辺は消して、下の図とつながって見えるように
                tb.create_line(x + 1, 35, x + w, 35, fill=C["panel"])
            tb.create_text(x + 12, 21, text=label, anchor="w", fill=C["text"] if on else C["dim"], font=fb if on else f,
                           tags=("tab", f"tab#{i}"))
            if closable:
                tb.create_text(x + w - 14, 21, text="×", fill=C["faint"], font=font(10), tags=("tab", f"close#{i}"))
            x += w + 4
        tb.create_text(x + 10, 21, text="ほかのプロジェクトで prefix+o を押すと、タブが増える", anchor="w", fill=C["faint"], font=f)

    def on_tab(self, e):
        for item in reversed(self.tabbar.find_overlapping(e.x - 2, e.y - 2, e.x + 2, e.y + 2)):
            for t in self.tabbar.gettags(item):
                if t.startswith("close#"):
                    return self.close_tab(int(t[6:]))
                if t.startswith("tab#"):
                    return self.switch(int(t[4:]))

    # -- 状態を読む ------------------------------------------------------------
    def rebuild_if_old(self, force=False):
        """状態が古ければ、裏で組み立てを頼む（カンバンの画面を閉じていても、構成図だけで新しく保つ）。
        カンバンの画面が開いていれば 90 秒ごとに作り直されるので、ここからはほとんど頼まない。"""
        now = datetime.now(timezone.utc).timestamp()
        if self.builder and self.builder.poll() is None:
            return  # まだ組み立て中（重ねない）
        if now - self.asked_at < REBUILD_AT_LEAST:
            return
        try:
            age = now - datetime.fromisoformat(self.state["generated_at"]).timestamp() if self.state else 1e9
        except (KeyError, ValueError):
            age = 1e9
        if not force and age < REBUILD_EVERY + 10:  # カンバンの画面の 90 秒とぶつからないよう少し余裕を持つ
            return
        self.asked_at = now
        self.builder = subprocess.Popen([sys.executable, str(BOARD), "build"], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        self.meta.configure(text="組み立て直している…")

    def watch_work(self):
        """herdr のペイン全部のブランチから、作業中の部品を決める（arch.working）。変わったときだけ描き直す。
        herdr と git を呼ぶのは裏の糸で（窓の糸で待つと、遅いときに窓が数秒固まった・2026-10-06 のレビュー）。"""
        done = {}
        threading.Thread(target=lambda: done.update(branches=pane_branches()), daemon=True).start()
        self.root.after(100, self.apply_work, done)

    def apply_work(self, done):
        if "branches" not in done:
            self.root.after(100, self.apply_work, done)
            return
        try:
            a = (self.state or {}).get("arch") or {}
            prs = {proj: m.get("prs", {}) for proj, m in (self.state or {}).get("projects", {}).items()}
            now = arch.working(a.get("nodes", []), (self.state or {}).get("cards", []), prs, done["branches"])
            if now != self.working:
                self.working = now
                self.redraw()
        except Exception:
            pass
        self.root.after(5000, self.watch_work)

    def tick(self):
        try:
            self.rebuild_if_old()
            changed = False
            mtime = STATE.stat().st_mtime if STATE.exists() else 0
            if mtime != self.state_mtime:
                self.state_mtime = mtime
                self.state = json.loads(STATE.read_text(encoding="utf-8"))
                changed = True
            if changed:
                self.rebuild()
        except Exception as e:  # 描けなくても閉じない。次の回にやり直す
            self.meta.configure(text=f"読めない: {e}")
        self.root.after(POLL_MS, self.tick)

    def rebuild(self):
        a = (self.state or {}).get("arch")
        if not a:
            self.meta.configure(text="構成図の宣言が無い（~/.herdr/kanban-data/<project>/arch.json）")
            return
        keep = arch.scope(a, self.scope)
        # どのプロジェクトの骨格を出すか。プロジェクトの視点ではそのプロジェクトの骨格だけ（無ければ出さない）。
        # 全体では、帯の名前を押して切り替える（初めは最初のもの）
        owners = sorted({o for n in a["nodes"] if n["id"] in keep for o in (n.get("steps") or {})})
        if self.scope:
            self.owner = self.scope if self.scope in owners else None
        else:
            self.owner = self.picked if self.picked in owners else (owners[0] if owners else None)
        self.owners = owners if not self.scope else ([self.owner] if self.owner else [])
        # 部品は写しにして、出す骨格の番号だけを step に入れる（タブごとに出す骨格が違っても混ざらない）
        nodes = [{**n, "step": (n.get("steps") or {}).get(self.owner)} for n in a["nodes"] if n["id"] in keep]
        view = {"zones": a["zones"], "nodes": nodes,
                "edges": [e for e in a["edges"] if e["from"] in keep and e["to"] in keep]}
        shape = (tuple(sorted((n["id"], n["zone"], n["blocked"]) for n in nodes)),
                 tuple((e["from"], e["to"], e.get("label", "")) for e in view["edges"]))
        self.title.configure(text="構成図 — " + (f"{self.scope} とつながる部品" if self.scope else "全体"))
        drift = a.get("drift", {})
        n_drift = sum(len(drift[p]["unclaimed"]) + len(drift[p]["missing"]) for p in {x["project"] for x in nodes} if p in drift)
        self.drift_btn.configure(text=f"図と現物のずれ {n_drift}" if n_drift else "")
        ch = a.get("changes", {})
        n_change = len([x for x in ch.get("nodes", {}) if x in {y["id"] for y in nodes}])
        self.change_btn.configure(text=f"前回から {n_change} 件" if n_change else "")
        self.tabs[self.cur]["scope"] = self.scope
        self.draw_tabs()
        self.meta.configure(text=f"{self.state['generated_at'][11:16]} 時点・{len(nodes)} 部品")
        if shape != self.shape:
            # 並べ直しは裏で行う（Graphviz に数百ミリ秒かかり、そのあいだ窓が固まっていた）。
            # 並べ終わるまでは前の図のまま（中身と並べ方は必ず組で入れ替える）
            self.shape, self.pending_view, done = shape, view, {}
            cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
            aspect = cw / ch if cw > 200 and ch > 200 else 1.6  # 窓の形に近くなるよう並べる（上下の空きを減らす）
            threading.Thread(target=lambda: done.update(lay=safe_layout(view, aspect)), daemon=True).start()
            self.wait_layout(view, done)
            return
        self.view = view
        self.redraw()
        if self.selected:
            self.show_side(self.selected)

    def wait_layout(self, view, done):
        if "lay" not in done:
            self.root.after(50, self.wait_layout, view, done)
            return
        if view is not self.pending_view:  # 待っているあいだに次の中身が来た
            return
        lay = done["lay"]
        if isinstance(lay, Exception):
            self.meta.configure(text=f"並べられない: {lay}")
            return
        self.view, self.lay, self.moved = view, lay, False
        self.fit()
        if self.selected:
            self.show_side(self.selected)

    # -- 描く --------------------------------------------------------------------
    def fit(self, reset=False):
        if reset:
            self.moved = False
        if not self.lay:
            return
        w, h = max(self.canvas.winfo_width(), 200), max(self.canvas.winfo_height(), 200)
        sw, sh = self.lay["size"]
        self.scale = min((w - 24) / sw, (h - 24) / sh, 1.4)
        self.ox, self.oy = (w - sw * self.scale) / 2, (h - sh * self.scale) / 2
        self.redraw()

    def xy(self, x, y):
        return self.ox + x * self.scale, self.oy + y * self.scale

    def rounded(self, x1, y1, x2, y2, r, **kw):
        r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
               x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def paper(self):
        """点の方眼の背景。窓の大きさが変わったときだけ 1 枚の画像として作り直す（点を 1 つずつ図形にすると重い）。"""
        w, h = max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)
        if getattr(self, "_paper_size", None) != (w, h):
            img = tk.PhotoImage(width=w, height=h)
            img.put(C["bg"], to=(0, 0, w, h))
            for y in range(DOT_STEP // 2, h, DOT_STEP):
                for x in range(DOT_STEP // 2, w, DOT_STEP):
                    img.put(C["dot"], to=(x, y, x + 2, y + 2))
            self._paper, self._paper_size = img, (w, h)
        self.canvas.create_image(0, 0, image=self._paper, anchor="nw", tags=("paper",))

    def redraw(self):
        cv, s = self.canvas, self.scale
        cv.delete("all")
        self.paper()
        if not self.view or not self.lay:
            return
        marks = iter(PROJECT_MARKS * 3)
        zone_mark = {z["id"]: KIND_MARK.get(z.get("kind")) or next(marks) for z in self.view["zones"]}
        zones = {z["id"]: z for z in self.view["zones"]}
        reasons = {}
        for zi, z in enumerate(self.view["zones"]):
            groups = []
            for n in self.view["nodes"]:
                if n["zone"] == z["id"] and n["blocked"] and n["blocked"] not in groups:
                    groups.append(n["blocked"])
            for gi, reason in enumerate(groups):
                reasons[f"z{zi}_b{gi}"] = reason
        # 枠（システム）: 白地に 1px の灰色。系の区別は名前の横の小さな色の四角だけ
        tags_later = []
        for f in self.lay["frames"]:
            x1, y1 = self.xy(*f["box"][:2])
            x2, y2 = self.xy(*f["box"][2:])
            if f["key"] in reasons:  # 止まっている所: 灰色の破線で囲み、札は部品のあとに描く
                cv.create_rectangle(x1, y1, x2, y2, outline=C["faint"], dash=(4, 3), width=1)
                tags_later.append((x1, y1, x2, reasons[f["key"]]))
                continue
            z = zones[self.view["zones"][int(f["key"][1:])]["id"]]
            self.rounded(x1, y1, x2, y2, R_ZONE * s, fill=zone_mark[z["id"]], outline=C["card_line"], width=1, tags=("zone",))  # 枠は薄い灰色の細い線
            cv.create_text(x1 + 16 * s, y1 + 10 * s, text=z["title"], anchor="nw", fill=C["text"], font=font(14 * s, True))
            if z.get("sub"):
                cv.create_text(x1 + 16 * s, y1 + 33 * s, text=z["sub"], anchor="nw", fill=C["dim"], font=font(9 * s))
        # 線。普段は枠の中の線と、枠どうしをまとめた線だけ。部品を押す・乗せると、その部品の線を行き先まで出す
        focus = self.hover_node or self.selected
        labels = []  # 名札は部品を描いたあとに、かぶらない場所を探して置く
        zone_of = {n["id"]: n["zone"] for n in self.view["nodes"]}
        mine = {i for i, e in enumerate(self.view["edges"]) if focus in (e["from"], e["to"])}
        near = {focus} | {x for i in mine for x in (self.view["edges"][i]["from"], self.view["edges"][i]["to"])}
        for b_i, b in enumerate(self.lay.get("bundles", [])):
            pts = [c for pt in b["points"] for c in self.xy(*pt)]
            quiet = bool(focus) or self.bones_only
            if len(pts) >= 4:
                cv.create_line(*pts, fill=C["edge_quiet"] if quiet else C["edge"], width=1 if quiet else 2,
                               dash=DASH[b["kind"]], arrow=b["arrow"], arrowshape=(9, 10, 4),
                               tags=("bundle", f"b#{b_i}"))
            if not quiet:
                labels.append((f"{len(b['edges'])} 本", font(9 * s), b["points"], ("bundle", f"b#{b_i}")))
        for i, (e, p) in enumerate(zip(self.view["edges"], self.lay["paths"])):
            cross = zone_of[e["from"]] != zone_of[e["to"]]
            if cross and i not in mine:
                continue  # 枠をまたぐ線は、まとめた線で出している
            pts = [c for pt in p["points"] for c in self.xy(*pt)]
            kind = "stop" if e.get("blocked") else e["status"]  # 線の種類は 4 つだけ: 本番・作りかけ・まだ無い・止まっている
            faded = (self.bones_only and not e.get("skeleton")) or (focus and i not in mine)
            strong = i in mine
            if len(pts) >= 4:  # 太さは整数だけ（1.6 のような値は、場所によって 1 と 2 に丸められて揺れた）
                cv.create_line(*pts, fill=C["edge_quiet"] if faded else (C["bone"] if strong else C["edge"]),
                               width=2 if strong else 1, dash=DASH[kind], arrow="last", arrowshape=(8, 9, 3),
                               tags=("edge", f"e#{i}"))
            if e.get("label") and (strong or self.hover_edge == i):  # 名札は、その線を見ているときだけ
                labels.append((e["label"], font(9 * s), p["points"], ("edge", f"e#{i}")))
        # 部品: 白いカードに 1px の灰色。状態は「丸 + 文字」（色だけに頼らない）
        sk = arch.through(self.bones())
        w, h = NODE_W * 72 * s, NODE_H * 72 * s
        for n in self.view["nodes"]:
            cx, cy = self.xy(*self.lay["pos"][n["id"]])
            x1, y1, x2, y2 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
            kind = "stop" if n["blocked"] else n["status"]
            sel = n["id"] == self.selected
            bone = n.get("skeleton")
            # 部品を選んでいるときは、つながっていない部品も薄くする（Ilograph の「関係ないものを隠す」に倣う）
            faded = (self.bones_only and not bone) or (focus and n["id"] not in near)
            tags = ("node", n["id"])
            if faded:
                self.rounded(x1, y1, x2, y2, R_NODE * s, fill=C["bg"], outline=C["edge_quiet"], width=1, tags=tags)
            else:
                if n["id"] == sk["next"]:  # 見どころは 1 か所だけ: 次に通す部品を紺の太い輪で囲む
                    g = 5 * s
                    self.rounded(x1 - g, y1 - g, x2 + g, y2 + g, (R_NODE + 4) * s, fill="", outline=C["bone"], width=3)
                    cv.create_text(x2, y1 - g - 3 * s, text="次はここ", anchor="se", fill=C["bone"], font=font(9 * s, True))
                self.rounded(x1, y1 + 1, x2, y2 + 1, R_NODE * s, fill=C["shadow"], outline="")  # 1px だけ下にずらした影
                fill = {"wip": C["tint_wip"], "stop": C["tint_stop"], "none": C["tint_none"]}.get(kind, C["card"])
                self.rounded(x1, y1, x2, y2, R_NODE * s, fill=fill, outline=C["bone"] if sel else C["card_line"],
                             width=2 if sel else 1, dash=(4, 3) if kind == "none" and not sel else (), tags=tags)
            pad = 12 * s
            sf, tf = font(9 * s), font(10 * s, True)
            word = "" if kind == "live" else STATUS_SHORT[kind]  # 本番は丸だけ（多数派なので文字を出すと題を削る）。例外だけ文字も
            ww = (sf.measure(word) + 12 * s) if word else 10 * s
            if not faded:  # 右上に状態の丸と文字
                r = 3.5 * s
                dx, dy = x2 - pad - ww + r, y1 + 15 * s
                cv.create_oval(dx - r, dy - r, dx + r, dy + r, fill=C[kind], outline="", tags=tags)
                cv.create_text(x2 - pad, dy, text=word, anchor="e", fill=C["dim"], font=sf, tags=tags)
            cv.create_text(x1 + pad, y1 + 15 * s, text=clip(n["title"], tf, w - pad * 2 - ww - 6 * s), anchor="w",
                           fill=C["faint"] if faded else C["text"], font=tf, tags=tags)
            cv.create_text(x1 + pad, y2 - 14 * s, text=clip(n.get("sub", ""), sf, w - pad * 2 - 30 * s), anchor="w",
                           fill=C["faint"], font=sf, tags=tags)
            work = self.working.get(n["id"])
            if work and not faded:  # 今どこを作業しているか（herdr のペインのブランチ → PR → カード → 部品）
                cv.create_text(x1 + 14 * s, y1 - 4 * s, text="作業中" + ("（今のペイン）" if work["focused"] else ""),
                               anchor="sw", fill=C["bone"], font=font(9 * s, True), tags=tags)
            moved = (self.state.get("arch") or {}).get("changes", {}).get("nodes", {}).get(n["id"])
            if moved and not faded:  # 前回から状態か待ちが変わった
                cv.create_text(x2 - pad, y2 - 14 * s, text="変化", anchor="e", fill=C["bone"], font=font(8.5 * s, True), tags=tags)
            if n.get("step") and not faded:  # 骨格の番号。切れ目なく本番の所までは塗り、その先は輪だけ
                r, mx, my = 9 * s, x1, y1
                through = n["step"] <= sk["through"]
                cv.create_oval(mx - r, my - r, mx + r, my + r, fill=C["bone"] if through else C["card"],
                               outline=C["bone"], width=1, tags=tags)
                cv.create_text(mx, my, text=str(n["step"]), fill="#ffffff" if through else C["bone"],
                               font=font(8.5 * s, True, NUM_FONT), tags=tags)
        for x1, y1, x2, reason in tags_later:  # 止まっている所の札（赤い丸 = 止まっている。文字は理由だけ、枠の幅に収める）
            f = font(9 * s)
            text = clip(reason, f, x2 - x1 - 34 * s)
            tw = f.measure(text)
            bx1, by1 = x1 + 6 * s, y1 + 3 * s  # 枠の内側の上（Graphviz が名前の場所を空けている）
            cv.create_rectangle(bx1, by1, bx1 + tw + 22 * s, by1 + 18 * s, fill=C["panel"], outline=C["line"])
            cv.create_oval(bx1 + 6 * s, by1 + 6 * s, bx1 + 12 * s, by1 + 12 * s, fill=C["stop"], outline="")
            cv.create_text(bx1 + 16 * s, by1 + 9 * s, text=text, anchor="w", fill=C["dim"], font=f)
        for text, f, points, tags in labels:
            self.place_label(text, f, points, tags)
        cv.addtag_all("content")  # 動かすのは図だけ。方眼の紙は動かさない
        cv.dtag("paper", "content")
        self.draw_strip()

    def bones(self):
        return sorted((n for n in (self.view or {}).get("nodes", []) if n.get("step")), key=lambda n: n["step"])

    def flow_go(self, d):
        """骨格を 1 つ進める・戻す。その部品を選んだ状態にして、出入りする線を出す。"""
        bones = self.bones()
        if not bones:
            return
        i = 0 if self.flow is None else max(0, min(len(bones) - 1, self.flow + d))
        self.flow = i
        n = bones[i]
        self.selected = n["id"]
        if self.lay and n["id"] in self.lay["pos"]:  # 見えていなければ真ん中へ
            px, py = self.xy(*self.lay["pos"][n["id"]])
            if not (40 < px < self.canvas.winfo_width() - 40 and 40 < py < self.canvas.winfo_height() - 40):
                self.ox += self.canvas.winfo_width() / 2 - px
                self.oy += self.canvas.winfo_height() / 2 - py
                self.moved = True
        self.redraw()
        self.show_side(n["id"])
        return n

    def flow_play(self):
        """Space: 骨格を 1 番から順にたどる。本番でない部品に来たら、そこで止まる（次に通す所）。もう一度押すと止める。"""
        if self.playing:
            self.root.after_cancel(self.playing)
            self.playing = None
            return
        if self.flow is None or self.flow >= len(self.bones()) - 1:
            self.flow = None
        self.flow_tick()

    def flow_tick(self):
        n = self.flow_go(+1 if self.flow is not None else 0)
        bones = self.bones()
        if not n or n["status"] != "live" or n["blocked"] or self.flow >= len(bones) - 1:
            self.playing = None
            return
        self.playing = self.root.after(1100, self.flow_tick)

    def flow_stop(self):
        if self.playing:
            self.root.after_cancel(self.playing)
        self.flow, self.playing, self.selected = None, None, None
        self.redraw()
        self.show_side(None)

    def place_label(self, text, f, points, tags):
        """線の上で、ほかの線・部品・名札にいちばんかぶらない場所に名札を置く（真ん中から外へ順に試す）。"""
        cv = self.canvas
        pts = [self.xy(*q) for q in points]
        if len(pts) < 2:
            return
        seg = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
        total = sum(seg) or 1
        w, h = f.measure(text) + 10, f.metrics("linespace") + 2
        own = set()
        for t in tags:
            own |= set(cv.find_withtag(t))
        best = None
        for frac in (0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8, 0.12, 0.88):
            d, x, y = frac * total, *pts[0]
            for (a, b), l in zip(zip(pts, pts[1:]), seg):
                if d <= l and l:
                    x, y = a[0] + (b[0] - a[0]) * d / l, a[1] + (b[1] - a[1]) * d / l
                    break
                d -= l
            box = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
            crowd = [i for i in cv.find_overlapping(*box) if i not in own and "paper" not in cv.gettags(i)
                     and "zone" not in cv.gettags(i)]
            if best is None or len(crowd) < best[0]:
                best = (len(crowd), x, y)
            if not crowd:
                break
        _, x, y = best
        self.pill(x - w / 2, y - h / 2, x + w / 2, y + h / 2, tags)  # 名札は丸端のチップ
        cv.create_text(x, y, text=text, fill=C["dim"], font=f, tags=tags)  # 部品のあとに描くので、いちばん上に出る

    def pill(self, x1, y1, x2, y2, tags=(), fill=None, outline=None, canvas=None):
        """丸端のチップ（両端が半円）。"""
        cv, r = canvas or self.canvas, (y2 - y1) / 2
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
               x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return cv.create_polygon(pts, smooth=True, fill=fill or C["panel"], outline=outline or C["line"], tags=tags)

    def draw_strip(self):
        """下の帯。骨格の部品を番号順に並べる。切れ目なく本番の所までは濃く、その先は薄く。押すとその部品へ。"""
        st = self.strip
        st.delete("all")
        bones = self.bones()
        if not bones:
            where = f"{self.scope} には" if self.scope else "この範囲には"
            st.create_text(16, 32, text=f"{where}ウォーキングスケルトンが無い（計画書の ◆ の項目か、宣言の step で決まる）",
                           anchor="w", fill=C["faint"], font=font(9))
            return
        sk = arch.through(bones)  # 個数ではなく、端から切れ目なく本番の所まで
        many = len(self.owners) > 1
        st.create_text(16, 22, text=f"{self.owner} の骨格" + (" ›" if many else ""), anchor="w",
                       fill=C["bone"] if many else C["faint"], font=font(9, True), tags=("owner",) if many else ())
        value = "全部本番" if sk["next"] is None else f"{sk['through']} 番まで本番"
        head = st.create_text(16, 41, text=f"{value} / 全 {len(bones)}", anchor="w", fill=C["text"], font=font(10, True))
        x0, width = max(st.bbox(head)[2], 150) + 24, max(st.winfo_width(), 400)
        f = font(9)
        each = (width - 16 - x0 - 12 * (len(bones) - 1)) / len(bones)  # 1 つあたりに使える幅
        widths = [max(36, min(f.measure(n["title"]) + 42, each)) for n in bones]
        x = x0
        for n, cw in zip(bones, widths):
            sel = n["id"] == self.selected
            done = n["step"] <= sk["through"]
            tags = ("chip", n["id"])
            self.pill(x, 18, x + cw, 46, tags, fill=C["bone"] if sel else C["panel"],
                      outline=C["bone"] if sel else C["line"], canvas=st)
            ink = "#ffffff" if sel else (C["text"] if done else C["faint"])
            st.create_oval(x + 7, 24, x + 23, 40, fill=("#ffffff" if sel else C["bone"]) if done else C["panel"],
                           outline="#ffffff" if sel else C["bone"], tags=tags)
            st.create_text(x + 15, 32, text=str(n["step"]), fill=(C["bone"] if sel else "#ffffff") if done else C["bone"],
                           font=font(8, True, NUM_FONT), tags=tags)
            st.create_text(x + 29, 32, text=clip(n["title"], f, cw - 36), anchor="w", fill=ink, font=f, tags=tags)
            if n["id"] == sk["next"]:  # 次に通す部品
                st.create_text(x + cw / 2, 9, text="次はここ", fill=C["bone"], font=font(8, True))
            if n is not bones[-1]:
                st.create_text(x + cw + 6, 32, text="›", fill=C["faint"], font=font(10))
            x += cw + 12

    def on_strip(self, e):
        hit = [t for i in self.strip.find_overlapping(e.x, e.y, e.x, e.y) for t in self.strip.gettags(i)
               if t not in ("chip", "current")]
        if "owner" in hit and len(self.owners) > 1:  # 全体で、出す骨格を次のプロジェクトへ
            self.picked = self.owners[(self.owners.index(self.owner) + 1) % len(self.owners)]
            self.flow = None
            return self.rebuild()
        if not hit or not self.lay or hit[-1] not in self.lay["pos"]:
            return
        self.selected = hit[-1]
        px, py = self.lay["pos"][self.selected]  # その部品を図の真ん中に持ってくる
        self.ox = self.canvas.winfo_width() / 2 - px * self.scale
        self.oy = self.canvas.winfo_height() / 2 - py * self.scale
        self.moved = True
        self.redraw()
        self.show_side(self.selected)

    def draw_legend(self):
        """線の種類 4 つ（形で分ける）と、状態の丸 4 つを、ヘッダーの右に小さく 1 行で。"""
        lg, x, f = self.legend, 4, font(9)
        for k in ("live", "wip", "none", "stop"):
            lg.create_line(x, 12, x + 24, 12, fill=C["edge"], width=1, dash=DASH[k])
            x += 30
            r = 3.5
            lg.create_oval(x - r, 12 - r, x + r, 12 + r, fill=C[k], outline="")
            t = lg.create_text(x + 8, 12, text=STATUS_SHORT[k], anchor="w", fill=C["dim"], font=f)
            x = lg.bbox(t)[2] + 18

    # -- 右の詳しい中身 ---------------------------------------------------------
    def panel(self, show):
        """右の欄を出す・たたむ。出し入れで図の倍率は変えない（選ぶたびに図が縮むと見失う）。"""
        if show == self.side_shown:
            return
        self.side_shown, self._keep_scale = show, True
        if show:
            self.side.pack(side="right", fill="y", before=self.canvas)
        else:
            self.side.pack_forget()

    def show_side(self, node_id):
        """部品の詳しい中身。上に要約（なぜ・次の一手・主な PR・AI に渡す文）、その下に畳む材料、最後に外へのリンク。
        中身は arch.py の detail が決める（ここは並べるだけ）。全部を同じ重さで並べない・空欄にしない・書き換えさせない。"""
        t = self.side
        t.configure(state="normal")
        t.delete("1.0", "end")
        self._links = {}
        n = next((x for x in (self.view or {}).get("nodes", []) if x["id"] == node_id), None)
        self.panel(bool(n))
        if not n:
            t.insert("end", "部品を押すと、ここに中身が出る\n", "h")
            t.insert("end", "ドラッグで移動・ホイールで拡大縮小・f で全体・s で骨格（ウォーキングスケルトン）だけ。"
                            "左右の矢印キーで骨格を 1 つずつ、Space で 1 番から再生（本番でない所で止まる）、Esc で終わる。"
                            "見るだけで、書き込みはできない。\n", "k")
            t.insert("end", "\nタブの範囲は、そのタブを開いたときに選んでいたペインのフォルダで決まる（リポジトリならそのプロジェクトとつながる部品、"
                            "リポジトリを並べたフォルダなら全部）。\n", "k")
            t.configure(state="disabled")
            return
        d = n.get("detail") or {}
        projects = (self.state or {}).get("projects", {})
        names = {x["id"]: x["title"] for x in self.view["nodes"]}
        gh = lambda project: (projects.get(project) or {}).get("github")
        sk = arch.through(self.bones())

        # -- 要約（スクロールせずに見える範囲） --
        t.insert("end", n["title"] + "\n", "h")
        kind = "stop" if n["blocked"] else n["status"]
        t.insert("end", "● ", kind)
        t.insert("end", STATUS_TEXT[kind] + "\n")
        rows = [("説明", n.get("sub", ""))]
        if self.working.get(n["id"]):
            rows.append(("作業中", self.working[n["id"]]["branch"]))
        ch = (self.state.get("arch") or {}).get("changes", {})
        if ch.get("nodes", {}).get(n["id"]):
            c = ch["nodes"][n["id"]]
            old = c["from"]
            rows.append((f"前回から", (STATUS_TEXT[old["status"]] if old else "新しく宣言") + " → " + STATUS_TEXT[c["to"]["status"]]
                         + (f"（{c['to']['blocked']}）" if c["to"]["blocked"] else "")))
        if n.get("step"):
            rows.append(("骨格", f"{n['step']} 番目" + ("（次はここ）" if n["id"] == sk["next"] else "")))
        if n["refs"]:
            rows.append(("計画書", "、".join(r["id"].split(":", 1)[1] + (" ◆" if r.get("critical") else "") for r in n["refs"])))
        for key, value in rows:
            if value:
                t.insert("end", key + "\t", "key")
                t.insert("end", value + "\n")
        t.insert("end", "なぜ\n", "label")
        t.insert("end", d.get("reason", "") + "\n", "stop" if n["blocked"] else "")
        t.insert("end", "次の一手\n", "label")
        t.insert("end", d.get("next", "") + "\n", "next")
        pr = d.get("main_pr")
        planned = bool(n["refs"])  # 計画書の項目を持たない部品（外部・他チームなど）には、やること・PR・メモが無い
        if planned:
            t.insert("end", "PR\n", "label")
            if pr:
                self.link(f"#{pr['number']} {pr['title']}", self.gh_url(gh(pr["project"]), f"pull/{pr['number']}"))
                t.insert("end", f"　{pr['why'] or pr['state'].lower()}\n", "k")
            else:
                t.insert("end", "まだ無い（PR を出すと、題の計画書の番号かカードの prs でつながる）\n", "k")
        btn = tk.Label(t, text="AI に渡す文をコピー", bg=C["panel"], fg=C["bone"], font=font(9, True), cursor="hand2",
                       padx=10, pady=4, highlightthickness=1, highlightbackground=C["line"])  # 枠線だけのボタン
        btn.bind("<Button-1>", lambda e: self.copy(d.get("ai", "")))
        t.insert("end", "\n")
        t.window_create("end", window=btn, padx=0, pady=4)
        t.insert("end", f"　{(self.state or {}).get('generated_at', '')[11:16]} 時点\n", "k")

        # -- 畳む材料 --
        items = d.get("items") or []
        planned and self.section(n["id"], "やること", f"（{d['progress']}）" if items else "", lambda: [
            t.insert("end", ("■ " if i["done"] else "□ ") + i["text"] + "\n", "done" if i["done"] else "")
            for i in items] if items else t.insert("end", "カードにやることが無い（作業を始めたらカードの items に足す）\n", "k"),
            default=bool(n["blocked"]))
        prs = d.get("prs") or []
        def show_prs():
            if not prs:
                t.insert("end", "つながっている PR はまだ無い\n", "k")
            for p in sorted(prs, key=lambda p: -p["number"]):
                self.link(f"#{p['number']}", self.gh_url(gh(p["project"]), f"pull/{p['number']}"))
                t.insert("end", f" {p['state'].lower()}" + (f"・{p['why']}" if p["state"] == "OPEN" and p["why"] else "")
                         + f"　{p['title']}\n", "k" if p["state"] != "OPEN" else "")
        planned and self.section(n["id"], "PR", f"（{len(prs)} 本）", show_prs)
        mine = [e for e in self.view["edges"] if n["id"] in (e["from"], e["to"])]
        checked = [e for e in mine if e.get("note")]
        def show_checks():
            if n.get("switch_note"):  # 部品の本番のスイッチ
                ok = "が無い" not in n["switch_note"]
                t.insert("end", ("✓ " if ok else "✗ ") + "本番のスイッチ\n", "live" if ok else "error")
                t.insert("end", "   " + n["switch_note"] + "\n", "k")
            if not checked and not n.get("switch_note"):
                t.insert("end", "コードで確かめている線は無い（宣言の線に check を足すと、origin/main で確かめる）\n", "k")
            for e in checked:
                ok = e["note"].startswith("コードで確かめた") and "が無い" not in e["note"]
                t.insert("end", ("✓ " if ok else "✗ ") + f"{names.get(e['from'])} → {names.get(e['to'])}\n", "live" if ok else "error")
                t.insert("end", "   " + e["note"] + "\n", "k")
        (planned or checked or n.get("switch_note")) and self.section(
            n["id"], "確かめた結果", f"（{len(checked) + bool(n.get('switch_note'))}）", show_checks)
        def show_edges():
            for title, end, other in (("入ってくる", "to", "from"), ("出ていく", "from", "to")):
                for e in [e for e in mine if e[end] == n["id"]]:
                    kind = "stop" if e.get("blocked") else e["status"]
                    t.insert("end", "● ", kind)
                    t.insert("end", f"{title}: ")
                    self.link(names.get(e[other], e[other]), None, select=e[other])
                    t.insert("end", (f"（{e['label']}）" if e.get("label") else "")
                             + (f"　{e['blocked']}" if e.get("blocked") else "") + "\n")
        ins = sum(1 for e in mine if e["to"] == n["id"])
        self.section(n["id"], "つながり", f"（入る {ins}・出る {len(mine) - ins}）", show_edges)
        notes = d.get("notes") or []
        planned and self.section(n["id"], "メモ（経緯）", "", lambda: [
            t.insert("end", f"{x['ref'].split(':', 1)[1]}: {x['text']}\n") for x in notes] if notes
            else t.insert("end", "メモは無い\n", "k"))

        # -- 外へのリンク（中身は出さない） --
        t.insert("end", "\n開く\n", "label")
        any_link = False
        for r in n["refs"]:
            project = r["id"].split(":", 1)[0]
            plan = (projects.get(project) or {}).get("plan") or {}
            if plan.get("path") and gh(project):
                self.link(f"計画書（{r['id'].split(':', 1)[1]}）", self.gh_url(gh(project), f"blob/main/{plan['path']}"))
                t.insert("end", "　")
                any_link = True
                break
        for label, key in (("コード", "paths"), ("設計", "docs")):
            for path in n.get(key) or []:
                self.link(f"{label}: {path.rsplit('/', 1)[-1]}", self.gh_url(gh(n["project"]), f"blob/main/{path}"))
                t.insert("end", "　")
                any_link = True
        if not any_link:
            t.insert("end", "リンクが無い（宣言に paths・docs を足すと、コードと設計へ飛べる）", "k")
        t.insert("end", "\n")
        t.configure(state="disabled")

    def list_panel(self, title, lines):
        """右の欄に一覧を出す（宣言漏れ・前回からの変化）。lines は (文字, タグ, 押したときの行き先 or None)。"""
        t = self.side
        self.panel(True)
        self.selected = None
        self.redraw()
        t.configure(state="normal")
        t.delete("1.0", "end")
        self._links = {}
        t.insert("end", title + "\n", "h")
        for text, tag, url in lines:
            if url:
                self.link(text, url)
                t.insert("end", "\n")
            else:
                t.insert("end", text + "\n", tag)
        t.configure(state="disabled")

    def projects_in_view(self):
        return sorted({n["project"] for n in (self.view or {}).get("nodes", [])})

    def show_drift(self):
        """図と現物のずれ（宣言漏れ・消えたファイル）。今のタブに出ているプロジェクトだけ。"""
        drift = (self.state.get("arch") or {}).get("drift", {})
        projects = (self.state or {}).get("projects", {})
        lines = [("見張っている場所にあるのに、どの部品の paths にも無いもの（宣言漏れ）と、paths に書いたのに "
                  "origin/main に無いもの。部品の paths に足すか、宣言の ignore に足すと消える。", "k", None)]
        for p in self.projects_in_view():
            d = drift.get(p)
            if not d or not (d["unclaimed"] or d["missing"]):
                continue
            gh = (projects.get(p) or {}).get("github")
            lines.append(("", "", None))
            lines.append((f"{p}", "label", None))
            for f in d["unclaimed"]:
                lines.append((f"宣言に無い: {f}", "", self.gh_url(gh, f"tree/main/{f}")))
            for m in d["missing"]:
                lines.append((f"無くなった: {m['path']}（{m['node'].split(':', 1)[1]}）", "error", None))
        self.list_panel("図と現物のずれ", lines)

    def show_changes(self):
        """前回（その日の最初の写し）からの変化。"""
        ch = (self.state.get("arch") or {}).get("changes", {})
        names = {n["id"]: n["title"] for n in (self.state.get("arch") or {}).get("nodes", [])}
        lines = [(f"{ch.get('since') or '—'} の初めから、状態か待ちが変わった部品。", "k", None)]
        for o, k in ch.get("skeletons", {}).items():
            lines.append((f"{o} の骨格: {k['from'] or 0} 番まで → {k['to']} 番まで本番", "next", None))
        for nid, c in ch.get("nodes", {}).items():
            old = c["from"]
            lines.append((f"{names.get(nid, nid)}: " + (STATUS_TEXT[old["status"]] if old else "新しく宣言") + " → "
                          + STATUS_TEXT[c["to"]["status"]] + (f"（{c['to']['blocked']}）" if c["to"]["blocked"] else ""), "", None))
        self.list_panel("前回からの変化", lines)

    def section(self, node_id, title, count, fill, default=False):
        """畳める見出し。押すと開く・閉じる（部品ごとに覚えておく）。"""
        t, key = self.side, (node_id, title)
        opened = self.opened.get(key, default)
        tag = f"sec:{title}"
        t.insert("end", ("⌄ " if opened else "› ") + title, ("section", tag))
        t.insert("end", count + "\n", ("k", tag))
        t.tag_bind(tag, "<Button-1>", lambda e: (self.opened.__setitem__(key, not opened), self.show_side(node_id)))
        if opened:
            fill()

    def link(self, text, url, select=None):
        """押せる文字。url があれば既定のブラウザで開く。select があれば図の中のその部品を選ぶ。"""
        t = self.side
        tag = f"link{len(self._links)}"
        self._links[tag] = (url, select)
        t.insert("end", text, ("link", tag))
        t.tag_bind(tag, "<Button-1>", lambda e, tag=tag: self.follow(tag))

    def follow(self, tag):
        url, select = self._links[tag]
        if url:
            webbrowser.open(url)
        elif select:
            self.selected = select
            self.redraw()
            self.show_side(select)

    @staticmethod
    def gh_url(github, rest):
        return f"https://github.com/{github}/{rest}" if github else None

    def copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.meta.configure(text="AI に渡す文をコピーした")

    # -- 操作 --------------------------------------------------------------------
    def on_resize(self, e):
        """大きさが変わり終わってから 1 回だけ収め直す（変わっている途中の古い大きさで収めると、図がずれた）。"""
        if getattr(self, "_resize", None):
            self.root.after_cancel(self._resize)
        keep, self._keep_scale = getattr(self, "_keep_scale", False), False  # 右の欄の出し入れでは収め直さない
        self._resize = self.root.after(80, lambda: self.fit() if not (self.moved or keep) else self.redraw())

    def on_press(self, e):
        self.drag = (e.x, e.y, self.ox, self.oy, False)

    def on_drag(self, e):
        if not self.drag:
            return
        x0, y0, ox, oy, _ = self.drag
        if abs(e.x - x0) + abs(e.y - y0) > 3:
            self.drag = (x0, y0, ox, oy, True)
            nx, ny = ox + e.x - x0, oy + e.y - y0
            self.canvas.move("content", nx - self.ox, ny - self.oy)  # 描き直さずにずらすだけ（速い）
            self.ox, self.oy = nx, ny
            self.moved = True

    def on_release(self, e):
        moved = self.drag and self.drag[4]
        self.drag = None
        if moved:
            return
        hit = self.hit(e.x, e.y)
        if hit and hit.startswith("b#"):  # まとめた線: 中身を右に一覧で出す
            self.selected = None
            self.redraw()
            self.show_bundle(int(hit[2:]))
            return
        self.selected = hit if hit and not hit.startswith("e#") else None
        self.redraw()
        self.show_side(self.selected)

    def hit(self, x, y):
        """その場所のいちばん上にあるもの: 部品の id / "e#<線の番号>" / "b#<まとめた線の番号>" / None。"""
        for item in reversed(self.canvas.find_overlapping(x - 3, y - 3, x + 3, y + 3)):
            for t in self.canvas.gettags(item):
                if ":" in t or t.startswith(("e#", "b#")):
                    return t
        return None

    def on_motion(self, e):
        """マウスを乗せた部品の線と、乗せた線の名札を出す（変わったときだけ描き直す）。"""
        if self.drag:
            return
        hit = self.hit(e.x, e.y)
        node = hit if hit and ":" in hit else None
        edge = int(hit[2:]) if hit and hit.startswith("e#") else None
        if (node, edge) != (self.hover_node, self.hover_edge):
            self.hover_node, self.hover_edge = node, edge
            self.redraw()

    def on_leave(self, e):
        if (self.hover_node, self.hover_edge) != (None, None):
            self.hover_node, self.hover_edge = None, None
            self.redraw()

    def show_bundle(self, b_i):
        """まとめた線の中身（どの部品からどの部品へ）を右に出す。"""
        b = self.lay["bundles"][b_i]
        self.panel(True)
        t, names = self.side, {x["id"]: x["title"] for x in self.view["nodes"]}
        zones = {z["id"]: z["title"] for z in self.view["zones"]}
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", f"{zones[b['zones'][0]]} と {zones[b['zones'][1]]}\n", "h")
        t.insert("end", f"{len(b['edges'])} 本の線をまとめている（色は中でいちばん悪い状態）\n", "k")
        for i in b["edges"]:
            e = self.view["edges"][i]
            t.insert("end", "● ", "stop" if e.get("blocked") else e["status"])
            t.insert("end", f"{names[e['from']]} → {names[e['to']]}" + (f"（{e['label']}）" if e.get("label") else "") + "\n")
            if e.get("blocked"):
                t.insert("end", "   " + e["blocked"] + "\n", "stop")
            if e.get("note"):
                t.insert("end", "   " + e["note"] + "\n", "k")
        t.configure(state="disabled")

    def on_wheel(self, e):
        f = math.exp(e.delta / 120 * 0.12)
        ns = min(3.0, max(0.25, self.scale * f))
        self.ox = e.x - (e.x - self.ox) * ns / self.scale
        self.oy = e.y - (e.y - self.oy) * ns / self.scale
        self.canvas.scale("content", e.x, e.y, ns / self.scale, ns / self.scale)  # 形だけその場で（文字の大きさは後で）
        self.scale = ns
        self.moved = True
        if self.pending:
            self.root.after_cancel(self.pending)
        self.pending = self.root.after(180, self.redraw)  # 回し終わってから 1 回だけ描き直す


_branches = {}


def pane_branches():
    """herdr のペイン全部のブランチ: {ブランチ名: 今選んでいるペインか}。main などは数えない。読めなければ空。"""
    try:
        out = subprocess.run(["herdr", "pane", "list"], capture_output=True, text=True, encoding="utf-8", timeout=3,
                             creationflags=NO_WINDOW)
        panes = json.loads(out.stdout)["result"]["panes"]
    except Exception:
        return {}
    branches = {}
    for p in panes:
        b = branch_of(p.get("cwd"))
        if b and b not in ("main", "master", "HEAD"):
            branches[b] = branches.get(b, False) or bool(p.get("focused"))
    return branches


def branch_of(cwd):
    """フォルダの今のブランチ（15 秒は前回のものを使う）。git でなければ None。"""
    if not cwd:
        return None
    hit = _branches.get(cwd)
    if hit and hit[0] + 15 > time.time():
        return hit[1]
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True,
                             encoding="utf-8", timeout=3, creationflags=NO_WINDOW)
        b = out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        b = None
    _branches[cwd] = (time.time(), b)
    return b


def opening_scope():
    """開いたときの 1 枚目のタブの範囲: herdr で選んでいるペインのプロジェクト。リポジトリを並べたフォルダや関係ないフォルダなら全体。"""
    try:
        repos = json.loads(STATE.read_text(encoding="utf-8"))["arch"]["repos"]
        scope = arch.project_of(focused_cwd(), repos)
    except Exception:
        scope = None
    return scope if scope else "all"


def focused_cwd():
    """herdr で今選んでいるペインのフォルダ。herdr が無ければ None。"""
    try:
        out = subprocess.run(["herdr", "pane", "list"], capture_output=True, text=True, encoding="utf-8", timeout=3,
                             creationflags=NO_WINDOW)
        panes = json.loads(out.stdout)["result"]["panes"]
        return next((p.get("cwd") for p in panes if p.get("focused")), None)
    except Exception:
        return None


def already_open():
    """2 つ目の窓を開かない。決まった番号の口を握れなければ、もう開いている。
    そのときは開いている窓に「前に出て」と知らせて終わる（裏で立てた窓は Windows が前に出さず、隠れて見えなかった）。"""
    import socket
    global _lock
    _lock = socket.socket()
    try:
        _lock.bind(("127.0.0.1", 8766))
        _lock.listen(4)
        _lock.setblocking(False)
        return False
    except OSError:
        msg = "show"
        try:  # 今選んでいるペインの範囲（リポジトリならそのプロジェクト・並べたフォルダなら全体）を、開いている窓に知らせる
            repos = json.loads(STATE.read_text(encoding="utf-8"))["arch"]["repos"]
            scope = arch.project_of(focused_cwd(), repos)
            if scope is not None:
                msg = "open:" + scope
        except Exception:
            pass
        try:  # 何を知らせたかを残す（タブが増えないときに、押したのが届いているかを確かめるため）
            with open(Path.home() / ".herdr" / "open-arch.log", "a", encoding="utf-8") as log:
                log.write(f"{datetime.now().isoformat(timespec='seconds')} 開いている窓へ: {msg}\n")
        except OSError:
            pass
        try:
            with socket.create_connection(("127.0.0.1", 8766), timeout=2) as c:
                c.sendall(msg.encode("utf-8"))
        except OSError:
            pass
        return True


if __name__ == "__main__":
    if already_open():
        sys.exit(0)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 高解像度の画面で文字がにじまないように
    except Exception:
        pass
    Viewer(sys.argv[1] if len(sys.argv) > 1 else None).root.mainloop()
