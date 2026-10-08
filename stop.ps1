# ปิดแอปคาราโอเกะที่ยังรันอยู่จากโฟลเดอร์ติดตั้ง ก่อนที่ตัวติดตั้งจะเขียนไฟล์ทับ
param([string]$Dir)
try {
    $here = $Dir.TrimEnd('\').ToLower()
    Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        ($_.Name -like "python*" -and $_.ExecutablePath -and $_.ExecutablePath.ToLower().StartsWith($here + "\")) -or
        ($_.Name -like "python*" -and $_.CommandLine -and $_.CommandLine.ToLower().Contains($here + "\karaoke_app.py")) -or
        ($_.CommandLine -and $_.CommandLine.ToLower().Contains($here + "\cache\window"))
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
} catch {}
exit 0
