"""
YouTube Downloader – Flask LAN (In-Memory + Clipboard)
------------------------------------------------------
Installieren:
    pip install flask pytubefix pyperclip

Starten:
    python yt_flask.py
"""

import threading
import uuid
import io
import re
import time
import socket
from urllib.parse import urlparse, quote
from flask import Flask, render_template_string, request, jsonify, send_file, abort
from pytubefix import YouTube

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

app = Flask(__name__)

_lock = threading.Lock()
jobs: dict = {}
PUBLIC_URL = None
KEEP_FOR = 15 * 60


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def is_valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip() or "video"


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def cleanup_worker():
    while True:
        time.sleep(60)
        cutoff = time.time() - KEEP_FOR
        with _lock:
            expired = [
                jid for jid, j in jobs.items()
                if j["status"] in ("done", "error", "cancelled", "freed")
                and (j.get("finished_at") or 0) < cutoff
            ]
            for jid in expired:
                buf = jobs[jid].get("buffer")
                if buf:
                    buf.close()
                del jobs[jid]


def free_job(job: dict):
    buf = job.get("buffer")
    if buf:
        buf.close()
    job["buffer"] = None
    job["size"] = 0
    job["status"] = "freed"
    job["finished_at"] = time.time()


# ---------------------------------------------------------------------------
# Cloudflare Tunnel
# ---------------------------------------------------------------------------

def get_local_ip() -> str:
    """Ermittelt die lokale LAN-IP-Adresse."""
    try:
        # Verbindung zu externem Host simulieren – kein Paket wird gesendet
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Download-Worker
# ---------------------------------------------------------------------------

def run_download(job_id: str, url: str, quality: str):
    with _lock:
        if jobs[job_id]["status"] == "cancelled":
            return
        jobs[job_id]["status"] = "running"

    try:
        def on_progress(stream, _chunk, bytes_remaining):
            with _lock:
                if jobs[job_id]["status"] == "cancelled":
                    raise InterruptedError("Abgebrochen")
            total = stream.filesize
            if total:
                pct = int((total - bytes_remaining) / total * 100)
                with _lock:
                    jobs[job_id]["progress"] = pct

        yt = YouTube(url, on_progress_callback=on_progress)

        with _lock:
            jobs[job_id]["title"]     = yt.title
            jobs[job_id]["thumbnail"] = yt.thumbnail_url
            jobs[job_id]["duration"]  = yt.length

        if quality == "audio":
            stream = yt.streams.get_audio_only()
        elif quality == "highest":
            stream = yt.streams.get_highest_resolution()
        elif quality == "720p":
            stream = (
                yt.streams.filter(res="720p", file_extension="mp4", progressive=True).first()
                or yt.streams.filter(progressive=True, file_extension="mp4")
                              .order_by("resolution").last()
            )
        else:
            stream = (
                yt.streams.filter(progressive=True, file_extension="mp4")
                          .order_by("resolution").first()
            )

        if not stream:
            raise ValueError("Kein passender Stream gefunden")

        buffer = io.BytesIO()
        stream.stream_to_buffer(buffer)
        buffer.seek(0)
        size = buffer.getbuffer().nbytes

        with _lock:
            if jobs[job_id]["status"] != "cancelled":
                jobs[job_id].update({
                    "status":      "done",
                    "buffer":      buffer,
                    "size":        size,
                    "progress":    100,
                    "finished_at": time.time(),
                })
            else:
                buffer.close()

    except InterruptedError:
        with _lock:
            jobs[job_id].update({"status": "cancelled", "finished_at": time.time()})
    except Exception as e:
        with _lock:
            jobs[job_id].update({"status": "error", "error": str(e), "finished_at": time.time()})


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YT Downloader</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#0a0a0f; --surface:#13131a; --surface2:#1c1c27;
    --border:#2a2a3d; --accent:#ff3c5f; --accent2:#ff8c42;
    --text:#e8e8f0; --muted:#6b6b8a; --success:#3cffa0; --warn:#f5a623;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;min-height:100vh;padding:2rem 1rem}
  body::before{
    content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
    background:
      radial-gradient(ellipse 80% 60% at 50% -20%,rgba(255,60,95,.12),transparent),
      radial-gradient(ellipse 60% 40% at 80% 100%,rgba(255,140,66,.08),transparent)
  }
  .container{max-width:780px;margin:0 auto;position:relative;z-index:1}

  header{text-align:center;margin-bottom:3rem}
  header h1{
    font-size:clamp(2rem,6vw,3.5rem);font-weight:800;letter-spacing:-.03em;
    background:linear-gradient(135deg,var(--accent),var(--accent2));
    -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text
  }
  header p{color:var(--muted);margin-top:.5rem;font-family:'DM Mono',monospace;font-size:.85rem}
  .ram-badge{
    display:inline-flex;align-items:center;gap:.4rem;
    background:rgba(60,255,160,.07);border:1px solid rgba(60,255,160,.25);border-radius:20px;
    padding:.3rem .85rem;font-family:'DM Mono',monospace;font-size:.75rem;color:var(--success);margin-top:.75rem
  }

  .tunnel-banner{
    background:var(--surface);border:1px solid rgba(245,166,35,.5);border-radius:10px;
    padding:.75rem 1.25rem;margin-bottom:1.5rem;
    font-family:'DM Mono',monospace;font-size:.82rem;color:var(--warn);
    display:flex;align-items:center;gap:.5rem;word-break:break-all;cursor:pointer;
    transition:border-color .2s,color .2s
  }
  .tunnel-banner .copy-hint{color:var(--muted);font-size:.75rem;margin-left:auto;white-space:nowrap;flex-shrink:0}
  .tunnel-banner.copied{border-color:var(--success);color:var(--success)}

  .card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:1.75rem;margin-bottom:1.5rem}
  .card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem;gap:.75rem;flex-wrap:wrap}
  .card-header h2{font-size:.9rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin:0}

  .textarea-wrap{position:relative}
  textarea{
    width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:10px;
    color:var(--text);font-family:'DM Mono',monospace;font-size:.85rem;
    padding:1rem 1rem 3rem 1rem;resize:vertical;min-height:130px;outline:none;transition:border-color .2s
  }
  textarea:focus{border-color:var(--accent)}
  textarea::placeholder{color:var(--muted)}

  .textarea-toolbar{
    position:absolute;bottom:.6rem;left:.75rem;right:.75rem;
    display:flex;align-items:center;gap:.5rem;pointer-events:none
  }
  .textarea-toolbar>*{pointer-events:all}
  .clip-btn{
    background:var(--surface);border:1px solid var(--border);border-radius:6px;
    color:var(--muted);font-family:'DM Mono',monospace;font-size:.75rem;
    padding:.3rem .75rem;cursor:pointer;transition:border-color .2s,color .2s;
    display:flex;align-items:center;gap:.35rem;white-space:nowrap
  }
  .clip-btn:hover{border-color:var(--accent2);color:var(--accent2)}
  .clip-btn.flash{border-color:var(--success);color:var(--success)}
  .clip-count{
    font-family:'DM Mono',monospace;font-size:.72rem;color:var(--muted);
    margin-left:auto
  }
  .clip-count b{color:var(--text)}

  .toggle-btn{
    display:flex;align-items:center;gap:.4rem;
    background:none;border:1px solid var(--border);border-radius:6px;
    color:var(--muted);font-family:'DM Mono',monospace;font-size:.72rem;
    padding:.3rem .7rem;cursor:pointer;transition:border-color .2s,color .2s;white-space:nowrap;
    user-select:none
  }
  .toggle-btn .pip{
    width:26px;height:14px;border-radius:7px;background:var(--surface2);
    border:1px solid var(--border);position:relative;transition:background .2s,border-color .2s;flex-shrink:0
  }
  .toggle-btn .pip::after{
    content:'';position:absolute;top:2px;left:2px;
    width:8px;height:8px;border-radius:50%;background:var(--muted);
    transition:transform .2s,background .2s
  }
  .toggle-btn.on{border-color:var(--success);color:var(--success)}
  .toggle-btn.on .pip{background:rgba(60,255,160,.15);border-color:var(--success)}
  .toggle-btn.on .pip::after{transform:translateX(12px);background:var(--success)}

  #clip-preview{
    display:none;background:var(--surface2);border:1px solid var(--border);
    border-radius:8px;margin-top:.5rem;overflow:hidden
  }
  #clip-preview.visible{display:block}
  .clip-preview-header{
    display:flex;align-items:center;justify-content:space-between;
    padding:.5rem .85rem;border-bottom:1px solid var(--border);
    font-family:'DM Mono',monospace;font-size:.72rem;color:var(--muted)
  }
  .clip-preview-header button{
    background:none;border:none;color:var(--muted);cursor:pointer;font-size:.9rem;padding:.1rem .3rem;
    transition:color .2s
  }
  .clip-preview-header button:hover{color:var(--accent)}
  .clip-line{
    display:flex;align-items:center;gap:.75rem;padding:.5rem .85rem;
    border-bottom:1px solid rgba(42,42,61,.5);cursor:pointer;transition:background .15s
  }
  .clip-line:last-child{border-bottom:none}
  .clip-line:hover{background:rgba(255,140,66,.04)}
  .clip-line.selected{background:rgba(60,255,160,.05)}
  .clip-line-check{
    width:16px;height:16px;border-radius:4px;border:1px solid var(--border);
    background:var(--surface);flex-shrink:0;display:flex;align-items:center;justify-content:center;
    font-size:.65rem;transition:background .15s,border-color .15s
  }
  .clip-line.selected .clip-line-check{background:var(--success);border-color:var(--success);color:#000}
  .clip-line-text{
    font-family:'DM Mono',monospace;font-size:.78rem;color:var(--text);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0
  }
  .clip-line-url{color:var(--accent2)}
  .clip-insert-btn{
    background:linear-gradient(135deg,var(--accent),var(--accent2));
    border:none;border-radius:6px;color:#fff;font-family:'Syne',sans-serif;
    font-weight:700;font-size:.78rem;padding:.35rem .85rem;cursor:pointer;
    white-space:nowrap;transition:opacity .2s
  }
  .clip-insert-btn:hover{opacity:.85}

  .row{display:flex;gap:.75rem;margin-top:.75rem;align-items:center;flex-wrap:wrap}
  select{
    background:var(--surface2);border:1px solid var(--border);border-radius:8px;
    color:var(--text);font-family:'Syne',sans-serif;font-size:.9rem;
    padding:.6rem .9rem;outline:none;cursor:pointer;flex:1;min-width:140px
  }
  .btn{
    background:linear-gradient(135deg,var(--accent),var(--accent2));
    border:none;border-radius:8px;color:#fff;font-family:'Syne',sans-serif;
    font-weight:700;font-size:.95rem;padding:.65rem 1.5rem;
    cursor:pointer;transition:opacity .2s,transform .1s;white-space:nowrap
  }
  .btn:hover{opacity:.88}.btn:active{transform:scale(.97)}.btn:disabled{opacity:.4;cursor:not-allowed}

  .ram-meter{
    background:var(--surface2);border:1px solid var(--border);border-radius:10px;
    padding:.85rem 1.1rem;margin-bottom:1rem;display:flex;align-items:center;gap:1rem;flex-wrap:wrap
  }
  .ram-info{flex:1;min-width:0}
  .ram-label{font-family:'DM Mono',monospace;font-size:.75rem;color:var(--muted);margin-bottom:.35rem}
  .ram-bar{height:6px;background:var(--border);border-radius:3px;overflow:hidden}
  .ram-fill{height:100%;border-radius:3px;transition:width .5s ease,background .5s}
  .ram-numbers{font-family:'DM Mono',monospace;font-size:.82rem;color:var(--text);white-space:nowrap}
  .ram-actions{display:flex;gap:.5rem;flex-wrap:wrap}
  .ghost-btn{
    background:none;border:1px solid var(--border);border-radius:6px;
    color:var(--muted);font-family:'DM Mono',monospace;font-size:.75rem;
    padding:.35rem .8rem;cursor:pointer;transition:border-color .2s,color .2s;white-space:nowrap
  }
  .ghost-btn:hover{border-color:var(--accent);color:var(--accent)}

  #stored-list{display:flex;flex-direction:column;gap:.6rem}
  .stored-item{
    background:var(--surface2);border:1px solid var(--border);border-radius:10px;
    padding:.8rem 1rem;display:grid;grid-template-columns:50px 1fr auto;gap:.75rem;align-items:center;
    transition:border-color .2s
  }
  .stored-item:hover{border-color:rgba(255,140,66,.3)}
  .stored-thumb{width:50px;height:36px;border-radius:5px;object-fit:cover;background:var(--surface);border:1px solid var(--border)}
  .stored-thumb-placeholder{width:50px;height:36px;border-radius:5px;background:var(--surface);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:1.1rem}
  .stored-info{min-width:0}
  .stored-title{font-weight:700;font-size:.85rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .stored-meta{font-family:'DM Mono',monospace;font-size:.7rem;color:var(--muted);margin-top:.2rem;display:flex;gap:.6rem;flex-wrap:wrap}
  .size-pill{display:inline-block;background:rgba(255,140,66,.1);border:1px solid rgba(255,140,66,.25);border-radius:4px;padding:.05rem .4rem;color:var(--accent2);font-size:.68rem}
  .stored-actions{display:flex;gap:.4rem;align-items:center}
  .dl-btn{
    background:var(--surface);border:1px solid var(--success);color:var(--success);
    font-size:.75rem;padding:.3rem .7rem;border-radius:6px;text-decoration:none;
    font-family:'Syne',sans-serif;font-weight:700;transition:background .2s;white-space:nowrap
  }
  .dl-btn:hover{background:rgba(60,255,160,.1)}
  .free-btn{
    background:none;border:1px solid var(--border);color:var(--muted);
    font-size:.72rem;padding:.3rem .65rem;border-radius:6px;
    font-family:'DM Mono',monospace;cursor:pointer;transition:border-color .2s,color .2s;white-space:nowrap
  }
  .free-btn:hover{border-color:var(--accent);color:var(--accent)}
  .freed-label{font-family:'DM Mono',monospace;font-size:.72rem;color:var(--muted)}

  #stats-bar{
    display:none;justify-content:space-between;align-items:center;
    background:var(--surface);border:1px solid var(--border);border-radius:10px;
    padding:.6rem 1.1rem;margin-bottom:1rem;
    font-family:'DM Mono',monospace;font-size:.78rem;color:var(--muted)
  }
  #stats-bar.visible{display:flex}
  #stats-bar span b{color:var(--text)}
  .clear-btn{
    background:none;border:1px solid var(--border);border-radius:6px;
    color:var(--muted);font-family:'DM Mono',monospace;font-size:.75rem;
    padding:.3rem .7rem;cursor:pointer;transition:border-color .2s,color .2s
  }
  .clear-btn:hover{border-color:var(--accent);color:var(--accent)}

  #jobs-list{display:flex;flex-direction:column;gap:.75rem}
  .job-item{
    background:var(--surface2);border:1px solid var(--border);border-radius:12px;
    padding:1rem;display:grid;grid-template-columns:60px 1fr auto;gap:.75rem;
    align-items:center;animation:slideIn .3s ease
  }
  @keyframes slideIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
  .job-thumb{width:60px;height:42px;border-radius:6px;object-fit:cover;background:var(--surface);border:1px solid var(--border)}
  .job-thumb-placeholder{width:60px;height:42px;border-radius:6px;background:var(--surface);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:1.3rem}
  .job-info{min-width:0}
  .job-title{font-weight:700;font-size:.9rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .job-meta{font-family:'DM Mono',monospace;font-size:.7rem;color:var(--muted);margin-top:.15rem;display:flex;gap:.75rem;flex-wrap:wrap}
  .job-status{font-family:'DM Mono',monospace;font-size:.75rem;margin-top:.3rem}
  .status-pending{color:var(--muted)}.status-running{color:var(--accent2)}
  .status-done{color:var(--success)}.status-error{color:var(--accent)}.status-cancelled{color:var(--muted)}
  .progress-bar{height:3px;background:var(--border);border-radius:2px;margin-top:.4rem;overflow:hidden}
  .progress-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:2px;transition:width .4s ease}
  .job-actions{display:flex;flex-direction:column;gap:.4rem;align-items:flex-end}
  .cancel-btn{
    background:none;border:1px solid var(--border);color:var(--muted);
    font-size:.72rem;padding:.3rem .7rem;border-radius:6px;
    font-family:'DM Mono',monospace;cursor:pointer;transition:border-color .2s,color .2s;white-space:nowrap
  }
  .cancel-btn:hover{border-color:var(--accent);color:var(--accent)}

  .empty-state{text-align:center;color:var(--muted);font-family:'DM Mono',monospace;font-size:.85rem;padding:2rem}
  .spinner{display:inline-block;animation:spin 1s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}

  #toast{
    position:fixed;bottom:1.5rem;right:1.5rem;
    background:var(--surface);border:1px solid var(--border);border-radius:10px;
    padding:.75rem 1.25rem;font-family:'DM Mono',monospace;font-size:.82rem;
    color:var(--text);opacity:0;transform:translateY(8px);
    transition:opacity .3s,transform .3s;pointer-events:none;z-index:100;max-width:320px
  }
  #toast.show{opacity:1;transform:none}

  @media(max-width:500px){
    .job-item{grid-template-columns:44px 1fr}
    .job-actions{grid-column:2;flex-direction:row}
    .job-thumb,.job-thumb-placeholder{width:44px;height:32px}
    .stored-item{grid-template-columns:44px 1fr}
    .stored-actions{grid-column:2;flex-direction:row}
  }
</style>
</head>
<body>
<div class="container">

  <header>
    <h1>YT Downloader</h1>
    <p>links eingeben → herunterladen → auf dem gerät speichern</p>
    <div class="ram-badge">⚡ 100% in-memory – keine Dateien auf der Festplatte</div>
  </header>

  {% if tunnel_url %}
  <div class="tunnel-banner" id="tunnelBanner" onclick="copyTunnel()" title="Klicken zum Kopieren">
    🌐 {{ tunnel_url }}
    <span class="copy-hint">📋 kopieren</span>
  </div>
  {% endif %}

  <!-- ── Links eingeben ── -->
  <div class="card">
    <h2 style="font-size:.9rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin-bottom:1rem">Links</h2>

    <div class="textarea-wrap">
      <textarea id="links" placeholder="https://www.youtube.com/watch?v=...&#10;https://youtu.be/...&#10;(eine URL pro Zeile)"></textarea>
      <div class="textarea-toolbar">
        <button class="clip-btn" id="clipBtn" onclick="pasteFromClipboard()">
          📋 Zwischenablage einfügen
        </button>
        <button class="toggle-btn on" id="autoNlBtn" onclick="toggleAutoNl()" title="Auto-Zeilenumbruch nach YT-Links">
          <span class="pip"></span>↵ Auto-Enter
        </button>
        <span class="clip-count" id="clipCount"></span>
      </div>
    </div>

    <!-- Clipboard-Preview -->
    <div id="clip-preview">
      <div class="clip-preview-header">
        <span id="clip-preview-label">0 Einträge gefunden</span>
        <div style="display:flex;gap:.5rem;align-items:center">
          <button onclick="selectAllClip()" title="Alle auswählen">☑ Alle</button>
          <button onclick="deselectAllClip()" title="Alle abwählen">☐ Keine</button>
          <button onclick="closeClipPreview()" title="Schließen">✕</button>
        </div>
      </div>
      <div id="clip-lines"></div>
      <div style="padding:.65rem .85rem;border-top:1px solid var(--border);display:flex;justify-content:flex-end">
        <button class="clip-insert-btn" onclick="insertSelected()">Ausgewählte einfügen</button>
      </div>
    </div>

    <div class="row">
      <select id="quality">
        <option value="highest">🎬 Beste Qualität</option>
        <option value="720p">📺 Max 720p</option>
        <option value="lowest">📱 Niedrigste Qualität</option>
        <option value="audio">🎵 Nur Audio</option>
      </select>
      <button class="btn" id="startBtn" onclick="startDownloads()">⬇ Herunterladen</button>
    </div>
  </div>

  <!-- ── Im RAM gespeicherte Videos ── -->
  <div class="card">
    <div class="card-header">
      <h2>Im RAM gespeichert</h2>
      <div class="ram-actions">
        <button class="ghost-btn" onclick="refreshStored()">↻ Aktualisieren</button>
        <button class="ghost-btn" onclick="freeAll()">🗑 Alles freigeben</button>
      </div>
    </div>
    <div class="ram-meter">
      <div class="ram-info">
        <div class="ram-label">RAM-Verbrauch dieser Videos</div>
        <div class="ram-bar"><div class="ram-fill" id="ramFill" style="width:0%"></div></div>
      </div>
      <div class="ram-numbers" id="ramNumbers">0 B</div>
    </div>
    <div id="stored-list"><div class="empty-state">Keine Videos im RAM.</div></div>
  </div>

  <!-- ── Laufende Downloads ── -->
  <div class="card">
    <div class="card-header">
      <h2>Downloads</h2>
      <button class="clear-btn" onclick="clearFinished()">abgeschlossene löschen</button>
    </div>
    <div id="stats-bar">
      <span><b id="stat-done">0</b> fertig &nbsp;·&nbsp; <b id="stat-run">0</b> läuft &nbsp;·&nbsp; <b id="stat-err">0</b> fehler</span>
    </div>
    <div id="jobs-list"><div class="empty-state">Noch keine Downloads gestartet.</div></div>
  </div>

</div>
<div id="toast"></div>

<script>
const jobMeta = {};
let polling = false;
let clipLines = [];

function toast(msg, dur = 2500) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), dur);
}

function copyTunnel() {
  const banner = document.getElementById('tunnelBanner');
  const url = banner.childNodes[1].textContent.trim();
  navigator.clipboard.writeText(url).then(() => {
    banner.classList.add('copied');
    banner.querySelector('.copy-hint').textContent = '✓ kopiert!';
    setTimeout(() => {
      banner.classList.remove('copied');
      banner.querySelector('.copy-hint').textContent = '📋 kopieren';
    }, 2000);
  });
}

function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n/1024).toFixed(1) + ' KB';
  if (n < 1073741824) return (n/1048576).toFixed(1) + ' MB';
  return (n/1073741824).toFixed(2) + ' GB';
}
function fmtDuration(sec) {
  if (!sec) return '';
  return `${Math.floor(sec/60)}:${String(sec%60).padStart(2,'0')}`;
}
function isUrl(s) {
  return /^https?:\/\//i.test(s.trim());
}

const RAM_SOFT_MAX = 500 * 1024 * 1024;
function updateRamMeter(totalBytes) {
  const pct = Math.min(100, (totalBytes / RAM_SOFT_MAX) * 100);
  const fill = document.getElementById('ramFill');
  fill.style.width = pct + '%';
  fill.style.background = pct > 80
    ? 'var(--accent)'
    : pct > 50
      ? 'linear-gradient(90deg,var(--accent2),var(--warn))'
      : 'linear-gradient(90deg,var(--success),var(--accent2))';
  document.getElementById('ramNumbers').textContent = fmtBytes(totalBytes);
}

async function pasteFromClipboard() {
  let text = '';
  try {
    text = await navigator.clipboard.readText();
  } catch(e) {
    try {
      const res = await fetch('/api/clipboard').then(r => r.json());
      text = res.text || '';
    } catch(e2) {
      toast('⚠ Kein Clipboard-Zugriff');
      return;
    }
  }

  if (!text.trim()) { toast('Zwischenablage ist leer'); return; }

  const lines = text.split('\n')
    .map(l => l.trim())
    .filter(l => l.length > 0);

  if (!lines.length) { toast('Keine Einträge gefunden'); return; }

  clipLines = lines.map(l => ({ text: l, isUrl: isUrl(l), selected: isUrl(l) }));
  renderClipPreview();

  const btn = document.getElementById('clipBtn');
  btn.classList.add('flash');
  setTimeout(() => btn.classList.remove('flash'), 600);
}

function renderClipPreview() {
  const preview  = document.getElementById('clip-preview');
  const linesDiv = document.getElementById('clip-lines');
  const label    = document.getElementById('clip-preview-label');
  const urlCount = clipLines.filter(l => l.isUrl).length;

  label.textContent = `${clipLines.length} ${clipLines.length === 1 ? 'Eintrag' : 'Einträge'} · ${urlCount} URL${urlCount !== 1 ? 's' : ''}`;

  linesDiv.innerHTML = '';
  clipLines.forEach((item, i) => {
    const el = document.createElement('div');
    el.className = 'clip-line' + (item.selected ? ' selected' : '');
    el.id = 'clip-line-' + i;
    el.onclick = () => toggleClipLine(i);

    const checkIcon = item.selected ? '✓' : '';
    const textClass = item.isUrl ? 'clip-line-text clip-line-url' : 'clip-line-text';
    const display = item.text.length > 70 ? item.text.slice(0, 67) + '…' : item.text;

    el.innerHTML = `
      <div class="clip-line-check">${checkIcon}</div>
      <span class="${textClass}" title="${item.text}">${display}</span>`;
    linesDiv.appendChild(el);
  });

  preview.classList.add('visible');
  updateClipCount();
}

function toggleClipLine(i) {
  clipLines[i].selected = !clipLines[i].selected;
  const el = document.getElementById('clip-line-' + i);
  el.classList.toggle('selected', clipLines[i].selected);
  el.querySelector('.clip-line-check').textContent = clipLines[i].selected ? '✓' : '';
  updateClipCount();
}

function selectAllClip() {
  clipLines.forEach((l, i) => {
    l.selected = true;
    const el = document.getElementById('clip-line-' + i);
    if (el) { el.classList.add('selected'); el.querySelector('.clip-line-check').textContent = '✓'; }
  });
  updateClipCount();
}

function deselectAllClip() {
  clipLines.forEach((l, i) => {
    l.selected = false;
    const el = document.getElementById('clip-line-' + i);
    if (el) { el.classList.remove('selected'); el.querySelector('.clip-line-check').textContent = ''; }
  });
  updateClipCount();
}

function updateClipCount() {
  const sel = clipLines.filter(l => l.selected).length;
  document.getElementById('clipCount').innerHTML =
    sel > 0 ? `<b>${sel}</b> ausgewählt` : '';
}

function closeClipPreview() {
  document.getElementById('clip-preview').classList.remove('visible');
  clipLines = [];
  document.getElementById('clipCount').innerHTML = '';
}

function insertSelected() {
  const selected = clipLines.filter(l => l.selected).map(l => l.text);
  if (!selected.length) { toast('Nichts ausgewählt'); return; }

  const ta = document.getElementById('links');
  const existing = ta.value.trim();
  const existingLines = new Set(existing ? existing.split('\n').map(l => l.trim()) : []);
  const newLines = selected.filter(l => !existingLines.has(l));

  ta.value = [...(existing ? [existing] : []), ...newLines].join('\n');

  toast(`✓ ${newLines.length} Zeile${newLines.length !== 1 ? 'n' : ''} eingefügt`);
  closeClipPreview();
}

async function refreshStored() {
  const data = await fetch('/api/stored').then(r => r.json()).catch(() => []);
  const list = document.getElementById('stored-list');
  list.innerHTML = '';

  if (!data.length) {
    list.innerHTML = '<div class="empty-state">Keine Videos im RAM.</div>';
    updateRamMeter(0);
    return;
  }

  const totalBytes = data.reduce((s, v) => s + (v.size || 0), 0);
  updateRamMeter(totalBytes);

  data.forEach(v => {
    const el = document.createElement('div');
    el.className = 'stored-item';
    el.id = 'stored-' + v.id;

    const thumbHtml = v.thumbnail
      ? `<img class="stored-thumb" src="${v.thumbnail}" alt="" loading="lazy">`
      : `<div class="stored-thumb-placeholder">🎬</div>`;

    const dur = v.duration ? `<span>${fmtDuration(v.duration)}</span>` : '';
    const size = v.size ? `<span class="size-pill">${fmtBytes(v.size)}</span>` : '';
    const age = v.finished_at
      ? `<span>vor ${Math.round((Date.now()/1000 - v.finished_at)/60)} Min.</span>`
      : '';

    el.innerHTML = `
      ${thumbHtml}
      <div class="stored-info">
        <div class="stored-title">${v.title || v.id}</div>
        <div class="stored-meta">${size}${dur}${age}</div>
      </div>
      <div class="stored-actions">
        <a class="dl-btn" href="/api/download/${v.id}" download>⬇</a>
        <button class="free-btn" onclick="freeSingle('${v.id}')">🗑</button>
      </div>`;
    list.appendChild(el);
  });
}

async function freeSingle(id) {
  await fetch('/api/free', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ids: [id]})
  });
  document.getElementById('stored-' + id)?.remove();
  if (!document.querySelectorAll('.stored-item').length)
    document.getElementById('stored-list').innerHTML = '<div class="empty-state">Keine Videos im RAM.</div>';
  toast('RAM freigegeben');
  refreshStored();
}

async function freeAll() {
  await fetch('/api/free', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ids: 'all'})
  });
  document.getElementById('stored-list').innerHTML = '<div class="empty-state">Keine Videos im RAM.</div>';
  updateRamMeter(0);
  toast('🗑 Gesamter RAM freigegeben');
}

function updateStats() {
  const all = Object.values(jobMeta);
  if (!all.length) { document.getElementById('stats-bar').classList.remove('visible'); return; }
  document.getElementById('stats-bar').classList.add('visible');
  document.getElementById('stat-done').textContent = all.filter(j => j.status === 'done').length;
  document.getElementById('stat-run').textContent  = all.filter(j => ['running','pending'].includes(j.status)).length;
  document.getElementById('stat-err').textContent  = all.filter(j => j.status === 'error').length;
}

function clearFinished() {
  Object.entries(jobMeta).forEach(([id, meta]) => {
    if (['done','error','cancelled','freed'].includes(meta.status)) {
      document.getElementById('job-' + id)?.remove();
      delete jobMeta[id];
    }
  });
  if (!Object.keys(jobMeta).length)
    document.getElementById('jobs-list').innerHTML = '<div class="empty-state">Noch keine Downloads gestartet.</div>';
  updateStats();
}

async function cancelJob(id) {
  await fetch('/api/cancel', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id})
  });
  if (jobMeta[id]) jobMeta[id].status = 'cancelled';
  renderJob(id, jobMeta[id].url, 'cancelled', 'Abgebrochen', 0, null, null, null);
  updateStats();
}

function renderJob(id, url, status, statusText, progress, title, thumbnail, duration) {
  const list = document.getElementById('jobs-list');
  list.querySelector('.empty-state')?.remove();

  let el = document.getElementById('job-' + id);
  if (!el) { el = document.createElement('div'); el.className='job-item'; el.id='job-'+id; list.prepend(el); }

  const icon = {done:'✅',error:'❌',running:'<span class="spinner">⏳</span>',cancelled:'🚫',pending:'🕐',freed:'🗑'}[status]||'🕐';
  const thumbHtml = thumbnail
    ? `<img class="job-thumb" src="${thumbnail}" alt="" loading="lazy">`
    : `<div class="job-thumb-placeholder">${icon}</div>`;

  const shortUrl = url.length > 42 ? url.slice(0,42)+'…' : url;
  const dur = duration ? `<span>${fmtDuration(duration)}</span>` : '';
  const progressHtml = status === 'running'
    ? `<div class="progress-bar"><div class="progress-fill" style="width:${progress}%"></div></div>` : '';

  let actionsHtml = '';
  if (status === 'done')
    actionsHtml = `<a class="dl-btn" href="/api/download/${id}" download>⬇ Laden</a>`;
  else if (status === 'running' || status === 'pending')
    actionsHtml = `<button class="cancel-btn" onclick="cancelJob('${id}')">✕ Stopp</button>`;
  else if (status === 'freed')
    actionsHtml = `<span class="freed-label">freigegeben</span>`;

  el.innerHTML = `
    ${thumbHtml}
    <div class="job-info">
      <div class="job-title">${title||(status==='pending'?'Wartet auf Start…':'Lade Infos…')}</div>
      <div class="job-meta"><span>${shortUrl}</span>${dur}</div>
      <div class="job-status status-${status}">${statusText}</div>
      ${progressHtml}
    </div>
    <div class="job-actions">${actionsHtml}</div>`;
}

async function startDownloads() {
  const raw = document.getElementById('links').value.trim();
  const quality = document.getElementById('quality').value;
  if (!raw) return;

  const urls = [...new Set(raw.split('\n').map(u=>u.trim()).filter(u=>u.startsWith('http')))];
  if (!urls.length) { toast('⚠ Keine gültigen URLs gefunden'); return; }

  const btn = document.getElementById('startBtn');
  btn.disabled = true;
  document.getElementById('links').value = '';

  await Promise.all(urls.map(async url => {
    try {
      const data = await fetch('/api/start', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({url, quality})
      }).then(r=>r.json());
      if (data.error) { toast('⚠ ' + data.error); return; }
      jobMeta[data.job_id] = {url, status:'pending'};
      renderJob(data.job_id, url, 'pending', 'Wartet…', 0, null, null, null);
    } catch(e) { toast('⚠ Verbindungsfehler'); }
  }));

  updateStats();
  btn.disabled = false;
  if (!polling) startPolling();
}

function startPolling() {
  polling = true;
  const iv = setInterval(async () => {
    const active = Object.entries(jobMeta)
      .filter(([,m]) => m.status==='pending'||m.status==='running')
      .map(([id])=>id);

    if (!active.length) { polling = false; clearInterval(iv); return; }

    const data = await fetch('/api/status', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ids: active})
    }).then(r=>r.json()).catch(()=>[]);

    let anyDone = false;
    data.forEach(j => {
      if (jobMeta[j.id]) jobMeta[j.id].status = j.status;
      const sm = {
        pending:'Wartet…', running:`Lädt… ${j.progress}%`,
        done:'Fertig!', error:'⚠ '+(j.error||'Fehler'), cancelled:'Abgebrochen'
      };
      renderJob(j.id, j.url, j.status, sm[j.status]||j.status,
        j.progress, j.title, j.thumbnail, j.duration);
      if (j.status === 'done') anyDone = true;
    });
    updateStats();
    if (anyDone) refreshStored();
  }, 1200);
}

let autoNl = true;

function toggleAutoNl() {
  autoNl = !autoNl;
  const btn = document.getElementById('autoNlBtn');
  btn.classList.toggle('on', autoNl);
  toast(autoNl ? '↵ Auto-Enter aktiviert' : '↵ Auto-Enter deaktiviert');
}

const YT_RE = /https?:\/\/(?:www\.)?(?:youtube\.com\/watch\?[^\s]*v=[\w-]+|youtu\.be\/[\w-]+)[^\s]*/gi;

document.addEventListener('DOMContentLoaded', () => {
  const ta = document.getElementById('links');

  ta.addEventListener('input', () => {
    if (!autoNl) return;

    const val = ta.value;
    const pos = ta.selectionStart;
    const before = val.slice(0, pos);
    const after  = val.slice(pos);

    if (after.startsWith('\n') || before.endsWith('\n')) return;

    const matches = [...before.matchAll(YT_RE)];
    if (!matches.length) return;

    const last = matches[matches.length - 1];
    const linkEnd = last.index + last[0].length;

    if (linkEnd === pos) {
      ta.value = val.slice(0, pos) + '\n' + val.slice(pos);
      ta.selectionStart = ta.selectionEnd = pos + 1;
    }
  });
});

refreshStored();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(HTML, tunnel_url=PUBLIC_URL)


@app.route("/api/clipboard")
def api_clipboard():
    if not HAS_PYPERCLIP:
        return jsonify({"text": "", "error": "pyperclip nicht installiert"})
    try:
        return jsonify({"text": pyperclip.paste() or ""})
    except Exception as e:
        return jsonify({"text": "", "error": str(e)})


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    quality = data.get("quality", "highest")

    if not url:
        return jsonify({"error": "Keine URL angegeben"}), 400
    if not is_valid_url(url):
        return jsonify({"error": "Ungültige URL"}), 400
    if quality not in ("highest", "720p", "lowest", "audio"):
        return jsonify({"error": "Ungültige Qualitätsoption"}), 400

    job_id = str(uuid.uuid4())[:8]
    with _lock:
        jobs[job_id] = {
            "id": job_id, "url": url, "status": "pending",
            "title": None, "thumbnail": None, "duration": None,
            "buffer": None, "size": 0, "error": None,
            "progress": 0, "finished_at": None,
        }

    threading.Thread(target=run_download, args=(job_id, url, quality), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/status", methods=["POST"])
def api_status():
    ids = (request.json or {}).get("ids", [])
    result = []
    with _lock:
        for jid in ids:
            j = jobs.get(jid)
            if j:
                result.append({
                    "id": j["id"], "url": j["url"], "status": j["status"],
                    "title": j["title"], "thumbnail": j["thumbnail"],
                    "duration": j["duration"], "progress": j["progress"],
                    "error": j["error"],
                })
    return jsonify(result)


@app.route("/api/stored")
def api_stored():
    with _lock:
        result = [
            {
                "id":          j["id"],
                "title":       j["title"],
                "thumbnail":   j["thumbnail"],
                "duration":    j["duration"],
                "size":        j["size"],
                "finished_at": j["finished_at"],
            }
            for j in jobs.values()
            if j["status"] == "done" and j.get("buffer") is not None
        ]
    result.sort(key=lambda x: x["finished_at"] or 0, reverse=True)
    return jsonify(result)


@app.route("/api/free", methods=["POST"])
def api_free():
    payload = request.json or {}
    ids = payload.get("ids", [])
    with _lock:
        targets = list(jobs.values()) if ids == "all" else [
            jobs[jid] for jid in ids if jid in jobs
        ]
        for job in targets:
            free_job(job)
    return jsonify({"ok": True, "freed": len(targets)})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    jid = (request.json or {}).get("id", "")
    with _lock:
        job = jobs.get(jid)
        if job and job["status"] in ("pending", "running"):
            job["status"] = "cancelled"
            job["finished_at"] = time.time()
    return jsonify({"ok": True})


@app.route("/api/download/<job_id>")
def api_download(job_id):
    with _lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        abort(404)
    buf = job.get("buffer")
    if not buf:
        abort(410)

    buf.seek(0)
    safe_title    = sanitize(job["title"] or job_id)
    download_name = f"{safe_title}.mp4"
    encoded       = quote(download_name)

    response = send_file(buf, mimetype="video/mp4", as_attachment=True, download_name=download_name)
    response.headers["Content-Disposition"] = (
        f"attachment; filename=\"{download_name.encode('ascii','replace').decode()}\"; "
        f"filename*=UTF-8''{encoded}"
    )
    return response


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    PORT = 5000

    threading.Thread(target=cleanup_worker, daemon=True).start()

    local_ip = get_local_ip()
    PUBLIC_URL = f"http://{local_ip}:{PORT}"

    print(f"""
╔══════════════════════════════════════════════════════╗
║  ✅  Server gestartet!                               ║
╠══════════════════════════════════════════════════════╣
║  🌐  Im WLAN erreichbar (Smartphone, Tablet …):      ║
║  {PUBLIC_URL:<52}║
╠══════════════════════════════════════════════════════╣
║  ⚡  Downloads nur im RAM – keine Festplatten-I/O    ║
║  🧹  RAM wird nach 15 Min. automatisch freigegeben   ║
╚══════════════════════════════════════════════════════╝
""")
    app.run(host="0.0.0.0", port=PORT, debug=False)
