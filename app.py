"""
YouTube Downloader – Render.com ready
--------------------------------------
Kein ngrok, kein pyngrok, kein Tunnel nötig.
Render hostet die App öffentlich erreichbar.

WICHTIG: Render Free-Tier hat kein persistentes Dateisystem.
Videos werden im /tmp Ordner gespeichert und sind nur kurz verfügbar.
Sofort nach dem Download-Button klicken!
"""

import threading
import uuid
import tempfile
import os
from pathlib import Path
from flask import Flask, render_template_string, request, jsonify, send_file, abort
from pytubefix import YouTube

app = Flask(__name__)

TEMP_DIR = Path(tempfile.gettempdir()) / "yt_downloads"
TEMP_DIR.mkdir(exist_ok=True)

jobs: dict = {}


HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YT Downloader</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0a0f; --surface: #13131a; --surface2: #1c1c27;
    --border: #2a2a3d; --accent: #ff3c5f; --accent2: #ff8c42;
    --text: #e8e8f0; --muted: #6b6b8a; --success: #3cffa0;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Syne', sans-serif; min-height: 100vh; padding: 2rem 1rem; }
  body::before {
    content: ''; position: fixed; inset: 0;
    background: radial-gradient(ellipse 80% 60% at 50% -20%, rgba(255,60,95,0.12), transparent),
                radial-gradient(ellipse 60% 40% at 80% 100%, rgba(255,140,66,0.08), transparent);
    pointer-events: none; z-index: 0;
  }
  .container { max-width: 760px; margin: 0 auto; position: relative; z-index: 1; }
  header { text-align: center; margin-bottom: 3rem; }
  header h1 {
    font-size: clamp(2rem, 6vw, 3.5rem); font-weight: 800; letter-spacing: -0.03em;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }
  header p { color: var(--muted); margin-top: 0.5rem; font-family: 'DM Mono', monospace; font-size: 0.85rem; }
  .warning {
    background: rgba(255,140,66,0.08); border: 1px solid rgba(255,140,66,0.4);
    border-radius: 10px; padding: 0.75rem 1.25rem; margin-bottom: 1.5rem;
    font-family: 'DM Mono', monospace; font-size: 0.8rem; color: #f5a623;
  }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 1.75rem; margin-bottom: 1.5rem; }
  .card h2 { font-size: 1rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted); margin-bottom: 1rem; }
  textarea {
    width: 100%; background: var(--surface2); border: 1px solid var(--border); border-radius: 10px;
    color: var(--text); font-family: 'DM Mono', monospace; font-size: 0.85rem;
    padding: 1rem; resize: vertical; min-height: 130px; outline: none; transition: border-color 0.2s;
  }
  textarea:focus { border-color: var(--accent); }
  textarea::placeholder { color: var(--muted); }
  .row { display: flex; gap: 0.75rem; margin-top: 0.75rem; align-items: center; flex-wrap: wrap; }
  select {
    background: var(--surface2); border: 1px solid var(--border); border-radius: 8px;
    color: var(--text); font-family: 'Syne', sans-serif; font-size: 0.9rem;
    padding: 0.6rem 0.9rem; outline: none; cursor: pointer; flex: 1; min-width: 140px;
  }
  button {
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    border: none; border-radius: 8px; color: white; font-family: 'Syne', sans-serif;
    font-weight: 700; font-size: 0.95rem; padding: 0.65rem 1.5rem;
    cursor: pointer; transition: opacity 0.2s, transform 0.1s; white-space: nowrap;
  }
  button:hover { opacity: 0.88; } button:active { transform: scale(0.97); } button:disabled { opacity: 0.4; cursor: not-allowed; }
  #jobs-list { display: flex; flex-direction: column; gap: 0.75rem; }
  .job-item {
    background: var(--surface2); border: 1px solid var(--border); border-radius: 10px;
    padding: 1rem 1.25rem; display: flex; align-items: center; gap: 1rem; animation: slideIn 0.3s ease;
  }
  @keyframes slideIn { from { opacity:0; transform:translateY(-8px); } to { opacity:1; transform:none; } }
  .job-icon { font-size: 1.4rem; min-width: 2rem; text-align: center; }
  .job-info { flex: 1; min-width: 0; }
  .job-title { font-weight: 700; font-size: 0.95rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .job-url { font-family: 'DM Mono', monospace; font-size: 0.72rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .job-status { font-family: 'DM Mono', monospace; font-size: 0.78rem; margin-top: 0.25rem; }
  .status-pending { color: var(--muted); } .status-running { color: var(--accent2); }
  .status-done { color: var(--success); } .status-error { color: var(--accent); }
  .progress-bar { height: 3px; background: var(--border); border-radius: 2px; margin-top: 0.4rem; overflow: hidden; }
  .progress-fill { height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent2)); border-radius: 2px; transition: width 0.3s; }
  .dl-btn {
    background: var(--surface); border: 1px solid var(--success); color: var(--success);
    font-size: 0.8rem; padding: 0.4rem 0.85rem; border-radius: 6px; text-decoration: none;
    font-family: 'Syne', sans-serif; font-weight: 700; transition: background 0.2s; white-space: nowrap;
  }
  .dl-btn:hover { background: rgba(60,255,160,0.1); }
  .empty-state { text-align: center; color: var(--muted); font-family: 'DM Mono', monospace; font-size: 0.85rem; padding: 2rem; }
  .spinner { display: inline-block; animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>YT Downloader</h1>
    <p>youtube links eingeben → herunterladen → auf dem gerät speichern</p>
  </header>

  <div class="warning">
    ⚠️ Sofort nach dem Download-Button auf "⬇ Laden" klicken — Dateien werden nicht dauerhaft gespeichert.
  </div>

  <div class="card">
    <h2>Links</h2>
    <textarea id="links" placeholder="https://www.youtube.com/watch?v=...
https://youtu.be/...
(eine URL pro Zeile)"></textarea>
    <div class="row">
      <select id="quality">
        <option value="highest">Beste Qualität</option>
        <option value="720p">Max 720p</option>
        <option value="lowest">Niedrigste Qualität</option>
        <option value="audio">Nur Audio</option>
      </select>
      <button id="startBtn" onclick="startDownloads()">⬇ Herunterladen</button>
    </div>
  </div>

  <div class="card">
    <h2>Downloads</h2>
    <div id="jobs-list"><div class="empty-state">Noch keine Downloads gestartet.</div></div>
  </div>
</div>
<script>
const jobIds = [];
let polling = false;
function startDownloads() {
  const raw = document.getElementById('links').value.trim();
  const quality = document.getElementById('quality').value;
  if (!raw) return;
  const urls = raw.split('\\n').map(u => u.trim()).filter(u => u.startsWith('http'));
  if (!urls.length) { alert('Keine gültigen URLs gefunden.'); return; }
  document.getElementById('startBtn').disabled = true;
  document.getElementById('links').value = '';
  Promise.all(urls.map(url =>
    fetch('/api/start', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({url, quality}) })
    .then(r => r.json()).then(data => { jobIds.push(data.job_id); renderJob(data.job_id, url, 'pending', 'Wartet…', 0, null); })
  )).then(() => { if (!polling) startPolling(); });
  setTimeout(() => document.getElementById('startBtn').disabled = false, 1000);
}
function renderJob(id, url, status, statusText, progress, title) {
  const list = document.getElementById('jobs-list');
  const empty = list.querySelector('.empty-state');
  if (empty) empty.remove();
  let el = document.getElementById('job-' + id);
  const icon = status==='done'?'✅':status==='error'?'❌':status==='running'?'<span class="spinner">⏳</span>':'🕐';
  const dlBtn = status==='done' ? `<a class="dl-btn" href="/api/download/${id}" download>⬇ Laden</a>` : '';
  const html = `<div class="job-icon">${icon}</div><div class="job-info">
    <div class="job-title">${title||'Lade Infos…'}</div>
    <div class="job-url">${url}</div>
    <div class="job-status status-${status}">${statusText}</div>
    ${status==='running'?`<div class="progress-bar"><div class="progress-fill" style="width:${progress}%"></div></div>`:''}
  </div>${dlBtn}`;
  if (!el) { el = document.createElement('div'); el.className='job-item'; el.id='job-'+id; list.prepend(el); }
  el.innerHTML = html;
}
function startPolling() {
  polling = true;
  const iv = setInterval(async () => {
    const active = jobIds.filter(id => { const el = document.getElementById('job-'+id); return el && !el.querySelector('.dl-btn') && !el.querySelector('.status-error'); });
    if (!active.length) { polling = false; clearInterval(iv); return; }
    const data = await fetch('/api/status', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids:active}) }).then(r=>r.json());
    data.forEach(j => {
      const sm = {pending:'Wartet…', running:`Lädt… ${j.progress}%`, done:'Fertig!', error:j.error||'Fehler'};
      renderJob(j.id, j.url, j.status, sm[j.status], j.progress, j.title);
    });
  }, 1200);
}
</script>
</body>
</html>
"""


def run_download(job_id, url, quality):
    jobs[job_id]["status"] = "running"
    try:
        def on_progress(stream, chunk, bytes_remaining):
            total = stream.filesize
            if total:
                jobs[job_id]["progress"] = int((total - bytes_remaining) / total * 100)

        yt = YouTube(url, on_progress_callback=on_progress)
        jobs[job_id]["title"] = yt.title
        filename = f"{job_id}.mp4"

        if quality == "audio":
            stream = yt.streams.get_audio_only()
        elif quality == "highest":
            stream = yt.streams.get_highest_resolution()
        elif quality == "720p":
            stream = (yt.streams.filter(res="720p", file_extension="mp4", progressive=True).first()
                      or yt.streams.filter(progressive=True, file_extension="mp4").order_by("resolution").last())
        else:
            stream = yt.streams.filter(progressive=True, file_extension="mp4").order_by("resolution").first()

        if not stream:
            raise ValueError("Kein passender Stream gefunden")

        stream.download(output_path=str(TEMP_DIR), filename=filename)
        jobs[job_id].update({"status": "done", "filename": filename, "progress": 100})
    except Exception as e:
        jobs[job_id].update({"status": "error", "error": str(e)})


@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.json
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Keine URL"}), 400
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"id": job_id, "url": url, "status": "pending",
                    "title": None, "filename": None, "error": None, "progress": 0}
    threading.Thread(target=run_download, args=(job_id, url, data.get("quality", "highest")), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/api/status", methods=["POST"])
def api_status():
    ids = request.json.get("ids", [])
    return jsonify([{"id": j, "url": jobs[j]["url"], "status": jobs[j]["status"],
                     "title": jobs[j]["title"], "progress": jobs[j]["progress"],
                     "error": jobs[j]["error"]} for j in ids if j in jobs])

@app.route("/api/download/<job_id>")
def api_download(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        abort(404)
    filepath = TEMP_DIR / job["filename"]
    if not filepath.exists():
        abort(404)
    safe = "".join(c for c in (job["title"] or job_id) if c.isalnum() or c in " _-")
    return send_file(filepath, as_attachment=True, download_name=f"{safe}.mp4")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
