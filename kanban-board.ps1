# 個人カンバンを、3列（やること / 自分の番 / 外部待ち）を横に並べた見た目で、herdr のペインに出し続ける。
# 中身は ~/.claude/skills/kanban/board.py が組み立てて ~/.herdr/kanban-state.json に書く（判断はそちらだけ）。
# この画面は、その結果を読んで描くのと、90 秒ごと（何かキーを押せばすぐ）に組み立て直しを頼むだけ。
# 2026-09-30 に 5列（カードを手で動かす）から作り直した。
# 箱（枠線）の描画と CJK 幅の計算は PwshSpectreConsole に任せる（手書きで何度もバグを踏んだため）。
# Ctrl+C か、ペインを閉じれば終了する。
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
$env:IgnoreSpectreEncoding = 'true'
$env:PYTHONIOENCODING = 'utf-8'
# 2026-09-21 に Documents\PowerShell が Documents\app\PowerShell へ移され、
# 標準のモジュール探索パスから外れて開けなくなった。見つからなければ移動先を探す。
if (-not (Get-Module -ListAvailable PwshSpectreConsole)) {
    $env:PSModulePath = "$env:USERPROFILE\Documents\app\PowerShell\Modules;$env:PSModulePath"
}
Import-Module PwshSpectreConsole -ErrorAction Stop

$boardPy = Join-Path $env:USERPROFILE '.claude\skills\kanban\board.py'
$statePath = Join-Path $env:USERPROFILE '.herdr\kanban-state.json'
# ブラウザの地図（map.html）の配信の口。動いていなければ裏で立てる（すでに動いていれば map_server.py がすぐ終わる）
$mapServer = Join-Path $env:USERPROFILE '.claude\skills\kanban\map_server.py'
$mapUrl = 'http://127.0.0.1:8765/'
try { Start-Process -FilePath python -ArgumentList "`"$mapServer`"" -WindowStyle Hidden | Out-Null } catch { }
$rebuildEvery = [TimeSpan]::FromSeconds(90)  # GitHub に問い合わせるので、短くしすぎない
# キーで今すぐ組み立てるときも、前の組み立てからこれだけは空ける。herdr のフォーカスやマウスの動きも
# キーとして届き、15〜45 秒ごとに組み立てて GitHub の上限を使い切っていた（2026-10-06 実測）
$rebuildAtLeast = [TimeSpan]::FromSeconds(20)

$columnOrder = @('やること', '自分の番', '外部待ち')
$columnColor = @{ 'やること' = "`e[97m"; '自分の番' = "`e[92m"; '外部待ち' = "`e[93m" }
$columnSpectreColor = @{ 'やること' = 'white'; '自分の番' = 'green1'; '外部待ち' = 'yellow1' }
$reset = "`e[0m"
# 1列あたりの表示上限。ペインの高さを超えると herdr のスクロールバーが出て、
# 再描画のたびに一番下へ飛ばされる（2026-09-11）。超えた分は「…他N件」の1行にまとめる。
$maxCardsPerColumn = 10

# --- 表示幅（全角=2, 半角=1）を考慮したパディング（見出しの1行だけに使う） -------------
function Get-DisplayWidth([string]$s) {
  $w = 0
  foreach ($ch in $s.EnumerateRunes()) {
    $cp = $ch.Value
    if (($cp -ge 0x1100 -and $cp -le 0x115F) -or ($cp -ge 0x2E80 -and $cp -le 0xA4CF) -or
        ($cp -ge 0xAC00 -and $cp -le 0xD7A3) -or ($cp -ge 0xF900 -and $cp -le 0xFAFF) -or
        ($cp -ge 0xFF00 -and $cp -le 0xFF60) -or ($cp -ge 0xFFE0 -and $cp -le 0xFFE6) -or
        ($cp -ge 0x1F300 -and $cp -le 0x1FAFF)) { $w += 2 } else { $w += 1 }
  }
  return $w
}

function Pad-Str([string]$s, [int]$width) {
  $w = Get-DisplayWidth $s
  if ($w -ge $width) {
    $out = ''; $acc = 0
    foreach ($ch in $s.EnumerateRunes()) {
      $cw = if ((Get-DisplayWidth $ch.ToString()) -eq 2) { 2 } else { 1 }
      if ($acc + $cw -gt $width - 1) { break }
      $out += $ch.ToString(); $acc += $cw
    }
    return $out + '…' + (' ' * [Math]::Max(0, $width - (Get-DisplayWidth $out) - 1))
  }
  return $s + (' ' * ($width - $w))
}

# プロジェクトごとに常に同じ色。kanban.json に "color" があればそれ（Spectre の色名）、無ければ名前から決める。
$projectPalette = @('orange1', 'deeppink3', 'turquoise2', 'mediumpurple1', 'chartreuse2', 'deepskyblue1', 'gold3', 'hotpink')
function Get-ProjectColor([string]$project, [string]$fixed) {
  if ($fixed) { return $fixed }
  $h = 0
  foreach ($ch in $project.ToCharArray()) { $h = ($h * 31 + [int]$ch) % $projectPalette.Count }
  return $projectPalette[[Math]::Abs($h) % $projectPalette.Count]
}

# --- カードを1枚の Panel（枠線付き箱）として描き、テキスト行の配列で返す -----
# 中身（題・理由）は Spectre のマークアップ記法（[...]）と衝突しうるので、必ずエスケープしてから渡す（2026-09-11 に実機で踏んだ）。
# 専用の AnsiConsole（StringWriter へ書く）に描かせて、行として受け取る。列の横並びは自前で組む
# （Format-SpectreColumns は幅の受け渡しが実測で合わなかったため使わない）。
function Render-CardPanel($card, [int]$colWidth) {
  $color = Get-ProjectColor $card.project $card.color
  # 計画書のカードは番号も出す（計画書と突き合わせる）。修正は「修正」、作業カードは題だけ（K-... の番号は読んでも分からない）
  $label = switch ($card.kind) {
    'plan' { "$(if ($card.critical) { '◆' })$($card.id) $($card.title)" }
    'fix' { "修正 $($card.title)" }
    'pr' { "$($card.id) $($card.title)" }
    default { $card.title }
  }
  $lines = @()
  $lines += (Get-SpectreEscapedText -Text $label)
  $lines += "[$color]〈$(Get-SpectreEscapedText -Text $card.project)〉[/]"
  $tail = @(@($card.progress) + @($card.why) | Where-Object { $_ })
  if ($tail.Count -gt 0) { $lines += "[grey]$(Get-SpectreEscapedText -Text ($tail -join ' / '))[/]" }
  $body = $lines -join "`n"

  $sw = [System.IO.StringWriter]::new()
  $settings = [Spectre.Console.AnsiConsoleSettings]::new()
  $settings.Out = [Spectre.Console.AnsiConsoleOutput]::new($sw)
  $settings.Ansi = [Spectre.Console.AnsiSupport]::Yes
  $settings.ColorSystem = [Spectre.Console.ColorSystemSupport]::Standard
  $capturedConsole = [Spectre.Console.AnsiConsole]::Create($settings)
  $capturedConsole.Profile.Width = $colWidth

  $panel = Format-SpectrePanel -Data $body -Border 'Rounded' -Width $colWidth -Color $color
  $capturedConsole.Write($panel)
  return ($sw.ToString() -split "`r?`n" | Where-Object { $_ -ne '' })
}

function Get-ColumnCards($cards, [string]$column) {
  return @($cards | Where-Object { $_.column -eq $column } | Sort-Object -Property project, order)
}

function Get-VisibleCards($group) {
  if ($group.Count -le $maxCardsPerColumn) { return @($group, 0) }
  return @($group[0..($maxCardsPerColumn - 1)], ($group.Count - $maxCardsPerColumn))
}

function Invoke-Rebuild {
  try { & python $boardPy build *> $null } catch { }
}

$prevSig = $null
$lastBuild = [DateTime]::MinValue
while ($true) {
  if (([DateTime]::Now - $lastBuild) -ge $rebuildEvery) {
    Invoke-Rebuild
    $lastBuild = [DateTime]::Now
  }

  # 結果のファイルの更新時刻とペインの幅だけで署名を作り、変わった時だけ描く（毎回描くとちらつく）。
  $stateTicks = if (Test-Path $statePath) { (Get-Item $statePath).LastWriteTimeUtc.Ticks } else { 0 }
  $width = 100
  try {
    $paneId = $env:HERDR_PANE_ID
    if ($paneId) {
      $layout = & herdr pane layout --pane $paneId 2>$null | ConvertFrom-Json
      $lay = $layout.result.layout
      # ズーム中は、分割前の狭い rect.width でなく表示領域全体の幅を使う（2026-09-11）。
      if ($lay.zoomed -and $lay.focused_pane_id -eq $paneId -and $lay.area.width -gt 0) {
        $width = $lay.area.width
      } else {
        $mine = $lay.panes | Where-Object { $_.pane_id -eq $paneId } | Select-Object -First 1
        if ($mine -and $mine.rect.width -gt 0) { $width = $mine.rect.width }
      }
    }
  } catch { }
  $sig = "$stateTicks|w=$width"

  if ($sig -ne $prevSig) {
    $prevSig = $sig
    $state = $null
    try { $state = Get-Content -LiteralPath $statePath -Raw -Encoding utf8 | ConvertFrom-Json } catch { }
    $cards = if ($state) { @($state.cards) } else { @() }
    $at = if ($state) { ([DateTimeOffset]::Parse($state.generated_at)).ToString('HH:mm') } else { '--:--' }

    # 画面クリアは Clear-Host（ANSI の `e[2J 等は herdr のペインで解釈されず積み上がった、2026-09-10）。
    try { Clear-Host } catch { }
    $headerLine = if ($width -lt 50) { "カンバン $at" } else {
      "── カンバン（$at 時点・90 秒ごと／何かキーで今すぐ・20 秒に 1 回まで） ── Ctrl+Cで終了"
    }
    Write-Host $headerLine
    Write-Host "地図: $mapUrl`n" -ForegroundColor DarkGray

    # herdr の申告幅と実際の折り返しが数文字ずれる（2026-09-10/11）。右端に余白を取る。
    $width = [Math]::Max(16, $width - 5)
    $colCount = $columnOrder.Count
    $minNeededForColumns = 20 * $colCount + ($colCount - 1) * 2

    if ($width -ge $minNeededForColumns) {
      $colWidth = [Math]::Max(20, [Math]::Floor(($width - ($colCount - 1) * 2) / $colCount))
      $leftover = [Math]::Max(0, $width - ($colWidth * $colCount + ($colCount - 1) * 2))
      $columns = @()
      for ($ci = 0; $ci -lt $colCount; $ci++) {
        $column = $columnOrder[$ci]
        $w = if ($ci -eq $colCount - 1) { $colWidth + $leftover } else { $colWidth }
        $group = Get-ColumnCards $cards $column
        $colLines = @("$($columnColor[$column])$(Pad-Str "$column ($($group.Count))" $w)$reset", ('─' * $w))
        $visible, $hiddenCount = Get-VisibleCards $group
        foreach ($c in $visible) {
          $colLines += (Render-CardPanel $c $w)
          $colLines += (' ' * $w)
        }
        if ($hiddenCount -gt 0) { $colLines += "$($columnColor[$column])$(Pad-Str "…他$($hiddenCount)件" $w)$reset" }
        $columns += , $colLines
      }
      $maxLines = ($columns | ForEach-Object { $_.Count } | Measure-Object -Maximum).Maximum
      for ($i = 0; $i -lt $maxLines; $i++) {
        $cells = for ($ci = 0; $ci -lt $columns.Count; $ci++) {
          $w = if ($ci -eq $columns.Count - 1) { $colWidth + $leftover } else { $colWidth }
          if ($i -lt $columns[$ci].Count) { $columns[$ci][$i] } else { ' ' * $w }
        }
        Write-Host ($cells -join '  ')
      }
    }
    else {
      # 狭いペインでは縦に積む。自分の番を先に出す（いちばん見たいもの）。
      $w = [Math]::Max(16, $width)
      foreach ($column in @('自分の番', 'やること', '外部待ち')) {
        $group = Get-ColumnCards $cards $column
        Write-Host "$($columnColor[$column])$(Pad-Str "$column ($($group.Count))" $w)$reset"
        Write-Host ('─' * $w)
        $visible, $hiddenCount = Get-VisibleCards $group
        foreach ($c in $visible) { (Render-CardPanel $c $w) | ForEach-Object { Write-Host $_ } }
        if ($hiddenCount -gt 0) { Write-Host "$($columnColor[$column])$(Pad-Str "…他$($hiddenCount)件" $w)$reset" }
        Write-Host ''
      }
    }

    if ($state -and $state.errors.Count -gt 0) {
      Write-Host "`n(読めなかったもの: $($state.errors.Count) 件。python $boardPy build で中身が見られる)" -ForegroundColor Magenta
    }
  }

  # 3 秒おきに変化を確かめる。何かキーを押すと、その場で組み立て直す。
  $waited = [TimeSpan]::Zero
  $interval = [TimeSpan]::FromMilliseconds(100)
  while ($waited -lt [TimeSpan]::FromSeconds(3)) {
    $keyHit = $false
    try { if ([Console]::KeyAvailable) { $keyHit = $true } } catch { }
    if ($keyHit) {
      try { while ([Console]::KeyAvailable) { [Console]::ReadKey($true) | Out-Null } } catch { }
      if (([DateTime]::Now - $lastBuild) -ge $rebuildAtLeast) {
        $lastBuild = [DateTime]::MinValue
        break
      }
    }
    Start-Sleep -Milliseconds 100
    $waited += $interval
  }
}
