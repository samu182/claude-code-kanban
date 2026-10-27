"""カンバンの中身を組み立てる（判断はここだけ）。画面・サイドバー・セッション開始の表示は、
これが書く ~/.herdr/kanban-state.json を読むだけ。

  python board.py build           組み立てて state を書き、文字で出す
  python board.py show [project]  state を文字で出す（組み立て直さない。state が無ければ組み立てる）

カンバンルート = kanban.json があるフォルダ（~/.herdr/kanban-data/*/ だけ。リポジトリの中には置かない）。
カードは 3 種類:
  plan … 計画書の表の 1 行。計画書に最初からある。cards/<id>.json は上書き（PR・やること・列）だけ
  fix  … 修正・エラー対処・レビューの宿題。cards/<id>.json に全部書く
  work … 計画書を持たないプロジェクトの作業カード
列は「やること → 自分の番 → 外部待ち」。開いている PR がつながっていれば PR の状態で決め、
無ければ手で置いた列、それも無ければ計画書の「待つもの」、どれも無ければやること。
外部待ちのカードには、その列に入ってから何日たったか（「3 日動きなし」）を足す。数え始めは前回の state から引き継ぐ。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "arch"))  # 構成図（arch/）の判断を、組み立てのついでに呼ぶ
import arch  # noqa: E402

HOME = Path.home()
FIXED_ROOTS = HOME / ".herdr" / "kanban-data"
STATE = HOME / ".herdr" / "kanban-state.json"
FETCH_EVERY = 600  # 秒。計画書は origin/main から読むので、fetch はこの間隔まで間引く
COLUMNS = ["やること", "自分の番", "外部待ち"]
BOTS = {"vercel", "github-actions", "copilot"}
ID = r"[A-Z]-\d+"

errors = []
# git・gh はコンソールのプログラム。コンソールを持たない処理（構成図の窓 = pythonw）から呼ぶと、
# 呼ぶたびにコンソールの窓が開いて入力を奪った（2026-10-06）。窓を出さずに呼ぶ
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
plans = {}  # project -> 計画書の全部の行（カードにしない節も）。地図（map.html）が読む
projects = {}  # project -> GitHub・色（地図が PR へのリンクと色に使う）
unread_plans = set()  # 計画書を持つのに読めなかったプロジェクト
degraded = set()  # PR が読めなかったプロジェクト。列がずれるので、動きなしの日数を数え直さない


def run(args, cwd=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])}: {r.stderr.strip()[:200]}")
    return r.stdout


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def find_roots():
    return [p.parent for p in FIXED_ROOTS.glob("*/kanban.json")]


# ---------------------------------------------------------------- 計画書

def expand_ids(text):
    """「B-2〜B-4」「A-2」を id の並びに。取り消し線（~~A-1~~）の中は済んだものなので数えない。"""
    text = re.sub(r"~~.*?~~", "", text)
    ids = []
    for a, b in re.findall(rf"({ID})\s*[〜~]\s*({ID})", text):
        if a[0] == b[0]:
            ids += [f"{a[0]}-{n}" for n in range(int(a[2:]), int(b[2:]) + 1)]
    ids += re.findall(ID, text)
    return list(dict.fromkeys(ids))


def plan_title(cell):
    struck = re.match(r"^\s*~~(.+?)~~", cell)  # 済んだ行は取り消し線の中が題（後ろの「済」などは題ではない）
    cell = (struck.group(1) if struck else cell).strip()
    m = re.match(r"^(.{0,6}?)\*\*(.+?)\*\*", cell)
    if m:
        return (m.group(1) + m.group(2)).strip()
    return re.split(r"（|<br>|。", cell.replace("**", ""))[0].strip()


def read_plan(repo_dir, plan, fetched):
    """計画書の節の表を読む。返すのは {id: {...}}（書いてある順）。
    plan に "number_ids": "P-" があれば、番号だけの行（「| 1 | ... |」の段の表）を読み、id は P-1 の形にする。
    その節は見出しの「カードにする」が無くても全部カードにする（人と共有している計画書の書き方を、カンバンのために変えないため）。"""
    prefix = plan.get("number_ids")
    if fetched.get(str(repo_dir), 0) + FETCH_EVERY < time.time():
        try:
            run(["git", "fetch", "-q", "origin", "main"], cwd=repo_dir)
            fetched[str(repo_dir)] = time.time()
        except RuntimeError as e:
            errors.append(f"{repo_dir.name}: fetch できない（前回の計画書で出す）: {e}")
    text = run(["git", "-c", "core.quotepath=off", "show", f"origin/main:{plan['path']}"], cwd=repo_dir)
    start = text.find(plan["section"])
    if start < 0:
        raise RuntimeError(f"計画書に「{plan['section']}」が見つからない")
    body = text[start + len(plan["section"]):]
    nxt = re.search(r"^## ", body, re.M)
    body = body[: nxt.start()] if nxt else body

    items, carded, deps_col, group = {}, bool(prefix), False, ""
    for line in body.splitlines():
        if line.startswith("### "):
            carded = bool(prefix) or ("カードにする" in line and "カードにしない" not in line)
            group = re.split(r"\s*[—-]\s*カードに", line[4:])[0].strip()
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        if cells[0] == "#":
            deps_col = len(cells) > 2 and "待つもの" in cells[2]
            continue
        if prefix:
            m = re.match(r"^(?:~~)?\s*◆?\s*(\d+)", cells[0])
            if not m:
                continue
            iid = prefix + m.group(1)
        else:
            m = re.search(ID, cells[0])
            if not m:
                continue
            iid = m.group(0)
        items[iid] = {
            "id": iid,
            "done": cells[0].startswith("~~"),
            "critical": "◆" in cells[0],
            "title": plan_title(cells[1]),
            "deps": [d for d in expand_ids(cells[2]) if d != iid] if deps_col and len(cells) > 2 else [],
            "card": carded,
            "group": group,
            "order": len(items),
        }
    return items


# ---------------------------------------------------------------- GitHub

def is_bot(login):
    return not login or login.endswith("[bot]") or login in BOTS


def judge_open_pr(p, me, now):
    """開いている PR がどちらの番か。返すのは {turn: me|them, why: 表示する 1 行}。"""
    n = p["number"]
    if p.get("isDraft"):
        return {"turn": "me", "why": f"#{n} ドラフト"}
    mine_at = [parse_time(c.get("committedDate")) for c in p.get("commits") or []]
    others = []
    for c in p.get("comments") or []:
        who = (c.get("author") or {}).get("login", "")
        if who == me:
            mine_at.append(parse_time(c["createdAt"]))
        elif not is_bot(who):
            others.append((parse_time(c["createdAt"]), "COMMENTED"))
    for r in p.get("reviews") or []:
        who = (r.get("author") or {}).get("login", "")
        if who == me:
            mine_at.append(parse_time(r.get("submittedAt")))
        elif not is_bot(who):
            others.append((parse_time(r.get("submittedAt")), r.get("state")))
    last_mine = max([t for t in mine_at if t], default=None)
    newer = sorted(o for o in others if o[0] and (not last_mine or o[0] > last_mine))
    if p.get("reviewDecision") == "CHANGES_REQUESTED" and newer:
        return {"turn": "me", "why": f"#{n} 直しを頼まれている"}
    replies = [o for o in newer if o[1] != "APPROVED"]
    if replies:
        return {"turn": "me", "why": f"#{n} 返事していないコメント {len(replies)} 件"}
    if p.get("reviewDecision") == "APPROVED" or (newer and newer[-1][1] == "APPROVED"):
        return {"turn": "them", "why": f"#{n} 承認済み・merge 待ち"}
    days = (now - last_mine).days if last_mine else 0
    return {"turn": "them", "why": f"#{n} レビュー待ち" + (f" {days} 日" if days else "")}


# gh pr list は commits の作者まで辿るので、自分の PR が多いと 1 回で数百点かかることがあった（GraphQL は 1 時間 5000 点、
# 実際に使い切った）。要る欄だけを自分で取りに行き、1 回 数点にする。
# 自分の開いている PR を検索で取る（gh pr list --author @me と同じ範囲）。全員の開いている PR を新しい順に決まった本数だけ取る形だと、
# 開いている PR が多いリポジトリで、しばらく動いていない自分の PR が漏れた
OPEN_QUERY = """query($q: String!) {
  search(query: $q, type: ISSUE, first: 50) {
    nodes { ... on PullRequest { number title body isDraft reviewDecision headRefName author { login }
        commits(last: 1) { nodes { commit { committedDate } } }
        comments(last: 50) { nodes { author { login } createdAt } }
        reviews(last: 50) { nodes { author { login } submittedAt state } } } } } }"""
ALL_QUERY = """query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, after: $after, orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor } nodes { number state title body } } } }"""
ALL_PAGES = 3  # 新しい方から 300 本まで（古い PR につないだカードの済みを判定するため）


def graphql(query, **variables):
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is not None:
            args += ["-f", f"{k}={v}"]
    return json.loads(run(args))["data"]


def flatten(p):
    """自分の問い合わせの形を、judge_open_pr が読む gh pr list の形に揃える。"""
    return {**p, "commits": [n["commit"] for n in p["commits"]["nodes"]],
            "comments": p["comments"]["nodes"], "reviews": p["reviews"]["nodes"]}


def read_prs(github, me):
    owner, name = github.split("/", 1)
    prs, after = {}, None
    for _ in range(ALL_PAGES):
        page = graphql(ALL_QUERY, owner=owner, name=name, after=after)["repository"]["pullRequests"]
        for p in page["nodes"]:
            prs[p["number"]] = {"number": p["number"], "state": p["state"], "title": p["title"], "body": p.get("body") or ""}
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    # 詳しく判定するのは自分の開いている PR だけ。他の人の PR がカードにつながっていても「開いている」とだけ出す（place）
    now = datetime.now(timezone.utc)
    for p in graphql(OPEN_QUERY, q=f"repo:{github} is:pr is:open author:@me")["search"]["nodes"]:
        if "number" not in p:
            continue
        pr = prs.setdefault(p["number"], {"number": p["number"], "title": p["title"], "body": p.get("body") or ""})
        pr["state"] = "OPEN"
        pr["branch"] = p.get("headRefName")  # 構成図の「今どこを作業しているか」（ペインのブランチ → PR）
        if (p.get("author") or {}).get("login") == me:
            pr["mine"] = True
            pr.update(judge_open_pr(flatten(p), me, now))
    return prs


def pr_refs(pr, section_no):
    """PR の題・本文に書いてある計画書の番号（「§3 C-2」）。"""
    text = pr["title"] + "\n" + (pr.get("body") or "")
    return set(re.findall(rf"§\s*{section_no}\s*({ID})", text))


# ---------------------------------------------------------------- 組み立て

def load_cards(root):
    cards = {}
    for f in sorted((root / "cards").glob("*.json")):
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
            c["_mtime"] = f.stat().st_mtime  # 手で列を置いたのはいつか（動きなしの数え始めの初め）
            cards[c["id"]] = c
        except Exception as e:
            errors.append(f"{f}: 読めない（{e}）")
    return cards


def progress(items):
    items = items or []
    return f"{sum(1 for i in items if i.get('done'))}/{len(items)}" if items else ""


def place(card, prs, deps_open):
    """列と、2 行目に出す理由。"""
    opened = [prs[n] for n in card.get("prs") or [] if n in prs and prs[n]["state"] == "OPEN"]
    if opened:
        col = "自分の番" if any(p.get("turn") == "me" for p in opened) else "外部待ち"
        return col, [p.get("why") or f"#{p['number']} 開いている" for p in opened]
    if card.get("column") in COLUMNS:
        return card["column"], [card["wait"]] if card.get("wait") else []
    if deps_open:
        return "外部待ち", [f"{'・'.join(deps_open)} 待ち"]
    return "やること", []


def build_root(root, fetched, me):
    conf = json.loads((root / "kanban.json").read_text(encoding="utf-8"))
    project, github, color = conf["project"], conf.get("github"), conf.get("color")
    repo_dir = Path(conf["repo_dir"]) if conf.get("repo_dir") else None
    overlays = load_cards(root)
    projects[project] = {"github": github, "color": color, "repo_dir": conf.get("repo_dir"), "plan": conf.get("plan"),
                         "prs": {}}
    out, linked_anywhere = [], set()

    try:
        prs = read_prs(github, me) if github else {}
    except Exception as e:  # GraphQL の data: null などは KeyError・TypeError になる。カードは PR の状態なしで残す
        errors.append(f"{project}: PR が読めない（PR の状態なしで出す）: {e}")
        degraded.add(project)
        prs = {}

    plan = {}
    if conf.get("plan") and repo_dir:
        try:
            plan = read_plan(repo_dir, conf["plan"], fetched)
        except Exception as e:
            errors.append(f"{project}: 計画書が読めない: {e}")
            unread_plans.add(project)  # 構成図で、この計画書を参照する部品を「本番」にしないため
    section_no = re.sub(r"\D", "", (conf.get("plan") or {}).get("section", "")) or "3"

    # PR の題・本文の「§3 C-2」で自動でつなぐ（手でつないだものに足す）
    auto = {}
    for pr in prs.values():
        for iid in pr_refs(pr, section_no):
            auto.setdefault(iid, set()).add(pr["number"])

    def emit(card, kind, title, critical, order, deps_open):
        card["prs"] = sorted(set(card.get("prs") or []) | (auto.get(card["id"], set()) if kind == "plan" else set()))
        linked_anywhere.update(card["prs"])
        col, whys = place(card, prs, deps_open)
        out.append({
            "project": project, "id": card["id"], "kind": kind, "title": title, "critical": critical,
            "column": col, "why": whys, "progress": progress(card.get("items")),
            "items": card.get("items") or [], "prs": card["prs"], "branches": card.get("branches") or [], "note": card.get("note", ""), "order": order,
            "seed": card.get("_mtime") if card.get("column") == col else None, "color": color,
        })

    for iid, row in plan.items():
        if not row["card"]:
            continue
        ov = overlays.pop(iid, {"id": iid})
        if row["done"] or ov.get("done"):
            linked_anywhere.update(ov.get("prs") or [])
            linked_anywhere.update(auto.get(iid, set()))
            continue
        deps_open = [d for d in row["deps"] if d in plan and not plan[d]["done"]]
        emit(ov, "plan", ov.get("title") or row["title"], row["critical"], row["order"], deps_open)

    for cid, c in overlays.items():
        kind = c.get("kind", "fix")
        if kind == "plan":
            errors.append(f"{project}: {cid} は計画書に無い（番号が変わった？）")
            continue
        linked = [prs[n] for n in c.get("prs") or [] if n in prs]
        finished = (linked and all(p["state"] != "OPEN" for p in linked)
                    and any(p["state"] == "MERGED" for p in linked)
                    and all(i.get("done") for i in c.get("items") or []))
        if c.get("done") or finished:
            linked_anywhere.update(c.get("prs") or [])
            continue
        emit(c, kind, c.get("title", cid), False, 1000 + len(out), [])

    if plan:
        placed = {c["id"]: c["column"] for c in out if c["kind"] == "plan"}
        plans[project] = [{
            "id": iid, "title": row["title"], "group": row["group"], "critical": row["critical"],
            "deps": row["deps"], "card": row["card"],
            "status": "済" if row["done"] or (row["card"] and iid not in placed)
                      else placed.get(iid, "決めること"),
        } for iid, row in plan.items()]

    # どのカードにもつながっていない、自分の開いている PR（見落とさないように出す）
    for pr in prs.values():
        if pr["state"] == "OPEN" and pr.get("mine") and pr["number"] not in linked_anywhere:
            out.append({
                "project": project, "id": f"#{pr['number']}", "kind": "pr", "title": pr["title"],
                "critical": False, "column": "自分の番" if pr["turn"] == "me" else "外部待ち",
                "why": [pr["why"].split(" ", 1)[1], "カードにつながっていない"], "progress": "",
                "items": [], "prs": [pr["number"]], "note": "", "order": 2000 + pr["number"], "color": color,
            })
    # カードにつながっている PR の題と状態（構成図の部品の詳しい中身に出す）。全部は入れない（state が大きくなる）
    for c in out:
        for n in c["prs"]:
            if n in prs:
                p = prs[n]
                projects[project]["prs"][str(n)] = {"number": n, "title": p["title"], "state": p["state"],
                                                    "turn": p.get("turn"), "why": p.get("why", ""), "branch": p.get("branch")}
    # 開いている PR はカードにつながっていなくてもブランチを残す（作業中の場所をブランチから探すため）
    for n, p in prs.items():
        if p.get("state") == "OPEN" and p.get("branch") and str(n) not in projects[project]["prs"]:
            projects[project]["prs"][str(n)] = {"number": n, "title": p["title"], "state": "OPEN", "turn": p.get("turn"),
                                                "why": p.get("why", ""), "branch": p["branch"]}
    return out


VERCEL_EVERY = 3600  # 秒。本番の環境変数の名前は 1 時間に 1 回まで（料金はかからないはずだが、念のため間引く・2026-10-06）
HISTORY = HOME / ".herdr" / "arch-history"


def vercel_envs(confs, old):
    """本番（Vercel）の環境変数の「名前」を読む。値は読まない。kanban.json の "vercel": {"project", "team"} があるプロジェクトだけ。
    vercel CLI の api（ログイン済みの CLI の認証を使う）で読む。読めなければ前回のものを使い、無ければ None（確かめられない）。"""
    out, now = {}, time.time()
    for project, conf in confs.items():
        v = conf.get("vercel")
        if not v:
            continue
        prev = old.get(project) or {}
        if prev.get("at", 0) + VERCEL_EVERY > now:
            out[project] = prev
            continue
        try:
            raw = run([shutil.which("vercel") or "vercel", "api", f"/v10/projects/{v['project']}/env?slug={v['team']}"],
                      cwd=str(HOME))
            data = json.loads(raw[raw.find("{"):])
            names = sorted({e["key"] for e in data.get("envs", []) if "production" in (e.get("target") or [])})
            out[project] = {"at": now, "names": names}
        except Exception as e:
            errors.append(f"{project}: 本番の環境変数の名前を読めない: {str(e)[:120]}")
            out[project] = {**prev, "at": now} if prev else {"at": now, "names": None}
    return out


def main_files(repo_dir, rc=None):
    """origin/main にあるファイルの一覧（宣言漏れ・消えたファイルを見つけるため）。読めなければ None。
    rc: そのリポジトリの使い回し（repo_cache）。同じ origin/main なら前の一覧を返す。"""
    if rc is not None and "files" in rc:
        return rc["files"]
    try:
        files = run(["git", "-c", "core.quotepath=off", "ls-tree", "-r", "--name-only", "origin/main"], cwd=repo_dir).splitlines()
    except Exception:
        return None
    if rc is not None:
        rc["files"] = files
    return files


# origin/main が動くのは fetch（FETCH_EVERY に 1 回まで）のときだけなのに、構成図のために組み立てのたびに
# git を 30 回あまり起動していた（2026-10-07）。origin/main のコミットが同じあいだは、ファイル一覧と宣言の確かめを使い回す
GIT_CACHE = HOME / ".herdr" / "kanban-git-cache.json"


def repo_cache(cache, repo_dir):
    """そのリポジトリの使い回し {"sha", "checks", "files"?}。origin/main が前回と違えば空から。読めなければ None（使い回さない）。"""
    try:
        sha = run(["git", "rev-parse", "origin/main"], cwd=repo_dir).strip()
    except Exception:
        return None
    rc = cache.get(str(repo_dir))
    if not rc or rc.get("sha") != sha:
        rc = {"sha": sha, "checks": {}}
    return rc


def cached_check(rcs, check=arch.git_check):
    """arch.compute に渡す check。rcs: {リポジトリの場所: repo_cache}。確かめられなかった（None）ものは覚えない。"""
    def cached(repo_dir, c):
        rc = rcs.get(str(repo_dir)) if repo_dir else None
        key = f"{c['path']}|{c.get('contains') or ''}"
        if rc is not None and key in rc["checks"]:
            return rc["checks"][key]
        found = check(repo_dir, c)
        if rc is not None and found is not None:
            rc["checks"][key] = found
        return found
    return cached


def cached_last_change(rcs, last_change=arch.git_last_change):
    """arch.compute に渡す last_change。origin/main が同じあいだは前の答えを使い回す。確かめられなかった（None）ものは覚えない。"""
    def cached(repo_dir, path):
        rc = rcs.get(str(repo_dir)) if repo_dir else None
        docs = rc.setdefault("docs", {}) if rc is not None else {}
        if path in docs:
            return docs[path]
        found = last_change(repo_dir, path)
        if rc is not None and found is not None:
            docs[path] = found
        return found
    return cached


def load_git_cache():
    try:
        return json.loads(GIT_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_git_cache(rcs):
    # カンバンの画面と構成図の窓が同時に組み立てることがあるので、書きかけはプロセスごとの名前にする。書けなくても次で作り直すだけ
    tmp = GIT_CACHE.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({k: v for k, v in rcs.items() if v is not None}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, GIT_CACHE)
    except OSError:
        tmp.unlink(missing_ok=True)


def history(architecture):
    """前回からの変化。その日の最初の組み立てで写しを残し（30 日ぶん）、今日より前のいちばん新しい写しと比べる。"""
    HISTORY.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    now = arch.snapshot(architecture)
    todays = HISTORY / f"{today}.json"
    if not todays.exists() and arch.trustworthy(architecture):  # 状態が分からない部品がある日は、写しを残さない
        todays.write_text(json.dumps(now, ensure_ascii=False), encoding="utf-8")
        for old in sorted(HISTORY.glob("*.json"))[:-30]:
            old.unlink()
    before = [f for f in sorted(HISTORY.glob("*.json")) if f.stem < today]
    if not before:
        return {"since": None, "nodes": {}, "skeletons": {}}
    return {"since": before[-1].stem, **arch.changes(json.loads(before[-1].read_text(encoding="utf-8")), now)}


def carry_since(cards, old_cards, now):
    """カードがいまの列にいつから居るか（since）を前回の state から引き継ぎ、外部待ちに日数を足す。
    since_column は数え始めたときの列。PR が読めなかった回は列がずれるので、前回の数え始めをそのまま持ち越す。"""
    prev = {(c["project"], c["id"]): c for c in old_cards}
    for c in cards:
        seed = c.pop("seed", None)
        p = prev.get((c["project"], c["id"]))
        if p and p.get("since") and (p.get("since_column") == c["column"] or c["project"] in degraded):
            c["since"], c["since_column"] = p["since"], p.get("since_column")
        else:
            start = datetime.fromtimestamp(seed, timezone.utc) if seed and seed < now.timestamp() else now
            c["since"], c["since_column"] = start.isoformat(timespec="seconds"), c["column"]
        days = (now - parse_time(c["since"])).days
        # PR のレビュー待ちは「レビュー待ち 3 日」が既に出ているので重ねない
        if (c["column"] == "外部待ち" and c["since_column"] == c["column"] and days >= 1
                and not any(re.search(r"\d+ 日", w) for w in c["why"])):
            c["why"] = c["why"] + [f"{days} 日動きなし"]


def build():
    old = {}
    if STATE.exists():
        try:
            old = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    fetched = old.get("fetched", {})
    try:
        me = run(["gh", "api", "user", "--jq", ".login"]).strip()
    except RuntimeError as e:
        errors.append(f"GitHub にログインできていない: {e}")
        me = ""
    cards = []
    for root in find_roots():
        try:
            cards += build_root(root, fetched, me)
        except Exception as e:
            errors.append(f"{root}: {e}")
    carry_since(cards, old.get("cards") or [], datetime.now(timezone.utc))
    try:
        roots = find_roots()
        confs = {json.loads((r / "kanban.json").read_text(encoding="utf-8"))["project"]: json.loads(
            (r / "kanban.json").read_text(encoding="utf-8")) for r in roots}
        envs = vercel_envs(confs, old.get("vercel_envs") or {})
        git_cache = load_git_cache()
        rcs = {str(m["repo_dir"]): repo_cache(git_cache, m["repo_dir"]) for m in projects.values() if m.get("repo_dir")}
        files = {p: main_files(m["repo_dir"], rcs[str(m["repo_dir"])]) for p, m in projects.items() if m.get("repo_dir")}
        architecture = arch.compute(arch.load(roots, FIXED_ROOTS / "arch-shared.json"), plans, cards,
                                    {p: m.get("repo_dir") for p, m in projects.items()}, check=cached_check(rcs), projects=projects,
                                    envs={p: set(v["names"]) if v.get("names") is not None else None for p, v in envs.items()},
                                    files={p: f for p, f in files.items() if f is not None}, unread=unread_plans,
                                    last_change=cached_last_change(rcs))
        architecture["changes"] = history(architecture)
        save_git_cache(rcs)
    except Exception as e:
        errors.append(f"構成図: 組み立てられない（前回の図のまま）: {e}")
        architecture, envs = old.get("arch"), old.get("vercel_envs") or {}
    state = {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
             "fetched": fetched, "errors": errors, "cards": cards, "plans": plans, "projects": projects, "arch": architecture,
             "vercel_envs": envs}
    # 画面が書きかけを読まないように、書き終えてから差し替える。カンバンの画面と構成図の窓が同時に組み立てることがあるので、
    # 書きかけはプロセスごとの名前にする（同じ .tmp を 2 つが書くと中身が混ざった）。差し替えが重なったら少し待ってやり直す
    tmp = STATE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    for attempt in range(5):
        try:
            tmp.replace(STATE)
            break
        except PermissionError:
            time.sleep(0.2 * (attempt + 1))
    else:
        tmp.unlink(missing_ok=True)
    return state


def show(state, project=None):
    lines = [f"カンバン（{state['generated_at']} 時点）"]
    for col in COLUMNS:
        group = [c for c in state["cards"] if c["column"] == col and (not project or c["project"] == project)]
        lines.append(f"■ {col}（{len(group)}）")
        for c in sorted(group, key=lambda c: (c["project"], c["order"])):
            tail = " / ".join(x for x in [c["progress"], *c["why"]] if x)
            mark = "◆" if c["critical"] else ""
            lines.append(f"  [{c['project']}] {mark}{c['id']} {c['title']}" + (f" — {tail}" if tail else ""))
    for e in state["errors"]:
        lines.append(f"（読めなかったもの: {e}）")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "build":
        print(show(build()))
    elif cmd == "show":
        state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else build()
        print(show(state, sys.argv[2] if len(sys.argv) > 2 else None))
    else:
        print(__doc__)
        sys.exit(1)
