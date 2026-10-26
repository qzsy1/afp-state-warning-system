"""Loopback-only simulation acceptance entry using the shared acquisition core."""
from __future__ import annotations

import argparse
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit
import uuid


PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>AFP 采集暂停隔离验收</title>
<style>body{font:16px system-ui;background:#eef4f8;color:#123b5d;max-width:1050px;margin:36px auto;padding:0 20px}
section{background:white;padding:24px;border-radius:12px;margin:18px 0}button,input{font:inherit;padding:10px;margin:6px}
button{background:#1979b7;color:white;border:0;border-radius:6px}button:disabled{opacity:.4}pre{white-space:pre-wrap;overflow-wrap:anywhere}
canvas{width:100%;height:260px}#message{color:#a34215}</style>
<h1>采集暂停与继续 · 隔离验收</h1>
<p>使用源码中的共享采集生命周期与模拟设备。仅监听本机；采集文件、时间戳及缓存全部写入独立验收目录。</p>
<section><label>采样频率 Hz <input id="rate" type="number" value="20" min="1" max="100"></label>
<p>模拟当前 77 通道采集方案；同一采集会话、试样和层号在中断与继续期间保持不变。</p>
<button id="start">开始采集</button><button id="pause" disabled>中断采集</button><button id="stop" disabled>停止并保存</button>
<p id="status">正在读取状态</p><p id="message" role="alert"></p><canvas id="plot" width="1000" height="260"></canvas>
<p>曲线显示“温度1”的实际采集时间，中断位置断线；刷新页面可恢复执行端状态。</p></section>
<section><h2>执行端状态与保存结果</h2><pre id="detail"></pre></section>
<script>
const $=id=>document.getElementById(id);let current={},busy=false;
async function read(){const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error('状态读取失败');return r.json()}
function render(data){current=data.status;const s=current,transition=['pausing','resuming','stopping_sampling','finalizing'].includes(s.capture_phase);
$('start').disabled=busy||s.session_active;$('rate').disabled=busy||s.session_active;
$('pause').disabled=busy||transition||!s.running;$('stop').disabled=busy||transition||!s.session_active;
$('pause').textContent=s.paused?'继续采集':'中断采集';
const names={running:'采集中',paused:'采集已中断',pausing:'正在中断',resuming:'正在建立新采样边界',completed:'本次采集已结束',finalization_failed:'保存失败'};
$('status').textContent=(names[s.capture_phase]||s.capture_phase)+' · '+s.sample_count+' 点 · 同一层采集';
$('detail').textContent=JSON.stringify({session_active:s.session_active,sampling:s.sampling,paused:s.paused,
capture_uuid:s.capture_uuid,layer:s.config?.layer,sample_count:s.sample_count,capture_phase:s.capture_phase,
finalization_complete:s.finalization_complete,capture_saved:s.capture_saved,layer_file:s.layer_file,
timestamp_file:s.timestamp_file,pause_intervals:s.pause_intervals,last_error:s.last_error,pause_error:s.pause_error},null,2);
const ctx=$('plot').getContext('2d');ctx.clearRect(0,0,1000,260);const rows=data.points||[];if(rows.length<2)return;
const t0=rows[0].time,t1=rows.at(-1).time,vals=rows.map(p=>p.value).filter(Number.isFinite),min=Math.min(...vals),max=Math.max(...vals);
const boundaries=new Set((s.pause_intervals||[]).map(p=>p.row_index));ctx.strokeStyle='#1979b7';ctx.lineWidth=2;ctx.beginPath();
rows.forEach((p,i)=>{const x=20+960*(p.time-t0)/Math.max(.001,t1-t0),y=230-200*(p.value-min)/Math.max(1,max-min);
if(!i||boundaries.has(p.index))ctx.moveTo(x,y);else ctx.lineTo(x,y)});ctx.stroke();ctx.fillStyle='#60798c';ctx.fillText((t1-t0).toFixed(2)+' 秒（保留中断间隔）',20,254)}
async function act(action){if(busy)return;busy=true;render({status:current});$('message').textContent='';
try{const r=await fetch('/api/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Preview-Token':'TOKEN'},
body:JSON.stringify({capture_uuid:current.capture_uuid,expected_revision:current.control_revision,rate:Number($('rate').value)})});
const reply=await r.json();if(!r.ok)throw Error(reply.error);render(reply);
if(action==='stop'&&!reply.status.finalization_complete)throw Error(reply.status.last_error||'保存尚未完成');
}catch(e){$('message').textContent=e.message}finally{busy=false;try{render(await read())}catch(e){$('message').textContent=e.message}}}
$('start').onclick=()=>act('start');$('pause').onclick=()=>act(current.paused?'resume':'pause');$('stop').onclick=()=>act('stop');
setInterval(()=>read().then(render).catch(e=>$('message').textContent=e.message),400);read().then(render);
</script></html>"""


def create_preview(root: Path, port: int = 0):
    root = root.resolve()
    staging = Path("D:/AFP_Runtime/.afp-staging").resolve()
    if staging not in root.parents:
        raise ValueError("隔离验收目录必须位于 D:/AFP_Runtime/.afp-staging 的独立子目录")
    root.mkdir(parents=True, exist_ok=True)
    (root / "capture").mkdir(exist_ok=True)
    from acquisition import AcquisitionManager, AcquisitionConfig, CURRENT_COLLECTION_SENSOR_COLUMNS
    from generate_thermocouple64_simulation import _row, CHANNELS, PROCESS_PARAMETER_COLUMNS, IDENTITY_COLUMNS
    source = root / "simulation.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["时间", *CHANNELS, *PROCESS_PARAMETER_COLUMNS, *IDENTITY_COLUMNS])
        writer.writeheader()
        writer.writerows(_row(index) for index in range(6000))
    manager = AcquisitionManager(root / "capture", recovery_scan=False)
    token = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, value, code=200):
            self.send_bytes(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json", code)

        def send_bytes(self, raw, content_type, code=200):
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def allowed(self):
            host = f"127.0.0.1:{self.server.server_port}"
            return (self.client_address[0] == "127.0.0.1" and self.headers.get("Host") == host
                    and self.headers.get("Origin", "http://" + host) == "http://" + host)

        def snapshot(self):
            with manager.lock:
                status = manager.status()
                base = manager.total_sample_count - len(manager.rows)
                points = [{"index": base + i, "time": stamp, "value": row.get("温度1")}
                          for i, (row, stamp) in enumerate(zip(manager.rows, manager.timestamps))][-1200:]
            return {"status": status, "points": points, "isolated_root": str(root)}

        def do_GET(self):
            if not self.allowed():
                self.send_json({"error": "loopback_origin_required"}, 403)
            elif urlsplit(self.path).path == "/":
                self.send_bytes(PAGE.replace("TOKEN", token).encode("utf-8"), "text/html")
            elif urlsplit(self.path).path == "/api/status":
                self.send_json(self.snapshot())
            else:
                self.send_json({"error": "preview_route_not_allowed"}, 404)

        def do_POST(self):
            if not self.allowed() or not secrets.compare_digest(self.headers.get("X-Preview-Token", ""), token):
                self.send_json({"error": "preview_control_denied"}, 403)
                return
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size < 4096:
                    raise ValueError("invalid_request_size")
                payload = json.loads(self.rfile.read(size))
                action = urlsplit(self.path).path
                if action == "/api/start":
                    cfg = AcquisitionConfig(acquisition_mode="simulation", processing_mode="capture_only",
                        dataset_schema="new_collection_v13_0", selected_sensors=list(CURRENT_COLLECTION_SENSOR_COLUMNS),
                        sample_rate_hz=max(1, min(100, float(payload.get("rate", 20)))),
                        simulation_source_type="single_csv", simulation_source_path=str(source), save_root=str(root / "capture"),
                        specimen_id="ISOLATED_PAUSE", run_id="ISOLATED_PAUSE", condition_id="ISOLATED")
                    if manager.status()["session_active"]:
                        raise RuntimeError("当前会话尚未结束，请继续或停止并保存")
                    manager.start(cfg)
                elif action in {"/api/pause", "/api/resume"}:
                    getattr(manager, action.rsplit("/", 1)[-1])(capture_uuid=payload.get("capture_uuid"),
                        expected_revision=payload.get("expected_revision"))
                elif action == "/api/stop":
                    manager.stop(capture_uuid=payload.get("capture_uuid"))
                else:
                    self.send_json({"error": "preview_route_not_allowed"}, 404)
                    return
                self.send_json(self.snapshot())
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    server.acquisition = manager
    server.preview_root = root
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    root = Path("D:/AFP_Runtime/.afp-staging/acquisition-pause") / ("preview-" + uuid.uuid4().hex[:10])
    server = create_preview(root, args.port)
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/", "root": str(root)}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        result = server.acquisition.stop()
        server.server_close()
        if result.get("save_failed"):
            print("隔离采集尚未保存完成：" + str(result.get("last_error")), flush=True)


if __name__ == "__main__":
    main()
