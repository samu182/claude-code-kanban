# 前の入口の名前（2026-10-07 に open-andon.ps1 へ改名）。herdr が設定を読み直すまで古い名前を呼ぶので、つなぐだけ置く。
# herdr を入り直したあとは消してよい。
& (Join-Path $PSScriptRoot 'open-andon.ps1')
