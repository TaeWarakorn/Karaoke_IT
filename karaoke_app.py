"""
KaraokeIT: วางลิงก์ YouTube -> แยกเสียงเป็น 4 ส่วน (ร้อง / กลอง / เบส / ดนตรีอื่น)
เปิด-ปิด และปรับระดับเสียงแต่ละส่วนได้สด ๆ พร้อมซับไตเติลเนื้อเพลงบนวิดีโอ

ติดตั้ง (ครั้งเดียว):
    pip install flask yt-dlp demucs soundfile
    และวาง ffmpeg.exe กับ ffprobe.exe ไว้ในโฟลเดอร์เดียวกับไฟล์นี้ (หรือให้อยู่ใน PATH)

รัน:
    python karaoke_app.py
หน้าต่างแอปจะเปิดขึ้นเอง
"""
import difflib
import hashlib
import html
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

BASE = Path(__file__).resolve().parent
LIB = BASE / "library"
LIB.mkdir(exist_ok=True)
STEMS = ["vocals", "drums", "bass", "other"]

# แอปรันแบบไม่มีหน้าต่างดำ (pythonw) จึงไม่มีจอให้พิมพ์ข้อความ ให้เขียนลงไฟล์ karaoke.log แทน
if sys.stdout is None or sys.stderr is None:
    sys.stdout = sys.stderr = open(BASE / "karaoke.log", "w", encoding="utf-8", buffering=1)

# โปรแกรมลูก (yt-dlp, demucs, ffmpeg) ใช้ python.exe ปกติ และห้ามเด้งหน้าต่างดำขึ้นมา
PY = sys.executable
if PY.lower().endswith("pythonw.exe") and Path(PY[:-5] + ".exe").exists():
    PY = PY[:-5] + ".exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ชุดที่ลงผ่านตัวติดตั้ง: เก็บโมเดล AI ไว้ในโฟลเดอร์ของแอปเอง
if (BASE / ".venv" / "installed.ok").exists():
    os.environ.setdefault("XDG_CACHE_HOME", str(BASE / "cache"))

# หา ffmpeg แบบระบุที่อยู่เต็ม และเพิ่มโฟลเดอร์ของแอปเข้า PATH ให้ yt-dlp / demucs หาเจอด้วย
os.environ["PATH"] = str(BASE) + os.pathsep + os.environ.get("PATH", "")
FFMPEG = shutil.which("ffmpeg") or ""

app = Flask(__name__)
jobs = {}
device = {"name": "unknown"}
SETTINGS_FILE = BASE / "settings.json"
try:
    settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
except Exception:
    settings = {}


def device_choice():
    """ตัวเลือกของผู้ใช้: auto (แอปเลือกเอง) / gpu / cpu"""
    choice = settings.get("device", "auto")
    return choice if choice in ("auto", "gpu", "cpu") else "auto"


def use_gpu():
    return device["name"] == "gpu" and device_choice() != "cpu"


def force_cpu():
    """บังคับ CPU เมื่อผู้ใช้เลือก CPU หรือเครื่องไม่มีการ์ดจอ (ถ้ายังตรวจเครื่องไม่เสร็จ ปล่อยให้ AI เลือกเอง)"""
    return device_choice() == "cpu" or device["name"] == "cpu"

SUB_SEARCH = 3  # รุ่นของวิธีค้นซับ เพลงในคลังที่ค้นด้วยรุ่นเก่าจะถูกค้นใหม่ให้


def explain(message):
    """เติมคำแนะนำภาษาไทยให้ข้อความ error ที่รู้สาเหตุ"""
    if "WinError 1114" in message or "c10.dll" in message:
        return (message + "\n\nสาเหตุ: เครื่องนี้ขาดส่วนเสริม Microsoft Visual C++ Redistributable หรือมีรุ่นเก่าเกินไป ตัว AI จึงเปิดไม่ขึ้น"
                "\nวิธีแก้: ปิดแอป แล้วรันตัวติดตั้ง KaraokeSetup.exe รุ่นล่าสุดทับลงโฟลเดอร์เดิม (จะติดตั้งส่วนที่ขาดให้เอง)"
                "\nหรือดาวน์โหลดติดตั้งเองจาก https://aka.ms/vs/17/release/vc_redist.x64.exe แล้วเปิดแอปใหม่")
    return message


def run(cmd, step):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=BASE,
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except FileNotFoundError:
        raise RuntimeError(f"{step}ไม่สำเร็จ: ไม่พบโปรแกรม {cmd[0]}")
    if r.returncode != 0:
        raise RuntimeError(f"{step}ไม่สำเร็จ: {r.stderr[-600:]}")
    return r.stdout.strip()


def detect_device():
    """เช็กว่า AI จะรันบนการ์ดจอได้ไหม และถ้าไม่ได้เป็นเพราะอะไร (รันเบื้องหลังตอนเปิดแอป)
    reason: ok / no_nvidia (ไม่มีการ์ดจอ NVIDIA) / cpu_build (ลง PyTorch แบบ CPU ไว้) / driver / error"""
    try:
        subprocess.run(["nvidia-smi"], capture_output=True, stdin=subprocess.DEVNULL,
                       creationflags=NO_WINDOW, timeout=20, check=True)
        has_card = True
    except Exception:
        has_card = False
    try:
        out = run([PY, "-c", "import torch,json;print(json.dumps([torch.cuda.is_available(), torch.version.cuda]))"],
                  "ตรวจสอบเครื่อง")
        works, build = json.loads(out.splitlines()[-1])
        if works:
            device.update(name="gpu", reason="ok")
        else:
            reason = "no_nvidia" if not has_card else ("cpu_build" if not build else "driver")
            device.update(name="cpu", reason=reason)
    except Exception:
        device.update(name="unknown", reason="error")


deno_lock = threading.Lock()


def ensure_deno():
    """yt-dlp ต้องมีตัวรัน JavaScript (deno) ไม่งั้น YouTube จะปฏิเสธการดาวน์โหลดบางคลิป (403)
    ถ้าเครื่องยังไม่มี ให้ดาวน์โหลด deno.exe มาไว้ในโฟลเดอร์ของแอปครั้งเดียว"""
    with deno_lock:
        if shutil.which("deno") or sys.platform != "win32":
            return
        try:
            zip_path = BASE / "deno.zip"
            urllib.request.urlretrieve(
                "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip", zip_path)
            with zipfile.ZipFile(zip_path) as z:
                z.extract("deno.exe", BASE)
            zip_path.unlink()
        except Exception:
            pass  # ไม่มีก็ยังลองดาวน์โหลดได้ แค่บางคลิปอาจไม่ผ่าน


# ---------- ซับไตเติล ----------
def pick_subs(info):
    """เลือกซับจาก YouTube: ซับที่คนทำก่อน ถ้าไม่มีใช้ซับอัตโนมัติภาษาต้นฉบับ"""
    lang = info.get("language")
    manual = [k for k in (info.get("subtitles") or {}) if k != "live_chat"]
    for want in (lang, "th", "en"):
        if not want:
            continue
        for k in manual:
            if k == want or k.startswith(want + "-"):
                return k, "manual"
    if manual:
        return manual[0], "manual"
    auto = list(info.get("automatic_captions") or {})
    for k in auto:
        if k.endswith("-orig"):
            return k, "auto"
    if lang and lang in auto:
        return lang, "auto"
    return None, None


def download_subs(url, info, folder):
    """ดาวน์โหลดซับเป็น subs.vtt คืนค่าชนิดซับ ('manual' / 'auto') หรือ None ถ้าไม่มี"""
    lang, kind = pick_subs(info)
    if not lang:
        return None
    try:
        run(
            [PY, "-m", "yt_dlp", "--no-playlist", "--skip-download",
             "--write-subs" if kind == "manual" else "--write-auto-subs",
             "--sub-langs", lang, "--sub-format", "vtt/best", "--convert-subs", "vtt",
             "-o", str(folder / "subs.%(ext)s"), url],
            "ดาวน์โหลดซับ",
        )
        found = [p for p in folder.glob("subs*.vtt")]
        if not found:
            return None
        if found[0].name != "subs.vtt":
            found[0].replace(folder / "subs.vtt")
        return kind
    except Exception:
        return None  # ไม่มีซับก็ยังร้องได้ ไม่ต้องให้งานทั้งหมดล้ม


def lrclib_subs(info, folder):
    """ค้นเนื้อเพลงแบบมีจังหวะจาก lrclib.net เลือกอันที่ความยาวเพลงใกล้กับคลิปที่สุด"""
    dur = info.get("duration") or 0
    queries = []
    if info.get("track"):
        q = {"track_name": info["track"]}
        artist = (info.get("artist") or "").split(",")[0].strip()
        if artist:
            q["artist_name"] = artist
        queries.append(q)
    title = re.sub(r"[\(\[【（「『].*?[\)\]】）」』]", " ", info.get("title") or "")
    title = re.sub(r"(?i)\b(official|music video|lyrics? video|mv|m/v|lyrics?|audio|video|hd|4k)\b", " ", title)
    title = re.sub(r"[|｜\-–—:_/]+", " ", title)
    title = " ".join(title.split())
    if title:
        queries.append({"q": title})
    for q in queries:
        try:
            req = urllib.request.Request("https://lrclib.net/api/search?" + urllib.parse.urlencode(q),
                                         headers={"User-Agent": "karaoke-app (personal use)"})
            results = json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8"))
        except Exception:
            continue
        best = None
        for r in results:
            if not r.get("syncedLyrics"):
                continue
            diff = abs((r.get("duration") or 0) - dur) if dur else 0
            if diff <= 15 and (best is None or diff < best[0]):
                best = (diff, r["syncedLyrics"])
        if best:
            (folder / "subs.lrc").write_text(best[1], encoding="utf-8")
            return "lrclib"
    return None


def find_subs(url, info, folder):
    """หาซับเรียงตามความถูกต้อง: ซับที่คนทำใน YouTube > เนื้อเพลงจาก lrclib.net > ซับอัตโนมัติของ YouTube"""
    _, kind = pick_subs(info)
    if kind == "manual":
        got = download_subs(url, info, folder)
        if got:
            return got
    got = lrclib_subs(info, folder)
    if got:
        return got
    if kind == "auto":
        return download_subs(url, info, folder)
    return None


def refresh_subs(folder, meta, url, info):
    """ค้นซับใหม่ให้เพลงในคลังที่ยังไม่มีซับ หรือมีแค่ซับอัตโนมัติ"""
    if not meta.get("subs"):
        meta["subs"] = find_subs(url, info, folder)
    elif meta["subs"] == "auto" and lrclib_subs(info, folder):
        meta["subs"] = "lrclib"
    meta.setdefault("lang", info.get("language") or "")
    meta["sub_search"] = SUB_SEARCH
    write_meta(folder, meta)


# ---------- ซับวิ่งตามเสียงร้อง: ให้ AI (Whisper) ฟังเสียงร้องแล้วจับเวลาทีละตัวอักษร ----------
def has_whisper():
    return importlib.util.find_spec("whisper") is not None


def whisper_worker(audio, out, lang, force=""):
    """รันในโปรเซสแยก: ถอดเสียงร้องพร้อมเวลาของแต่ละคำ แล้วเขียนผลเป็น JSON"""
    import torch
    import whisper
    from whisper.tokenizer import LANGUAGES

    lang = (lang or "").split("-")[0].lower()
    gpu = torch.cuda.is_available() and force != "cpu"
    names = ["large-v3", "medium"] if gpu else ["small"]
    for name in names:
        try:
            model = whisper.load_model(name, device="cuda" if gpu else "cpu")
            result = model.transcribe(audio, language=lang if lang in LANGUAGES else None,
                                      word_timestamps=True, condition_on_previous_text=False)
            break
        except RuntimeError as e:  # หน่วยความจำการ์ดจอไม่พอ ให้ลองโมเดลที่เล็กลง
            if "out of memory" not in str(e).lower() or name == names[-1]:
                raise
            model = None
            torch.cuda.empty_cache()
    segs = [{"start": g["start"], "end": g["end"],
             "words": [{"w": w["word"], "s": w["start"], "e": w["end"]} for w in g.get("words", [])]}
            for g in result["segments"]]
    Path(out).write_text(json.dumps({"model": name, "language": result.get("language"), "segments": segs},
                                    ensure_ascii=False), encoding="utf-8")


def parse_lrc(text):
    out = []
    for line in text.splitlines():
        tags = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", line)
        t = re.sub(r"\[[^\]]*\]|<[^>]*>", "", line).strip()
        if t:
            out += [{"start": int(m) * 60 + float(sec), "text": t} for m, sec in tags]
    return sorted(out, key=lambda c: c["start"])


def parse_vtt(text):
    def sec(x):
        m = re.search(r"(?:(\d+):)?(\d+):(\d+)[.,](\d+)", x)
        return int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3]) + float("0." + m[4]) if m else None

    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = block.split("\n")
        i = next((k for k, l in enumerate(lines) if "-->" in l), None)
        if i is None:
            continue
        a, b = lines[i].split("-->")[:2]
        body = " ".join(html.unescape(re.sub(r"<[^>]+>", "", l)).strip() for l in lines[i + 1:]).strip()
        if body and sec(a) is not None:
            out.append({"start": sec(a), "text": body})
    return out


def load_lines(folder, meta):
    """เนื้อเพลงทีละบรรทัด (พร้อมเวลาเริ่มของบรรทัด) จากซับที่มีอยู่ ถ้าเป็นซับที่เชื่อถือได้"""
    kind = meta.get("subs")
    if kind in ("lrclib", "custom") and (folder / "subs.lrc").exists():
        return parse_lrc((folder / "subs.lrc").read_text(encoding="utf-8"))
    if kind == "manual" and (folder / "subs.vtt").exists():
        return parse_vtt((folder / "subs.vtt").read_text(encoding="utf-8"))
    return None


def keep_char(c):
    return unicodedata.category(c)[0] in "LMN"


def fill_times(ct):
    """เติมเวลาให้ตัวอักษรที่จับคู่ไม่ได้ โดยเฉลี่ยจากตัวข้างเคียง แล้วบังคับให้เวลาไม่ย้อนกลับ"""
    known = [i for i, t in enumerate(ct) if t is not None]
    out = list(ct)
    for i in range(len(ct)):
        if out[i] is None:
            left = max((k for k in known if k < i), default=None)
            right = min((k for k in known if k > i), default=None)
            if left is None:
                out[i] = ct[right]
            elif right is None:
                out[i] = ct[left]
            else:
                out[i] = ct[left] + (ct[right] - ct[left]) * (i - left) / (right - left)
    for i in range(1, len(out)):
        out[i] = max(out[i], out[i - 1])
    return out


def align(lines, wh):
    """จับคู่เนื้อเพลงที่ถูกต้องกับสิ่งที่ AI ได้ยิน เพื่อเอาเวลาของแต่ละตัวอักษร คืน None ถ้าจับคู่ไม่ได้"""
    W = []
    for g in wh["segments"]:
        for w in g["words"]:
            chars = [c for c in w["w"] if keep_char(c)]
            for k, c in enumerate(chars):
                W.append((c.lower(), w["s"] + (w["e"] - w["s"]) * k / len(chars)))
    L = [(c.lower(), li, ci) for li, ln in enumerate(lines) for ci, c in enumerate(ln["text"]) if keep_char(c)]
    n = len(lines)
    if not W or not L:
        return None
    ct = [[None] * len(ln["text"]) for ln in lines]
    hit, total = [0] * n, [0] * n
    for _, li, _ in L:
        total[li] += 1
    sm = difflib.SequenceMatcher(None, [x[0] for x in L], [x[0] for x in W], autojunk=False)
    for a, b, size in sm.get_matching_blocks():
        if size < 2:
            continue
        for k in range(size):
            _, li, ci = L[a + k]
            ct[li][ci] = W[b + k][1]
            hit[li] += 1
    ok = [total[i] > 0 and hit[i] >= max(2, 0.3 * total[i]) for i in range(n)]
    starts = [next(t for t in ct[i] if t is not None) if ok[i] else None for i in range(n)]
    timed = all("start" in ln for ln in lines)
    off = 0
    if timed and any(ok):
        diffs = sorted(starts[i] - lines[i]["start"] for i in range(n) if ok[i])
        off = diffs[len(diffs) // 2]
        for i in range(n):  # บรรทัดที่เวลาหลุดจากซับเดิมมากผิดปกติ ถือว่าจับคู่ผิด (เช่น ท่อนซ้ำ)
            if ok[i] and abs(starts[i] - lines[i]["start"] - off) > 4:
                ok[i] = False
    if sum(ok) < max(1, 0.3 * n):
        return None
    for i in range(n):
        if ok[i]:
            ct[i] = fill_times(ct[i])
            starts[i] = ct[i][0]
        else:
            ct[i] = None
            if timed:
                starts[i] = max(0, lines[i]["start"] + off)
    if not timed:  # บรรทัดที่จับไม่ได้: แบ่งเวลาเท่า ๆ กันระหว่างบรรทัดข้างเคียงที่จับได้
        i = 0
        while i < n:
            if ok[i]:
                i += 1
                continue
            j = i
            while j < n and not ok[j]:
                j += 1
            before = ct[i - 1][-1] if i > 0 else 0
            after = starts[j] if j < n else before + 4 * (j - i + 1)
            for k in range(i, j):
                starts[k] = before + (after - before) * (k - i + 1) / (j - i + 1)
            i = j
    cues = []
    for i in range(n):
        if i and starts[i] <= starts[i - 1]:
            starts[i] = starts[i - 1] + 0.05
        if ct[i]:
            ct[i] = [round(max(t, starts[i]), 2) for t in ct[i]]
    for i in range(n):
        last = ct[i][-1] + 1.5 if ct[i] else starts[i] + 8
        end = min(starts[i + 1], last) if i + 1 < n else last
        cue = {"start": round(starts[i], 2), "end": round(max(end, starts[i] + 0.3), 2), "text": lines[i]["text"]}
        if ct[i]:
            cue["ct"] = ct[i]
        cues.append(cue)
    return cues


def whisper_only(wh):
    """ไม่มีเนื้อเพลงให้เทียบ: ใช้ข้อความที่ AI ถอดได้เป็นซับเลย"""
    cues = []
    for g in wh["segments"]:
        text, ct = "", []
        for w in g["words"]:
            piece = w["w"] if text else w["w"].lstrip()
            ct += [round(w["s"] + (w["e"] - w["s"]) * k / len(piece), 2) for k in range(len(piece))]
            text += piece
        text = text.rstrip()
        if text:
            cues.append({"start": ct[0], "end": round(g["words"][-1]["e"] + 1.0, 2), "text": text, "ct": ct[:len(text)]})
    for i in range(len(cues) - 1):
        cues[i]["end"] = max(min(cues[i]["end"], cues[i + 1]["start"]), cues[i]["start"] + 0.3)
    return cues


def lrc_stamp(t):
    return f"[{int(t // 60):02d}:{t % 60:05.2f}]"


def ai_sync(folder, meta, say, lyrics=None):
    """ทำให้ซับวิ่งตามเสียงร้อง คืน meta ที่อัปเดตแล้ว"""
    if not has_whisper():
        raise RuntimeError("ยังไม่ได้ติดตั้ง Whisper: เปิด Terminal รันคำสั่ง  python -m pip install openai-whisper  แล้วเปิดแอปใหม่")
    heard = folder / "whisper.json"
    if not heard.exists():
        say("AI กำลังฟังเสียงร้อง...")
        # ครั้งแรกสุดต้องดาวน์โหลดโมเดล (ราว 3 GB บนการ์ดจอ) จึงบอกความคืบหน้าเป็น MB ให้เห็นว่าไม่ได้ค้าง
        cache = Path(os.getenv("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "whisper"
        done = threading.Event()

        def report():
            def size():
                try:
                    return sum(f.stat().st_size for f in cache.glob("*") if f.is_file())
                except OSError:
                    return 0
            last = size()
            while not done.wait(3):
                now = size()
                if now != last:
                    say(f"กำลังดาวน์โหลดโมเดลฟังเสียงร้อง (ครั้งแรกครั้งเดียว): {now / 1048576:,.0f} MB จากราว 3,000 MB")
                elif last:
                    say("AI กำลังฟังเสียงร้อง...")
                last = now

        threading.Thread(target=report, daemon=True).start()
        try:
            run([PY, str(Path(__file__).resolve()), "--whisper", str(folder / "vocals.mp3"), str(heard),
                 meta.get("lang") or "-", "cpu" if force_cpu() else "gpu"], "ถอดเสียงร้อง")
        finally:
            done.set()
    wh = json.loads(heard.read_text(encoding="utf-8"))
    say("กำลังจับจังหวะเนื้อเพลง...")
    if lyrics is not None:
        lines = [{"text": l.strip()} for l in lyrics.splitlines() if l.strip()]
        kind = "custom"
    else:
        lines, kind = load_lines(folder, meta), meta.get("subs")
    if lines:
        cues = align(lines, wh)
        if cues is None:
            if lyrics is not None:
                raise RuntimeError("AI จับคู่เนื้อเพลงกับเสียงร้องไม่ได้ ตรวจว่าเนื้อเพลงตรงกับเพลงนี้ หรือใช้วิธีเคาะจังหวะแทน")
            raise RuntimeError("AI จับคู่เนื้อเพลงกับเสียงร้องไม่ได้ ซับยังใช้จังหวะเดิมต่อไป")
    else:
        cues, kind = whisper_only(wh), "ai"
        if not cues:
            raise RuntimeError("AI ไม่ได้ยินเสียงร้องในเพลงนี้")
    (folder / "subs.json").write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
    if lyrics is not None:
        (folder / "subs.lrc").write_text("\n".join(lrc_stamp(c["start"]) + c["text"] for c in cues), encoding="utf-8")
    meta.update(subs=kind, timed=True)
    write_meta(folder, meta)
    return meta


def write_meta(folder, meta):
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


# ---------- งานหลัก ----------
def process(job_id, url, height):
    job = jobs[job_id]

    def step(p, msg):
        job.update(progress=p, message=msg)

    try:
        if not FFMPEG:
            raise RuntimeError(f"ไม่พบ ffmpeg กรุณาวาง ffmpeg.exe ไว้ในโฟลเดอร์ {BASE}")

        if not shutil.which("deno") and sys.platform == "win32":
            step(3, "กำลังเตรียมตัวช่วยดาวน์โหลด (ทำครั้งแรกครั้งเดียว)...")
            ensure_deno()

        step(5, "กำลังตรวจสอบลิงก์...")
        raw = run([PY, "-m", "yt_dlp", "--encoding", "utf-8", "--no-playlist", "--skip-download",
                   "--dump-json", url], "ตรวจสอบลิงก์")
        info = json.loads(raw.splitlines()[-1])
        vid, title = info["id"], info.get("title") or info["id"]
        dest = LIB / vid

        if (dest / "meta.json").exists():
            meta = json.loads((dest / "meta.json").read_text(encoding="utf-8"))
            if meta.get("sub_search") != SUB_SEARCH:
                step(60, "กำลังหาซับไตเติล...")
                refresh_subs(dest, meta, url, info)
        else:
            tmp = LIB / f"_tmp_{job_id}"
            tmp.mkdir()
            video_error = []

            def get_video():
                # YouTube ปฏิเสธเป็นบางครั้ง (403) จึงลองซ้ำ และครั้งสุดท้ายลองไฟล์แบบรวมภาพ+เสียงสำเร็จรูป
                formats = [f"bv*[height<={height}]+ba/b[height<={height}]/b"] * 2 + [f"b[height<={height}]/b"]
                last = None
                for n, fmt in enumerate(formats):
                    if n:
                        time.sleep(4)
                    try:
                        run(
                            [PY, "-m", "yt_dlp", "--no-playlist", "--retries", "5", "--fragment-retries", "5",
                             "-f", fmt, "--merge-output-format", "mp4", "--remux-video", "mp4",
                             "-o", str(tmp / "video.%(ext)s"), url],
                            "ดาวน์โหลดวิดีโอ",
                        )
                        if (tmp / "video.mp4").exists():
                            return
                        last = RuntimeError("ดาวน์โหลดวิดีโอไม่สำเร็จ: ไม่ได้ไฟล์ mp4")
                    except Exception as e:
                        last = e
                video_error.append(last)

            video_thread = threading.Thread(target=get_video, daemon=True)
            try:
                step(12, "กำลังดาวน์โหลดเสียง...")
                run([PY, "-m", "yt_dlp", "--no-playlist", "--retries", "5", "-f", "ba/b",
                     "-o", str(tmp / "src.%(ext)s"), url], "ดาวน์โหลดเสียง")
                src = next(tmp.glob("src.*"))
                # ดาวน์โหลดวิดีโอไปพร้อมกับการแยกเสียง (เริ่มหลังเสียงมาครบ จะได้ไม่ยิง YouTube พร้อมกัน)
                video_thread.start()
                run([FFMPEG, "-y", "-i", str(src), "-vn", "-ac", "2", "-ar", "44100",
                     str(tmp / "audio.wav")], "เตรียมไฟล์เสียง")

                step(22, "กำลังหาซับไตเติล...")
                subs = find_subs(url, info, tmp)

                where = "การ์ดจอ" if use_gpu() else "CPU อาจใช้เวลาหลายนาที"
                step(30, f"AI กำลังแยกเสียง ({where})...")
                demucs = [PY, "-m", "demucs", "-n", "htdemucs", "--overlap", "0.1",
                          "--mp3", "--mp3-bitrate", "192",
                          "-o", str(tmp / "sep"), str(tmp / "audio.wav")]
                try:
                    run(demucs + (["-d", "cpu"] if force_cpu() else []), "แยกเสียง")
                except RuntimeError:
                    if force_cpu():
                        raise
                    # การ์ดจอทำไม่สำเร็จ (เช่น หน่วยความจำการ์ดจอไม่พอ) ให้ลองใหม่ด้วย CPU
                    step(35, "การ์ดจอทำไม่สำเร็จ กำลังลองใหม่ด้วย CPU...")
                    run(demucs + ["-d", "cpu"], "แยกเสียง")
                for s in STEMS:
                    shutil.move(str(tmp / "sep" / "htdemucs" / "audio" / f"{s}.mp3"), str(tmp / f"{s}.mp3"))

                meta = {"id": vid, "title": title, "subs": subs, "sub_search": SUB_SEARCH,
                        "lang": info.get("language") or ""}
                write_meta(tmp, meta)
                if use_gpu() and has_whisper():
                    try:  # ซับวิ่งตามเสียงร้อง ถ้าทำไม่สำเร็จก็ยังใช้ซับแบบเดิมได้
                        meta = ai_sync(tmp, meta, lambda m: step(80, m))
                    except Exception:
                        pass

                step(94, "กำลังรอวิดีโอดาวน์โหลดเสร็จ...")
                video_thread.join()
                if video_error:
                    raise video_error[0]

                src.unlink()
                (tmp / "audio.wav").unlink()
                shutil.rmtree(tmp / "sep")
                if dest.exists():
                    shutil.rmtree(dest)
                tmp.rename(dest)
            finally:
                if video_thread.is_alive():
                    video_thread.join()
                shutil.rmtree(tmp, ignore_errors=True)

        job.update(status="done", progress=100, message="เสร็จแล้ว", song=meta)
    except Exception as e:  # แสดงข้อผิดพลาดบนหน้าเว็บ พร้อมบอกว่าพังที่ขั้นตอนไหน
        job.update(status="error", message=explain(f"{job.get('message', '')}\n{e}"))


def process_file(job_id, src, title):
    """ทำคาราโอเกะจากไฟล์วิดีโอ/เสียงในเครื่อง (ไม่ผ่าน YouTube) ขั้นตอนแยกเสียงและซับเหมือนเพลงจากลิงก์"""
    job = jobs[job_id]
    tmp = src.parent

    def step(p, msg):
        job.update(progress=p, message=msg)

    try:
        if not FFMPEG:
            raise RuntimeError(f"ไม่พบ ffmpeg กรุณาวาง ffmpeg.exe ไว้ในโฟลเดอร์ {BASE}")
        vid = "file-" + hashlib.sha1(f"{title}|{src.stat().st_size}".encode("utf-8")).hexdigest()[:10]
        dest = LIB / vid
        if (dest / "meta.json").exists():  # เคยทำไฟล์นี้แล้ว
            meta = json.loads((dest / "meta.json").read_text(encoding="utf-8"))
        else:
            step(8, "กำลังอ่านเสียงจากไฟล์...")
            try:
                run([FFMPEG, "-y", "-i", str(src), "-vn", "-ac", "2", "-ar", "44100", str(tmp / "audio.wav")],
                    "อ่านเสียง")
            except RuntimeError:
                raise RuntimeError("ไฟล์นี้ไม่มีเสียง หรือเป็นไฟล์ที่อ่านไม่ได้")
            seconds = max(0, ((tmp / "audio.wav").stat().st_size - 44) / (44100 * 4))

            step(14, "กำลังเตรียมวิดีโอ...")
            video = str(tmp / "video.mp4")
            try:  # ปกติแค่ย้ายภาพใส่ไฟล์ใหม่ ไม่ต้องเข้ารหัสซ้ำ
                run([FFMPEG, "-y", "-i", str(src), "-map", "0:v:0", "-c:v", "copy", "-an",
                     "-movflags", "+faststart", video], "เตรียมวิดีโอ")
            except RuntimeError:
                try:  # รูปแบบที่ย้ายตรง ๆ ไม่ได้: แปลงเป็น H.264
                    step(16, "กำลังแปลงวิดีโอให้เล่นในแอปได้ (อาจใช้เวลาสักครู่)...")
                    run([FFMPEG, "-y", "-i", str(src), "-map", "0:v:0", "-c:v", "libx264", "-preset", "veryfast",
                         "-crf", "23", "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", video], "แปลงวิดีโอ")
                except RuntimeError:  # ไฟล์เสียงล้วน: ใช้พื้นหลังสีดำ
                    run([FFMPEG, "-y", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=10", "-t", f"{seconds:.2f}",
                         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", video], "สร้างพื้นหลัง")

            step(22, "กำลังหาเนื้อเพลงจากชื่อเพลง...")
            try:
                subs = lrclib_subs({"title": title, "duration": seconds}, tmp)
            except Exception:
                subs = None

            where = "การ์ดจอ" if use_gpu() else "CPU อาจใช้เวลาหลายนาที"
            step(30, f"AI กำลังแยกเสียง ({where})...")
            demucs = [PY, "-m", "demucs", "-n", "htdemucs", "--overlap", "0.1",
                      "--mp3", "--mp3-bitrate", "192",
                      "-o", str(tmp / "sep"), str(tmp / "audio.wav")]
            try:
                run(demucs + (["-d", "cpu"] if force_cpu() else []), "แยกเสียง")
            except RuntimeError:
                if force_cpu():
                    raise
                step(35, "การ์ดจอทำไม่สำเร็จ กำลังลองใหม่ด้วย CPU...")
                run(demucs + ["-d", "cpu"], "แยกเสียง")
            for s in STEMS:
                shutil.move(str(tmp / "sep" / "htdemucs" / "audio" / f"{s}.mp3"), str(tmp / f"{s}.mp3"))

            meta = {"id": vid, "title": title, "subs": subs, "sub_search": SUB_SEARCH, "lang": "", "source": "file"}
            write_meta(tmp, meta)
            if use_gpu() and has_whisper():
                try:  # มีเนื้อเพลง: จับจังหวะ / ไม่มี: ให้ AI ถอดเนื้อจากเสียงร้อง
                    meta = ai_sync(tmp, meta, lambda m: step(80, m))
                except Exception:
                    pass

            src.unlink()
            (tmp / "audio.wav").unlink()
            shutil.rmtree(tmp / "sep")
            if dest.exists():
                shutil.rmtree(dest)
            tmp.rename(dest)

        job.update(status="done", progress=100, message="เสร็จแล้ว", song=meta)
    except Exception as e:
        job.update(status="error", message=explain(f"{job.get('message', '')}\n{e}"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="ไม่ได้เลือกไฟล์"), 400
    title = (request.form.get("title") or "").strip() or Path(f.filename).stem
    title = " ".join(title.replace("_", " ").split()) or "เพลงจากไฟล์"
    ext = Path(f.filename).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
        ext = ".mp4"
    job_id = uuid.uuid4().hex[:10]
    tmp = LIB / f"_tmp_{job_id}"
    tmp.mkdir()
    src = tmp / f"upload{ext}"
    f.save(src)
    jobs[job_id] = {"status": "running", "progress": 5, "message": "รับไฟล์แล้ว..."}
    threading.Thread(target=process_file, args=(job_id, src, title), daemon=True).start()
    return jsonify(job=job_id)


@app.post("/api/start")
def start():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify(error="กรุณาวางลิงก์ YouTube"), 400
    height = int(data.get("height") or 720)
    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "running", "progress": 0, "message": "เริ่มต้น..."}
    threading.Thread(target=process, args=(job_id, url, height), daemon=True).start()
    return jsonify(job=job_id)


@app.get("/api/status/<job_id>")
def status(job_id):
    return jsonify(jobs.get(job_id) or {"status": "error", "message": "ไม่พบงานนี้"})


# ---------- ตรวจหาอัปเดตของโปรแกรมเสริม ----------
# ไม่รวม PyTorch / Demucs เพราะอัปเดตเองแล้วอาจทำให้ใช้การ์ดจอไม่ได้
PACKAGES = [
    {"name": "yt-dlp", "spec": "yt-dlp[default]", "label": "ตัวดาวน์โหลดจาก YouTube (yt-dlp)", "required": True},
    {"name": "yt-dlp-ejs", "spec": "yt-dlp[default]", "label": "ส่วนเสริมของตัวดาวน์โหลด", "required": True},
    {"name": "openai-whisper", "spec": "openai-whisper", "label": "AI จับจังหวะซับ (Whisper)", "required": True},
]


def ver_key(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def check_updates():
    items = []
    for pkg in PACKAGES:
        try:
            installed = importlib.metadata.version(pkg["name"])
        except importlib.metadata.PackageNotFoundError:
            installed = None
        try:
            req = urllib.request.Request(f"https://pypi.org/pypi/{pkg['name']}/json",
                                         headers={"User-Agent": "karaoke-app"})
            latest = json.loads(urllib.request.urlopen(req, timeout=10).read())["info"]["version"]
        except Exception:
            latest = None
        update = bool(latest) and (installed is None or ver_key(latest) > ver_key(installed))
        items.append({"name": pkg["name"], "spec": pkg["spec"], "label": pkg["label"],
                      "installed": installed, "latest": latest, "update": update})
    return items


@app.get("/api/updates")
def get_updates():
    importlib.invalidate_caches()
    return jsonify(items=check_updates())


@app.post("/api/updates")
def run_updates():
    job_id = uuid.uuid4().hex[:10]
    job = jobs[job_id] = {"status": "running", "progress": 10, "message": "กำลังตรวจหาอัปเดต..."}

    def work():
        try:
            specs = []
            for it in check_updates():
                if it["update"] and it["spec"] not in specs:
                    specs.append(it["spec"])
            for n, spec in enumerate(specs):
                job.update(progress=20 + 70 * n // len(specs), message=f"กำลังอัปเดต {spec} ...")
                run([PY, "-m", "pip", "install", "-U", spec], "อัปเดต")
            importlib.invalidate_caches()
            job.update(status="done", progress=100, message="อัปเดตเสร็จแล้ว" if specs else "ทุกอย่างเป็นรุ่นล่าสุดแล้ว")
        except Exception as e:
            job.update(status="error", message=str(e))

    threading.Thread(target=work, daemon=True).start()
    return jsonify(job=job_id)


@app.post("/api/enable-gpu")
def enable_gpu():
    """เครื่องมีการ์ดจอ NVIDIA แต่ลง PyTorch แบบ CPU ไว้: ติดตั้งแบบใช้การ์ดจอให้"""
    job_id = uuid.uuid4().hex[:10]
    job = jobs[job_id] = {"status": "running", "progress": 15,
                          "message": "กำลังดาวน์โหลดส่วนเสริมสำหรับการ์ดจอ (ราว 3 GB ใช้เวลาหลายนาที)..."}

    def work():
        try:
            run([PY, "-m", "pip", "install", "-U", "torch", "torchaudio",
                 "--index-url", "https://download.pytorch.org/whl/cu126"], "ติดตั้งส่วนเสริมการ์ดจอ")
            job.update(progress=90, message="กำลังตรวจสอบการ์ดจอ...")
            detect_device()
            if device["name"] == "gpu":
                job.update(status="done", progress=100, message="เปิดใช้การ์ดจอแล้ว")
            else:
                job.update(status="error", message="ติดตั้งแล้วแต่ยังใช้การ์ดจอไม่ได้ ลองอัปเดตไดรเวอร์การ์ดจอ NVIDIA")
        except Exception as e:
            job.update(status="error", message=str(e))

    threading.Thread(target=work, daemon=True).start()
    return jsonify(job=job_id)


@app.post("/api/quit")
def quit_app():
    """ปุ่มปิดโปรแกรมบนหน้าเว็บ"""
    threading.Timer(0.4, lambda: os._exit(0)).start()
    return jsonify(ok=True)


@app.get("/api/device")
def get_device():
    return jsonify(name=device["name"], reason=device.get("reason", ""), choice=device_choice(),
                   using="gpu" if use_gpu() else "cpu")


@app.post("/api/device")
def set_device():
    choice = (request.get_json(silent=True) or {}).get("choice")
    if choice not in ("auto", "gpu", "cpu"):
        return jsonify(error="ตัวเลือกไม่ถูกต้อง"), 400
    settings["device"] = choice
    try:
        SETTINGS_FILE.write_text(json.dumps(settings), encoding="utf-8")
    except OSError:
        pass
    return get_device()


ORDER_FILE = LIB / "order.json"


def sweep_library():
    """ตอนเปิดแอป: เก็บกวาดโฟลเดอร์ค้างในคลัง
    - งานที่ค้างกลางทาง (_tmp_...) แต่แยกเสียงและโหลดวิดีโอครบแล้ว: กู้เป็นเพลงในคลังให้ ไม่ต้องทำใหม่
    - งานที่ค้างและไฟล์ไม่ครบ กับเพลงที่ลบไม่หมดเพราะไฟล์ถูกล็อก: ลบทิ้ง"""
    for d in list(LIB.iterdir()):
        if not d.is_dir():
            continue
        if d.name.startswith("_tmp_"):
            try:
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                vid = str(meta["id"])
                complete = all((d / f).exists() for f in ["video.mp4"] + [f"{x}.mp3" for x in STEMS])
                if complete and re.fullmatch(r"[\w-]+", vid) and not (LIB / vid).exists():
                    for junk in list(d.glob("src.*")) + list(d.glob("upload.*")) + [d / "audio.wav"]:
                        junk.unlink(missing_ok=True)
                    shutil.rmtree(d / "sep", ignore_errors=True)
                    d.rename(LIB / vid)
                    continue
            except Exception:
                pass
            shutil.rmtree(d, ignore_errors=True)
        elif not (d / "meta.json").exists():
            shutil.rmtree(d, ignore_errors=True)


def load_order():
    try:
        return [str(x) for x in json.loads(ORDER_FILE.read_text(encoding="utf-8"))]
    except Exception:
        return []


@app.get("/api/library")
def library():
    """เพลงในคลัง เรียงตามลำดับที่ผู้ใช้จัดไว้ เพลงใหม่ที่ยังไม่เคยจัดจะอยู่บนสุด"""
    songs = {}
    for meta in sorted(LIB.glob("*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            song = json.loads(meta.read_text(encoding="utf-8"))
            if meta.parent.name == song["id"]:  # ข้ามงานที่ยังประมวลผลไม่เสร็จ (โฟลเดอร์ _tmp_...)
                songs[song["id"]] = song
        except Exception:
            continue
    order = load_order()
    fresh = [song for vid, song in songs.items() if vid not in order]
    return jsonify(fresh + [songs[vid] for vid in order if vid in songs])


@app.post("/api/library/order")
def set_order():
    ids = (request.get_json(silent=True) or {}).get("ids") or []
    ORDER_FILE.write_text(json.dumps([str(x) for x in ids if re.fullmatch(r"[\w-]+", str(x))]), encoding="utf-8")
    return "", 204


@app.delete("/api/library/<vid>")
def delete_song(vid):
    d = LIB / vid
    if not re.fullmatch(r"[\w-]+", vid) or not (d / "meta.json").exists():
        return "ไม่พบเพลงนี้", 404
    for attempt in range(4):  # Windows อาจยังล็อกไฟล์ที่เพิ่งเล่นอยู่ครู่หนึ่ง
        try:
            shutil.rmtree(d)
            break
        except OSError:
            time.sleep(0.4)
    if d.exists():
        # ยังมีไฟล์ถูกล็อก: เอาเพลงออกจากคลังก่อน (ลบ meta.json) ไฟล์ที่เหลือจะถูกเก็บกวาดตอนเปิดแอปครั้งหน้า
        try:
            (d / "meta.json").unlink(missing_ok=True)
        except OSError:
            return "ลบไม่สำเร็จ ลองปิดแล้วเปิดแอปใหม่ แล้วลบอีกครั้ง", 409
    ORDER_FILE.write_text(json.dumps([x for x in load_order() if x != vid]), encoding="utf-8")
    return "", 204


@app.get("/api/subs/<vid>")
def fetch_subs(vid):
    """หาซับให้เพลงในคลังที่ทำไว้ก่อนมีระบบซับ"""
    d = LIB / vid
    if not re.fullmatch(r"[\w-]+", vid) or not (d / "meta.json").exists():
        return "ไม่พบเพลงนี้", 404
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    if meta.get("sub_search") != SUB_SEARCH and meta.get("subs") in (None, "auto") and not vid.startswith("file-"):
        url = f"https://www.youtube.com/watch?v={vid}"
        try:
            raw = run([PY, "-m", "yt_dlp", "--encoding", "utf-8", "--no-playlist", "--skip-download",
                       "--dump-json", url], "ตรวจสอบลิงก์")
            refresh_subs(d, meta, url, json.loads(raw.splitlines()[-1]))
        except Exception:
            pass
    return jsonify(meta)


@app.post("/api/subs/<vid>")
def save_subs(vid):
    """บันทึกซับที่ผู้ใช้ทำเองหรือเลือกไฟล์เอง (รูปแบบ LRC) ไว้กับเพลง"""
    d = LIB / vid
    if not re.fullmatch(r"[\w-]+", vid) or not (d / "meta.json").exists():
        return "ไม่พบเพลงนี้", 404
    (d / "subs.lrc").write_text(request.get_data(as_text=True), encoding="utf-8")
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    meta.update(subs="custom", timed=False)
    (d / "subs.json").unlink(missing_ok=True)
    write_meta(d, meta)
    return jsonify(meta)


@app.post("/api/ai-subs/<vid>")
def start_ai_subs(vid):
    """ให้ AI จับจังหวะซับ (ส่งเนื้อเพลงมาด้วยได้ ถ้าอยากใช้เนื้อเพลงที่วางเอง)"""
    d = LIB / vid
    if not re.fullmatch(r"[\w-]+", vid) or not (d / "meta.json").exists():
        return jsonify(error="ไม่พบเพลงนี้"), 404
    lyrics = (request.get_json(silent=True) or {}).get("lyrics")
    job_id = uuid.uuid4().hex[:10]
    job = jobs[job_id] = {"status": "running", "progress": 10, "message": "เริ่มต้น..."}

    def work():
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            meta = ai_sync(d, meta, lambda m: job.update(progress=50, message=m), lyrics)
            job.update(status="done", progress=100, message="เสร็จแล้ว", song=meta)
        except Exception as e:
            job.update(status="error", message=str(e))

    threading.Thread(target=work, daemon=True).start()
    return jsonify(job=job_id)


@app.get("/media/<vid>/<name>")
def media(vid, name):
    return send_from_directory(LIB / vid, name)


@app.get("/api/export")
def export():
    """บันทึกวิดีโอตามระดับเสียงที่ตั้งไว้ในมิกเซอร์"""
    vid = request.args.get("id", "")
    d = LIB / vid
    if not re.fullmatch(r"[\w-]+", vid) or not (d / "meta.json").exists():
        return "ไม่พบเพลงนี้", 404
    title = json.loads((d / "meta.json").read_text(encoding="utf-8"))["title"]

    cmd = [FFMPEG, "-y", "-i", str(d / "video.mp4")]
    parts = []
    for i, s in enumerate(STEMS, start=1):
        level = max(0.0, min(150.0, float(request.args.get(s, 100)))) / 100
        cmd += ["-i", str(d / f"{s}.mp3")]
        parts.append(f"[{i}:a]volume={level}[a{i}]")
    mix = "".join(f"[a{i}]" for i in range(1, len(STEMS) + 1))
    parts.append(f"{mix}amix=inputs={len(STEMS)}:normalize=0[a]")
    out = d / "export.mp4"
    cmd += ["-filter_complex", ";".join(parts), "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)]
    try:
        run(cmd, "บันทึกวิดีโอ")
    except RuntimeError as e:
        return str(e), 500
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)[:80]
    return send_file(out, as_attachment=True, download_name=f"karaoke_{safe}.mp4")


@app.get("/")
def index():
    return PAGE


@app.get("/favicon.ico")
def favicon():
    if (BASE / "karaoke.ico").exists():
        return send_file(BASE / "karaoke.ico")
    return "", 404


PORTS = range(5177, 5197)  # ลองทีละพอร์ต เผื่อพอร์ตแรกมีโปรแกรมอื่นใช้อยู่
URL = f"http://127.0.0.1:{PORTS[0]}"


@app.get("/api/ping")
def ping():
    return jsonify(app="karaoke")


# ---------- ปิดโปรแกรมเองเมื่อหน้าต่างแอปถูกปิด (แอปไม่มีหน้าต่างดำให้กดปิดแล้ว) ----------
life = {"start": time.time(), "alive": 0.0, "bye": 0.0}


@app.post("/api/alive")
def alive():
    life["alive"] = time.time()
    return "", 204


@app.post("/api/bye")
def bye():
    life["bye"] = time.time()
    return "", 204


def watchdog():
    while True:
        time.sleep(2)
        now = time.time()
        closed = life["bye"] and now - life["bye"] > 15 and life["alive"] < life["bye"]   # ปิดหน้าต่างแล้วไม่กลับมา
        silent = life["alive"] and now - life["alive"] > 180                              # หน้าต่างหายไปเฉย ๆ
        never = not life["alive"] and now - life["start"] > 300                           # ไม่เคยมีหน้าต่างเปิดเลย
        if closed or silent or never:
            os._exit(0)


def find_app_browser():
    """หา Edge หรือ Chrome เพื่อเปิดแอปเป็นหน้าต่างของตัวเอง (ไม่มีแท็บและแถบที่อยู่)"""
    roots = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for sub in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
        for root in roots:
            exe = Path(root) / sub if root else None
            if exe and exe.exists():
                return str(exe)
    return None


def open_window(quit_when_closed=True):
    exe = find_app_browser()
    if not exe:
        webbrowser.open(URL)  # ไม่มี Edge/Chrome: เปิดในเบราว์เซอร์ปกติแทน
        return
    profile = BASE / "cache" / "window"
    profile.mkdir(parents=True, exist_ok=True)
    win = subprocess.Popen([exe, f"--app={URL}", f"--user-data-dir={profile}", "--no-first-run",
                            "--no-default-browser-check", "--window-size=1440,920"])
    if quit_when_closed:
        started = time.time()
        win.wait()
        if time.time() - started > 5:  # ปิดหน้าต่างแอป = ปิดโปรแกรม
            os._exit(0)


def is_karaoke(port):
    """พอร์ตนี้มีแอปคาราโอเกะของเรารันอยู่หรือเปล่า (ไม่ใช่โปรแกรมอื่น)"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            return json.loads(r.read()).get("app") == "karaoke"
    except Exception:
        return False


def port_free(port):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


PAGE = r"""<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KaraokeIT</title>
<link rel="icon" href="/favicon.ico">
<link href="https://fonts.googleapis.com/css2?family=Kanit:wght@300;400;500;600&family=Chakra+Petch:wght@400;500;600&family=Sarabun:wght@300;400;500;600&display=swap" rel="stylesheet">
<script>
  // ใช้หน้าตาที่เลือกไว้ตั้งแต่ก่อนวาดหน้า จะได้ไม่กะพริบเป็นแบบเดิมก่อน
  try { const look = localStorage.getItem("look"); if (look) document.documentElement.dataset.look = look; } catch (e) {}
</script>
<style>
  :root {
    --bg: #0e0b16; --panel: #1a1526; --panel2: #231c33; --line: #332a48;
    --text: #f3eefc; --muted: #9a8fb3; --pink: #ff4d9d; --amber: #ffb648;
    --hover: #2e2544; --switch: #40365a; --glow1: #3a1650; --glow2: #3d2a10; --go2: #ff7a59;
    --current: #ff8fc0; --bad: #ff8080; --on-amber: #1a1200; --radius: 20px;
    --font: "Kanit", "Leelawadee UI", sans-serif;
    color-scheme: dark;
  }
  /* ===== หน้าตาแบบอื่น ๆ เลือกได้จากปุ่ม "หน้าตา" มุมขวาบน ===== */
  :root[data-look="neon"] {            /* นีออน: ตู้คาราโอเกะยามค่ำ ฟ้า-ม่วงสะท้อนแสง */
    --bg: #05060f; --panel: #0b1022; --panel2: #121a35; --line: #24306a;
    --text: #e8f4ff; --muted: #7f93c9; --pink: #19e3ff; --amber: #ff3df2;
    --hover: #1a2550; --switch: #26315f; --glow1: #0a2a6b; --glow2: #4a0a5e; --go2: #7b5cff;
    --current: #7ff2ff; --on-amber: #1a0019; --radius: 12px;
    --font: "Chakra Petch", "Kanit", "Leelawadee UI", sans-serif;
  }
  :root[data-look="neon"] .card { box-shadow: 0 0 0 1px rgba(25, 227, 255, .12), 0 0 28px rgba(25, 227, 255, .07); }
  :root[data-look="neon"] h1 { text-shadow: 0 0 18px rgba(25, 227, 255, .55); letter-spacing: 2px; }
  :root[data-look="neon"] .go:not(.alt) { box-shadow: 0 0 22px rgba(25, 227, 255, .35); }

  :root[data-look="light"] {           /* สว่าง: พื้นครีม อ่านง่ายตอนกลางวัน */
    --bg: #f6f1ea; --panel: #ffffff; --panel2: #f1ebe2; --line: #e0d6c8;
    --text: #2a2430; --muted: #7b7085; --pink: #e8456f; --amber: #b86a00;
    --hover: #e7dfd3; --switch: #cfc4b5; --glow1: #ffd9e2; --glow2: #ffe8c2; --go2: #ff8a4c;
    --current: #c92a57; --bad: #c62828; --on-amber: #ffffff; --radius: 18px;
    --font: "Sarabun", "Kanit", "Leelawadee UI", sans-serif;
  }
  :root[data-look="light"] { color-scheme: light; }
  :root[data-look="light"] body { font-weight: 400; }
  :root[data-look="light"] .card { box-shadow: 0 6px 24px rgba(60, 40, 20, .08); }
  :root[data-look="light"] .chip.gpu { border-color: #2e8b3d; color: #2e8b3d; }
  :root[data-look="light"] .lib .del:hover { background: #d0324f; }

  :root[data-look="cinema"] {          /* โรงหนัง: ดำ-ทอง วิดีโอเต็มความกว้าง แผงควบคุมอยู่ด้านล่าง */
    --bg: #0a0a0a; --panel: #141414; --panel2: #1e1e1e; --line: #2c2c2c;
    --text: #f5f0e6; --muted: #9c948a; --pink: #e7b84b; --amber: #e7b84b;
    --hover: #2a2a2a; --switch: #3a3a3a; --glow1: #2b2108; --glow2: #1a1a1a; --go2: #c8862a;
    --current: #f3d489; --on-amber: #171000; --radius: 8px;
  }
  :root[data-look="cinema"] .wrap { max-width: 1500px; }
  :root[data-look="cinema"] .player:not(:fullscreen) .stage { aspect-ratio: auto; height: 64vh; }
  :root[data-look="cinema"] .main { grid-template-columns: 1fr; }
  :root[data-look="cinema"] .main > div:first-child { display: contents; }
  :root[data-look="cinema"] .player { order: 1; }
  :root[data-look="cinema"] .side { order: 2; grid-template-columns: 1fr 1fr; align-items: start; }
  :root[data-look="cinema"] #tapCard { order: 3; margin-top: 0; }
  :root[data-look="cinema"] .lib { order: 4; margin-top: 0; }
  :root[data-look="cinema"] .go:not(.alt) { color: #171000; }
  :root[data-look="cinema"] h1 { letter-spacing: 3px; text-transform: uppercase; font-weight: 500; }
  @media (max-width: 960px) { :root[data-look="cinema"] .side { grid-template-columns: 1fr; } }

  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: var(--font); font-weight: 300;
    color: var(--text); background: var(--bg);
    background-image: radial-gradient(900px 500px at 85% -10%, var(--glow1) 0%, transparent 60%),
                      radial-gradient(700px 400px at -10% 110%, var(--glow2) 0%, transparent 60%);
    min-height: 100vh;
  }
  .wrap { max-width: 1280px; margin: 0 auto; padding: 28px 20px 60px; }
  header { display: flex; align-items: baseline; gap: 14px; margin-bottom: 22px; flex-wrap: wrap; }
  h1 { font-size: 30px; font-weight: 600; margin: 0; letter-spacing: .5px; }
  h1 span { color: var(--pink); }
  header p { margin: 0; color: var(--muted); flex: 1; }
  .chip { font-size: 13px; padding: 4px 12px; border-radius: 20px; border: 1px solid var(--line); color: var(--muted); }
  button.chip { background: transparent; cursor: pointer; }
  select.chip { background: var(--panel); cursor: pointer; padding: 4px 10px; font-size: 13px; }
  select.chip:disabled { cursor: default; }
  button.chip:hover { border-color: var(--pink); color: var(--text); }
  button.chip.has { border-color: var(--amber); color: var(--on-amber); background: var(--amber); font-weight: 500; }
  #updBox { margin: -8px 0 18px; padding: 16px 18px; }
  #updBox .row { display: flex; gap: 12px; padding: 6px 0; font-size: 14px; }
  #updBox .row span:first-child { flex: 1; }
  #updBox .row .new { color: var(--amber); }
  #updBox .row .ok { color: var(--muted); }
  #updBox .go { margin-top: 10px; padding: 9px 20px; font-size: 14px; }
  .chip.gpu { border-color: #8cf08c; color: #8cf08c; }
  .chip.cpu { border-color: var(--amber); color: var(--amber); }

  .bar { display: flex; gap: 10px; flex-wrap: wrap; }
  .bar input[type=text] {
    flex: 1; min-width: 240px; padding: 14px 18px; border-radius: 14px; border: 1px solid var(--line);
    background: var(--panel); color: var(--text); font: inherit; font-size: 16px; outline: none;
  }
  .bar input[type=text]:focus { border-color: var(--pink); }
  .urlbox { flex: 1; min-width: 240px; position: relative; display: flex; }
  .urlbox input[type=text] { width: 100%; padding-right: 52px; }
  #urlClear {
    position: absolute; right: 10px; top: 50%; transform: translateY(-50%); width: 32px; height: 32px;
    border-radius: 50%; border: 0; background: var(--panel2); color: var(--muted); font-size: 14px;
  }
  #urlClear:hover { background: var(--pink); color: #fff; }
  select, button { font: inherit; color: var(--text); cursor: pointer; }
  select { padding: 0 14px; border-radius: 14px; border: 1px solid var(--line); background: var(--panel); }
  .go {
    padding: 14px 26px; border: 0; border-radius: 14px; font-weight: 500; font-size: 16px;
    background: linear-gradient(135deg, var(--pink), var(--go2)); color: #fff;
  }
  .go:disabled { opacity: .5; cursor: default; }
  .go.alt { background: var(--panel2); border: 1px solid var(--line); padding: 14px 18px; color: var(--text); }
  .go.alt:hover { border-color: var(--pink); }

  #progress { display: none; margin-top: 14px; }
  #progress .track { height: 8px; border-radius: 8px; background: var(--panel2); overflow: hidden; }
  #progress .fill { height: 100%; width: 0; background: linear-gradient(90deg, var(--pink), var(--amber)); transition: width .5s; }
  #progress .msg { margin-top: 8px; color: var(--muted); font-size: 14px; }
  #progress.error .msg { color: var(--bad); white-space: pre-wrap; }

  .main { display: grid; grid-template-columns: 1fr 360px; gap: 20px; margin-top: 24px; }
  @media (max-width: 960px) { .main { grid-template-columns: 1fr; } }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); }
  .side { display: grid; gap: 20px; align-content: start; }

  .player { overflow: hidden; }
  .stage {
    position: relative; background: #000; aspect-ratio: 16/9; display: grid; place-items: center; overflow: hidden;
    container-type: inline-size;
  }
  /* วิดีโอลอยอยู่ในกรอบเวที: วิดีโอสัดส่วนไหน (จัตุรัส / แนวตั้ง) ก็ย่อให้พอดีกรอบ ไม่ดันกรอบจนล้นจอ */
  .stage video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; display: block; cursor: pointer; }
  .stage .empty { position: absolute; color: var(--muted); text-align: center; padding: 20px; }
  .player:fullscreen { border-radius: 0; display: flex; flex-direction: column; }
  .player:fullscreen .stage { flex: 1; aspect-ratio: auto; min-height: 0; }
  .player:fullscreen { background: #000; }
  .player:fullscreen #title { display: none; }
  #subs {
    position: absolute; left: 4%; right: 4%; bottom: 6%; text-align: center; pointer-events: none;
    font-weight: 500; line-height: 1.35; white-space: pre-line;
    filter: drop-shadow(0 0 3px #000) drop-shadow(0 2px 3px #000); --hl: var(--pink);
  }
  #subCur .sung { color: var(--hl); }
  #subCur { font-size: calc(var(--sub, 3.4) * 1cqw); color: #fff; }
  #subNext { font-size: calc(var(--sub, 3.4) * .68cqw); color: #ffd9ec; opacity: .7; margin-top: .3em; }
  .controls { display: flex; align-items: center; gap: 12px; padding: 12px 16px; }
  .icon {
    width: 42px; height: 42px; border-radius: 50%; border: 0; background: var(--panel2);
    display: grid; place-items: center; font-size: 16px; flex: none;
  }
  .icon.play { background: var(--pink); }
  .vol { display: flex; align-items: center; gap: 6px; flex: none; }
  .vol input[type=range] { width: 96px; }
  #muteBtn { border: 0; background: transparent; font-size: 18px; padding: 4px; border-radius: 8px; line-height: 1; }
  #muteBtn:hover { background: var(--panel2); }
  .vol .time { width: 38px; }
  .time { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; flex: none; }
  #title { padding: 0 18px 16px; font-weight: 500; font-size: 17px; }

  input[type=range] { accent-color: var(--c, var(--pink)); width: 100%; height: 22px; cursor: pointer; }

  .panel { padding: 18px; }
  .panel h2, .lib h2 { margin: 0 0 4px; font-size: 18px; font-weight: 500; }
  .panel .hint { color: var(--muted); font-size: 13px; margin: 0 0 14px; }
  .headrow { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
  .headrow h2 { flex: 1; margin: 0; }
  .presets { display: flex; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; }
  .presets button, .ghost {
    flex: 1; padding: 9px; border-radius: 10px; border: 1px solid var(--line); background: var(--panel2); font-size: 14px;
  }
  .presets button:hover, .ghost:hover { border-color: var(--pink); }
  .ghost { width: 100%; }
  details.more summary {
    list-style: none; cursor: pointer; text-align: center; padding: 9px; border-radius: 10px; font-size: 14px;
    border: 1px solid var(--line); background: var(--panel2); user-select: none;
  }
  details.more summary::-webkit-details-marker { display: none; }
  details.more summary::after { content: "  ▾"; color: var(--muted); }
  details.more[open] summary::after { content: "  ▴"; }
  details.more summary:hover { border-color: var(--pink); }
  .ch { padding: 12px 14px; border-radius: 14px; background: var(--panel2); margin-bottom: 10px; transition: opacity .2s; }
  .ch.off { opacity: .45; }
  #channels .ch.off { opacity: 1; }
  .ch .top { display: flex; align-items: center; gap: 10px; }
  .ch .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--c); }
  .ch .name { flex: 1; font-weight: 400; }
  .ch .val { color: var(--muted); font-size: 13px; min-width: 44px; text-align: right; font-variant-numeric: tabular-nums; }
  .switch { position: relative; width: 46px; height: 26px; flex: none; }
  .switch input { opacity: 0; width: 100%; height: 100%; margin: 0; position: absolute; cursor: pointer; z-index: 1; }
  .switch i { position: absolute; inset: 0; border-radius: 26px; background: var(--switch); transition: .2s; }
  .switch i::after {
    content: ""; position: absolute; top: 3px; left: 3px; width: 20px; height: 20px; border-radius: 50%;
    background: #fff; transition: .2s;
  }
  .switch input:checked + i { background: var(--c, var(--pink)); }
  .switch input:checked + i::after { left: 23px; }
  .switch input:focus-visible + i { outline: 2px solid #fff; outline-offset: 2px; }
  dialog {
    width: min(420px, 92vw); padding: 18px; border-radius: 20px; border: 1px solid var(--line);
    background: var(--panel); color: var(--text); font: inherit;
  }
  dialog::backdrop { background: rgba(5, 3, 10, .65); }
  dialog h2 { margin: 0; font-size: 18px; font-weight: 500; flex: 1; }
  dialog .hint { color: var(--muted); font-size: 13px; margin: 4px 0 14px; }
  .ch.master { background: transparent; border: 1px dashed var(--line); }
  .save {
    width: 100%; margin-top: 6px; padding: 12px; border-radius: 12px; border: 1px solid var(--amber);
    background: transparent; color: var(--amber); font-weight: 500;
  }
  .save:hover { background: var(--amber); color: var(--on-amber); }
  .save:disabled { opacity: .4; cursor: default; }

  #tapCard { margin-top: 20px; }
  #tapText {
    width: 100%; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--line); resize: vertical;
    background: var(--panel2); color: var(--text); font: inherit; line-height: 1.6; outline: none;
  }
  #tapText:focus { border-color: var(--pink); }
  #tapNow { min-height: 2.2em; margin: 12px 0; font-size: 20px; font-weight: 500; color: var(--amber); }
  .presets button:disabled { opacity: .4; cursor: default; }
  .lib { margin-top: 20px; padding: 18px; }
  .lib ul { list-style: none; margin: 10px 0 0; padding: 0; display: grid; gap: 6px; }
  .lib li { display: flex; align-items: stretch; gap: 6px; border-radius: 10px; }
  .lib li button.song {
    flex: 1; min-width: 0; text-align: left; padding: 10px 14px; border-radius: 10px; border: 0; background: var(--panel2);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .lib li button.song:hover { background: var(--hover); }
  /* เพลงที่เปิดอยู่: ขีดสีชมพูด้านซ้ายและตัวอักษรสีชมพู */
  .lib li.current button.song {
    box-shadow: inset 5px 0 0 var(--pink); background: var(--hover); color: var(--current); font-weight: 500; padding-left: 20px;
  }
  .lib .grip {
    display: grid; place-items: center; width: 34px; border-radius: 10px; background: var(--panel2);
    color: var(--muted); font-size: 18px; cursor: grab; user-select: none; flex: none;
  }
  .lib .grip:hover { color: var(--text); background: var(--hover); }
  .lib .del {
    flex: none; min-width: 40px; padding: 0 10px; border-radius: 10px; border: 0; background: var(--panel2);
    color: var(--muted); font-size: 14px;
  }
  .lib .del:hover { color: #fff; background: #7a2740; }
  #libMsg { margin: 8px 0 0; }
  #libMsg.bad { color: var(--bad); }
  .lib li.dragging { opacity: .4; }
  .lib li.dropBefore { box-shadow: 0 -3px 0 var(--pink); }
  .lib li.dropAfter { box-shadow: 0 3px 0 var(--pink); }
  #libSearch {
    width: min(320px, 55%); padding: 8px 14px; border-radius: 12px; border: 1px solid var(--line);
    background: var(--panel2); color: var(--text); font: inherit; font-size: 14px; outline: none;
  }
  #libSearch:focus { border-color: var(--pink); }
  .lib .none { color: var(--muted); font-size: 14px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>🎤 Karaoke<span>IT</span></h1>
    <p>วางลิงก์ YouTube แล้วมิกซ์เสียงเองได้ทุกชิ้น</p>
    <select class="chip" id="look" title="เปลี่ยนหน้าตาของแอป" aria-label="หน้าตาของแอป">
      <option value="">หน้าตา: ดั้งเดิม</option>
      <option value="neon">หน้าตา: นีออน</option>
      <option value="light">หน้าตา: สว่าง</option>
      <option value="cinema">หน้าตา: โรงหนัง</option>
    </select>
    <button class="chip has" id="gpuFix" hidden>เปิดใช้การ์ดจอ</button>
    <button class="chip" id="updBtn">ตรวจหาอัปเดต</button>
    <button class="chip" id="quitBtn">ปิดโปรแกรม</button>
    <select class="chip" id="device" title="เลือกว่าจะใช้อะไรประมวลผล" disabled>
      <option value="auto">กำลังตรวจสอบเครื่อง...</option>
    </select>
  </header>
  <div class="card" id="updBox" hidden></div>

  <div class="bar">
    <div class="urlbox">
      <input type="text" id="url" placeholder="วางลิงก์ YouTube ที่นี่  หรือพิมพ์ชื่อเพลงแล้วกด ไฟล์ในเครื่อง">
      <button id="urlClear" title="ล้างลิงก์" aria-label="ล้างลิงก์" hidden>✕</button>
    </div>
    <select id="height" title="ความละเอียดสูงสุด">
      <option value="360">360p</option><option value="480">480p</option>
      <option value="720" selected>720p</option><option value="1080">1080p</option>
    </select>
    <button class="go" id="go">สร้างคาราโอเกะ</button>
    <button class="go alt" id="pick" title="ทำคาราโอเกะจากไฟล์วิดีโอในเครื่อง">📁 ไฟล์ในเครื่อง</button>
    <input type="file" id="pickFile" accept="video/*,audio/*,.mp4,.mkv,.webm,.mov,.avi,.mp3,.m4a,.wav,.flac" hidden>
  </div>
  <div id="progress"><div class="track"><div class="fill"></div></div><div class="msg"></div></div>

  <div class="main">
    <div>
      <div class="card player" id="player">
        <div class="stage">
          <video id="video" muted playsinline preload="auto"></video>
          <div class="empty" id="empty">ยังไม่มีเพลง<br>วางลิงก์ด้านบน หรือเลือกจากคลังเพลง</div>
          <div id="subs"><div id="subCur"></div><div id="subNext"></div></div>
        </div>
        <div class="controls">
          <button class="icon play" id="playBtn" title="เล่น / หยุด">▶</button>
          <span class="vol" title="ระดับเสียงรวม">
            <button id="muteBtn" title="ปิด / เปิดเสียง" aria-label="ปิด / เปิดเสียง">🔊</button>
            <input type="range" id="master" min="0" max="100" value="100" aria-label="ระดับเสียงรวม">
            <span class="time" id="masterVal">100%</span>
          </span>
          <span class="time" id="cur">0:00</span>
          <input type="range" id="seek" min="0" max="0" step="0.1" value="0" aria-label="ตำแหน่งเพลง">
          <span class="time" id="dur">0:00</span>
          <button class="icon" id="fsBtn" title="เต็มจอ">⛶</button>
        </div>
        <div id="title"></div>
      </div>

      <div class="card panel" id="tapCard" hidden>
        <div class="headrow">
          <h2>ทำซับเอง</h2>
          <button class="ghost" id="tapClose" style="flex:none;width:auto;padding:6px 16px">ปิด</button>
        </div>
        <p class="hint">1) วางเนื้อเพลง หนึ่งบรรทัดต่อหนึ่งท่อน &nbsp; 2) กด "ให้ AI จับจังหวะให้" &nbsp; หรือจะเคาะเอง: กดเริ่ม แล้วกด Enter ทุกครั้งที่นักร้องเริ่มร้องบรรทัดที่แสดงอยู่ แล้วกดบันทึก</p>
        <textarea id="tapText" rows="8" placeholder="วางเนื้อเพลงที่นี่..."></textarea>
        <div id="tapNow"></div>
        <div class="presets">
          <button id="tapAi">ให้ AI จับจังหวะให้</button>
          <button id="tapStart">เริ่มเคาะจังหวะ</button>
          <button id="tapHit" disabled>เคาะ (Enter)</button>
          <button id="tapUndo" disabled>ย้อนหนึ่งบรรทัด</button>
          <button id="tapDone" disabled>บันทึก</button>
        </div>
      </div>

      <div class="card lib">
        <div class="headrow">
          <h2>คลังเพลง</h2>
          <input type="search" id="libSearch" placeholder="🔍 ค้นหาเพลงในคลัง..." aria-label="ค้นหาเพลงในคลัง">
        </div>
        <p class="hint" id="libMsg" hidden></p>
        <ul id="lib"></ul>
      </div>
    </div>

    <div class="side">
      <div class="card panel">
        <h2>เสียง</h2>
        <p class="hint">เปิด-ปิดและปรับระดับเสียงร้อง ส่วนอื่นอยู่ในมิกเซอร์</p>
        <div class="presets">
          <button id="pKaraoke">คาราโอเกะ</button>
          <button id="pOriginal">ต้นฉบับ</button>
          <button id="pMore">🎚 มิกเซอร์</button>
        </div>
        <div id="channels"></div>
        <button class="save" id="save" disabled title="บันทึกเป็นไฟล์วิดีโอตามเสียงที่มิกซ์ไว้">⬇ ดาวน์โหลด</button>
      </div>

      <div class="card panel">
        <div class="headrow">
          <h2>ซับไตเติล</h2>
          <label class="switch"><input type="checkbox" id="subOn" checked aria-label="เปิด-ปิดซับ"><i></i></label>
        </div>
        <p class="hint" id="subStatus">ยังไม่มีเพลง</p>
        <div class="ch">
          <div class="top"><span class="name">ขนาดตัวอักษร</span><span class="val" id="subSizeVal"></span></div>
          <input type="range" id="subSize" min="2" max="6" step="0.2" value="3.4" aria-label="ขนาดซับ">
        </div>
        <div class="ch">
          <div class="top"><span class="name">เลื่อนจังหวะซับ</span><span class="val" id="subOffsetVal"></span></div>
          <input type="range" id="subOffset" min="-5" max="5" step="0.1" value="0" aria-label="เลื่อนจังหวะซับ">
        </div>
        <details class="more">
          <summary>ตัวเลือกซับเพิ่มเติม</summary>
          <button class="ghost" id="subPick" style="margin-top:8px">เลือกไฟล์ซับเอง (.srt / .vtt / .lrc)</button>
          <input type="file" id="subFile" accept=".srt,.vtt,.lrc,.txt" hidden>
          <button class="ghost" id="tapOpen" style="margin-top:8px">ใส่เนื้อเพลงเอง (วางเนื้อเพลง)</button>
          <button class="ghost" id="aiSync" style="margin-top:8px">ให้ AI จับจังหวะซับใหม่</button>
        </details>
      </div>
    </div>
  </div>
</div>

<dialog id="delDlg">
  <h2>ลบเพลงนี้ออกจากคลัง?</h2>
  <p class="hint" id="delName" style="color:var(--text);font-size:15px"></p>
  <p class="hint">ไฟล์วิดีโอ เสียง และซับของเพลงนี้จะถูกลบถาวร ถ้าอยากได้อีกต้องวางลิงก์สร้างใหม่</p>
  <div class="presets" style="margin:0">
    <button id="delNo">ยกเลิก</button>
    <button id="delYes" style="background:#d0324f;border-color:#d0324f;color:#fff">ลบเพลง</button>
  </div>
</dialog>

<dialog id="mixDlg">
  <div class="headrow">
    <h2>มิกเซอร์</h2>
    <button class="ghost" id="mixClose" style="flex:none;width:auto;padding:6px 16px">ปิด</button>
  </div>
  <p class="hint">เปิด-ปิด และปรับระดับเครื่องดนตรีแต่ละส่วน</p>
  <div id="moreChannels"></div>
</dialog>

<script>
const STEMS = [
  { id: "vocals", name: "เสียงร้อง",   color: "#ff4d9d", on: false, level: 100 },
  { id: "drums",  name: "กลอง",        color: "#ffb648", on: true,  level: 100 },
  { id: "bass",   name: "เบส",         color: "#5ad1ff", on: true,  level: 100 },
  { id: "other",  name: "ดนตรีอื่น ๆ", color: "#8cf08c", on: true,  level: 100 },
];
const $ = (id) => document.getElementById(id);
const video = $("video");
let ctx, masterGain, currentSong = null;

/* ---------- มิกเซอร์ ---------- */
STEMS.forEach((s) => {
  s.audio = new Audio();
  s.audio.preload = "auto";
  const row = document.createElement("div");
  row.className = "ch";
  row.style.setProperty("--c", s.color);
  row.innerHTML = `
    <div class="top">
      <span class="dot"></span><span class="name">${s.name}</span><span class="val"></span>
      <label class="switch"><input type="checkbox" aria-label="เปิด-ปิด ${s.name}"><i></i></label>
    </div>
    <input type="range" min="0" max="150" step="1" aria-label="ระดับเสียง ${s.name}">`;
  s.row = row;
  s.toggle = row.querySelector("input[type=checkbox]");
  s.slider = row.querySelector("input[type=range]");
  s.val = row.querySelector(".val");
  s.toggle.onchange = () => { s.on = s.toggle.checked; apply(); };
  s.slider.oninput = () => { s.level = +s.slider.value; apply(); };
  // หน้าแรกมีเฉพาะเสียงร้อง ส่วนกลอง เบส และดนตรีอื่น ๆ อยู่ในหน้าต่างมิกเซอร์
  $(s.id === "vocals" ? "channels" : "moreChannels").appendChild(row);
});
$("pMore").onclick = () => $("mixDlg").showModal();
$("mixClose").onclick = () => $("mixDlg").close();
$("mixDlg").onclick = (e) => { if (e.target === $("mixDlg")) $("mixDlg").close(); };

function apply() {
  STEMS.forEach((s) => {
    s.toggle.checked = s.on;
    s.slider.value = s.level;
    s.val.textContent = s.level + "%";
    s.row.classList.toggle("off", !s.on);
    if (s.gain) s.gain.gain.value = s.on ? s.level / 100 : 0;
  });
  const silent = muted || +$("master").value === 0;
  if (masterGain) masterGain.gain.value = muted ? 0 : $("master").value / 100;
  $("masterVal").textContent = muted ? "ปิด" : $("master").value + "%";
  $("muteBtn").textContent = silent ? "🔇" : "🔊";
}
let muted = false;
$("muteBtn").onclick = () => { muted = !muted; apply(); };
$("master").oninput = () => { muted = false; apply(); };   // เลื่อนแถบเสียง = เปิดเสียงกลับ

function preset(fn) { STEMS.forEach((s) => { s.on = fn(s); s.level = 100; }); apply(); }
$("pKaraoke").onclick = () => preset((s) => s.id !== "vocals");
$("pOriginal").onclick = () => preset(() => true);

function initAudio() {
  if (ctx) return;
  ctx = new (window.AudioContext || window.webkitAudioContext)();
  masterGain = ctx.createGain();
  masterGain.connect(ctx.destination);
  STEMS.forEach((s) => {
    s.gain = ctx.createGain();
    ctx.createMediaElementSource(s.audio).connect(s.gain).connect(masterGain);
  });
  apply();
}

/* ---------- ซับไตเติล ---------- */
let cues = [];
function toSec(s) {
  const m = s.match(/(?:(\d+):)?(\d+):(\d+)[.,](\d+)/);
  return m ? (+(m[1] || 0)) * 3600 + (+m[2]) * 60 + (+m[3]) + (+("0." + m[4])) : NaN;
}
function clean(t) {
  return t.replace(/<[^>]+>/g, "").replace(/\{\\[^}]*\}/g, "")
    .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&nbsp;/g, " ").trim();
}
function parseSubs(text, auto) {
  text = text.replace(/\r/g, "");
  const out = [];
  if (!text.includes("-->")) {                       // ไฟล์ .lrc
    text.split("\n").forEach((line) => {
      const tags = [...line.matchAll(/\[(\d+):(\d+(?:\.\d+)?)\]/g)];
      const t = clean(line.replace(/\[[^\]]*\]/g, ""));
      if (t) tags.forEach((m) => out.push({ start: (+m[1]) * 60 + (+m[2]), end: 0, text: t }));
    });
    out.sort((a, b) => a.start - b.start);
    out.forEach((c, i) => { c.end = Math.min(i + 1 < out.length ? out[i + 1].start : c.start + 8, c.start + 12); });
    return out;
  }
  text.split(/\n{2,}/).forEach((block) => {          // ไฟล์ .vtt / .srt
    const lines = block.split("\n");
    const i = lines.findIndex((l) => l.includes("-->"));
    if (i < 0) return;
    const [a, b] = lines[i].split("-->");
    const start = toSec(a), end = toSec(b);
    if (isNaN(start) || isNaN(end)) return;
    let body = lines.slice(i + 1).map(clean).filter(Boolean);
    if (!body.length) return;
    if (auto) {                                      // ซับอัตโนมัติของ YouTube ซ้ำบรรทัดกัน ต้องกรองออก
      if (end - start < 0.05) return;
      body = [body[body.length - 1]];
    }
    const t = body.join("\n");
    if (auto && out.length && out[out.length - 1].text === t) return;
    out.push({ start, end, text: t });
  });
  return out;
}
/* ซับวิ่ง: แบ่งบรรทัดเป็นตัวอักษร แต่ละตัวมีเวลาเริ่ม-จบ แล้วระบายสีตามเวลาเพลง */
const graphemes = window.Intl && Intl.Segmenter ? new Intl.Segmenter("th", { granularity: "grapheme" }) : null;
let shownCue = null, letters = [];
function buildLine(cue) {
  const box = $("subCur");
  box.textContent = "";
  letters = [];
  if (!cue) return;
  const parts = graphemes ? [...graphemes.segment(cue.text)].map((x) => x.segment) : Array.from(cue.text);
  const n = parts.length;
  const guess = Math.min(cue.end - cue.start, Math.max(1.2, n * 0.22));   // ไม่มีเวลารายตัวอักษร: ประมาณเอา
  let cp = 0;
  parts.forEach((p, i) => {
    const el = document.createElement("span");
    el.textContent = p;
    box.appendChild(el);
    const s = cue.ct && cue.ct[cp] != null ? cue.ct[cp] : cue.start + guess * i / n;
    letters.push({ el, s, e: 0, state: "" });
    cp += Array.from(p).length;
  });
  letters.forEach((w, i) => {
    w.e = i + 1 < n ? letters[i + 1].s : Math.max(Math.min(cue.end, w.s + 0.6), w.s + 0.05);
    if (w.e <= w.s) w.e = w.s + 0.01;
  });
}
function paintLine(t) {
  letters.forEach((w) => {
    let state = "todo", p = 0;
    if (t >= w.e) state = "sung";
    else if (t > w.s) { state = "now"; p = Math.round((t - w.s) / (w.e - w.s) * 100); }
    if (w.state === state + p) return;
    w.state = state + p;
    w.el.className = state === "sung" ? "sung" : "";
    w.el.style.cssText = state === "now"
      ? `background:linear-gradient(90deg,var(--hl) ${p}%,#fff ${p}%);-webkit-background-clip:text;background-clip:text;color:transparent`
      : "";
  });
}
function renderSubs() {
  let cur = null, next = "", t = 0;
  if ($("subOn").checked && cues.length) {
    t = video.currentTime - (+$("subOffset").value);
    let i = -1;
    for (let k = 0; k < cues.length && cues[k].start <= t; k++) i = k;
    if (i >= 0 && t < cues[i].end) cur = cues[i];
    const n = cues[i + 1];
    if (n && n.start - t < 6) next = n.text;
  }
  if (cur !== shownCue) { shownCue = cur; buildLine(cur); }
  if (cur) paintLine(t);
  if ($("subNext").textContent !== next) $("subNext").textContent = next;
}
function subUi() {
  $("subs").style.setProperty("--sub", $("subSize").value);
  $("subSizeVal").textContent = (+$("subSize").value).toFixed(1);
  const o = +$("subOffset").value;
  $("subOffsetVal").textContent = (o > 0 ? "+" : "") + o.toFixed(1) + " วิ";
  renderSubs();
}
["subOn", "subSize", "subOffset"].forEach((id) => { $(id).oninput = subUi; });
(function loop() { renderSubs(); requestAnimationFrame(loop); })();
$("subPick").onclick = () => $("subFile").click();
$("subFile").onchange = async () => {
  const f = $("subFile").files[0];
  if (!f) return;
  const parsed = parseSubs(await f.text(), false);
  $("subFile").value = "";
  if (!parsed.length) { $("subStatus").textContent = "อ่านไฟล์ซับนี้ไม่ได้"; return; }
  cues = parsed;
  saveCues(`ใช้ไฟล์ ${f.name}`);
};

/* บันทึกซับ (ไฟล์ที่เลือกเอง / ซับที่เคาะเอง) ไว้กับเพลง จะได้ไม่ต้องทำใหม่ทุกครั้ง */
async function saveCues(label) {
  const song = currentSong;
  if (!song) { $("subStatus").textContent = label; return; }
  const stamp = (t) => `[${String(Math.floor(t / 60)).padStart(2, "0")}:${(t % 60).toFixed(2).padStart(5, "0")}]`;
  const lrc = cues.map((c) => stamp(c.start) + c.text.replace(/\n/g, " ")).join("\n");
  let ok = false;
  try { ok = (await fetch(`/api/subs/${song.id}`, { method: "POST", body: lrc })).ok; } catch (e) {}
  if (ok) song.subs = "custom";
  $("subStatus").textContent = label + (ok ? " — บันทึกไว้กับเพลงนี้แล้ว" : " (บันทึกไม่สำเร็จ ใช้ได้เฉพาะรอบนี้)");
}

const SUB_LABEL = {
  manual: "ซับจาก YouTube",
  auto: "ซับอัตโนมัติจาก YouTube (มักมีคำผิด ลองวางเนื้อเพลงเองจะแม่นกว่า)",
  lrclib: "เนื้อเพลงจาก lrclib.net",
  custom: "เนื้อเพลงที่คุณใส่เอง",
  ai: "AI ถอดจากเสียงร้อง (อาจมีคำผิด ลองวางเนื้อเพลงเองจะแม่นกว่า)",
};
async function loadSubs(song) {
  cues = [];
  $("subOffset").value = 0; subUi();
  $("subStatus").textContent = "กำลังหาซับไตเติล...";
  try { Object.assign(song, await (await fetch(`/api/subs/${song.id}`)).json()); } catch (e) {}
  if (currentSong !== song) return;
  if (!song.subs) {
    $("subStatus").textContent = "หาซับของเพลงนี้ไม่เจอ — ใส่เนื้อเพลงเอง หรือให้ AI ถอดจากเสียงร้องได้ที่ ตัวเลือกซับเพิ่มเติม";
    return;
  }
  try {
    const file = song.timed ? "subs.json"
      : song.subs === "lrclib" || song.subs === "custom" ? "subs.lrc" : "subs.vtt";
    const res = await fetch(`/media/${song.id}/${file}?t=${Date.now()}`);
    if (!res.ok) throw 0;
    const parsed = song.timed ? await res.json() : parseSubs(await res.text(), song.subs === "auto");
    if (currentSong !== song) return;
    cues = parsed;
    $("subStatus").textContent = (SUB_LABEL[song.subs] || "มีซับ")
      + (song.timed ? " · AI จับจังหวะแล้ว" : " · ยังไม่ได้ให้ AI จับจังหวะ ซับจะวิ่งแบบประมาณ");
  } catch (e) {
    $("subStatus").textContent = "โหลดซับไม่สำเร็จ — ใส่เนื้อเพลงเองได้ที่ ตัวเลือกซับเพิ่มเติม";
  }
}

/* ---------- ให้ AI จับจังหวะซับ (ส่งเนื้อเพลงที่วางเองไปด้วยได้) ---------- */
let aiBusy = false;
async function aiSync(lyrics) {
  const song = currentSong;
  if (!song) { $("subStatus").textContent = "เปิดเพลงก่อน"; return false; }
  if (aiBusy) return false;
  aiBusy = true;
  let ok = false;
  showProgress(5, "เริ่มต้น...");
  try {
    const res = await fetch(`/api/ai-subs/${song.id}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(lyrics ? { lyrics } : {}),
    });
    const { job, error } = await res.json();
    if (error) throw new Error(error);
    while (true) {
      await new Promise((r) => setTimeout(r, 1200));
      const st = await (await fetch("/api/status/" + job)).json();
      if (st.status === "error") throw new Error(st.message);
      showProgress(st.progress, st.message);
      if (st.status === "done") { Object.assign(song, st.song); break; }
    }
    ok = true;
    setTimeout(() => { $("progress").style.display = "none"; }, 1500);
    if (currentSong === song) await loadSubs(song);
  } catch (e) {
    showProgress(0, e.message, true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  aiBusy = false;
  return ok;
}
$("aiSync").onclick = () => aiSync(null);

/* ---------- ทำซับเอง: วางเนื้อเพลงแล้วเคาะจังหวะ ---------- */
let tap = null;
function tapUi() {
  const on = !!tap, n = on ? tap.times.length : 0;
  $("tapHit").disabled = !on || n >= tap.lines.length;
  $("tapUndo").disabled = !on || !n;
  $("tapDone").disabled = !on || !n;
  $("tapText").disabled = on;
  $("tapStart").textContent = on ? "เริ่มใหม่ตั้งแต่ต้น" : "เริ่มเคาะจังหวะ";
  $("tapNow").textContent = !on ? ""
    : n < tap.lines.length ? `(${n + 1}/${tap.lines.length})  ${tap.lines[n]}` : "ครบทุกบรรทัดแล้ว กดบันทึก";
}
function tapHit() {
  if (!tap || tap.times.length >= tap.lines.length) return;
  tap.times.push(Math.max(0, video.currentTime - 0.15));   // หักเวลาที่มือช้ากว่าหูเล็กน้อย
  tapUi();
}
$("tapOpen").onclick = () => {
  if (!currentSong) { $("subStatus").textContent = "เปิดเพลงก่อน แล้วค่อยทำซับ"; return; }
  $("tapCard").hidden = false;
  $("tapCard").scrollIntoView({ behavior: "smooth", block: "center" });
};
$("tapClose").onclick = () => { tap = null; tapUi(); $("tapCard").hidden = true; };
$("tapStart").onclick = () => {
  const lines = $("tapText").value.split("\n").map((l) => l.trim()).filter(Boolean);
  if (!currentSong || !lines.length) { $("tapNow").textContent = "วางเนื้อเพลงก่อน"; return; }
  tap = { lines, times: [] };
  preset(() => true);                                 // เปิดเสียงร้องไว้ฟังจังหวะ
  initAudio(); ctx.resume();
  video.currentTime = 0; video.play();
  document.activeElement.blur();
  tapUi();
};
$("tapAi").onclick = async () => {
  const text = $("tapText").value.trim();
  if (!text) { $("tapNow").textContent = "วางเนื้อเพลงก่อน"; return; }
  tap = null; tapUi();
  $("tapNow").textContent = "AI กำลังจับจังหวะ ดูความคืบหน้าที่แถบด้านบน...";
  const ok = await aiSync(text);
  $("tapNow").textContent = ok ? "" : "AI จับจังหวะไม่สำเร็จ ดูข้อความด้านบน หรือลองเคาะเอง";
  if (ok) $("tapCard").hidden = true;
};
$("tapHit").onclick = tapHit;
$("tapUndo").onclick = () => {
  if (!tap || !tap.times.length) return;
  tap.times.pop();
  video.currentTime = tap.times.length ? tap.times[tap.times.length - 1] : 0;
  tapUi();
};
$("tapDone").onclick = async () => {
  if (!tap || !tap.times.length) return;
  const { lines, times } = tap;
  cues = times.map((t, i) => ({ start: t, end: Math.min(i + 1 < times.length ? times[i + 1] : t + 8, t + 12), text: lines[i] }));
  tap = null; tapUi();
  video.pause();
  preset((s) => s.id !== "vocals");
  $("tapCard").hidden = true;
  await saveCues("ซับที่คุณทำเอง");
};

/* ---------- เครื่องเล่น: วิดีโอ (ปิดเสียง) เป็นตัวคุมเวลา เสียง 4 ชิ้นเล่นตาม ---------- */
const fmt = (t) => { t = Math.floor(t || 0); return Math.floor(t / 60) + ":" + String(t % 60).padStart(2, "0"); };
function syncAll(force) {
  STEMS.forEach((s) => {
    if (force || Math.abs(s.audio.currentTime - video.currentTime) > 0.06) s.audio.currentTime = video.currentTime;
  });
}
let wantPlay = false;   // ผู้ใช้ตั้งใจให้เล่นอยู่หรือเปล่า (ต่างจาก video.paused ที่เบราว์เซอร์อาจหยุดเองตอนย่อหน้าต่าง)
function togglePlay() {
  if (!currentSong) return;
  initAudio(); ctx.resume();
  if (wantPlay) {
    wantPlay = false;
    video.pause();
    STEMS.forEach((s) => s.audio.pause());
    $("playBtn").textContent = "▶";
  } else {
    wantPlay = true;
    if (video.ended) video.currentTime = 0;
    video.play();
  }
}
$("playBtn").onclick = togglePlay;
video.onclick = togglePlay;
video.addEventListener("playing", () => { syncAll(true); STEMS.forEach((s) => s.audio.play()); $("playBtn").textContent = "⏸"; });
["pause", "waiting", "ended"].forEach((e) => video.addEventListener(e, () => {
  if (e === "ended") wantPlay = false;
  // ย่อหรือซ่อนหน้าต่าง: เบราว์เซอร์หยุดภาพเองเพื่อประหยัดเครื่อง แต่เสียงเพลงต้องเล่นต่อ
  if (wantPlay && document.hidden) return;
  STEMS.forEach((s) => s.audio.pause());
  if (e !== "waiting") $("playBtn").textContent = "▶";
}));
video.addEventListener("seeked", () => syncAll(true));
video.addEventListener("loadedmetadata", () => { $("seek").max = video.duration; $("dur").textContent = fmt(video.duration); });
video.addEventListener("timeupdate", () => { $("seek").value = video.currentTime; $("cur").textContent = fmt(video.currentTime); });
$("seek").oninput = () => { video.currentTime = +$("seek").value; };
setInterval(() => { if (!video.paused && !document.hidden) syncAll(false); }, 500);
document.addEventListener("visibilitychange", () => {
  if (document.hidden || !wantPlay) return;
  // กลับมาเปิดหน้าต่าง: ให้ภาพตามเสียงที่เล่นล่วงหน้าไปแล้ว
  const t = STEMS[0].audio.currentTime;
  if (Math.abs(video.currentTime - t) > 0.3) video.currentTime = t;
  video.play().catch(() => {});
});
STEMS[0].audio.addEventListener("ended", () => { wantPlay = false; $("playBtn").textContent = "▶"; });
$("fsBtn").onclick = () => document.fullscreenElement ? document.exitFullscreen() : $("player").requestFullscreen();
document.addEventListener("keydown", (e) => {
  const tag = e.target.tagName;
  if (e.code === "Enter" && tap && tag !== "TEXTAREA") { e.preventDefault(); tapHit(); return; }
  if (e.code === "Space" && tag !== "INPUT" && tag !== "BUTTON" && tag !== "TEXTAREA") { e.preventDefault(); togglePlay(); }
});

function loadSong(song) {
  currentSong = song;
  wantPlay = false;
  video.pause();
  STEMS.forEach((s) => s.audio.pause());
  $("playBtn").textContent = "▶";
  video.src = `/media/${song.id}/video.mp4`;
  STEMS.forEach((s) => { s.audio.src = `/media/${song.id}/${s.id}.mp3`; });
  $("empty").style.display = "none";
  $("title").textContent = song.title;
  $("save").disabled = false;
  loadSubs(song);
  renderLibrary();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ---------- สร้างคาราโอเกะ ---------- */
function showProgress(p, msg, error) {
  const box = $("progress");
  box.style.display = "block";
  box.classList.toggle("error", !!error);
  box.querySelector(".fill").style.width = p + "%";
  box.querySelector(".msg").textContent = msg;
}
/* ติดตามงานจนเสร็จ: คืน true ถ้าเพลงใหม่ถูกเก็บเข้าคลังเฉย ๆ เพราะมีเพลงอื่นเปิดอยู่ */
async function followJob(job) {
  while (true) {
    await new Promise((r) => setTimeout(r, 1200));
    const st = await (await fetch("/api/status/" + job)).json();
    if (st.status === "error") throw new Error(st.message);
    showProgress(st.progress, st.message);
    if (st.status === "done") {
      await refreshLibrary();
      if (currentSong) {
        // มีเพลงเปิดอยู่: ไม่แย่งจอ แค่เพิ่มเพลงใหม่เข้าคลังแล้วบอกให้รู้
        showProgress(100, `เพลงใหม่พร้อมแล้ว: ${st.song.title} — กดเลือกได้จากคลังเพลง`);
        return true;
      }
      loadSong(st.song);
      return false;
    }
  }
}
function busy(on) { $("go").disabled = on; $("pick").disabled = on; }

/* ทำคาราโอเกะจากไฟล์ในเครื่อง (ถ้าพิมพ์ชื่อเพลงไว้ในช่องด้านบน จะใช้ชื่อนั้นค้นเนื้อเพลง) */
$("pick").onclick = () => $("pickFile").click();
$("pickFile").onchange = async () => {
  const file = $("pickFile").files[0];
  $("pickFile").value = "";
  if (!file) return;
  const typed = $("url").value.trim(), useTitle = typed && !/^https?:/i.test(typed);
  const data = new FormData();
  data.append("file", file);
  if (useTitle) data.append("title", typed);
  busy(true);
  showProgress(2, `กำลังรับไฟล์ ${file.name} ...`);
  try {
    const { job, error } = await (await fetch("/api/upload", { method: "POST", body: data })).json();
    if (error) throw new Error(error);
    const queued = await followJob(job);
    if (useTitle) { $("url").value = ""; syncUrlClear(); }
    setTimeout(() => { $("progress").style.display = "none"; }, queued ? 8000 : 1500);
  } catch (e) {
    showProgress(0, e.message, true);
  }
  busy(false);
};

$("go").onclick = async () => {
  const url = $("url").value.trim();
  if (!url) { showProgress(0, "กรุณาวางลิงก์ YouTube", true); return; }
  if (!/^https?:/i.test(url)) {
    showProgress(0, "ข้อความในช่องนี้ไม่ใช่ลิงก์ ถ้าจะใช้ไฟล์ในเครื่อง ให้กดปุ่ม ไฟล์ในเครื่อง", true);
    return;
  }
  busy(true);
  let queued = false;
  showProgress(2, "เริ่มต้น...");
  try {
    const res = await fetch("/api/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, height: $("height").value }),
    });
    const { job, error } = await res.json();
    if (error) throw new Error(error);
    queued = await followJob(job);
    $("url").value = "";
    syncUrlClear();
    setTimeout(() => { $("progress").style.display = "none"; }, queued ? 8000 : 1500);
  } catch (e) {
    showProgress(0, e.message, true);
  }
  busy(false);
};
$("url").addEventListener("keydown", (e) => { if (e.key === "Enter") $("go").click(); });

/* ปุ่มกากบาทล้างลิงก์ (โผล่เฉพาะตอนมีข้อความในช่อง) */
function syncUrlClear() { $("urlClear").hidden = !$("url").value; }
$("url").addEventListener("input", syncUrlClear);
$("urlClear").onclick = () => { $("url").value = ""; syncUrlClear(); $("url").focus(); };

/* ---------- บันทึกวิดีโอ ---------- */
$("save").onclick = () => {
  if (!currentSong) return;
  const q = new URLSearchParams({ id: currentSong.id });
  STEMS.forEach((s) => q.set(s.id, s.on ? s.level : 0));
  window.location = "/api/export?" + q;
};

/* ---------- คลังเพลง ---------- */
let librarySongs = [];
let dragId = null;
function renderLibrary() {
  const ul = $("lib"), words = $("libSearch").value.toLowerCase().split(/\s+/).filter(Boolean);
  const found = librarySongs.filter((song) => words.every((w) => song.title.toLowerCase().includes(w)));
  ul.textContent = "";
  if (!found.length) {
    const li = document.createElement("li");
    li.className = "none";
    li.textContent = librarySongs.length ? "ไม่พบเพลงที่ค้นหา" : "ยังไม่มีเพลงที่เคยทำไว้";
    ul.appendChild(li);
  }
  found.forEach((song) => {
    const li = document.createElement("li");
    li.dataset.id = song.id;
    if (currentSong && currentSong.id === song.id) li.classList.add("current");

    // ขีดสามขีด: ลากเพื่อเลื่อนเพลงขึ้นลง (ซ่อนตอนกำลังค้นหา เพราะรายการไม่ครบ)
    const grip = document.createElement("span");
    grip.className = "grip";
    grip.textContent = "≡";
    grip.title = "ลากเพื่อเลื่อนลำดับเพลง";
    grip.hidden = words.length > 0;
    grip.draggable = true;
    grip.addEventListener("dragstart", (e) => {
      dragId = song.id;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", song.id);
      e.dataTransfer.setDragImage(li, 20, 20);
      li.classList.add("dragging");
    });
    grip.addEventListener("dragend", () => { dragId = null; renderLibrary(); });
    li.addEventListener("dragover", (e) => {
      if (!dragId || dragId === song.id) return;
      e.preventDefault();
      const r = li.getBoundingClientRect(), after = e.clientY - r.top > r.height / 2;
      li.classList.toggle("dropAfter", after);
      li.classList.toggle("dropBefore", !after);
    });
    li.addEventListener("dragleave", () => li.classList.remove("dropAfter", "dropBefore"));
    li.addEventListener("drop", (e) => {
      if (!dragId || dragId === song.id) return;
      e.preventDefault();
      const after = li.classList.contains("dropAfter");
      const moved = librarySongs.find((x) => x.id === dragId);
      librarySongs = librarySongs.filter((x) => x.id !== dragId);
      librarySongs.splice(librarySongs.findIndex((x) => x.id === song.id) + (after ? 1 : 0), 0, moved);
      dragId = null;
      renderLibrary();
      fetch("/api/library/order", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: librarySongs.map((x) => x.id) }),
      }).catch(() => {});
    });

    const b = document.createElement("button");
    b.className = "song";
    b.textContent = "♪  " + song.title;
    b.title = song.title;
    b.onclick = () => loadSong(song);

    // ปุ่มลบ: เปิดหน้าต่างถามยืนยันก่อนลบจริง
    const del = document.createElement("button");
    del.className = "del";
    del.textContent = "🗑";
    del.title = "ลบเพลงนี้ออกจากคลัง";
    del.onclick = () => {
      delTarget = song;
      $("delName").textContent = song.title;
      $("delDlg").showModal();
    };

    li.append(grip, b, del);
    ul.appendChild(li);
  });
}
let delTarget = null, libMsgTimer = 0;
function libMessage(text, bad) {
  const m = $("libMsg");
  m.textContent = text;
  m.classList.toggle("bad", !!bad);
  m.hidden = false;
  clearTimeout(libMsgTimer);
  libMsgTimer = setTimeout(() => { m.hidden = true; }, bad ? 12000 : 5000);
}
$("delNo").onclick = () => $("delDlg").close();
$("delYes").onclick = async () => {
  const song = delTarget;
  $("delDlg").close();
  if (!song) return;
  if (currentSong && currentSong.id === song.id) unloadSong();
  try {
    const res = await fetch(`/api/library/${song.id}`, { method: "DELETE" });
    if (res.ok) libMessage(`ลบเพลง "${song.title}" แล้ว`);
    else libMessage(await res.text(), true);
  } catch (e) {
    libMessage("ลบไม่สำเร็จ: ติดต่อโปรแกรมเบื้องหลังไม่ได้ ลองปิดแล้วเปิดแอปใหม่", true);
  }
  refreshLibrary();
};
function unloadSong() {
  currentSong = null;
  wantPlay = false;
  cues = [];
  [video, ...STEMS.map((s) => s.audio)].forEach((m) => { m.pause(); m.removeAttribute("src"); m.load(); });
  $("playBtn").textContent = "▶";
  $("empty").style.display = "";
  $("title").textContent = "";
  $("save").disabled = true;
  $("subStatus").textContent = "ยังไม่มีเพลง";
  renderLibrary();
}
async function refreshLibrary() {
  librarySongs = await (await fetch("/api/library")).json();
  renderLibrary();
}
$("libSearch").oninput = renderLibrary;

/* ---------- แสดงว่าเครื่องประมวลผลด้วยอะไร ---------- */
const GPU_WHY = {
  no_nvidia: "การ์ดจอ (เครื่องนี้ไม่มีการ์ดจอ NVIDIA)",
  cpu_build: "การ์ดจอ (ยังไม่ได้ติดตั้งส่วนเสริม กดปุ่ม เปิดใช้การ์ดจอ)",
  driver: "การ์ดจอ (ไดรเวอร์ NVIDIA เก่าเกินไป ต้องอัปเดตไดรเวอร์)",
  error: "การ์ดจอ (ตรวจสอบเครื่องไม่สำเร็จ)",
};
function renderDevice(d) {
  const sel = $("device"), hasGpu = d.name === "gpu";
  sel.textContent = "";
  [
    ["auto", "ประมวลผล: อัตโนมัติ (" + (hasGpu ? "การ์ดจอ" : "CPU") + ")", false],
    ["gpu", hasGpu ? "ประมวลผล: การ์ดจอ (เร็ว)" : (GPU_WHY[d.reason] || "การ์ดจอ (เครื่องนี้ใช้ไม่ได้)"), !hasGpu],
    ["cpu", "ประมวลผล: CPU (ช้า)", false],
  ].forEach(([value, label, off]) => {
    const o = document.createElement("option");
    o.value = value; o.textContent = label; o.disabled = off;
    sel.appendChild(o);
  });
  sel.value = d.choice === "gpu" && !hasGpu ? "auto" : d.choice;
  sel.disabled = false;
  sel.className = "chip " + (d.using === "gpu" ? "gpu" : "cpu");
  $("gpuFix").hidden = d.reason !== "cpu_build";
}
async function showDevice() {
  // การตรวจเครื่องครั้งแรกอาจนานเป็นนาที จึงรอจนกว่าจะรู้ผลจริง ไม่เดาว่าเป็น CPU
  for (let i = 0; i < 150; i++) {
    let d = null;
    try { d = await (await fetch("/api/device")).json(); } catch (e) {}
    if (d && d.name !== "unknown") { renderDevice(d); return; }
    if (d && d.reason === "error") { renderDevice({ ...d, name: "cpu" }); return; }
    await new Promise((r) => setTimeout(r, 2000));
  }
}
$("gpuFix").onclick = async () => {
  $("gpuFix").disabled = true;
  showProgress(5, "เริ่มต้น...");
  try {
    const { job } = await (await fetch("/api/enable-gpu", { method: "POST" })).json();
    while (true) {
      await new Promise((r) => setTimeout(r, 1500));
      const st = await (await fetch("/api/status/" + job)).json();
      if (st.status === "error") throw new Error(st.message);
      showProgress(st.progress, st.message);
      if (st.status === "done") break;
    }
    setTimeout(() => { $("progress").style.display = "none"; }, 2500);
  } catch (e) {
    showProgress(0, e.message, true);
  }
  $("gpuFix").disabled = false;
  showDevice();
};
$("device").onchange = async () => {
  const res = await fetch("/api/device", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ choice: $("device").value }),
  });
  renderDevice(await res.json());
};

/* ---------- ตรวจหาอัปเดต ---------- */
let updItems = null;
function renderUpdates() {
  const btn = $("updBtn"), box = $("updBox");
  const n = updItems ? updItems.filter((i) => i.update).length : 0;
  btn.textContent = n ? `มีอัปเดตใหม่ (${n})` : "ตรวจหาอัปเดต";
  btn.classList.toggle("has", n > 0);
  box.textContent = "";
  if (!updItems) { box.textContent = "ตรวจหาอัปเดตไม่ได้ ลองเช็กอินเทอร์เน็ตแล้วกดอีกครั้ง"; return; }
  updItems.forEach((i) => {
    const row = document.createElement("div"), a = document.createElement("span"), b = document.createElement("span");
    row.className = "row";
    a.textContent = i.label;
    if (!i.latest) { b.textContent = (i.installed || "ยังไม่ได้ติดตั้ง") + " · เช็กรุ่นล่าสุดไม่ได้"; b.className = "ok"; }
    else if (!i.installed) { b.textContent = "ยังไม่ได้ติดตั้ง → " + i.latest; b.className = "new"; }
    else if (i.update) { b.textContent = i.installed + " → " + i.latest; b.className = "new"; }
    else { b.textContent = i.installed + " · ล่าสุดแล้ว"; b.className = "ok"; }
    row.append(a, b);
    box.appendChild(row);
  });
  if (n) {
    const go = document.createElement("button");
    go.className = "go";
    go.textContent = "อัปเดตเลย";
    go.onclick = () => installUpdates(go);
    box.appendChild(go);
  }
}
async function checkUpdates() {
  $("updBtn").textContent = "กำลังตรวจ...";
  try { updItems = (await (await fetch("/api/updates")).json()).items; } catch (e) { updItems = null; }
  renderUpdates();
}
async function installUpdates(button) {
  button.disabled = true;
  showProgress(5, "เริ่มอัปเดต...");
  try {
    const { job } = await (await fetch("/api/updates", { method: "POST" })).json();
    while (true) {
      await new Promise((r) => setTimeout(r, 1200));
      const st = await (await fetch("/api/status/" + job)).json();
      if (st.status === "error") throw new Error(st.message);
      showProgress(st.progress, st.message);
      if (st.status === "done") break;
    }
    setTimeout(() => { $("progress").style.display = "none"; }, 2500);
  } catch (e) {
    showProgress(0, e.message, true);
  }
  await checkUpdates();
}
$("updBtn").onclick = async () => {
  if (!$("updBox").hidden) { $("updBox").hidden = true; return; }
  await checkUpdates();
  $("updBox").hidden = false;
};

$("quitBtn").onclick = async () => {
  try { await fetch("/api/quit", { method: "POST" }); } catch (e) {}
  window.close();
  document.body.innerHTML = '<p style="text-align:center;margin-top:30vh;font-size:20px">ปิดโปรแกรมแล้ว ปิดแท็บนี้ได้เลย</p>';
};

/* ---------- เปลี่ยนหน้าตาของแอป (จำค่าไว้ในเครื่อง) ---------- */
$("look").value = document.documentElement.dataset.look || "";
$("look").onchange = () => {
  const look = $("look").value;
  if (look) document.documentElement.dataset.look = look;
  else delete document.documentElement.dataset.look;
  try { localStorage.setItem("look", look); } catch (e) {}
};

/* บอกโปรแกรมเบื้องหลังว่าหน้าต่างยังเปิดอยู่ และบอกลาเมื่อปิดหน้าต่าง */
const heartbeat = () => fetch("/api/alive", { method: "POST" }).catch(() => {});
heartbeat();
setInterval(heartbeat, 10000);
window.addEventListener("pagehide", () => navigator.sendBeacon("/api/bye"));

apply();
checkUpdates();
subUi();
refreshLibrary();
showDevice();
</script>
</body>
</html>
"""

def prepare():
    """ตัวติดตั้งเรียกใช้: ดาวน์โหลดสิ่งที่ปกติจะโหลดตอนทำเพลงแรก จะได้ใช้งานได้ทันทีหลังติดตั้ง
    (พิมพ์เป็นภาษาอังกฤษล้วน เพราะตัวติดตั้งอ่านผ่านท่อที่แสดงภาษาไทยไม่ได้)"""
    def attempt(label, work):
        print(f"Downloading {label}...", flush=True)
        try:
            work()
            print("   ok", flush=True)
        except Exception as e:  # ไม่สำเร็จก็ไม่เป็นไร แอปจะโหลดเองตอนใช้งาน
            print(f"   skipped ({type(e).__name__}); the app will fetch it on first use", flush=True)

    def demucs_model():
        from demucs.pretrained import get_model
        get_model("htdemucs")

    def whisper_model():
        import torch
        import whisper
        name = "large-v3" if torch.cuda.is_available() else "small"
        root = os.path.join(os.getenv("XDG_CACHE_HOME", os.path.join(os.path.expanduser("~"), ".cache")), "whisper")
        os.makedirs(root, exist_ok=True)
        whisper._download(whisper._MODELS[name], root, False)

    attempt("the YouTube helper (about 40 MB)", ensure_deno)
    attempt("the vocal remover model (about 80 MB)", demucs_model)
    attempt("the lyric timing model (0.5 to 3 GB)", whisper_model)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--prepare":
        prepare()
        sys.exit(0)
    if len(sys.argv) >= 4 and sys.argv[1] == "--whisper":
        whisper_worker(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "",
                       sys.argv[5] if len(sys.argv) > 5 else "")
        sys.exit(0)
    sweep_library()
    threading.Thread(target=detect_device, daemon=True).start()
    threading.Thread(target=ensure_deno, daemon=True).start()
    running = next((p for p in PORTS if is_karaoke(p)), None)
    if running:  # เปิดแอปซ้ำ: แค่เปิดหน้าต่างไปที่ตัวที่รันอยู่แล้ว
        URL = f"http://127.0.0.1:{running}"
        open_window(quit_when_closed=False)
        sys.exit(0)
    port = next((p for p in PORTS if port_free(p)), PORTS[0])
    URL = f"http://127.0.0.1:{port}"
    threading.Timer(1.0, open_window).start()
    threading.Thread(target=watchdog, daemon=True).start()
    app.run(host="127.0.0.1", port=port, threaded=True)
