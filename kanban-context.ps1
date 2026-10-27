# SessionStart: 今いるリポジトリのカンバン（まだ終わっていないカード）を Claude の文脈に入れる。
# カンバンを使うかを Claude の判断だけに任せると飛ばされるので、毎回必ず目に入れる。
# 中身は ~/.claude/skills/kanban/board.py が組み立てた ~/.herdr/kanban-state.json を読むだけ（2026-09-30 に作り直し）。
# 古ければ裏で組み立て直しを頼む（待たない。フックの持ち時間は 10 秒）。
# 何かで失敗しても exit 0（セッション開始を止めない）。

$ErrorActionPreference = "Stop"
try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
    if (-not [string]::IsNullOrWhiteSpace($payload.agent_id)) { exit 0 }
    $dir = $payload.cwd
    if ([string]::IsNullOrWhiteSpace($dir)) { exit 0 }

    # .git を上へ探す。ファイルなら worktree なので gitdir から本体を辿る（git は起動しない）
    $repo = $null
    while ($dir) {
        $git = Join-Path $dir ".git"
        if (Test-Path $git -PathType Container) { $repo = $dir; break }
        if (Test-Path $git -PathType Leaf) {
            $line = (Get-Content $git -TotalCount 1) -replace '^gitdir:\s*', ''
            $common = $line -replace '[\\/]worktrees[\\/][^\\/]+[\\/]?$', ''
            $repo = Split-Path $common -Parent
            break
        }
        $parent = Split-Path $dir -Parent
        if ($parent -eq $dir) { break }
        $dir = $parent
    }
    if (-not $repo) { exit 0 }
    $project = Split-Path $repo -Leaf

    $statePath = Join-Path $env:USERPROFILE ".herdr\kanban-state.json"
    $boardPy = Join-Path $env:USERPROFILE ".claude\skills\kanban\board.py"
    if (-not (Test-Path $statePath)) { exit 0 }
    $state = Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $mine = @($state.cards | Where-Object { $_.project -eq $project })
    if ($mine.Count -eq 0) { exit 0 }

    $age = [DateTimeOffset]::Now - [DateTimeOffset]::Parse($state.generated_at)
    if ($age.TotalMinutes -gt 15) {
        Start-Process -FilePath python -ArgumentList "`"$boardPy`" build" -WindowStyle Hidden | Out-Null
    }

    $out = @()
    $out += "[kanban] $project のカンバン（$([DateTimeOffset]::Parse($state.generated_at).ToString('MM-dd HH:mm')) 時点。最新は python `"$boardPy`" build）"
    if (@($mine | Where-Object { $_.kind -eq 'plan' }).Count -gt 0) {
        $out += "作業依頼を受けたら kanban スキルに沿って動く。カードは計画書の項目が軸（新しく作らない）。修正・エラー対処だけ fix カードを作る。PR を出したら、そのカードにつながるか確かめる。"
    } else {
        $out += "作業依頼を受けたら kanban スキルに沿って動く。このプロジェクトは計画書を軸にしていないので、作業カード（work）を使う。PR を出したら、そのカードにつながるか確かめる。"
    }
    foreach ($col in @('自分の番', 'やること', '外部待ち')) {
        $group = @($mine | Where-Object { $_.column -eq $col } | Sort-Object order)
        if ($group.Count -eq 0) { continue }
        $out += "■ $col ($($group.Count))"
        foreach ($c in $group) {
            $label = if ($c.kind -eq 'fix') { "修正 $($c.title)" } else { "$($c.id) $($c.title)" }
            $tail = @(@($c.progress) + @($c.why) | Where-Object { $_ }) -join ' / '
            $out += "- $label$(if ($tail) { " — $tail" })"
        }
    }
    $out -join "`n"
} catch {
    exit 0
}
exit 0
