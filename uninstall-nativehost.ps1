# Native Messaging ホストの登録を解除し、配置したファイルも消す。
$hostName = 'com.ytdlext.host'

foreach ($browser in 'Google\Chrome', 'Microsoft\Edge') {
    $key = "HKCU:\Software\$browser\NativeMessagingHosts\$hostName"
    if (Test-Path $key) { Remove-Item $key -Force; Write-Host "削除: $key" }
}

$installDir = Join-Path $env:LOCALAPPDATA 'yt-dl-ext'
if (Test-Path $installDir) {
    Remove-Item $installDir -Recurse -Force
    Write-Host "削除: $installDir"
}

Write-Host '解除した。拡張は chrome://extensions から削除してください。'
