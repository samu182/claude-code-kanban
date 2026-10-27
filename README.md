# kanban

AI（Claude Code）と一緒に、複数のプロジェクトを並行して進めるための個人のカンバンです。
Claude Code のスキル（`SKILL.md`）と、ターミナル（herdr）に出す画面でできています。

## 何に困っていたか

最初は、カード 1 枚 = PR 1 本にして、5 つの列を手で動かしていました。
すると、カードを動かし忘れて PR の実態とずれ、閉じられないカードが残り続けました（あるプロジェクトでは、完了にできたカードがほんの一部だけでした）。

## どうしたか

**人もAIも、カードを手で動かさない。**列は、すでにある情報から自動で決めます。

- **カードの軸は計画書の項目。**計画書の表の 1 行がそのまま 1 枚になるので、カードを「作る」作業がありません
- **列は PR の状態で決まる。**ドラフト・返事していないコメントがある → 自分の番。レビュー待ち・merge 待ち → 外部待ち
- **待ちは計画書の「待つもの」の欄から。**先に済ませる項目が残っていれば外部待ち
- **外部待ちには「3 日動きなし」が付く。**待っているだけで止まっているものに気づける
- **つなぎ忘れも出す。**どのカードにもつながっていない自分の PR は、それ自体がカードとして出ます

## 仕組み

```
計画書（各リポジトリの origin/main）＋ カード（~/.herdr/kanban-data）＋ GitHub の PR
        │
        ▼
  board.py … 判断はここだけ。列と理由を決めて state を書く
        │
        ▼
  ~/.herdr/kanban-state.json
        │
        ├─► kanban-board.ps1    ターミナルの画面（3 列）
        ├─► kanban-context.ps1  Claude Code のセッション開始時に、そのリポジトリのカードを見せる
        ├─► map.html            ブラウザの地図（計画書の項目と依存を図にする）
        └─► arch/               構成図の窓（下の節）
```

表示する側は state を読むだけで、判断を持ちません。判断は `board.py` にまとめ、テスト（`test_board.py`）で押さえています。

| ファイル | 中身 |
|---|---|
| `SKILL.md` | Claude Code に渡す決まり（いつカードを触るか・列の決まり方） |
| `board.py` / `test_board.py` | 組み立てと、そのテスト（`python -m unittest test_board`） |
| `kanban-board.ps1` | herdr のペインに出す画面（PwshSpectreConsole で描く） |
| `kanban-context.ps1` | Claude Code の SessionStart フック |
| `map.html` / `map_server.py` | ブラウザの地図と、それを手元だけに配る口 |
| `arch/` | 構成図（下の節） |
| `examples/kanban.json` | プロジェクトの設定の例 |

## 構成図（`arch/`）

カンバンと同じ材料（計画書・カード・PR）から、**システムの構成と、どこまでできたか**を小さな窓に描きます。
枠 = システム、四角 = 部品、線 = 呼び出し。部品と線は宣言（`arch.json`）に書き、状態は計画書とコードから自動で決めます。

- **状態は自動。**計画書の項目が全部済めば本番、手が付いていれば作りかけ、外部待ちなら待ち。線は origin/main にその記述があるかでも確かめます
- **ウォーキングスケルトンは「端から何番まで切れ目なく本番か」で見る。**部品に通る順の番号を振り、下の帯で進み具合を出します。矢印キーで 1 つずつたどれます
- **線は普段まとめて、見たいときだけ出す。**枠どうしの線は 1 本にまとめて本数を書き、部品を選んだときだけ行き先まで出します
- **部品を押すと「なぜこの状態か」と「次の一手」。**AI（Claude Code）にそのまま渡せる 1 文もコピーできます
- **軽い。**ブラウザを使わず、Rust（egui）で CPU で描くので約 30 MB（ブラウザのタブ 1 枚は約 235 MB だった。GPU で描くとドライバーだけで約 100 MB 使った）。窓は別リポジトリ `andon`。ここ（`arch/`）は判断だけを持つ

| ファイル | 中身 |
|---|---|
| `arch/arch.py` / `arch/test_arch.py` | 状態の判断と、そのテスト（`arch/` で `python -m unittest test_arch`） |
| `arch/open-andon.ps1` | herdr のキーから開くための入口（`~/.herdr/bin/andon.exe` を開く。無ければ `arch_view.py`） |
| `arch/arch_view.py` | 前の窓（tkinter）。Rust 版に移したので、しばらく残してから消す |
| `arch/example.json` | 宣言の例 |

## 入れ方（自分の環境）

1. このフォルダを `~/.claude/skills/kanban` から見えるようにする（Windows ならジャンクション）
2. `~/.claude/settings.json` の `hooks.SessionStart` に `kanban-context.ps1` を足す
3. herdr の設定で、ペインのコマンドに `kanban-board.ps1` を指定する
4. `~/.herdr/kanban-data/<project>/kanban.json` を置き、`python board.py build`

カードや計画書の中身はこのリポジトリに入りません（`~/.herdr/kanban-data/` に置きます）。
