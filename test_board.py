"""board.py の判断のところだけを確かめる。  python -m unittest test_board  （kanban フォルダで）"""

import unittest
from datetime import datetime, timezone

import board

PLAN = """
## 3. 店を開けるまでの作業

### A. 方針 — カードにしない

| # | 方針 | 誰と |
|---|---|---|
| ~~◆ A-1~~ | ~~送料の決め方~~ 済 | |
| A-2 | 返品の受け付け方 | 店長と話す |

### B. 下ごしらえ — カードにする

| # | 中身 | 注記 |
|---|---|---|
| ◆ B-1 | 本番の DB を用意する（注文と在庫の表） | 業者に頼む |
| B-2 | API: **商品の一覧を返す口**（管理画面から使う） | いまは固定の 3 件を返すだけ |
| ~~B-4~~ | ~~API: 送料の表~~ 済 | |
| ◆ B-5 | **入荷の手順**: 在庫を数えて入れる | |

### C. 画面（shop-front）— カードにする

| # | 中身 | 待つもの |
|---|---|---|
| C-1 | **商品ページの部品** | B-2〜B-4 |
| ◆ C-2 | **カートから注文を作る**<br>半分できた | ~~A-1~~ 決まった |
| C-3 | **返品の受付画面 ＋ 確認のメール** | A-2 |
| ◆ C-4 | **注文確定のメール** | 文面は店長の確認待ち（C-4 を作るまでに返す） |

## 4. 次の章
| X-1 | ここは読まない | |
"""


class ReadPlan(unittest.TestCase):
    def setUp(self):
        board.run = lambda args, cwd=None: PLAN if args[-1].startswith("origin/main:") else ""
        self.items = board.read_plan(board.Path("."), {"path": "計画.md", "section": "## 3."}, {".": 1e12})

    def test_rows_and_sections(self):
        self.assertEqual(list(self.items), ["A-1", "A-2", "B-1", "B-2", "B-4", "B-5", "C-1", "C-2", "C-3", "C-4"])
        self.assertNotIn("X-1", self.items)
        self.assertFalse(self.items["A-2"]["card"])      # 「カードにしない」
        self.assertTrue(self.items["B-1"]["card"])

    def test_groups_and_struck_titles(self):
        self.assertEqual(self.items["A-1"]["group"], "A. 方針")
        self.assertEqual(self.items["C-2"]["group"], "C. 画面（shop-front）")
        self.assertEqual(self.items["B-4"]["title"], "API: 送料の表")  # 取り消し線は題に残さない

    def test_done_and_critical(self):
        self.assertTrue(self.items["A-1"]["done"])
        self.assertTrue(self.items["B-4"]["done"])
        self.assertFalse(self.items["B-1"]["done"])
        self.assertTrue(self.items["B-1"]["critical"])
        self.assertFalse(self.items["B-2"]["critical"])

    def test_titles(self):
        self.assertEqual(self.items["B-1"]["title"], "本番の DB を用意する")
        self.assertEqual(self.items["B-2"]["title"], "API: 商品の一覧を返す口")
        self.assertEqual(self.items["B-5"]["title"], "入荷の手順")
        self.assertEqual(self.items["C-2"]["title"], "カートから注文を作る")

    def test_deps_only_from_the_waiting_column(self):
        self.assertEqual(self.items["C-1"]["deps"], ["B-2", "B-3", "B-4"])
        self.assertEqual(self.items["C-2"]["deps"], [])  # 取り消し線の A-1 は済んだもの
        self.assertEqual(self.items["C-3"]["deps"], ["A-2"])
        self.assertEqual(self.items["B-1"]["deps"], [])  # B の 3 列目は注記で、待つものではない
        self.assertEqual(self.items["C-4"]["deps"], [])  # 自分の番号は待つものではない


STEPS = """
## 5. 段取り

| 段 | 作業 | 終わりの形 | 要るもの | 日数 |
|---|---|---|---|---|
| 1 | **最初の注文**：カートから決済まで | 注文が届く | 決済の口 | 4 日 |
| 2 | 商品を増やす | 20 品 | — | 2 日 |
| ~~3~~ | ~~売れ筋を調べる~~ 済 | 表 1 枚 | なし | 1 日 |

## 6. 次の章
| 9 | ここは読まない | | | |
"""


class ReadPlanNumbered(unittest.TestCase):
    def test_number_only_rows_become_prefixed_cards(self):
        board.run = lambda args, cwd=None: STEPS if args[-1].startswith("origin/main:") else ""
        items = board.read_plan(board.Path("."), {"path": "計画.md", "section": "## 5.", "number_ids": "P-"}, {".": 1e12})
        self.assertEqual(list(items), ["P-1", "P-2", "P-3"])
        self.assertTrue(items["P-3"]["done"])
        self.assertTrue(all(i["card"] for i in items.values()))  # 見出しが無くても全部カード
        self.assertEqual(items["P-1"]["title"], "最初の注文")
        self.assertEqual(items["P-1"]["deps"], [])  # 3 列目は完了条件で、待つものではない


T0 = "2026-09-29T00:00:00Z"
T1 = "2026-09-29T05:00:00Z"
T2 = "2026-09-29T09:00:00Z"
NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


def pr(**kw):
    base = {"number": 5, "isDraft": False, "commits": [{"committedDate": T1}], "comments": [], "reviews": [],
            "reviewDecision": ""}
    base.update(kw)
    return base


class JudgeOpenPr(unittest.TestCase):
    def judge(self, p):
        return board.judge_open_pr(p, "me", NOW)

    def test_draft_is_mine(self):
        self.assertEqual(self.judge(pr(isDraft=True)), {"turn": "me", "why": "#5 ドラフト"})

    def test_waiting_for_review(self):
        self.assertEqual(self.judge(pr())["turn"], "them")

    def test_comment_after_my_last_push_is_mine(self):
        c = {"author": {"login": "reviewer"}, "createdAt": T2}
        self.assertEqual(self.judge(pr(comments=[c])), {"turn": "me", "why": "#5 返事していないコメント 1 件"})

    def test_answered_comment_goes_back(self):
        c = {"author": {"login": "reviewer"}, "createdAt": T1}
        mine = {"author": {"login": "me"}, "createdAt": T2}
        self.assertEqual(self.judge(pr(commits=[{"committedDate": T0}], comments=[c, mine]))["turn"], "them")

    def test_bots_do_not_count(self):
        c = {"author": {"login": "vercel"}, "createdAt": T2}
        d = {"author": {"login": "github-actions"}, "createdAt": T2}
        self.assertEqual(self.judge(pr(comments=[c, d]))["turn"], "them")

    def test_approved_waits_for_merge(self):
        r = {"author": {"login": "reviewer"}, "submittedAt": T2, "state": "APPROVED"}
        self.assertEqual(self.judge(pr(reviews=[r], reviewDecision="APPROVED")),
                         {"turn": "them", "why": "#5 承認済み・merge 待ち"})


class Place(unittest.TestCase):
    PRS = {
        1: {"number": 1, "state": "OPEN", "turn": "them", "why": "#1 レビュー待ち"},
        2: {"number": 2, "state": "OPEN", "turn": "me", "why": "#2 ドラフト"},
        3: {"number": 3, "state": "MERGED"},
    }

    def test_open_pr_decides(self):
        self.assertEqual(board.place({"prs": [1]}, self.PRS, []), ("外部待ち", ["#1 レビュー待ち"]))

    def test_any_pr_of_mine_wins(self):
        self.assertEqual(board.place({"prs": [1, 2]}, self.PRS, [])[0], "自分の番")

    def test_manual_column_when_no_open_pr(self):
        card = {"prs": [3], "column": "外部待ち", "wait": "API 側の作業"}
        self.assertEqual(board.place(card, self.PRS, []), ("外部待ち", ["API 側の作業"]))

    def test_open_deps_wait(self):
        self.assertEqual(board.place({"prs": []}, self.PRS, ["A-2"]), ("外部待ち", ["A-2 待ち"]))

    def test_open_pr_not_read_in_detail_does_not_break(self):
        # 自分以外の PR や、読み込んだ本数の外の PR は「開いている」ことしか分からない
        prs = {4: {"number": 4, "state": "OPEN"}}
        self.assertEqual(board.place({"prs": [4]}, prs, []), ("外部待ち", ["#4 開いている"]))

    def test_default_is_todo(self):
        self.assertEqual(board.place({"prs": [3]}, self.PRS, []), ("やること", []))


class PrRefs(unittest.TestCase):
    def test_section_number_in_title(self):
        p = {"title": "feat: 注文確定のメールを出す（計画書 §3 C-4）", "body": "§3 C-3 の続き"}
        self.assertEqual(board.pr_refs(p, "3"), {"C-4", "C-3"})

    def test_other_ids_are_not_links(self):
        p = {"title": "docs: A-1 を決める（台帳 S-3）", "body": ""}
        self.assertEqual(board.pr_refs(p, "3"), set())


def card(column="外部待ち", why=None, **kw):
    c = {"project": "p", "id": "C-1", "column": column, "why": ["API 側の作業"] if why is None else why, "seed": None}
    c.update(kw)
    return c


class CarrySince(unittest.TestCase):
    def setUp(self):
        board.degraded.clear()

    def test_first_time_starts_now(self):
        c = card()
        board.carry_since([c], [], NOW)
        self.assertEqual((c["since"], c["since_column"]), (NOW.isoformat(timespec="seconds"), "外部待ち"))
        self.assertEqual(c["why"], ["API 側の作業"])  # 0 日は出さない
        self.assertNotIn("seed", c)

    def test_manual_column_starts_from_card_file(self):
        c = card(seed=datetime(2026, 9, 26, tzinfo=timezone.utc).timestamp())
        board.carry_since([c], [], NOW)
        self.assertEqual(c["why"], ["API 側の作業", "4 日動きなし"])

    def test_same_column_keeps_counting(self):
        old = card(since="2026-09-27T00:00:00+00:00", since_column="外部待ち")
        c = card()
        board.carry_since([c], [old], NOW)
        self.assertEqual(c["why"][-1], "3 日動きなし")

    def test_column_change_restarts(self):
        old = card(column="自分の番", since="2026-09-20T00:00:00+00:00", since_column="自分の番")
        c = card()
        board.carry_since([c], [old], NOW)
        self.assertEqual(c["since"], NOW.isoformat(timespec="seconds"))

    def test_only_waiting_column_shows_days(self):
        old = card(column="自分の番", since="2026-09-20T00:00:00+00:00", since_column="自分の番")
        c = card(column="自分の番", why=[])
        board.carry_since([c], [old], NOW)
        self.assertEqual(c["why"], [])

    def test_review_wait_days_are_not_doubled(self):
        old = card(since="2026-09-27T00:00:00+00:00", since_column="外部待ち")
        c = card(why=["#5 レビュー待ち 3 日"])
        board.carry_since([c], [old], NOW)
        self.assertEqual(c["why"], ["#5 レビュー待ち 3 日"])

    def test_unreadable_prs_do_not_restart(self):
        # PR が読めない回は外部待ちがやることに落ちるが、数え始めは持ち越す。戻ったら続きから
        board.degraded.add("p")
        old = card(since="2026-09-27T00:00:00+00:00", since_column="外部待ち")
        mid = card(column="やること", why=[])
        board.carry_since([mid], [old], NOW)
        self.assertEqual((mid["since_column"], mid["why"]), ("外部待ち", []))
        board.degraded.clear()
        c = card()
        board.carry_since([c], [mid], NOW)
        self.assertEqual(c["why"][-1], "3 日動きなし")


class GitCache(unittest.TestCase):
    """構成図の確かめは、origin/main のコミットが同じあいだ使い回す"""

    def setUp(self):
        self.sha, self.calls, self.real_run = "aaa", [], board.run
        board.run = lambda args, cwd=None: self.sha + "\n"
        self.check = lambda repo_dir, c: self.calls.append(c["path"]) or self.found
        self.found = True

    def tearDown(self):
        board.run = self.real_run

    def build(self, cache):
        rcs = {"r": board.repo_cache(cache, "r")}
        check = board.cached_check(rcs, self.check)
        return rcs, check("r", {"path": "a.ts", "contains": "x"})

    def test_same_main_asks_git_once(self):
        rcs, _ = self.build({})
        _, found = self.build(rcs)
        self.assertEqual((found, self.calls), (True, ["a.ts"]))

    def test_new_main_asks_again(self):
        rcs, _ = self.build({})
        self.sha, self.found = "bbb", False
        _, found = self.build(rcs)
        self.assertEqual((found, self.calls), (False, ["a.ts", "a.ts"]))

    def test_unknown_is_not_kept(self):
        self.found = None
        rcs, _ = self.build({})
        self.build(rcs)
        self.assertEqual(len(self.calls), 2)

    def test_files_are_kept_with_main(self):
        rc = board.repo_cache({}, "r")
        board.run = lambda args, cwd=None: "a.ts\nb.ts"
        self.assertEqual(board.main_files("r", rc), ["a.ts", "b.ts"])
        board.run = lambda args, cwd=None: (_ for _ in ()).throw(AssertionError("git を呼んだ"))
        self.assertEqual(board.main_files("r", rc), ["a.ts", "b.ts"])


    def test_last_change_is_kept_with_main(self):
        calls = []
        last = lambda repo_dir, path: calls.append(path) or {"at": 1, "hash": "h", "subject": "s"}
        rcs = {"r": board.repo_cache({}, "r")}
        board.cached_last_change(rcs, last)("r", "docs/a.md")
        rcs = {"r": board.repo_cache(rcs, "r")}
        self.assertEqual(board.cached_last_change(rcs, last)("r", "docs/a.md")["hash"], "h")
        self.assertEqual(calls, ["docs/a.md"])

if __name__ == "__main__":
    unittest.main()
