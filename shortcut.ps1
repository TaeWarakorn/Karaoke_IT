# สร้างไอคอน KaraokeIT บนหน้าจอ Desktop (ทำครั้งเดียว) และลบไอคอนชื่อเดิม "คาราโอเกะ"
try {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $old = Join-Path $desktop 'คาราโอเกะ.lnk'
    if (Test-Path $old) { Remove-Item $old -Force -ErrorAction SilentlyContinue }
    $link = Join-Path $desktop 'KaraokeIT.lnk'
    if (!(Test-Path $link)) {
        $s = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
        $s.TargetPath = Join-Path $PSScriptRoot 'Karaoke.bat'
        $s.WorkingDirectory = $PSScriptRoot
        $s.IconLocation = Join-Path $PSScriptRoot 'karaoke.ico'
        $s.WindowStyle = 7
        $s.Description = 'KaraokeIT'
        $s.Save()
    }
} catch {}
