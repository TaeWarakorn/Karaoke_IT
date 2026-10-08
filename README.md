# KaraokeIT

แอปคาราโอเกะบน Windows: วางลิงก์ YouTube (หรือเลือกไฟล์วิดีโอในเครื่อง) แล้วแอปใช้ AI แยกเสียงร้องออกจากดนตรี
ได้วิดีโอเดิมพร้อมดนตรีล้วนและซับเนื้อเพลงที่วิ่งตามเสียงร้อง

## ความสามารถ

- แยกเสียงเป็น 4 ส่วน (ร้อง กลอง เบส ดนตรีอื่น) ด้วย [Demucs](https://github.com/facebookresearch/demucs) เปิด-ปิดและปรับระดับแต่ละส่วนได้สด ๆ
- ซับเนื้อเพลงจาก YouTube หรือ [lrclib.net](https://lrclib.net) และให้ AI ([Whisper](https://github.com/openai/whisper)) จับจังหวะซับให้วิ่งตามเสียงร้อง
- ใส่เนื้อเพลงเองแล้วให้ AI จับจังหวะ หรือเคาะจังหวะเองได้
- คลังเพลง: ค้นหา ลากเรียงลำดับ ลบเพลง ใช้งานแบบออฟไลน์ได้
- ดาวน์โหลดเป็นวิดีโอตามเสียงที่มิกซ์ไว้
- ใช้การ์ดจอ NVIDIA ได้ (เร็ว) หรือ CPU
- หน้าตา 4 แบบ: ดั้งเดิม นีออน สว่าง โรงหนัง

## ติดตั้ง (ผู้ใช้ทั่วไป)

ดาวน์โหลด `KaraokeIT-Setup.exe` จากหน้า [Releases](../../releases) แล้วดับเบิลคลิก
ตัวติดตั้งจะลง Python, AI และ ffmpeg ให้เองทั้งหมด (ต้องต่ออินเทอร์เน็ต ใช้พื้นที่ราว 11 GB)

## รันจากโค้ด (นักพัฒนา)

```
pip install flask "yt-dlp[default]" demucs soundfile openai-whisper
python karaoke_app.py
```

ต้องมี `ffmpeg.exe` และ `ffprobe.exe` อยู่ในโฟลเดอร์เดียวกับ `karaoke_app.py` หรืออยู่ใน PATH
ถ้ามีการ์ดจอ NVIDIA ให้ลง PyTorch รุ่นการ์ดจอ:
`pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126`

## สร้างตัวติดตั้ง

ใช้ [NSIS](https://nsis.sourceforge.io) 3.x:

```
makensis installer/KaraokeIT.nsi
```

ได้ไฟล์ `dist/KaraokeIT-Setup.exe`

## ไฟล์ในโปรเจกต์

| ไฟล์ | หน้าที่ |
|---|---|
| `karaoke_app.py` | ตัวแอปทั้งหมด (เซิร์ฟเวอร์ Flask + หน้าจอ) |
| `Karaoke.bat` | ตัวเปิดแอป |
| `setup.ps1` | ดาวน์โหลดและติดตั้งส่วนประกอบ (ตัวติดตั้งเรียกใช้) |
| `stop.ps1` | ปิดแอปที่ค้างอยู่ก่อนติดตั้งทับ |
| `shortcut.ps1` | สร้างไอคอนบน Desktop |
| `karaoke.ico` | ไอคอนแอป |
| `readme.txt` | คู่มือสั้น ๆ ที่ติดไปกับตัวติดตั้ง |
| `installer/KaraokeIT.nsi` | สคริปต์สร้างตัวติดตั้ง |

## หมายเหตุ

ใช้สำหรับร้องเล่นส่วนตัวเท่านั้น เพลงส่วนใหญ่มีลิขสิทธิ์ และการดาวน์โหลดจาก YouTube อาจขัดกับข้อตกลงการใช้งานของ YouTube
