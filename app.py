#!/usr/bin/env python3
"""
قناة حسين التلفزيونية — بث مباشر طويل من قائمة تشغيل فيديوهات.

الفكرة: تطبيق ويب بسيط يعمل على سحابة مجانية (Render). ترفع فيديوهات
أو تضيف روابطها، ترتّبها في قائمة تشغيل، تدخل رابط خادم RTMP ومفتاح
البث من فيسبوك، وتضغط «ابدأ البث» — فيعيد ffmpeg تشغيل القائمة في
حلقة مستمرة ويدفعها إلى فيسبوك لايف، بلا حد 15 دقيقة وبلا Loop مدفوع.

أمان:
- مفتاح البث يُدخل من الواجهة وقت التشغيل فقط، ويُحفظ في ذاكرة
  العملية فقط — لا يُكتب في أي ملف ولا في أي سجل أبداً.
- إن ضُبط متغير البيئة ADMIN_TOKEN، فكل أوامر الواجهة تتطلب رمزاً
  يُدخل في حقل أعلى الصفحة (يُحفظ في متصفحك فقط، وليس في أي ملف).

متغيرات البيئة:
  PORT         : المنفذ (Render يضبطه تلقائياً، افتراضي 5000)
  ADMIN_TOKEN  : رمز حماية لوحة التحكم (اختياري محلياً، يُولَّد
                 تلقائياً في render.yaml)
  MAX_UPLOAD_MB: أكبر حجم رفع بالميجا (افتراضي 500)
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request, render_template_string

BASE = os.path.dirname(os.path.abspath(__file__))
VIDEOS_DIR = os.path.join(BASE, "videos")
PLAYLIST_FILE = os.path.join(BASE, "playlist.json")
PORT = int(os.environ.get("PORT", "5000"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))
ALLOWED_EXTS = {".mp4", ".mov", ".mkv", ".webm"}

os.makedirs(VIDEOS_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

_lock = threading.Lock()
_playlist = []          # قائمة dicts: id, name, filename, source_url, duration, width, height, fps, vcodec, acodec, has_audio, size
_log = deque(maxlen=60)  # آخر سطور سجل ffmpeg
_state = {
    "running": False,
    "current": None,        # اسم الفيديو الحالي
    "current_started_at": None,
    "started_at": None,
    "retries": 0,
    "last_error": None,
}
_stream_thread = None
_stop_event = threading.Event()
_current_proc = None


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    _log.append(line)
    print(line, flush=True)


# ---------- قائمة التشغيل: حفظ/تحميل/فحص ----------

def _save_playlist():
    tmp = PLAYLIST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_playlist, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PLAYLIST_FILE)


def probe_video(path):
    """يفحص ملف فيديو بـ ffprobe ويعيد مواصفاته أو None إن تعذّر."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format",
             "-of", "json", path],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout)
        v = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "video"), None)
        a = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "audio"), None)
        if not v:
            return None
        fps_raw = v.get("r_frame_rate", "30/1")
        try:
            num, den = fps_raw.split("/")
            fps = round(int(num) / int(den)) if int(den) else 30
        except Exception:
            fps = 30
        return {
            "duration": float(data.get("format", {}).get("duration", 0) or 0),
            "width": int(v.get("width", 0)),
            "height": int(v.get("height", 0)),
            "fps": fps,
            "vcodec": v.get("codec_name", ""),
            "acodec": a.get("codec_name", "") if a else "",
            "has_audio": a is not None,
            "size": os.path.getsize(path),
        }
    except Exception as e:
        log(f"تعذّر فحص {os.path.basename(path)}: {e}")
        return None


def add_entry(filename, source_url=None):
    """يضيف ملفاً موجوداً في videos/ إلى القائمة بعد فحصه."""
    path = os.path.join(VIDEOS_DIR, filename)
    info = probe_video(path)
    if not info:
        return None
    entry = {
        "id": uuid.uuid4().hex[:8],
        "name": filename,
        "filename": filename,
        "source_url": source_url,
        "added_at": time.strftime("%Y-%m-%d %H:%M"),
        **info,
    }
    with _lock:
        _playlist.append(entry)
        _save_playlist()
    log(f"أُضيف إلى القائمة: {filename} ({info['width']}x{info['height']}، {info['duration']:.0f} ثانية)")
    return entry


def load_playlist():
    global _playlist
    if os.path.exists(PLAYLIST_FILE):
        try:
            with open(PLAYLIST_FILE, encoding="utf-8") as f:
                _playlist = json.load(f)
        except Exception:
            _playlist = []
    # استيراد أي ملف فيديو في videos/ ليس في القائمة بعد
    known = {e["filename"] for e in _playlist}
    for fn in sorted(os.listdir(VIDEOS_DIR)):
        if os.path.splitext(fn)[1].lower() in ALLOWED_EXTS and fn not in known:
            add_entry(fn)
    # حذف إدخالات ملفاتها غير موجودة (ما لم يكن لها رابط مصدر للاستعادة)
    missing = [e for e in _playlist
               if not os.path.exists(os.path.join(VIDEOS_DIR, e["filename"]))]
    if missing:
        with _lock:
            _playlist = [e for e in _playlist
                         if os.path.exists(os.path.join(VIDEOS_DIR, e["filename"]))
                         or e.get("source_url")]
            _save_playlist()
        for e in missing:
            if e.get("source_url"):
                log(f"ملف مفقود وسيُستعاد من رابطه: {e['filename']}")


def restore_missing():
    """يعيد تنزيل الفيديوهات المفقودة التي لها رابط مصدر (بعد إعادة تشغيل الخدمة)."""
    for entry in list(_playlist):
        path = os.path.join(VIDEOS_DIR, entry["filename"])
        if not os.path.exists(path) and entry.get("source_url"):
            log(f"استعادة {entry['filename']} من رابطه...")
            if download_url(entry["source_url"], entry["filename"]):
                log(f"تمت استعادة {entry['filename']}")
            else:
                log(f"فشلت استعادة {entry['filename']}")


def download_url(url, filename=None):
    """ينزّل ملف فيديو من رابط مباشر إلى videos/. يعيد اسم الملف أو None."""
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "video" not in ctype and "octet-stream" not in ctype:
                log(f"الرابط لا يبدو ملف فيديو (Content-Type: {ctype})")
                return None
            if not filename:
                name = os.path.basename(urlparse(url).path) or "video.mp4"
                filename = re.sub(r"[^A-Za-z0-9_.\-]", "_", name)
                if os.path.splitext(filename)[1].lower() not in ALLOWED_EXTS:
                    filename += ".mp4"
            dest = os.path.join(VIDEOS_DIR, filename)
            # تجنّب الكتابة فوق ملف موجود باسم آخر
            base, ext = os.path.splitext(filename)
            i = 1
            while os.path.exists(dest):
                dest = os.path.join(VIDEOS_DIR, f"{base}-{i}{ext}")
                filename = os.path.basename(dest)
                i += 1
            total = 0
            with open(dest, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    total += len(chunk)
                    if total > 1024 * 1024 * 1024:  # حد 1GB
                        out.close()
                        os.remove(dest)
                        log("الملف أكبر من 1GB — أُلغي التنزيل")
                        return None
        log(f"نُزّل {filename} ({total / 1024 / 1024:.1f}MB)")
        return filename
    except Exception as e:
        log(f"فشل تنزيل الرابط: {e}")
        return None

# ---------- مشغّل البث ----------

def build_ffmpeg_cmd(path, target, dest):
    """يبني أمر ffmpeg لملف واحد.
    - إن طابق الملف مواصفات الهدف (h264+aac ونفس الأبعاد ومعدل الإطارات)
      يُنسخ مباشرة (-c copy) لتوفير المعالج على الخطة المجانية.
    - وإلا يُعاد ترميزه إلى مواصفات الهدف (توسيط مع أشرطة سوداء إن لزم)."""
    info = probe_video(path)
    tw, th, tfps = target["width"], target["height"], target["fps"]
    match = (
        info is not None
        and info["vcodec"] == "h264"
        and info["acodec"] == "aac"
        and info["width"] == tw
        and info["height"] == th
        and info["fps"] == tfps
        and info["has_audio"]
    )
    if match:
        log(f"نسخ مباشر (بلا إعادة ترميز): {os.path.basename(path)}")
        return ["ffmpeg", "-re", "-i", path,
                "-c:v", "copy", "-c:a", "copy",
                "-f", "flv", dest]
    log(f"إعادة ترميز إلى {tw}x{th} @{tfps}fps: {os.path.basename(path)}")
    cmd = ["ffmpeg", "-re", "-i", path]
    if info is not None and not info["has_audio"]:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += [
        "-vf", f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
               f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,fps={tfps}",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", "1500k", "-maxrate", "1800k", "-bufsize", "3000k",
        "-r", str(tfps),
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-f", "flv", dest,
    ]
    return cmd


def _drain_stderr(proc):
    """يقرأ سجل ffmpeg سطراً بسطر إلى الذاكرة (للعرض في الواجهة)."""
    try:
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith("frame="):
                continue  # سطور تقدّم الترميز (تتكرر كل إطار)
            if "Failed to update header" in line:
                continue  # ضجيج معروف من حاوية FLV أثناء البث
            if line and ("error" in line.lower() or "failed" in line.lower()
                         or "Duration" in line):
                _log.append(line[-200:])
    except Exception:
        pass


def stream_worker(rtmp_url, stream_key):
    """حلقة البث: يعيد تشغيل القائمة كاملة مراراً حتى يُطلب الإيقاف.
    مفتاح البث يبقى في ذاكرة هذه الدالة فقط — لا يُحفظ أبداً."""
    global _current_proc
    dest = rtmp_url.rstrip("/") + "/" + stream_key
    with _lock:
        playlist = list(_playlist)
    if not playlist:
        _state["last_error"] = "قائمة التشغيل فارغة"
        _state["running"] = False
        return
    target = {"width": playlist[0]["width"], "height": playlist[0]["height"],
              "fps": playlist[0]["fps"]}
    log(f"بدء البث إلى {rtmp_url} — الهدف {target['width']}x{target['height']} @{target['fps']}fps")
    _state["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _state["retries"] = 0
    _state["last_error"] = None
    while not _stop_event.is_set():
        for entry in playlist:
            if _stop_event.is_set():
                break
            path = os.path.join(VIDEOS_DIR, entry["filename"])
            if not os.path.exists(path):
                log(f"تخطّي ملف مفقود: {entry['filename']}")
                continue
            _state["current"] = entry["name"]
            _state["current_started_at"] = time.strftime("%H:%M:%S")
            cmd = build_ffmpeg_cmd(path, target, dest)
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, text=False)
                _current_proc = proc
                threading.Thread(target=_drain_stderr, args=(proc,),
                                 daemon=True).start()
                while proc.poll() is None and not _stop_event.is_set():
                    time.sleep(0.5)
                if _stop_event.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
                rc = proc.returncode
                if rc != 0:
                    _state["retries"] += 1
                    _state["last_error"] = f"خرج ffmpeg بالرمز {rc} أثناء {entry['name']}"
                    log(f"ffmpeg خرج بالرمز {rc} — إعادة المحاولة بعد 3 ثوانٍ")
                    time.sleep(3)
                else:
                    _state["retries"] = 0
            except Exception as e:
                _state["last_error"] = str(e)
                log(f"خطأ في تشغيل ffmpeg: {e}")
                time.sleep(3)
        # نهاية القائمة: أعد من البداية (حلقة مستمرة)
        if not _stop_event.is_set():
            log("اكتملت القائمة — إعادة من البداية")
    _current_proc = None
    _state["running"] = False
    _state["current"] = None
    log("توقف البث")


# ---------- المصادقة ----------

def require_token():
    """إن ضُبط ADMIN_TOKEN فكل طلبات API المتغيّرة تتطلبه في ترويسة."""
    if not ADMIN_TOKEN:
        return None
    if request.path == "/health" or request.method == "GET" and request.path == "/":
        return None
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "رمز الحماية غير صحيح"}), 401
    return None


@app.before_request
def _auth_gate():
    if request.path.startswith("/api/"):
        return require_token()


# ---------- الصفحات والواجهة ----------

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/status")
def status():
    with _lock:
        running = _state["running"]
        current = _state["current"]
        started = _state["started_at"]
    elapsed = None
    if running and started:
        try:
            t0 = time.mktime(time.strptime(started, "%Y-%m-%d %H:%M:%S"))
            elapsed = int(time.time() - t0)
        except Exception:
            elapsed = None
    total = sum(e.get("duration", 0) for e in _playlist)
    return jsonify({
        "ok": True,
        "running": running,
        "current": current,
        "current_started_at": _state["current_started_at"],
        "started_at": started,
        "elapsed_seconds": elapsed,
        "retries": _state["retries"],
        "last_error": _state["last_error"],
        "log": list(_log)[-25:],
        "playlist_count": len(_playlist),
        "playlist_total_seconds": int(total),
        "needs_token": bool(ADMIN_TOKEN),
    })


@app.route("/api/playlist")
def playlist():
    return jsonify({"ok": True, "playlist": _playlist})


@app.route("/api/videos/upload", methods=["POST"])
def upload_video():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "لم يُرفع أي ملف"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        return jsonify({"ok": False, "error": f"صيغة غير مدعومة: {ext}"}), 400
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", f.filename)
    dest = os.path.join(VIDEOS_DIR, safe)
    if os.path.exists(dest):
        base, e = os.path.splitext(safe)
        safe = f"{base}-{uuid.uuid4().hex[:4]}{e}"
        dest = os.path.join(VIDEOS_DIR, safe)
    f.save(dest)
    entry = add_entry(safe)
    if not entry:
        os.remove(dest)
        return jsonify({"ok": False, "error": "تعذّر قراءة الفيديو — تأكد أنه ملف h264+aac صالح"}), 400
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/videos/add-url", methods=["POST"])
def add_url():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "الرابط يجب أن يبدأ بـ http:// أو https://"}), 400
    filename = download_url(url)
    if not filename:
        return jsonify({"ok": False, "error": "تعذّر تنزيل الرابط — يجب أن يكون رابط ملف فيديو مباشر (.mp4 مثلاً)"}), 400
    entry = add_entry(filename, source_url=url)
    if not entry:
        os.remove(os.path.join(VIDEOS_DIR, filename))
        return jsonify({"ok": False, "error": "تعذّر قراءة الفيديو بعد التنزيل"}), 400
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/videos/restore", methods=["POST"])
def restore():
    threading.Thread(target=restore_missing, daemon=True).start()
    return jsonify({"ok": True, "message": "بدأت الاستعادة في الخلفية"})


@app.route("/api/videos/<vid>/move", methods=["POST"])
def move_video(vid):
    data = request.get_json(force=True, silent=True) or {}
    direction = data.get("direction")
    with _lock:
        idx = next((i for i, e in enumerate(_playlist) if e["id"] == vid), None)
        if idx is None:
            return jsonify({"ok": False, "error": "غير موجود"}), 404
        if direction == "up" and idx > 0:
            _playlist[idx - 1], _playlist[idx] = _playlist[idx], _playlist[idx - 1]
        elif direction == "down" and idx < len(_playlist) - 1:
            _playlist[idx + 1], _playlist[idx] = _playlist[idx], _playlist[idx + 1]
        else:
            return jsonify({"ok": False, "error": "لا يمكن النقل"}), 400
        _save_playlist()
    return jsonify({"ok": True})


@app.route("/api/videos/<vid>", methods=["DELETE"])
def delete_video(vid):
    with _lock:
        idx = next((i for i, e in enumerate(_playlist) if e["id"] == vid), None)
        if idx is None:
            return jsonify({"ok": False, "error": "غير موجود"}), 404
        entry = _playlist.pop(idx)
        _save_playlist()
    try:
        os.remove(os.path.join(VIDEOS_DIR, entry["filename"]))
    except OSError:
        pass
    log(f"حُذف من القائمة: {entry['name']}")
    return jsonify({"ok": True})


@app.route("/api/stream/start", methods=["POST"])
def start_stream():
    global _stream_thread
    if _state["running"]:
        return jsonify({"ok": False, "error": "البث يعمل بالفعل"}), 409
    data = request.get_json(force=True, silent=True) or {}
    rtmp_url = (data.get("rtmp_url") or "").strip()
    stream_key = (data.get("stream_key") or "").strip()
    if not rtmp_url.startswith(("rtmp://", "rtmps://")):
        return jsonify({"ok": False, "error": "رابط الخادم يجب أن يبدأ بـ rtmp:// أو rtmps://"}), 400
    if not stream_key:
        return jsonify({"ok": False, "error": "أدخل مفتاح البث"}), 400
    if not _playlist:
        return jsonify({"ok": False, "error": "قائمة التشغيل فارغة — أضف فيديو أولاً"}), 400
    _stop_event.clear()
    _state["running"] = True
    _stream_thread = threading.Thread(
        target=stream_worker, args=(rtmp_url, stream_key), daemon=True)
    _stream_thread.start()
    return jsonify({"ok": True, "message": "بدأ البث"})


@app.route("/api/stream/stop", methods=["POST"])
def stop_stream():
    global _current_proc
    if not _state["running"]:
        return jsonify({"ok": True, "message": "البث متوقف أصلاً"})
    _stop_event.set()
    if _current_proc is not None and _current_proc.poll() is None:
        _current_proc.terminate()
    return jsonify({"ok": True, "message": "جارٍ إيقاف البث..."})

PAGE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>قناة حسين التلفزيونية 📺</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: system-ui, "Segoe UI", Tahoma, sans-serif; background: #101418; color: #eef2f5; margin: 0; padding: 16px; }
  .wrap { max-width: 860px; margin: 0 auto; }
  h1 { font-size: 24px; margin: 4px 0 2px; }
  .sub { color: #9fb0bd; margin-bottom: 18px; }
  .card { background: #1a2129; border: 1px solid #2a343f; border-radius: 12px; padding: 16px; margin-bottom: 16px; }
  .card h2 { font-size: 18px; margin: 0 0 12px; }
  .badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-weight: bold; font-size: 14px; }
  .on { background: #d21f3c; color: #fff; }
  .off { background: #3a4653; color: #cfd8e0; }
  label { display: block; font-size: 13px; color: #9fb0bd; margin: 10px 0 4px; }
  input[type=text], input[type=password], input[type=url] { width: 100%; padding: 10px; border-radius: 8px; border: 1px solid #33404d; background: #0f1419; color: #fff; font-size: 15px; direction: ltr; text-align: left; }
  button { padding: 10px 18px; border: none; border-radius: 8px; font-size: 15px; font-weight: bold; cursor: pointer; margin: 4px 2px; }
  .go { background: #d21f3c; color: #fff; }
  .stop { background: #555; color: #fff; }
  .small { background: #243447; color: #cfe0ef; padding: 6px 12px; font-size: 13px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  td, th { padding: 8px 4px; border-bottom: 1px solid #2a343f; text-align: right; }
  .muted { color: #9fb0bd; font-size: 13px; }
  pre { background: #0b0f13; border: 1px solid #2a343f; border-radius: 8px; padding: 10px; font-size: 12px; max-height: 180px; overflow: auto; direction: ltr; text-align: left; white-space: pre-wrap; }
  .note { background: #2a2118; border: 1px solid #6b5223; color: #f0d9a0; border-radius: 8px; padding: 10px; font-size: 13px; margin-top: 10px; }
  #msg { min-height: 20px; color: #8fd6a0; font-size: 14px; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: end; }
  .row > div { flex: 1; min-width: 200px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>📺 قناة حسين التلفزيونية</h1>
  <div class="sub">بث مباشر طويل من قائمة تشغيل — يعيد الفيديوهات في حلقة مستمرة إلى فيسبوك لايف</div>

  <div class="card" id="tokenCard" style="display:none">
    <h2>🔐 رمز الحماية</h2>
    <label>أدخل رمز لوحة التحكم (من متغير ADMIN_TOKEN في Render)</label>
    <input type="password" id="token" placeholder="ADMIN_TOKEN">
    <button class="small" onclick="saveToken()">حفظ في هذا المتصفح</button>
  </div>

  <div class="card">
    <h2>حالة البث <span id="badge" class="badge off">متوقف</span></h2>
    <div id="statusLine" class="muted">لا يوجد بث جارٍ</div>
    <div id="errLine" style="color:#ff9a9a;font-size:13px"></div>
    <label>رابط خادم RTMP (من فيسبوك Live Producer)</label>
    <input type="text" id="rtmpUrl" value="rtmps://live-api-s.facebook.com:443/rtmp/">
    <label>مفتاح البث (يُستخدم الآن فقط ولا يُحفظ في أي مكان)</label>
    <input type="password" id="streamKey" placeholder="مفتاح البث الدائم من فيسبوك">
    <div style="margin-top:12px">
      <button class="go" onclick="startStream()">▶ ابدأ البث</button>
      <button class="stop" onclick="stopStream()">⏹ إيقاف البث</button>
    </div>
    <div id="msg"></div>
    <label>سجل البث (آخر الأحداث)</label>
    <pre id="log">—</pre>
  </div>

  <div class="card">
    <h2>🎬 قائمة التشغيل <span class="muted" id="totalLine"></span></h2>
    <div class="muted">تُبث الفيديوهات بالترتيب، ثم تعود من البداية تلقائياً. أول فيديو يحدد مقاس البث (الطولي 720×1280 أو العرضي).</div>
    <table id="plist"><thead><tr><th>#</th><th>الفيديو</th><th>المدة</th><th>المقاس</th><th></th></tr></thead><tbody></tbody></table>
    <div style="margin-top:10px">
      <button class="small" onclick="restoreVideos()">↻ استعادة الفيديوهات المفقودة (من روابطها)</button>
    </div>
  </div>

  <div class="card">
    <h2>➕ إضافة فيديو</h2>
    <div class="row">
      <div>
        <label>رفع ملف من جهازك (mp4 / mov / mkv — حتى 500MB)</label>
        <input type="file" id="fileInput" accept="video/*">
        <button class="small" onclick="uploadFile()">رفع وإضافة للقائمة</button>
      </div>
      <div>
        <label>أو لصق رابط ملف فيديو مباشر (.mp4)</label>
        <input type="url" id="urlInput" placeholder="https://.../video.mp4">
        <button class="small" onclick="addUrl()">تنزيل وإضافة للقائمة</button>
      </div>
    </div>
    <div class="note">⚠️ الملفات المرفوعة تُفقد عند إعادة تشغيل الخدمة على الخطة المجانية. الفيديوهات المضافة برابط يمكن استعادتها بزر «استعادة» أعلاه، لأن رابطها محفوظ في القائمة.</div>
  </div>
</div>

<script>
let token = sessionStorage.getItem("tv_token") || "";
function headers(extra) {
  let h = Object.assign({}, extra || {});
  if (token) h["X-Admin-Token"] = token;
  return h;
}
function showMsg(t, ok) {
  let el = document.getElementById("msg");
  el.textContent = t;
  el.style.color = ok === false ? "#ff9a9a" : "#8fd6a0";
}
function saveToken() {
  token = document.getElementById("token").value.trim();
  sessionStorage.setItem("tv_token", token);
  showMsg("تم حفظ الرمز في هذا المتصفح");
  refresh();
}
async function api(path, opts) {
  let r = await fetch(path, opts || {});
  let j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("خطأ " + r.status));
  return j;
}
function fmt(s) {
  s = Math.floor(s || 0);
  let h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? h + ":" : "") + String(m).padStart(2, "0") + ":" + String(sec).padStart(2, "0");
}
async function refresh() {
  try {
    let s = await api("/api/status");
    document.getElementById("tokenCard").style.display = s.needs_token ? "block" : "none";
    let b = document.getElementById("badge");
    b.textContent = s.running ? "● مباشر الآن" : "متوقف";
    b.className = "badge " + (s.running ? "on" : "off");
    document.getElementById("statusLine").textContent = s.running
      ? ("الفيديو الحالي: " + (s.current || "—") + " • منذ " + fmt(s.elapsed_seconds) + " • بدأ " + (s.started_at || ""))
      : "لا يوجد بث جارٍ";
    document.getElementById("errLine").textContent = s.last_error ? ("آخر خطأ: " + s.last_error) : "";
    document.getElementById("log").textContent = (s.log || []).join("\n") || "—";
    document.getElementById("totalLine").textContent = s.playlist_count
      ? ("• " + s.playlist_count + " فيديوهات • المجموع " + fmt(s.playlist_total_seconds) + " • الدورة تعيد نفسها بلا توقف")
      : "";
    let p = await api("/api/playlist");
    let tb = document.querySelector("#plist tbody");
    tb.innerHTML = "";
    p.playlist.forEach((v, i) => {
      let tr = document.createElement("tr");
      let cur = (s.running && s.current === v.name) ? " ▶" : "";
      tr.innerHTML = "<td>" + (i + 1) + "</td><td>" + v.name + cur + "</td><td>" + fmt(v.duration) + "</td><td>" + v.width + "×" + v.height + "</td>"
        + "<td><button class='small' onclick='moveVideo(\"" + v.id + "\",\"up\")'>↑</button>"
        + "<button class='small' onclick='moveVideo(\"" + v.id + "\",\"down\")'>↓</button>"
        + "<button class='small' onclick='delVideo(\"" + v.id + "\")'>حذف</button></td>";
      tb.appendChild(tr);
    });
  } catch (e) {
    if (String(e.message).indexOf("رمز الحماية") >= 0) {
      document.getElementById("tokenCard").style.display = "block";
      showMsg("أدخل رمز الحماية أعلى الصفحة", false);
    }
  }
}
async function startStream() {
  try {
    let rtmp = document.getElementById("rtmpUrl").value.trim();
    let key = document.getElementById("streamKey").value.trim();
    await api("/api/stream/start", { method: "POST", headers: headers({"Content-Type": "application/json"}),
      body: JSON.stringify({ rtmp_url: rtmp, stream_key: key }) });
    document.getElementById("streamKey").value = "";
    showMsg("بدأ البث — تفقّد فيسبوك بعد دقيقة");
    refresh();
  } catch (e) { showMsg(e.message, false); }
}
async function stopStream() {
  try { await api("/api/stream/stop", { method: "POST", headers: headers() }); showMsg("أُرسل أمر الإيقاف"); refresh(); }
  catch (e) { showMsg(e.message, false); }
}
async function uploadFile() {
  let f = document.getElementById("fileInput").files[0];
  if (!f) return showMsg("اختر ملفاً أولاً", false);
  let fd = new FormData(); fd.append("file", f);
  showMsg("جارٍ الرفع...");
  try { await api("/api/videos/upload", { method: "POST", headers: headers(), body: fd }); showMsg("تمت الإضافة للقائمة"); refresh(); }
  catch (e) { showMsg(e.message, false); }
}
async function addUrl() {
  let u = document.getElementById("urlInput").value.trim();
  if (!u) return showMsg("الصق رابطاً أولاً", false);
  showMsg("جارٍ التنزيل...");
  try { await api("/api/videos/add-url", { method: "POST", headers: headers({"Content-Type": "application/json"}), body: JSON.stringify({ url: u }) }); showMsg("تمت الإضافة للقائمة"); refresh(); }
  catch (e) { showMsg(e.message, false); }
}
async function restoreVideos() {
  try { await api("/api/videos/restore", { method: "POST", headers: headers() }); showMsg("بدأت الاستعادة في الخلفية"); setTimeout(refresh, 5000); }
  catch (e) { showMsg(e.message, false); }
}
async function moveVideo(id, dir) {
  try { await api("/api/videos/" + id + "/move", { method: "POST", headers: headers({"Content-Type": "application/json"}), body: JSON.stringify({ direction: dir }) }); refresh(); }
  catch (e) { showMsg(e.message, false); }
}
async function delVideo(id) {
  if (!confirm("حذف هذا الفيديو من القائمة؟")) return;
  try { await api("/api/videos/" + id, { method: "DELETE", headers: headers() }); refresh(); }
  catch (e) { showMsg(e.message, false); }
}
setInterval(refresh, 5000);
refresh();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    load_playlist()
    threading.Thread(target=restore_missing, daemon=True).start()
    log(f"قناة حسين التلفزيونية تعمل على المنفذ {PORT} — {len(_playlist)} فيديو في القائمة")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
