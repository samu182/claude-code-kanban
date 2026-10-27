# herdr のキーからあんどん（構成図の窓）を開く。すでに開いていれば、窓がすぐ終わる（開いている窓にタブを足させて前に出す）。
# 窓は Rust 版（~/.herdr/bin/andon.exe。別リポジトリ andon の install.ps1 が置く）。
# それが無ければ Python 版（arch_view.py）を開く（Rust 版で困ったら、exe を消せば戻る）。
# Start-Process で立てると、herdr がキーのコマンドの終わりに子のプロセスごと閉じるらしく、窓が開かなかった（2026-10-06）。
# Windows の管理の口（Win32_Process.Create）から立てて、herdr の持ち物にならないようにする。
$exe = Join-Path $env:USERPROFILE '.herdr\bin\andon.exe'
$view = Join-Path $PSScriptRoot 'arch_view.py'
$log = Join-Path $env:USERPROFILE '.herdr\open-andon.log'
try {
    if (Test-Path $exe) {
        $cmd = "`"$exe`""
    } else {
        # WindowsApps の pythonw は起動用の別名で、管理の口からは立てられない（結果 8）。本物の場所を使う
        $pyw = Get-Command pythonw -All -ErrorAction Stop | ForEach-Object Source | Where-Object { $_ -notlike '*\WindowsApps\*' } | Select-Object -First 1
        if (-not $pyw) { throw "pythonw が見つからない（WindowsApps の別名しか無い）" }
        $cmd = "`"$pyw`" `"$view`""
    }
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
    "$(Get-Date -Format s) 呼ばれた: 結果 $($r.ReturnValue) / pid $($r.ProcessId) / $cmd" | Out-File $log -Append -Encoding utf8
} catch {
    "$(Get-Date -Format s) 失敗: $_" | Out-File $log -Append -Encoding utf8
}
