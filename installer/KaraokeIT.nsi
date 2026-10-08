Unicode true
!include "MUI2.nsh"

Name "KaraokeIT"
OutFile "..\dist\KaraokeIT-Setup.exe"
InstallDir "C:\KaraokeIT"
; ถ้าเคยติดตั้งไว้แล้ว (รวมถึงตอนที่ยังใช้ชื่อเดิม) ให้ใช้โฟลเดอร์เดิมเป็นค่าเริ่มต้น
InstallDirRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaraokeApp" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "KaraokeIT"
ShowInstDetails show

!define MUI_ICON "..\karaoke.ico"
!define MUI_UNICON "..\karaoke.ico"
!define MUI_ABORTWARNING

!define MUI_WELCOMEPAGE_TITLE "ติดตั้งKaraokeIT"
!define MUI_WELCOMEPAGE_TEXT "วางลิงก์ YouTube แล้วแอปจะตัดเสียงร้องออก เหลือดนตรีพร้อมวิดีโอและซับเนื้อเพลง$\r$\n$\r$\nการติดตั้งต้องต่ออินเทอร์เน็ต ใช้เวลาราว 15-30 นาที และพื้นที่ว่างราว 11 GB$\r$\n$\r$\nติดตั้งครั้งเดียวจบ เสร็จแล้วแอปจะเปิดขึ้นมาให้ใช้ได้ทันที$\r$\n$\r$\nกด ถัดไป เพื่อเลือกที่ติดตั้ง"
!insertmacro MUI_PAGE_WELCOME

!define MUI_DIRECTORYPAGE_TEXT_TOP "เลือกไดรฟ์และโฟลเดอร์ที่จะติดตั้ง แล้วกด ติดตั้ง$\r$\n$\r$\nควรใช้ชื่อโฟลเดอร์ภาษาอังกฤษ เช่น D:\KaraokeIT เพลงที่ทำแล้วจะเก็บไว้ในโฟลเดอร์นี้ด้วย"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_TITLE "ติดตั้งเสร็จแล้ว"
!define MUI_FINISHPAGE_TEXT "KaraokeITเปิดขึ้นมาให้แล้ว วางลิงก์ YouTube ใช้งานได้เลย$\r$\n$\r$\nครั้งต่อไปดับเบิลคลิกไอคอน KaraokeIT บนหน้าจอ Desktop"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Thai"

; รันขั้นติดตั้งแบบซ่อนหน้าต่าง แล้วแสดงผลลัพธ์ในหน้าต่างตัวติดตั้งเอง
!macro RunStep N TEXT
  DetailPrint ""
  DetailPrint "${TEXT}"
  nsExec::ExecToLog 'powershell.exe -NoProfile -NonInteractive -InputFormat None -ExecutionPolicy Bypass -File "$INSTDIR\setup.ps1" -Step ${N} -Plain'
  Pop $0
  StrCmp $0 "0" stepok${N}
    Goto failed
  stepok${N}:
!macroend

!define UNREG "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaraokeApp"

Function .onVerifyInstDir
  ; ห้ามติดตั้งลงรากไดรฟ์ตรง ๆ (เช่น D:\) ต้องมีโฟลเดอร์ของแอปเอง
  StrLen $0 "$INSTDIR"
  IntCmp $0 4 ok 0 ok
    Abort
  ok:
FunctionEnd

Section "Karaoke"
  SectionIn RO
  AddSize 11000000
  ; ปิดแอปที่เปิดค้างอยู่ก่อน ไม่งั้นไฟล์ของแอปอาจถูกล็อกและเขียนทับไม่ได้
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File "..\stop.ps1"
  DetailPrint "กำลังปิดKaraokeITที่เปิดค้างอยู่ (ถ้ามี)"
  nsExec::ExecToLog 'powershell.exe -NoProfile -NonInteractive -InputFormat None -ExecutionPolicy Bypass -File "$PLUGINSDIR\stop.ps1" -Dir "$INSTDIR"'
  Pop $0

  SetOutPath "$INSTDIR"
  File "..\karaoke_app.py"
  File "..\Karaoke.bat"
  File "..\setup.ps1"
  File "..\shortcut.ps1"
  File "..\karaoke.ico"
  File "..\readme.txt"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; ลบไอคอนชื่อเดิม (ตอนโปรแกรมยังชื่อ คาราโอเกะ) จะได้ไม่มีไอคอนซ้ำสองอัน
  Delete "$DESKTOP\คาราโอเกะ.lnk"
  RMDir /r "$SMPROGRAMS\คาราโอเกะ"
  CreateDirectory "$SMPROGRAMS\KaraokeIT"
  CreateShortcut "$SMPROGRAMS\KaraokeIT\ถอนการติดตั้ง.lnk" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "${UNREG}" "DisplayName" "KaraokeIT"
  WriteRegStr HKCU "${UNREG}" "DisplayIcon" "$INSTDIR\karaoke.ico"
  WriteRegStr HKCU "${UNREG}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNREG}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "${UNREG}" "NoModify" 1
  WriteRegDWORD HKCU "${UNREG}" "NoRepair" 1

  DetailPrint "กำลังดาวน์โหลดและติดตั้งส่วนประกอบ ใช้เวลาราว 15-30 นาที"
  DetailPrint "ความคืบหน้าจะขึ้นในช่องนี้ ระหว่างนี้อย่าปิดหน้าต่าง"
  !insertmacro RunStep 1 "ขั้นที่ 1/5: เตรียมตัวติดตั้ง"
  !insertmacro RunStep 2 "ขั้นที่ 2/5: ติดตั้ง Python สำหรับแอปนี้"
  !insertmacro RunStep 3 "ขั้นที่ 3/5: ติดตั้ง AI แยกเสียงและส่วนประกอบ (ขั้นนี้นานที่สุด)"
  !insertmacro RunStep 4 "ขั้นที่ 4/5: ดาวน์โหลด ffmpeg"
  !insertmacro RunStep 5 "ขั้นที่ 5/5: ดาวน์โหลดโมเดล AI ให้พร้อมใช้ทันที"
  DetailPrint ""
  DetailPrint "ติดตั้งเสร็จแล้ว กำลังเปิดKaraokeIT..."
  ; ไอคอนเปิดแอปตรง ๆ แบบไม่มีหน้าต่างดำ (pythonw) ถ้าไม่มีจึงใช้ Karaoke.bat
  SetOutPath "$INSTDIR"
  IfFileExists "$INSTDIR\.venv\Scripts\pythonw.exe" 0 usebat
    CreateShortcut "$DESKTOP\KaraokeIT.lnk" "$INSTDIR\.venv\Scripts\pythonw.exe" '"$INSTDIR\karaoke_app.py"' "$INSTDIR\karaoke.ico" 0
    CreateShortcut "$SMPROGRAMS\KaraokeIT\KaraokeIT.lnk" "$INSTDIR\.venv\Scripts\pythonw.exe" '"$INSTDIR\karaoke_app.py"' "$INSTDIR\karaoke.ico" 0
    Goto launch
  usebat:
    CreateShortcut "$DESKTOP\KaraokeIT.lnk" "$INSTDIR\Karaoke.bat" "" "$INSTDIR\karaoke.ico" 0 SW_SHOWMINIMIZED
    CreateShortcut "$SMPROGRAMS\KaraokeIT\KaraokeIT.lnk" "$INSTDIR\Karaoke.bat" "" "$INSTDIR\karaoke.ico" 0 SW_SHOWMINIMIZED
  launch:
  ExecShell "" "$DESKTOP\KaraokeIT.lnk"
  Goto done
  failed:
    DetailPrint "ติดตั้งส่วนประกอบไม่ครบ"
    CreateShortcut "$DESKTOP\KaraokeIT.lnk" "$INSTDIR\Karaoke.bat" "" "$INSTDIR\karaoke.ico" 0
    MessageBox MB_ICONEXCLAMATION "ติดตั้งส่วนประกอบไม่สำเร็จ$\r$\n$\r$\nดูบรรทัดที่ขึ้นต้นด้วย ERROR ในช่องรายละเอียด$\r$\nลองปิดKaraokeITที่เปิดอยู่ เช็กอินเทอร์เน็ต แล้วเปิดตัวติดตั้งนี้อีกครั้ง ตัวติดตั้งจะทำต่อจากเดิม"
  done:
SectionEnd

Section "Uninstall"
  MessageBox MB_YESNO|MB_ICONQUESTION "ลบเพลงที่ทำไว้ในคลังด้วยไหม" IDNO keep
    RMDir /r "$INSTDIR\library"
  keep:
  RMDir /r "$INSTDIR\.venv"
  RMDir /r "$INSTDIR\tools"
  RMDir /r "$INSTDIR\cache"
  RMDir /r "$INSTDIR\__pycache__"
  Delete "$INSTDIR\karaoke_app.py"
  Delete "$INSTDIR\Karaoke.bat"
  Delete "$INSTDIR\setup.ps1"
  Delete "$INSTDIR\shortcut.ps1"
  Delete "$INSTDIR\karaoke.ico"
  Delete "$INSTDIR\readme.txt"
  Delete "$INSTDIR\ffmpeg.exe"
  Delete "$INSTDIR\ffprobe.exe"
  Delete "$INSTDIR\deno.exe"
  Delete "$INSTDIR\deno.zip"
  Delete "$INSTDIR\karaoke.log"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  Delete "$DESKTOP\KaraokeIT.lnk"
  RMDir /r "$SMPROGRAMS\KaraokeIT"
  Delete "$DESKTOP\คาราโอเกะ.lnk"
  RMDir /r "$SMPROGRAMS\คาราโอเกะ"
  DeleteRegKey HKCU "${UNREG}"
SectionEnd
