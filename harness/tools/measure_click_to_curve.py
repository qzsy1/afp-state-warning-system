"""Measure a visible AFP WebView using native Windows input and loopback CDP."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import time
import urllib.request

import websocket


class DesktopPage:
    def __init__(self, port=9223):
        pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
        page = next(p for p in pages if p["type"] == "page" and "127.0.0.1:8771/" in p["url"])
        self.socket = websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True, timeout=10)
        self.serial = 0

    def call(self, method, **params):
        self.serial += 1
        self.socket.send(json.dumps(dict(id=self.serial, method=method, params=params)))
        while True:
            reply = json.loads(self.socket.recv())
            if reply.get("id") == self.serial:
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                return reply["result"]

    def evaluate(self, expression):
        reply = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in reply:
            raise RuntimeError(reply["exceptionDetails"])
        return reply["result"].get("value")

    def click(self, element_id, pid, block_ms=0):
        user = ctypes.windll.user32
        user.SetProcessDPIAware()
        handles = []
        callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback
        def visit(hwnd, _):
            owner = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                title = ctypes.create_unicode_buffer(512)
                user.GetWindowTextW(hwnd, title, 512)
                if title.value.startswith("AFP "):
                    handles.append(hwnd)
            return True

        user.EnumWindows(visit, 0)
        if not handles:
            raise RuntimeError("AFP visible window not found")
        hwnd = handles[0]
        user.ShowWindow(hwnd, 3)
        user.SetForegroundWindow(hwnd)
        if user.GetForegroundWindow() != hwnd:
            current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
            foreground_thread = user.GetWindowThreadProcessId(user.GetForegroundWindow(), None)
            attached = user.AttachThreadInput(current_thread, foreground_thread, True)
            try:
                user.BringWindowToTop(hwnd)
                user.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    user.AttachThreadInput(current_thread, foreground_thread, False)
        if user.GetForegroundWindow() != hwnd:
            user.keybd_event(0x12, 0, 0, 0)
            user.keybd_event(0x12, 0, 2, 0)
            user.SetForegroundWindow(hwnd)
        self.call("Page.bringToFront")
        geometry = self.evaluate("""(async()=>{const b=document.getElementById(%s);
          for(let n=b.parentElement;n;n=n.parentElement)if(n.tagName==='DETAILS')n.open=true;
          b.scrollIntoView({block:'center',behavior:'instant'});
          await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
          const r=b.getBoundingClientRect();
          return {x:r.x+r.width/2,y:r.y+r.height/2,w:innerWidth,h:innerHeight,
            hit:document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===b,
            disabled:b.disabled,visible:document.visibilityState};})()""" % json.dumps(element_id))
        if geometry["disabled"] or geometry["visible"] != "visible" or not geometry["hit"]:
            raise RuntimeError("Button cannot receive visible input: " + json.dumps(geometry))
        rectangle = wintypes.RECT()
        user.GetClientRect(hwnd, ctypes.byref(rectangle))
        origin = wintypes.POINT(0, 0)
        user.ClientToScreen(hwnd, ctypes.byref(origin))
        x = origin.x + round(geometry["x"] * rectangle.right / geometry["w"])
        y = origin.y + round(geometry["y"] * rectangle.bottom / geometry["h"])
        user.SetCursorPos(x, y)
        if user.GetForegroundWindow() != hwnd:
            raise RuntimeError("AFP window is not foreground")
        if block_ms:
            self.evaluate("setTimeout(()=>{const end=performance.now()+%s;while(performance.now()<end){}},0);true" % int(block_ms))
            time.sleep(0.1)
        entered = time.perf_counter()
        # Native down/up enters the OS input queue; CDP does not synthesize the click.
        user.mouse_event(0x0002, 0, 0, 0, 0)
        user.mouse_event(0x0004, 0, 0, 0, 0)
        return dict(native_input_monotonic=entered, clock_domain="host_perf_counter_seconds", screen_x=x, screen_y=y, hwnd=hwnd)


BASELINE_HOOK = r"""(()=>{
if(window.afpDesktopEvidence)return false;
window.afpDesktopEvidence={events:[],draws:[]};
const log=(stage,detail={})=>afpDesktopEvidence.events.push({stage,at:performance.now(),...detail});
document.getElementById('startAcquisitionButton').addEventListener('click',e=>{
  log('input',{input:e.timeStamp,trusted:e.isTrusted});
},true);
for(const name of ['acquireRealControl','testSensorConnection','waitForCaptureStart','loadRealtime']){
  const original=window[name];window[name]=async function(...args){
    log(name+':begin');try{return await original.apply(this,args);}
    finally{log(name+':end');}
  };
}
const post=postJson;window.postJson=async function(url,...args){
  log('request:begin',{url});try{return await post(url,...args);}
  finally{log('request:end',{url});}
};
const draw=drawChannelChart;window.drawChannelChart=function(canvas,channel,...args){
  const result=draw(canvas,channel,...args);
  afpDesktopEvidence.draws.push({at:performance.now(),channel:channel.name,
    points:channel.actual.filter(Number.isFinite).length,connected:!!canvas?.isConnected});
  if(afpDesktopEvidence.draws.length>2000)afpDesktopEvidence.draws.shift();return result;
};return true;})()"""


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--port", type=int, default=9223)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=45)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--button", default="startAcquisitionButton")
    parser.add_argument("--block-main-thread-ms", type=int, default=0)
    args = parser.parse_args()
    if args.output.resolve().drive.upper() != "D:":
        raise ValueError("Evidence must be on D:")
    args.output.mkdir(parents=True, exist_ok=True)
    page = DesktopPage(args.port)
    if args.baseline:
        page.evaluate(BASELINE_HOOK)
    metadata = page.evaluate("""({url:location.href,visibility:document.visibilityState,
      application:state.bootstrap?.application,
      config:(()=>{const c=acquisitionConfig({allowUnverifiedPhysical:true});return {
        selected_sensors:c.selected_sensors,assignments:c.interface_channel_assignments,
        interfaces:c.interfaces.map(i=>({id:i.id,enabled:i.enabled,endpoint:i.endpoint,driver:i.driver,read_profile:i.read_profile})),
        save_root:c.save_root,specimen_id:c.specimen_id,processing_mode:c.processing_mode,
        auto_process:controls.autoProcessParameters.checked,mysql:c.mysql_enabled,mysql_local:c.mysql_local_enabled};})()})""")
    previous_id = page.evaluate("window.afpCurveTrace?.id")
    evidence = dict(metadata=metadata, input=page.click(args.button, args.pid, args.block_main_thread_ms), frames=[])
    from PIL import ImageGrab
    deadline = time.perf_counter() + args.seconds
    while time.perf_counter() < deadline:
        started = time.perf_counter()
        path = args.output / f"frame-{len(evidence['frames']):04d}.png"
        snapshot = page.evaluate("({trace:window.afpCurveTrace?.snapshot(),baseline:window.afpDesktopEvidence,status:state.acquisitionStatus?.capture_phase,text:document.getElementById('acquisitionStatus')?.textContent})")
        snapshot["foreground"] = ctypes.windll.user32.GetForegroundWindow() == evidence["input"]["hwnd"]
        bitmap = ImageGrab.grab()
        snapshot["screenshot_captured_perf_counter"] = time.perf_counter()
        bitmap.save(path, compress_level=1)
        evidence["frames"].append(dict(monotonic=started, path=str(path), **snapshot))
        (args.output / "events.json").write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        if (snapshot.get("trace") or {}).get("complete") and snapshot["trace"].get("start_request_id") != previous_id:
            break
        time.sleep(0.1)
    page.socket.close()
    print(json.dumps(dict(output=str(args.output), frames=len(evidence["frames"]), last=evidence["frames"][-1].get("text")), ensure_ascii=False))


if __name__ == "__main__":
    main()
