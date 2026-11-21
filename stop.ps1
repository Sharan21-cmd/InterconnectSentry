
foreach ($port in @(5173, 8000, 8765)) {
    $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
        Stop-Process -Id $connection.OwningProcess -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped process on port $port"
    }
}
