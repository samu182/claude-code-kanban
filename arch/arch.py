"""構成図の中身を決める（判断はここだけ。描くのは arch_view.py）。board.py の build から呼ばれ、state の "arch" に入る。

宣言（人と Claude が書く）:
  ~/.herdr/kanban-data/<project>/arch.json   そのプロジェクトの枠・部品・線
  ~/.herdr/kanban-data/arch-shared.json      どこにも属さない部品（外部・他チーム・外部サービス）
  id は「<project>:<id>」。同じファイルの中は <id> だけでよい。shared の部品は「shared:<id>」

状態（自動で決まる）:
  部品  live（本番で動いている）/ wip（作りかけ）/ none（まだ無い）
        plan に挙げた計画書の項目が全部済 → live。どれかが済・やることに手が付いている・PR がある → wip。どれも無い → none
        plan が無い部品は宣言の status（無ければ live）
  止まっている  済んでいない項目のカードが外部待ちなら、その理由（「B-2 待ち」など）を blocked に入れる
  骨格  plan に計画書の ◆（最初の区切りに要る）の項目があるか、宣言に "skeleton": true があれば skeleton。線は plan があればそれ、無ければ両端が骨格なら骨格
        線の宣言に "skeleton"（true / false）があれば、それが勝つ
  順番  骨格の部品に、通る順の番号（step）。宣言に "step" があればそれ、無ければ骨格の線をたどって振る
  線    plan があればそれで決める。無ければ両端の弱い方。止まっている（blocked）は、plan の項目が外部待ちか、
        本番で動いていない線のどちらかの端が止まっているとき
        check（{"path", "contains"}）があれば、そのリポジトリの origin/main で確かめる。見つからなければ none
  ずれ  宣言漏れ・消えたファイル・図に無い計画に加え、部品の docs の設計書が arch.json より後に変わったもの（設計が変わった）
"""

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

RANK = {"none": 0, "wip": 1, "live": 2}
SHARED = "shared"


def qualify(ref, project):
    return ref if ":" in ref else f"{project}:{ref}"


def load(roots, shared_file):
    """宣言を全部読み、id を <project>:<id> に揃えて 1 つにする。"""
    sources = [(json.loads((r / "kanban.json").read_text(encoding="utf-8"))["project"], r / "arch.json") for r in roots]
    sources.append((SHARED, shared_file))
    arch = {"zones": [], "nodes": [], "edges": [], "watch": {}, "plan_ignore": {}, "declared_at": {}}
    for project, f in sources:
        if not f.exists():
            continue
        arch["declared_at"][project] = f.stat().st_mtime  # 宣言を最後に見直した時（設計が変わったかを比べる元）
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("watch"):
            arch["watch"][project] = {"dirs": d["watch"], "ignore": d.get("ignore", [])}
        if d.get("plan_ignore"):
            arch["plan_ignore"][project] = d["plan_ignore"]
        for z in d.get("zones", []):
            arch["zones"].append({**z, "id": qualify(z["id"], project), "project": project})
        for n in d.get("nodes", []):
            arch["nodes"].append({**n, "id": qualify(n["id"], project), "zone": qualify(n["zone"], project),
                                  "project": project, "plan": [qualify(p, project) for p in n.get("plan", [])]})
        for e in d.get("edges", []):
            arch["edges"].append({**e, "from": qualify(e["from"], project), "to": qualify(e["to"], project),
                                  "project": project, "plan": [qualify(p, project) for p in e.get("plan", [])]})
    return arch


def ref_states(plans, cards):
    """「<project>:<id>」→ {status, why, started}。計画書の行にカードの様子を重ねる。"""
    by_card = {f"{c['project']}:{c['id']}": c for c in cards}
    out = {}
    for project, rows in plans.items():
        for r in rows:
            key = f"{project}:{r['id']}"
            c = by_card.get(key) or {}
            done_items = int(c["progress"].split("/")[0]) if c.get("progress") else 0
            out[key] = {"status": r["status"], "why": (c.get("why") or [""])[0], "critical": bool(r.get("critical")),
                        "started": bool(done_items or c.get("prs") or r["status"] == "自分の番")}
    # 計画書を持たないプロジェクトのカード（作業カード・修正カード）も、部品の plan に書ける。状態はカードの列。
    # 済んだカードは state から消えるので、見つからない参照は数えない（宣言の status のまま）
    for key, c in by_card.items():
        if key not in out and c.get("kind") in ("work", "fix"):
            done_items = int(c["progress"].split("/")[0]) if c.get("progress") else 0
            out[key] = {"status": c["column"], "why": (c.get("why") or [""])[0], "critical": False,
                        "started": bool(done_items or c.get("prs") or c["column"] == "自分の番")}
    return out


def judge(refs, states, default="live"):
    """計画書の項目の並びから、部品（または線）の状態と、止まっている理由を決める。"""
    known = [states[r] for r in refs if r in states]
    if not known:
        return default, ""
    if all(s["status"] == "済" for s in known):
        return "live", ""
    blocked = next((s["why"] or "外部待ち" for s in known if s["status"] == "外部待ち"), "")
    if any(s["status"] == "済" or s["started"] for s in known):
        return "wip", blocked
    return "none", blocked


def skeleton(refs, states):
    """ウォーキングスケルトン（最初に端から端まで通す 1 本）に入るか。計画書の ◆ の項目を 1 つでも持てば入る。"""
    return any(states.get(r, {}).get("critical") for r in refs)


def git_check(repo_dir, check):
    """宣言の check を origin/main で確かめる。返すのは True / False / None（確かめられない）。"""
    if not repo_dir:
        return None
    r = subprocess.run(["git", "-c", "core.quotepath=off", "show", f"origin/main:{check['path']}"],
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # 窓を出さない（board.py と同じ）
                       cwd=repo_dir, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        return False if "does not exist" in r.stderr or "exists on disk, but not in" in r.stderr else None
    return check["contains"] in r.stdout if check.get("contains") else True


def git_last_change(repo_dir, path):
    """origin/main でそのファイルを最後に変えたコミット {"at": 秒, "hash", "subject"}。確かめられなければ None。"""
    if not repo_dir:
        return None
    try:
        r = subprocess.run(["git", "-c", "core.quotepath=off", "log", "-1", "--format=%ct%x09%h%x09%s", "origin/main", "--", path],
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                           cwd=repo_dir, capture_output=True, text=True, encoding="utf-8")
    except OSError:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    at, h, subject = r.stdout.strip().split("	", 2)
    return {"at": int(at), "hash": h, "subject": subject}


STATUS_TEXT = {"live": "本番で動いている", "wip": "作りかけ", "none": "まだ無い"}


def waiting_for(blocked):
    """「B-2 待ち」「#7 レビュー待ち 3 日」から、待っているもの（「B-2 」「#7 レビュー」）を取り出す。"""
    return re.sub(r"\s*待ち.*$", "", blocked).strip() + " "


def short(text, limit=28):
    """1 行に収める（やることの文は長いことがある。全文は畳んだ一覧に出る）。"""
    text = text.split("。")[0]
    return text if len(text) <= limit else text[:limit] + "…"


def detail(n, cards_by_key, projects):
    """部品を押したときに出す中身。上に「なぜこの状態か」と「次の一手」、その下に畳む材料（やること・PR・メモ）。
    状態は自動で決めているので、理由が見えないと信用されない（OpsLevel が通った・落ちた確かめを並べるのと同じ考え方）。"""
    refs = n["plan"]
    ids = "・".join(r.split(":", 1)[1] for r in refs)
    items, prs, notes = [], [], []
    for r in refs:
        c = cards_by_key.get(r)
        if not c:
            continue
        project = r.split(":", 1)[0]
        items += [{"text": i["text"], "done": bool(i.get("done")), "ref": r} for i in c.get("items") or []]
        known = (projects.get(project) or {}).get("prs") or {}
        for num in c.get("prs") or []:
            p = known.get(str(num))
            if p and all(x["number"] != num for x in prs):
                prs.append({**p, "project": project, "ref": r, "why": (p.get("why") or "").split(" ", 1)[-1]})
        if c.get("note"):
            notes.append({"ref": r, "text": c["note"]})
    opened = [p for p in prs if p["state"] == "OPEN"]
    my_turn = [p for p in opened if p.get("turn") == "me"]
    waiting = [p for p in opened if p.get("turn") != "me"]
    undone = [i for i in items if not i["done"]]
    progress = f"{len(items) - len(undone)}/{len(items)}" if items else ""

    if n.get("unknown"):
        reason = n["unknown"]
    elif n["status"] == "live":
        reason = "計画書の項目が全部済んでいる" if refs else "計画書の項目を持たない（宣言で本番としている）"
    elif n["blocked"]:
        reason = f"{n['blocked']}（ほかの作業・人を待っている）"
    elif items:
        reason = f"やること {progress}" + (f"・残り「{short(undone[0]['text'])}」" + ("ほか" if len(undone) > 1 else "") if undone else "")
    elif opened:
        reason = f"#{opened[0]['number']} {opened[0]['why']}"
    elif n["status"] == "wip":
        reason = f"計画書の項目（{ids}）の一部が済んでいる" if refs else "計画書の項目を持たない（宣言で作りかけとしている）"
    else:
        reason = f"まだ手を付けていない（計画書 {ids}）" if refs else "まだ手を付けていない"

    if n.get("unknown"):
        nxt = "計画書を読める状態に戻す（カンバンの「読めなかったもの」を見る）"
    elif my_turn:
        nxt = f"#{my_turn[0]['number']} に対応する（{my_turn[0]['why']}）"
    elif n["blocked"]:
        nxt = f"{waiting_for(n['blocked'])}を待つ" + (f"（その間にできること:「{undone[0]['text']}」）" if undone else "")
    elif undone:
        nxt = undone[0]["text"]
    elif waiting:
        nxt = f"#{waiting[0]['number']} のレビューを待つ"
    elif n["status"] == "live":
        nxt = "なし（本番で動いている）"
    else:
        nxt = f"計画書 {ids} に着手する" if refs else "計画書に項目を足すか決める"

    main_pr = (my_turn or waiting or sorted(prs, key=lambda p: -p["number"]) or [None])[0]
    ai = [f"{n['project']} の「{n.get('title', n['id'])}」" + (f"（計画書 {ids}）" if ids else "") + "を進めたい。",
          f"今の状態: {STATUS_TEXT[n['status']]}。なぜ: {reason}。次の一手: {nxt}。"]
    if opened:
        ai.append("開いている PR: " + "、".join(f"#{p['number']}（{p['why']}）" for p in opened) + "。")
    if n.get("paths"):
        ai.append("コード: " + "、".join(n["paths"]) + "。")
    if n.get("docs"):
        ai.append("設計: " + "、".join(n["docs"]) + "。")
    return {"reason": reason, "next": nxt, "main_pr": main_pr, "items": items, "progress": progress,
            "prs": prs, "notes": notes, "ai": "".join(ai)}


def switch_note(item, envs):
    """宣言の "switch"（本番でつなぐかを決める環境変数）を確かめる。返すのは (本番に無い変数, 説明)。
    値は見ない（名前があるかだけ）。名前があっても値が off のことはあるので「ある」までしか言わない。"""
    names = item.get("switch")
    if not names:
        return [], ""
    names = [names] if isinstance(names, str) else names
    if envs is None:
        return [], "本番のスイッチは確かめられない（Vercel を読めない）"
    missing = [x for x in names if x not in envs]
    if missing:
        return missing, "本番に " + "・".join(missing) + " が無い（コードはあってもスイッチが入っていない）"
    return [], "本番に " + "・".join(names) + " がある（値は見ていない）"


def unreadable(refs, states, unread):
    """参照の中に、計画書を読めなかったプロジェクトのものがあれば、その説明（無ければ ""）。
    読めないときに「見つからない参照は数えない」で判断すると、宣言の既定（本番）になってしまう。"""
    gone = sorted({r.split(":", 1)[0] for r in refs if r not in states and r.split(":", 1)[0] in unread})
    return f"{'・'.join(gone)} の計画書を読めないので、状態が分からない" if gone else ""


def compute(arch, plans, cards, repos, check=git_check, projects=None, envs=None, files=None, unread=(), last_change=None):
    """envs: {プロジェクト: 本番の環境変数の名前の集合}（読めなければ無い）。files: {プロジェクト: origin/main のファイル一覧}。
    unread: 計画書を持つのに読めなかったプロジェクト。last_change: (リポジトリ, パス) → そのファイルを最後に変えたコミット（git_last_change）。"""
    envs, files, unread = envs or {}, files or {}, set(unread)
    states = ref_states(plans, cards)
    cards_by_key = {f"{c['project']}:{c['id']}": c for c in cards}
    nodes = {}
    for n in arch["nodes"]:
        status, blocked = judge(n["plan"], states, n.get("status", "live"))
        unknown = unreadable(n["plan"], states, unread)
        if unknown:
            status, blocked = "none", ""
        off, n_note = switch_note(n, envs.get(n["project"]))
        if off and status == "live":
            status = "wip"  # スイッチが本番に無いなら、本番で動いているとは言えない
        nodes[n["id"]] = {**n, "status": status, "blocked": blocked, "switch_note": n_note, "unknown": unknown,
                          "skeleton": bool(n.get("skeleton")) or skeleton(n["plan"], states),
                          "refs": [{"id": r, **states.get(r, {"status": "計画書に無い", "why": ""})} for r in n["plan"]]}
    edges = []
    for e in arch["edges"]:
        if e["from"] not in nodes or e["to"] not in nodes:
            continue
        if e["plan"]:
            status, blocked = judge(e["plan"], states)
            if unreadable(e["plan"], states, unread):
                status, blocked = "none", ""
        else:
            status = min(nodes[e["from"]]["status"], nodes[e["to"]]["status"], key=RANK.get)
            # 本番で動いていない線で、どちらかの端が止まっていれば、線も止まっている（理由はその端の）
            ends = [nodes[x]["blocked"] for x in (e["from"], e["to"]) if nodes[x]["blocked"]]
            blocked = ends[0] if ends and status != "live" else ""
        note = ""
        if e.get("check"):
            found = check(repos.get(e["project"]), e["check"])
            if found is False:
                status, note = "none", f"コードに見つからない: {e['check']['path']}"
            elif found is None:
                note = "コードを確かめられない（リポジトリが無い）"
            else:
                note = f"コードで確かめた: {e['check']['path']}"
        off, s_note = switch_note(e, envs.get(e["project"]))
        if off and status == "live":
            status = "wip"
        note = "　".join(x for x in (note, s_note) if x)
        bone = skeleton(e["plan"], states) if e["plan"] else (nodes[e["from"]]["skeleton"] and nodes[e["to"]]["skeleton"])
        if isinstance(e.get("skeleton"), bool):  # 宣言が勝つ（骨格の部品どうしでも、通り道でない線は "skeleton": false）
            bone = e["skeleton"]
        edges.append({**e, "status": status, "note": note, "skeleton": bone, "blocked": blocked})
    for n in nodes.values():
        n["detail"] = detail(n, cards_by_key, projects or {})
    owned = steps(nodes, edges)
    for nid, n in nodes.items():
        n.pop("step", None)
        n["steps"] = owned.get(nid, {})
    owners = sorted({o for s in owned.values() for o in s})
    out_drift = {p: drift(w, [n for n in nodes.values() if n["project"] == p], files[p])
                 for p, w in arch.get("watch", {}).items() if p in files}
    for p, rows in plans.items():
        left = unplanned(p, rows, arch, arch.get("plan_ignore", {}).get(p, []))
        if left:
            out_drift.setdefault(p, {"unclaimed": [], "missing": []})["unplanned"] = left
    for p, at in arch.get("declared_at", {}).items():
        stale = stale_docs([n for n in nodes.values() if n["project"] == p], at, last_change, repos.get(p)) if last_change else []
        if stale:
            out_drift.setdefault(p, {"unclaimed": [], "missing": []})["stale_docs"] = stale
    return {"zones": arch["zones"], "nodes": list(nodes.values()), "edges": edges, "repos": repos,
            "drift": out_drift,
            "skeletons": {o: through([{**n, "step": n["steps"].get(o)} for n in nodes.values()]) for o in owners}}


def through(nodes):
    """ウォーキングスケルトンが、端から何番まで切れ目なく本番で通っているか。
    大事なのは「いくつできたか」ではなく「端から端までつながっているか」なので、途中で 1 つでも本番でなければそこで止める。
    返すのは {"through": 切れ目なく本番の最後の番号, "total": 全部の数, "next": 次に通す部品の id（全部通っていれば None）}。"""
    bones = sorted((n for n in nodes if n.get("step")), key=lambda n: n["step"])
    k = 0
    for n in bones:
        if n["status"] != "live" or n["blocked"]:
            break
        k = n["step"]
    nxt = next((n["id"] for n in bones if n["step"] > k), None)
    return {"through": k, "total": len(bones), "next": nxt}


def steps(nodes, edges):
    """ウォーキングスケルトンを通る順の番号（1 から）を、どのプロジェクトの骨格かと組で返す: {部品: {プロジェクト: 番号}}。
    骨格は計画書の持ち物なので、ほかのプロジェクトの部品が通り道にあっても、番号はその計画書のプロジェクトのもの。
    宣言の "step" が数なら、その部品のプロジェクトの骨格。{"shop-front": 4} のように書けば、ほかのプロジェクトの骨格に入れられる。
    宣言の無いプロジェクトは、骨格の線を向きを無視してたどり、入ってくる骨格の線が無い部品から順に振る（宣言の順で同点を崩す）。"""
    declared = {}
    for nid, n in nodes.items():
        st = n.get("step")
        if isinstance(st, int):
            declared[nid] = {n["project"]: st}
        elif isinstance(st, dict):
            declared[nid] = {k: v for k, v in st.items() if isinstance(v, int)}
    owners = {o for d in declared.values() for o in d}  # 宣言のあるプロジェクトは、宣言だけで決める
    # 番号を宣言した部品は、自分のプロジェクトの骨格にも自動で数えない（共通の部品が「shared」の骨格を作っていた）
    bones = [nid for nid, n in nodes.items() if n["skeleton"] and n["project"] not in owners and nid not in declared]
    near = {nid: [] for nid in bones}
    for e in edges:
        if e["skeleton"] and e["from"] in near and e["to"] in near:
            near[e["from"]].append(e["to"])
            near[e["to"]].append(e["from"])
    has_in = {e["to"] for e in edges if e["skeleton"]}
    order, seen = [], set()
    for root in [b for b in bones if b not in has_in] + bones:
        queue = [root]
        while queue:
            nid = queue.pop(0)
            if nid in seen:
                continue
            seen.add(nid)
            order.append(nid)
            queue += [x for x in near[nid] if x not in seen]
    out, counts = {k: dict(v) for k, v in declared.items()}, {}
    for nid in order:  # 自動の番号は、骨格の持ち主（部品のプロジェクト）ごとに 1 から数える
        owner = nodes[nid]["project"]
        counts[owner] = counts.get(owner, 0) + 1
        out.setdefault(nid, {})[owner] = counts[owner]
    return out


def drift(watch, nodes, files):
    """図と現物のずれ。見張る場所（watch の dirs）の直下にあるのに、どの部品の paths にも無いもの（宣言漏れ）と、
    部品の paths に書いたのに origin/main に無いもの（消えた・名前が変わった）を出す。テストと ignore は数えない。"""
    have = set(files)
    claimed = [p for n in nodes for p in n.get("paths") or []]
    missing = [{"node": n["id"], "path": p} for n in nodes for p in n.get("paths") or []
               if p not in have and not any(f.startswith(p.rstrip("/") + "/") for f in have)]
    unclaimed = []
    for d in watch["dirs"]:
        base = d.rstrip("/") + "/"
        children = sorted({base + f[len(base):].split("/")[0] for f in files if f.startswith(base)})
        for c in children:
            if re.search(r"\.(test|spec)\.[jt]sx?$|/__tests__|/tests?$", c) or c in watch["ignore"]:
                continue
            if not any(p == c or p.startswith(c + "/") or c.startswith(p.rstrip("/") + "/") for p in claimed):
                unclaimed.append(c)
    return {"unclaimed": unclaimed, "missing": missing}


def stale_docs(nodes, declared_at, last_change, repo_dir):
    """設計が変わったのに宣言を見直していないもの。部品の docs に挙げた設計書が、origin/main で arch.json の保存より後に
    変わっていれば出す。置き場・名前・説明は宣言に書いた文字のままなので、決定が設計書に入っても図は変わらない
    （実例: 設計書でメールの送り先が 1 つに決まったのに、図は決まる前の「A か B へ」のままだった）。
    宣言を直すか、直すところが無ければ arch.json を保存し直すと消える。リポジトリの外の docs（~ や絶対パス）は見ない。"""
    if not repo_dir:
        return []
    by_doc = {}
    for n in nodes:
        for d in n.get("docs") or []:
            if not d.startswith(("~", "/", "%")) and ":" not in d:
                by_doc.setdefault(d, []).append(n["id"])
    out = []
    for d, ids in sorted(by_doc.items()):
        c = last_change(repo_dir, d)
        if c and c["at"] > declared_at:
            out.append({"doc": d, "nodes": ids, "hash": c["hash"], "subject": c["subject"],
                        "date": datetime.fromtimestamp(c["at"]).strftime("%m/%d %H:%M")})
    return out


def unplanned(project, rows, arch, ignore):
    """計画書の項目のうち、どの部品・線の plan にも無いもの（図に描かれていない作業）。plan_ignore に書いたもの
    （決めごと・手順書・設定作業など部品にならない項目）は数えない。計画書に項目が足されたことに図の側で気づくため。"""
    refs = {r for x in arch["nodes"] + arch["edges"] for r in x.get("plan", [])}
    return [{"id": r["id"], "title": r.get("title", "")} for r in rows
            if f"{project}:{r['id']}" not in refs and r["id"] not in ignore]


def trustworthy(computed):
    """その日の写しに残してよいか。状態が分からない部品があれば残さない（間違った状態が翌日の比べる元になる）。"""
    return not any(n.get("unknown") for n in computed["nodes"])


def snapshot(computed):
    """前回からの変化を数えるための、その日の初めの写し（部品の状態・待ち・骨格がどこまで通ったか）。"""
    return {"nodes": {n["id"]: {"status": n["status"], "blocked": n["blocked"]} for n in computed["nodes"]},
            "skeletons": {o: k["through"] for o, k in computed.get("skeletons", {}).items()}}


def changes(before, now):
    """前回の写しと今を比べる。状態か待ちが変わった部品と、骨格の通った番号の変化。新しく宣言した部品も数える。"""
    out = {"nodes": {}, "skeletons": {}}
    for nid, cur in now["nodes"].items():
        old = before["nodes"].get(nid)
        if old != cur:
            out["nodes"][nid] = {"from": old, "to": cur}
    for o, k in now["skeletons"].items():
        if before["skeletons"].get(o) != k:
            out["skeletons"][o] = {"from": before["skeletons"].get(o), "to": k}
    return out


def norm(name):
    """ブランチ名とカードの id を比べるための形（feat/・fix/ などの前置きと記号を落とす）。"""
    return re.sub(r"[^a-z0-9]", "", re.sub(r"^(feat|fix|docs|chore|refactor|test)[/-]", "", name.lower()))


def working(nodes, cards, prs, branches):
    """今どこを作業しているか。herdr のペインのブランチ → そのブランチの開いている PR → その PR をつないだカード → 部品。
    PR がまだ無いブランチは、カードの branches に書いてあればそのカード（plan カードの id はブランチ名とそろわないため）、
    無ければ名前がカードの id と同じなら（fix/login-button と fix-login-button）そのカード。
    branches: {ブランチ名: 今選んでいるペインか}。返すのは {部品: {"branch", "focused"}}。"""
    card_of_pr, card_by_name = {}, {}
    for c in cards:
        key = f"{c['project']}:{c['id']}"
        card_by_name[norm(c["id"])] = key
        for b in c.get("branches") or []:
            card_by_name[norm(b)] = key
        for num in c.get("prs") or []:
            card_of_pr[(c["project"], num)] = key
    keys = {}
    for branch, focused in branches.items():
        hit = [card_of_pr[(proj, p["number"])] for proj, ps in prs.items() for p in ps.values()
               if p.get("branch") == branch and p.get("state") == "OPEN" and (proj, p["number"]) in card_of_pr]
        if not hit and norm(branch) in card_by_name:
            hit = [card_by_name[norm(branch)]]
        for k in hit:
            keys[k] = {"branch": branch, "focused": focused or keys.get(k, {}).get("focused", False)}
    return {n["id"]: keys[r] for n in nodes for r in n["plan"] if r in keys}


def scope(arch, project):
    """表示する部品の id。project が None なら全部。あれば、そのプロジェクトの部品と、線 1 本でつながる部品。
    ほかのプロジェクトの部品でも、そのプロジェクトの骨格に番号を持つものは入れる（骨格の番号が抜けて見えないように）。"""
    ids = {n["id"] for n in arch["nodes"]}
    if not project:
        return ids
    own = {n["id"] for n in arch["nodes"] if n["project"] == project}
    near = {e["to"] for e in arch["edges"] if e["from"] in own} | {e["from"] for e in arch["edges"] if e["to"] in own}
    bones = {n["id"] for n in arch["nodes"] if project in n.get("steps", {})}
    return own | (near & ids) | bones


def project_of(cwd, repos):
    """フォルダから、どのプロジェクトの範囲を見せるか。リポジトリ（と横の <repo>-wt-*）ならそのプロジェクト、
    リポジトリを並べたフォルダ（~/src など）なら全部（""）、関係ないフォルダなら None（今の範囲のまま）。"""
    if not cwd:
        return None
    here = Path(cwd).resolve()
    chain = [here, *here.parents]
    parents = set()
    for project, repo in repos.items():
        if not repo:
            continue
        root = Path(repo).resolve()
        parents.add(root.parent)
        if any(p == root or (p.parent == root.parent and p.name.startswith(root.name + "-wt-")) for p in chain):
            return project
    return "" if here in parents else None
