"""arch.py の判断のところだけを確かめる。  python -m unittest test_arch  （kanban フォルダで）"""

import json
import unittest

import arch

PLANS = {"shop": [
    {"id": "C-1", "status": "済", "critical": True}, {"id": "C-2", "status": "やること", "critical": True}, {"id": "C-3", "status": "外部待ち"},
    {"id": "C-4", "status": "自分の番"}, {"id": "B-1", "status": "外部待ち"},
]}
CARDS = [
    {"project": "shop", "id": "C-2", "progress": "1/3", "prs": [], "why": []},
    {"project": "shop", "id": "C-3", "progress": "", "prs": [], "why": ["B-1 待ち"]},
]
ARCH = {
    "zones": [{"id": "shop:z", "project": "shop"}, {"id": "api:z", "project": "api"}],
    "nodes": [
        {"id": "shop:db", "zone": "shop:z", "project": "shop", "plan": ["shop:C-1"]},
        {"id": "shop:cart", "zone": "shop:z", "project": "shop", "plan": ["shop:C-2"]},
        {"id": "shop:mail", "zone": "shop:z", "project": "shop", "plan": ["shop:C-3"]},
        {"id": "shop:pay", "zone": "shop:z", "project": "shop", "plan": ["shop:C-1", "shop:C-3"]},
        {"id": "api:stock", "zone": "api:z", "project": "api", "plan": []},
        {"id": "api:admin", "zone": "api:z", "project": "api", "plan": [], "status": "none"},
    ],
    "edges": [
        {"from": "shop:cart", "to": "shop:db", "project": "shop", "plan": []},
        {"from": "shop:cart", "to": "api:stock", "project": "shop", "plan": [], "check": {"path": "src/a.ts", "contains": "Stock"}},
        {"from": "shop:mail", "to": "api:stock", "project": "shop", "plan": ["shop:B-1"]},
        {"from": "api:stock", "to": "api:admin", "project": "api", "plan": []},
    ],
}


class Judge(unittest.TestCase):
    def setUp(self):
        self.out = arch.compute(ARCH, PLANS, CARDS, {"shop": "C:/repo/shop"}, check=lambda repo, c: True)
        self.nodes = {n["id"]: n for n in self.out["nodes"]}

    def test_all_done_is_live(self):
        self.assertEqual(self.nodes["shop:db"]["status"], "live")

    def test_started_is_wip(self):
        self.assertEqual(self.nodes["shop:cart"]["status"], "wip")  # やることだが 1/3 済んでいる

    def test_waiting_and_untouched_is_none_with_reason(self):
        self.assertEqual((self.nodes["shop:mail"]["status"], self.nodes["shop:mail"]["blocked"]), ("none", "B-1 待ち"))

    def test_partly_done_is_wip_and_keeps_reason(self):
        self.assertEqual((self.nodes["shop:pay"]["status"], self.nodes["shop:pay"]["blocked"]), ("wip", "B-1 待ち"))

    def test_without_plan_uses_declared(self):
        self.assertEqual(self.nodes["api:stock"]["status"], "live")
        self.assertEqual(self.nodes["api:admin"]["status"], "none")

    def test_edge_is_the_weaker_end(self):
        e = {(x["from"], x["to"]): x for x in self.out["edges"]}
        self.assertEqual(e[("shop:cart", "shop:db")]["status"], "wip")
        self.assertEqual(e[("shop:mail", "api:stock")]["status"], "none")  # plan で決まる（B-1 は外部待ち）

    def test_edge_blocked_by_plan_or_by_a_stopped_end(self):
        e = {(x["from"], x["to"]): x for x in self.out["edges"]}
        self.assertEqual(e[("shop:mail", "api:stock")]["blocked"], "外部待ち")  # B-1 は外部待ち（カード無し）
        self.assertEqual(e[("shop:cart", "shop:db")]["blocked"], "")  # どちらの端も止まっていない

    def test_check_not_found_cuts_the_edge(self):
        out = arch.compute(ARCH, PLANS, CARDS, {"shop": "C:/repo/shop"}, check=lambda repo, c: False)
        e = next(x for x in out["edges"] if x["to"] == "api:stock" and x["from"] == "shop:cart")
        self.assertEqual((e["status"], e["note"]), ("none", "コードに見つからない: src/a.ts"))

    def test_check_unavailable_keeps_status(self):
        out = arch.compute(ARCH, PLANS, CARDS, {}, check=lambda repo, c: None)
        e = next(x for x in out["edges"] if x["to"] == "api:stock" and x["from"] == "shop:cart")
        self.assertEqual(e["status"], "wip")
        self.assertIn("確かめられない", e["note"])


class Skeleton(unittest.TestCase):
    def setUp(self):
        out = arch.compute(ARCH, PLANS, CARDS, {}, check=lambda repo, c: True)
        self.nodes = {n["id"]: n["skeleton"] for n in out["nodes"]}
        self.edges = {(e["from"], e["to"]): e["skeleton"] for e in out["edges"]}

    def test_node_with_a_critical_item(self):
        self.assertTrue(self.nodes["shop:db"])
        self.assertTrue(self.nodes["shop:pay"])  # C-1 が ◆
        self.assertFalse(self.nodes["shop:mail"])
        self.assertFalse(self.nodes["api:stock"])  # 計画書の項目を持たない

    def test_edge_between_two_skeleton_nodes(self):
        self.assertTrue(self.edges[("shop:cart", "shop:db")])
        self.assertFalse(self.edges[("shop:cart", "api:stock")])

    def test_declared_edge_skeleton_wins(self):
        a = json.loads(json.dumps(ARCH))
        e = next(x for x in a["edges"] if (x["from"], x["to"]) == ("shop:cart", "shop:db"))
        e["skeleton"] = False
        out = arch.compute(a, PLANS, CARDS, {}, check=lambda repo, c: True)
        self.assertFalse(next(x for x in out["edges"] if (x["from"], x["to"]) == ("shop:cart", "shop:db"))["skeleton"])


class Steps(unittest.TestCase):
    def test_follow_skeleton_edges_from_the_start(self):
        out = arch.compute(ARCH, PLANS, CARDS, {}, check=lambda repo, c: True)
        got = {n["id"]: n["steps"] for n in out["nodes"] if n["steps"]}
        self.assertEqual(got, {"shop:cart": {"shop": 1}, "shop:db": {"shop": 2}, "shop:pay": {"shop": 3}})  # 骨格でない部品には振らない

    def test_declared_steps_win(self):
        a = {**ARCH, "nodes": [{**n, "step": 9} if n["id"] == "shop:db" else
                               {**n, "step": {"shop": 10}} if n["id"] == "api:stock" else n for n in ARCH["nodes"]]}
        out = arch.compute(a, PLANS, CARDS, {}, check=lambda repo, c: True)
        got = {n["id"]: n["steps"] for n in out["nodes"] if n["steps"]}
        self.assertEqual(got, {"shop:db": {"shop": 9}, "api:stock": {"shop": 10}})  # api の部品でも shop の骨格に入る
        self.assertEqual(list(out["skeletons"]), ["shop"])

    def test_cards_can_be_referenced_without_a_plan(self):
        cards = CARDS + [{"project": "api", "id": "fix-x", "kind": "fix", "column": "外部待ち", "why": ["#9 レビュー待ち"],
                          "progress": "", "prs": [9]}]
        a = {**ARCH, "nodes": [{**n, "plan": ["api:fix-x"]} if n["id"] == "api:admin" else n for n in ARCH["nodes"]]}
        n = next(x for x in arch.compute(a, PLANS, cards, {}, check=lambda r, c: True)["nodes"] if x["id"] == "api:admin")
        self.assertEqual((n["status"], n["blocked"]), ("wip", "#9 レビュー待ち"))


class Through(unittest.TestCase):
    def node(self, step, status="live", blocked=""):
        return {"id": f"n{step}", "step": step, "status": status, "blocked": blocked}

    def test_stops_at_the_first_gap_even_if_later_ones_are_live(self):
        got = arch.through([self.node(1), self.node(2), self.node(3, "wip"), self.node(4)])
        self.assertEqual(got, {"through": 2, "total": 4, "next": "n3"})

    def test_blocked_counts_as_a_gap(self):
        self.assertEqual(arch.through([self.node(1, blocked="B-2 待ち"), self.node(2)])["through"], 0)

    def test_all_through(self):
        self.assertEqual(arch.through([self.node(1), self.node(2)]), {"through": 2, "total": 2, "next": None})


class Detail(unittest.TestCase):
    def node(self, **kw):
        n = {"id": "shop:mail", "project": "shop", "title": "メール", "plan": ["shop:C-4"], "status": "wip", "blocked": ""}
        n.update(kw)
        return n

    def card(self, **kw):
        c = {"project": "shop", "id": "C-4", "items": [{"text": "文面を決める", "done": True}, {"text": "送信の口をつなぐ", "done": False}],
             "prs": [6], "note": "経緯"}
        c.update(kw)
        return {"shop:C-4": c}

    PROJECTS = {"shop": {"prs": {"6": {"number": 6, "title": "feat: 注文確定のメール", "state": "OPEN", "turn": "them",
                                       "why": "#6 レビュー待ち 3 日"}}}}

    def test_reason_and_next_from_checklist(self):
        d = arch.detail(self.node(), self.card(), self.PROJECTS)
        self.assertEqual(d["reason"], "やること 1/2・残り「送信の口をつなぐ」")
        self.assertEqual(d["next"], "送信の口をつなぐ")
        self.assertEqual(d["main_pr"]["number"], 6)
        self.assertIn("#6（レビュー待ち 3 日）", d["ai"])

    def test_my_turn_pr_comes_first(self):
        projects = {"shop": {"prs": {"6": {**self.PROJECTS["shop"]["prs"]["6"], "turn": "me", "why": "#6 直しを頼まれている"}}}}
        self.assertEqual(arch.detail(self.node(), self.card(), projects)["next"], "#6 に対応する（直しを頼まれている）")

    def test_blocked_says_what_to_wait_for(self):
        d = arch.detail(self.node(blocked="B-2 待ち"), self.card(), self.PROJECTS)
        self.assertEqual(d["reason"], "B-2 待ち（ほかの作業・人を待っている）")
        self.assertTrue(d["next"].startswith("B-2 を待つ"))

    def test_untouched_without_card(self):
        d = arch.detail(self.node(status="none"), {}, {})
        self.assertEqual((d["reason"], d["next"]), ("まだ手を付けていない（計画書 C-4）", "計画書 C-4 に着手する"))


class ReviewFixes(unittest.TestCase):
    def test_unreadable_plan_is_not_live(self):
        # 計画書を読めないと PLANS に shop が無い。既定の「本番」にせず、状態が分からないと出す
        out = arch.compute(ARCH, {}, CARDS, {}, check=lambda r, c: True, unread={"shop"})
        n = next(x for x in out["nodes"] if x["id"] == "shop:db")
        self.assertEqual(n["status"], "none")
        self.assertIn("計画書を読めない", n["unknown"])
        self.assertFalse(arch.trustworthy(out))
        self.assertTrue(arch.trustworthy(arch.compute(ARCH, PLANS, CARDS, {}, check=lambda r, c: True)))

    def test_auto_numbers_start_at_one_per_project(self):
        nodes = {"a:1": {"project": "a", "skeleton": True}, "a:2": {"project": "a", "skeleton": True},
                 "b:1": {"project": "b", "skeleton": True}, "b:2": {"project": "b", "skeleton": True}}
        edges = [{"from": "a:1", "to": "a:2", "skeleton": True}, {"from": "b:1", "to": "b:2", "skeleton": True}]
        got = arch.steps(nodes, edges)
        self.assertEqual(got, {"a:1": {"a": 1}, "a:2": {"a": 2}, "b:1": {"b": 1}, "b:2": {"b": 2}})

    def test_declared_project_does_not_stop_auto_numbers_elsewhere(self):
        nodes = {"a:1": {"project": "a", "skeleton": True, "step": 1},
                 "b:1": {"project": "b", "skeleton": True}, "b:2": {"project": "b", "skeleton": True}}
        edges = [{"from": "b:1", "to": "b:2", "skeleton": True}]
        got = arch.steps(nodes, edges)
        self.assertEqual(got, {"a:1": {"a": 1}, "b:1": {"b": 1}, "b:2": {"b": 2}})

    def test_shared_part_numbered_for_another_project_gets_no_own_skeleton(self):
        nodes = {"shared:checkout": {"project": "shared", "skeleton": True, "step": {"a": 1}},
                 "shared:payment": {"project": "shared", "skeleton": True, "step": {"a": 2}},
                 "a:in": {"project": "a", "skeleton": True, "step": 3}}
        edges = [{"from": "shared:checkout", "to": "shared:payment", "skeleton": True}]
        got = arch.steps(nodes, edges)
        self.assertEqual(got, {"shared:checkout": {"a": 1}, "shared:payment": {"a": 2}, "a:in": {"a": 3}})


class Drift(unittest.TestCase):
    FILES = ["src/core/cart.ts", "src/core/price.ts", "src/core/price.test.ts", "src/adapters/mail/template.ts",
             "src/adapters/mail/sender.ts", "src/adapters/db/orders.ts", "src/core/types.ts"]
    NODES = [{"id": "p:a", "paths": ["src/core/price.ts", "src/core/gone.ts"]},
             {"id": "p:b", "paths": ["src/adapters/mail/"]}]

    def test_unclaimed_children_and_missing_paths(self):
        d = arch.drift({"dirs": ["src/core/", "src/adapters/"], "ignore": ["src/core/types.ts"]}, self.NODES, self.FILES)
        self.assertEqual(d["unclaimed"], ["src/core/cart.ts", "src/adapters/db"])  # テスト・ignore・持ち主のある所は数えない
        self.assertEqual(d["missing"], [{"node": "p:a", "path": "src/core/gone.ts"}])


class StaleDocs(unittest.TestCase):
    LOG = {"docs/plan.md": {"at": 200, "hash": "abc", "subject": "送料の決め方を書いた"}, "docs/old.md": {"at": 50, "hash": "def", "subject": "前"}}

    def last(self, repo, path):
        return self.LOG.get(path)

    def test_docs_changed_after_declaration(self):
        nodes = [{"id": "p:a", "docs": ["docs/plan.md", "docs/old.md"]}, {"id": "p:b", "docs": ["docs/plan.md", "~/x.md"]},
                 {"id": "p:c", "docs": ["docs/none.md"]}]
        got = arch.stale_docs(nodes, 100, self.last, "C:/repo")
        self.assertEqual([(x["doc"], x["nodes"], x["hash"]) for x in got], [("docs/plan.md", ["p:a", "p:b"], "abc")])

    def test_declaration_saved_after_change_or_no_repo(self):
        nodes = [{"id": "p:a", "docs": ["docs/plan.md"]}]
        self.assertEqual(arch.stale_docs(nodes, 300, self.last, "C:/repo"), [])
        self.assertEqual(arch.stale_docs(nodes, 100, self.last, None), [])

    def test_compute_puts_them_in_drift(self):
        a = {**ARCH, "declared_at": {"shop": 100},
             "nodes": [{**n, "docs": ["docs/plan.md"]} if n["id"] == "shop:db" else n for n in ARCH["nodes"]]}
        out = arch.compute(a, PLANS, CARDS, {"shop": "C:/repo/shop"}, check=lambda repo, c: True, last_change=self.last)
        self.assertEqual(out["drift"]["shop"]["stale_docs"][0]["nodes"], ["shop:db"])
        self.assertNotIn("stale_docs", arch.compute(a, PLANS, CARDS, {"shop": "C:/repo/shop"}, check=lambda repo, c: True)["drift"]["shop"])


class Reason(unittest.TestCase):
    def test_wip_without_plan_says_it_is_declared(self):
        n = {"id": "p:search", "project": "p", "title": "商品検索", "plan": [], "status": "wip", "blocked": "", "paths": [], "docs": []}
        self.assertEqual(arch.detail(n, {}, {})["reason"], "計画書の項目を持たない（宣言で作りかけとしている）")


class Unplanned(unittest.TestCase):
    def test_plan_rows_without_a_node_unless_ignored(self):
        a = {"nodes": [{"id": "p:a", "plan": ["p:C-1"]}], "edges": [{"plan": ["p:C-2"]}]}
        rows = [{"id": "C-1", "title": "一"}, {"id": "C-2", "title": "二"}, {"id": "C-3", "title": "三"}, {"id": "A-1", "title": "決め"}]
        self.assertEqual(arch.unplanned("p", rows, a, ["A-1"]), [{"id": "C-3", "title": "三"}])


class Switch(unittest.TestCase):
    def test_missing_switch_caps_live_at_wip(self):
        a = {**ARCH, "edges": [{**e, "switch": "SHOP_STOCK"} if e["to"] == "api:stock" and e["from"] == "shop:cart" else e
                               for e in ARCH["edges"]],
             "nodes": [{**n, "switch": ["API_ADMIN"], "status": "live"} if n["id"] == "api:admin" else n for n in ARCH["nodes"]]}
        out = arch.compute(a, PLANS, CARDS, {}, check=lambda r, c: True, envs={"shop": {"OTHER"}, "api": {"API_ADMIN"}})
        e = next(x for x in out["edges"] if x["from"] == "shop:cart" and x["to"] == "api:stock")
        self.assertIn("本番に SHOP_STOCK が無い", e["note"])
        n = next(x for x in out["nodes"] if x["id"] == "api:admin")
        self.assertEqual((n["status"], n["switch_note"]), ("live", "本番に API_ADMIN がある（値は見ていない）"))

    def test_unknown_when_vercel_is_unreadable(self):
        self.assertEqual(arch.switch_note({"switch": "X"}, None)[0], [])


class Changes(unittest.TestCase):
    def test_status_and_skeleton_changes(self):
        before = {"nodes": {"a": {"status": "wip", "blocked": ""}, "b": {"status": "live", "blocked": ""}}, "skeletons": {"p": 3}}
        now = {"nodes": {"a": {"status": "live", "blocked": ""}, "b": {"status": "live", "blocked": ""},
                         "c": {"status": "none", "blocked": ""}}, "skeletons": {"p": 5}}
        got = arch.changes(before, now)
        self.assertEqual(set(got["nodes"]), {"a", "c"})  # 変わった部品と、新しく宣言した部品
        self.assertEqual(got["skeletons"], {"p": {"from": 3, "to": 5}})


class Working(unittest.TestCase):
    def test_branch_to_pr_to_card_to_node_and_name_match(self):
        nodes = [{"id": "p:mail", "plan": ["p:C-4"]}, {"id": "p:login", "plan": ["p:fix-login-button"]}, {"id": "p:db", "plan": []}]
        cards = [{"project": "p", "id": "C-4", "prs": [7]}, {"project": "p", "id": "fix-login-button", "prs": []}]
        prs = {"p": {"7": {"number": 7, "state": "OPEN", "branch": "feat/order-mail"}}}
        got = arch.working(nodes, cards, prs, {"feat/order-mail": True, "fix/login-button": False, "main": False})
        self.assertEqual(got, {"p:mail": {"branch": "feat/order-mail", "focused": True},
                               "p:login": {"branch": "fix/login-button", "focused": False}})

    def test_branch_written_on_card_before_pr(self):
        nodes = [{"id": "p:returns", "plan": ["p:C-3"]}]
        cards = [{"project": "p", "id": "C-3", "prs": [], "branches": ["feat/returns"]}]
        got = arch.working(nodes, cards, {"p": {}}, {"feat/returns": False})
        self.assertEqual(got, {"p:returns": {"branch": "feat/returns", "focused": False}})


class Scope(unittest.TestCase):
    def test_project_and_one_hop(self):
        self.assertEqual(arch.scope(ARCH, "api"), {"api:stock", "api:admin", "shop:cart", "shop:mail"})

    def test_everything(self):
        self.assertEqual(len(arch.scope(ARCH, "")), 6)

    def test_parts_on_the_project_skeleton_even_when_far(self):
        a = {"nodes": [{"id": "api:stock", "project": "api"}, {"id": "x:payment", "project": "x", "steps": {"api": 2}},
                       {"id": "x:other", "project": "x", "steps": {"x": 1}}],
             "edges": []}
        self.assertEqual(arch.scope(a, "api"), {"api:stock", "x:payment"})


class ProjectOf(unittest.TestCase):
    REPOS = {"shop": "C:/dev/shop", "api": "C:/dev/api"}

    def test_repo_and_inside(self):
        self.assertEqual(arch.project_of("C:/dev/shop", self.REPOS), "shop")
        self.assertEqual(arch.project_of("C:/dev/shop/src/x", self.REPOS), "shop")

    def test_worktree_next_to_repo(self):
        self.assertEqual(arch.project_of("C:/dev/api-wt-fix", self.REPOS), "api")
        self.assertEqual(arch.project_of("C:/dev/api-wt-fix/src", self.REPOS), "api")

    def test_folder_of_repos_is_everything(self):
        self.assertEqual(arch.project_of("C:/dev", self.REPOS), "")

    def test_unrelated_keeps_current(self):
        self.assertIsNone(arch.project_of("C:/elsewhere", self.REPOS))


if __name__ == "__main__":
    unittest.main()
