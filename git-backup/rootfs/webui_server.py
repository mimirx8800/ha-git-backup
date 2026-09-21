#!/usr/bin/env python3
import http.server
import json
import os
import urllib.parse

STATUS_FILE = "/data/status.json"
DEPLOY_STATUS_FILE = "/data/deploy_status.json"
SSH_PUB_FILE = "/data/ssh/id_ed25519.pub"


class BackupHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        if isinstance(body, str):
            body = body.encode()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.serve_html()
        elif path == "/api/status":
            self.serve_json_file(
                STATUS_FILE,
                {"status": "unknown", "message": "Status not available"},
            )
        elif path == "/api/deploy-status":
            self.serve_json_file(
                DEPLOY_STATUS_FILE,
                {
                    "status": "unknown",
                    "message": "Deploy status not available",
                    "files": [],
                    "blocked": [],
                    "conflicts": [],
                },
            )
        elif path == "/api/ssh-key":
            self.serve_ssh_key()
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/backup":
            self.trigger("/tmp/trigger_backup", "backup")
        elif path == "/api/deploy-preview":
            self.trigger("/tmp/trigger_deploy_preview", "deploy-preview")
        elif path == "/api/deploy-apply":
            self.trigger("/tmp/trigger_deploy_apply", "deploy-apply")
        else:
            self.send_error(404)

    def serve_json_file(self, path, fallback):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = handle.read()
            json.loads(payload)
        except Exception:
            payload = json.dumps(fallback)
        self._send(200, "application/json", payload)

    def serve_ssh_key(self):
        try:
            with open(SSH_PUB_FILE, "r", encoding="utf-8") as handle:
                key = handle.read().strip()
        except Exception:
            key = "No SSH key generated. Enable auto_generate_ssh_key if SSH is used."
        self._send(200, "text/plain; charset=utf-8", key)

    def trigger(self, path, action):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("1")
        self._send(
            200,
            "application/json",
            json.dumps({"status": "triggered", "action": action}),
        )

    def serve_html(self):
        html = """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Git Config Backup</title>
<style>
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;margin:0;padding:20px;background:#f5f5f5;color:#333}
.container{max-width:850px;margin:0 auto}
h1{color:#1976d2}
.card{background:white;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 2px 4px rgba(0,0,0,.1)}
.card h2{margin-top:0;font-size:1.2em}
.status{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.dot{width:12px;height:12px;border-radius:50%;background:#aaa}
.dot.success{background:#4caf50}.dot.error{background:#f44336}.dot.running{background:#ff9800}.dot.idle{background:#2196f3}
.info{color:#666;font-size:.9em}.info code{background:#eee;padding:2px 6px;border-radius:3px}
button{border:0;border-radius:4px;padding:10px 16px;color:white;cursor:pointer;font-size:14px;margin:4px 5px 4px 0}
button:disabled{background:#bbb!important;cursor:not-allowed}
.primary{background:#1976d2}.secondary{background:#546e7a}.danger{background:#d32f2f}
.files{font-family:monospace;margin-top:8px;white-space:pre-wrap}
.ssh{background:#263238;color:#aed581;padding:15px;border-radius:4px;font-family:monospace;font-size:12px;word-break:break-all;white-space:pre-wrap}
</style>
</head>
<body>
<div class="container">
<h1>Git Config Backup</h1>

<div class="card">
<h2>Backup status</h2>
<div class="status"><div class="dot" id="backupDot"></div><span id="backupText">Loading...</span></div>
<div class="info" id="backupDetails"></div>
<br>
<button class="primary" id="backupBtn" onclick="triggerBackup()">Run Backup Now</button>
</div>

<div class="card">
<h2>ChatGPT Config Deploy</h2>
<p class="info">
Manual safety gate. Only <code>automations.yaml</code>, <code>scripts.yaml</code> and
<code>scenes.yaml</code> can be deployed. A fresh backup and Home Assistant configuration
check are performed before a proposal is accepted.
</p>
<div class="status"><div class="dot" id="deployDot"></div><span id="deployText">Loading...</span></div>
<div class="info" id="deployDetails"></div>
<div class="files" id="deployFiles"></div>
<br>
<button class="secondary" id="previewBtn" onclick="triggerPreview()">Preview changes</button>
<button class="danger" id="applyBtn" onclick="triggerApply()">Apply ChatGPT changes</button>
</div>

<div class="card">
<h2>SSH Public Key</h2>
<p class="info">Only needed when the repository uses SSH authentication.</p>
<div class="ssh" id="sshKey">Loading...</div>
</div>
</div>

<script>
function cls(status){
  if(['success','ready','no_changes'].includes(status)) return 'success';
  if(['error','blocked','rolled_back'].includes(status)) return 'error';
  if(status==='running') return 'running';
  return 'idle';
}
function updateBackup(){
  fetch('api/status').then(r=>r.json()).then(d=>{
    document.getElementById('backupDot').className='dot '+cls(d.status);
    document.getElementById('backupText').textContent=d.message||d.status||'Unknown';
    let s='';
    if(d.repository) s+='Repository: '+d.repository+'<br>';
    if(d.branch) s+='Branch: '+d.branch+'<br>';
    if(d.last_commit) s+='Last commit: '+d.last_commit+'<br>';
    if(d.last_update) s+='Updated: '+new Date(d.last_update).toLocaleString();
    document.getElementById('backupDetails').innerHTML=s;
  }).catch(()=>{document.getElementById('backupText').textContent='Status unavailable';});
}
function updateDeploy(){
  fetch('api/deploy-status').then(r=>r.json()).then(d=>{
    document.getElementById('deployDot').className='dot '+cls(d.status);
    document.getElementById('deployText').textContent=d.message||d.status||'Unknown';
    let s='';
    if(d.branch) s+='Deploy branch: '+d.branch+'<br>';
    if(d.conflicts&&d.conflicts.length) s+='Conflicts: '+d.conflicts.join(', ')+'<br>';
    if(d.blocked&&d.blocked.length) s+='Blocked: '+d.blocked.join(', ')+'<br>';
    if(d.last_update) s+='Updated: '+new Date(d.last_update).toLocaleString();
    document.getElementById('deployDetails').innerHTML=s;
    document.getElementById('deployFiles').textContent=
      d.files&&d.files.length ? 'Pending: '+d.files.join(', ') : '';
  }).catch(()=>{document.getElementById('deployText').textContent='Deploy status unavailable';});
}
function disableDeploy(v){
  document.getElementById('previewBtn').disabled=v;
  document.getElementById('applyBtn').disabled=v;
}
function triggerBackup(){
  const b=document.getElementById('backupBtn');b.disabled=true;
  fetch('api/backup',{method:'POST'}).finally(()=>setTimeout(()=>{b.disabled=false;updateBackup();},1800));
}
function triggerPreview(){
  disableDeploy(true);
  fetch('api/deploy-preview',{method:'POST'}).finally(()=>{
    setTimeout(updateDeploy,1500);setTimeout(()=>disableDeploy(false),1800);
  });
}
function triggerApply(){
  if(!confirm('Apply the pending ChatGPT changes to Home Assistant? A fresh backup and configuration validation will run first.')) return;
  disableDeploy(true);
  fetch('api/deploy-apply',{method:'POST'}).finally(()=>{
    updateDeploy();setTimeout(updateDeploy,4000);setTimeout(updateDeploy,12000);
    setTimeout(()=>disableDeploy(false),13000);
  });
}
fetch('api/ssh-key').then(r=>r.text()).then(t=>{document.getElementById('sshKey').textContent=t;});
updateBackup();updateDeploy();
setInterval(updateBackup,10000);setInterval(updateDeploy,10000);
</script>
</body>
</html>"""
        self._send(200, "text/html; charset=utf-8", html)


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 8099), BackupHandler)
    print("Web UI running on port 8099")
    server.serve_forever()
