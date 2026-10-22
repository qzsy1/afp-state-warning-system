"""Measure device/capture separation through a diagnostic WebView connection.

CDP pointer input is trusted browser input. OS foreground display acceptance
requires independent desktop frames; compositor screenshots do not grant it.
"""
import argparse
import base64
import csv
import io
import json
import math
from pathlib import Path
import time

import psutil
from measure_click_to_curve import DesktopPage


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")


def pointer(page, button):
    location = page.evaluate("""(async()=>{const b=document.getElementById(%s);
      for(let p=b.parentElement;p;p=p.parentElement)if(p.tagName==='DETAILS')p.open=true;
      b.scrollIntoView({block:'center',behavior:'instant'});
      await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
      const r=b.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,disabled:b.disabled};})()""" % json.dumps(button))
    if location["disabled"]:
        raise RuntimeError("button disabled: " + button)
    for kind in ("mousePressed", "mouseReleased"):
        page.call("Input.dispatchMouseEvent", type=kind, x=location["x"], y=location["y"], button="left", clickCount=1)


def status(page):
    return page.evaluate("(async()=>await(await fetch('/api/acquisition/status?acquisition_mode=real',{cache:'no-store'})).json())()")


def resources(pid):
    samples = []
    for process in [psutil.Process(pid), *psutil.Process(pid).children(recursive=True)]:
        try:
            times = process.cpu_times()
            samples.append(dict(pid=process.pid, rss=process.memory_info().rss, cpu_seconds=times.user+times.system))
        except psutil.Error:
            continue
    return samples


def observe(page, pid, seconds):
    samples, end = [], time.monotonic()+seconds
    while time.monotonic() < end:
        samples.append(dict(host_monotonic=time.monotonic(), processes=resources(pid), browser=page.evaluate("""({
          browser_ms:performance.now(), samples:state.acquisitionStatus?.sample_count,
          connection:state.acquisitionStatus?.data_connection, queue:state.acquisitionStatus?.persistence_queue_depth,
          connection_freshness:(state.acquisitionStatus?.data_connection?.interfaces||[]).map(i=>({
            interface_id:i.interface_id,age_seconds:state.acquisitionStatus.status_monotonic-i.last_sample_monotonic,
            source_sequence:i.source_sequence,state:i.state})),
          high:state.acquisitionStatus?.persistence_queue_high_water_rows,
          coalesced:state.browserCoalescedPayloads, long_tasks:window.separationLongTasks||[],
          draws:window.separationDraws, esr_text:document.getElementById('esrNumericValues')?.textContent,
          freshness:(state.payload?.channels||[]).map(c=>({name:c.source_channel_name||c.name,
            age:c.source_observations?.at(-1)?.received_monotonic == null ? null :
              (state.payload.curve_transport?.assembled-c.source_observations.at(-1).received_monotonic)})),
          matrix:window.afpM3232MatrixEvidence})""")))
        time.sleep(min(1, max(0, end-time.monotonic())))
    return samples


def first_row_exists(path):
    try:
        raw = Path(path).read_bytes()
        if not raw.endswith(b'\n'):
            raw = raw[:raw.rfind(b'\n')+1]
        rows = list(csv.DictReader(io.StringIO(raw.decode('gb18030'))))
        return any(any((key.startswith(('温度','CH','应变')) or key == '薄膜压力原始响应') and value and math.isfinite(float(value))
            for key, value in row.items()) for row in rows)
    except (OSError, ValueError, UnicodeError, csv.Error):
        return False


def run(args):
    page = DesktopPage()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    original = page.evaluate("({save:controls.saveRoot.value,specimen:controls.liveSpecimen.value,layer:controls.newLayer.value})")
    dump(out/'original-ui.json', original)
    preparation_deadline=time.monotonic()+90
    while page.evaluate('Boolean(state.hardwareCheckInProgress || state.restoringInterfacePreferences)'):
        if time.monotonic()>=preparation_deadline:
            raise RuntimeError('initial preparation did not finish')
        time.sleep(.1)
    frozen = json.loads(Path(args.config).read_text('utf8'))
    preparation_deadline=time.monotonic()+90
    stable_since=None
    while time.monotonic()<preparation_deadline:
        try:
            current = page.evaluate('acquisitionConfig({allowUnverifiedPhysical:true})')
            differences=[key for key in ('interfaces','interface_channel_assignments','sample_rate_hz','processing_mode','selected_sensors')
                         if current[key] != frozen[key]]
            busy=page.evaluate('Boolean(state.hardwareCheckInProgress || state.restoringInterfacePreferences)')
            if not differences and not busy:
                stable_since=stable_since or time.monotonic()
                if time.monotonic()-stable_since>=1:break
            else:stable_since=None
        except RuntimeError:
            stable_since=None
        time.sleep(.1)
    else:
        raise RuntimeError('page configuration did not settle to the frozen enabled configuration')
    capture = Path('D:/AFP_Capture') / out.name
    capture.mkdir(exist_ok=False)
    probe=capture/'.write-check'
    probe.write_bytes(b'ok'); probe.unlink()
    page.evaluate('controls.saveRoot.value='+json.dumps(str(capture))+";controls.liveSpecimen.value='SEPARATION_MEASUREMENT';controls.newLayer.value='0';true")
    pid=next(p.pid for p in psutil.process_iter(['name','exe']) if p.info['name']=='AFP_Integrated_System_Modular.exe')
    initial=status(page)
    if initial['running'] or initial['capture_phase'] not in ('idle','completed'):
        raise RuntimeError('existing capture is not safely stopped')
    dump(out/'identity.json', {'config':current,'pid':pid,'exe':psutil.Process(pid).exe(), 'initial':initial})
    page.evaluate("""(()=>{window.separationLongTasks=[];window.separationObserver?.disconnect();
      window.separationFirstMatrix={};let matrix=window.afpM3232MatrixEvidence;
      Object.defineProperty(window,'afpM3232MatrixEvidence',{configurable:true,get:()=>matrix,set:value=>{
        matrix=value;if(value?.capture_generation && !separationFirstMatrix[value.capture_generation]){
          separationFirstMatrix[value.capture_generation]=value;
          const keys=Object.keys(separationFirstMatrix);if(keys.length>16)delete separationFirstMatrix[keys[0]];}}});
      window.separationDraws={};for(const name of ['drawChannelChart','drawThermocouple64Now']){
        const original=window[name];if(typeof original!=='function')continue;
        window[name]=function(...args){separationDraws[name]=(separationDraws[name]||0)+1;return original.apply(this,args);};}
      window.separationObserver=new PerformanceObserver(list=>{
        for(const e of list.getEntries())separationLongTasks.push({at:e.startTime,ms:e.duration});
        separationLongTasks=separationLongTasks.slice(-256);});separationObserver.observe({type:'longtask'});
      document.getElementById('stopAcquisitionButton').addEventListener('click',e=>window.separationStop={input_ms:e.timeStamp,trusted:e.isTrusted},true);
      return true;})()""")
    rounds=[]
    try:
        preparation_deadline=time.monotonic()+60
        while page.evaluate('Boolean(state.hardwareCheckInProgress || state.restoringInterfacePreferences)'):
            if time.monotonic()>=preparation_deadline:
                raise RuntimeError('initial preparation did not finish')
            time.sleep(.1)
        if args.connected:
            pointer(page,'connectDataButton')
            deadline=time.monotonic()+50
            while time.monotonic()<deadline:
                connection=page.evaluate('window.afpConnectionTrace')
                if connection and ('ready_observed_ms' in connection or connection.get('error')):
                    break
                time.sleep(.05)
            dump(out/'connect.json',connection)
            if not connection or connection.get('error') or 'ready_observed_ms' not in connection:
                raise RuntimeError('connection did not become ready')
        for index in range(args.rounds):
            if args.idle_seconds and index==args.idle_round:
                dump(out/'standby-resources.json',observe(page,pid,args.idle_seconds))
            before=status(page)
            previous=before.get('capture_generation')
            pointer(page,'startAcquisitionButton')
            deadline=time.monotonic()+40
            item={'round':index,'input_method':'CDP trusted pointer','desktop_foreground_verified':False}
            work=None
            while time.monotonic()<deadline:
                quick=page.evaluate("""(()=>{const t=window.afpCurveTrace,s=state.acquisitionStatus;return {
                  now:performance.now(),input:t?.input?.timestamp,complete:t?.complete,
                  generation:s?.capture_generation,phase:s?.capture_phase,error:s?.last_error,
                  raw:s?.raw_file,matrix:window.afpM3232MatrixEvidence,
                  all_sources:s?.capture_timeline?.events?.some(e=>e.stage==='S2_all_source_observations'),
                  write_event:s?.capture_timeline?.events?.find(e=>e.stage==='first_valid_sample_written')};})()""")
                if quick.get('generation')!=previous and quick.get('raw'):
                    work=quick['raw']+'.partial'
                    if 'first_main_row_observed_browser_ms' not in item and first_row_exists(work):
                        item['first_main_row_observed_browser_ms']=page.evaluate('performance.now()')
                if quick.get('write_event') and quick.get('generation')!=previous and 'first_write_event_observed_browser_ms' not in item:
                    item['first_write_event_observed_browser_ms']=quick['now']
                    item['write_event']=quick['write_event']
                if quick.get('all_sources') and quick.get('generation')!=previous and 'all_sources_observed_browser_ms' not in item:
                    item['all_sources_observed_browser_ms']=quick['now']
                if quick.get('complete') and quick.get('matrix') and 'first_main_row_observed_browser_ms' in item:
                    break
                if quick.get('generation')!=previous and quick.get('phase') in ('start_failed','finalization_failed','stop_timed_out'):
                    break
                time.sleep(.02)
            item['trace']=page.evaluate('window.afpCurveTrace?.snapshot()')
            item['matrix']=page.evaluate('window.afpM3232MatrixEvidence')
            item['first_matrix']=page.evaluate('window.separationFirstMatrix[window.afpCurveTrace?.generation]')
            item['backend']=status(page)
            trace=item['trace'] or {}
            item['valid']=bool(trace.get('complete') and item['backend']['running'] and item['matrix'])
            dump(out/f'round-{index:02d}-start.json',item)
            if not item['backend']['running']:
                rounds.append(item)
                dump(out/'rounds.json',rounds)
                raise RuntimeError('start was rejected or failed; evidence retained')
            if index==args.rounds-1 and item['valid']:
                dump(out/'active-resources.json',observe(page,pid,args.active_seconds))
            # Compositor frames are saved for pixel inspection, independently of OS foreground proof.
            image=page.call('Page.captureScreenshot',format='png')['data']
            (out/f'round-{index:02d}-compositor.png').write_bytes(base64.b64decode(image))
            pointer(page,'stopAcquisitionButton')
            stop_deadline=time.monotonic()+90
            while time.monotonic()<stop_deadline:
                stopped=status(page)
                if 'sampling_stopped_observed_browser_ms' not in item and any(e['stage']=='sampling_loop_stopped' for e in stopped.get('capture_timeline',{}).get('events',[])):
                    item['sampling_stopped_observed_browser_ms']=page.evaluate('performance.now()')
                if not stopped['running'] and stopped['finalization_complete']:
                    break
                time.sleep(.05)
            item['stop']={'input':page.evaluate('window.separationStop'), 'observed_browser_ms':page.evaluate('performance.now()'), 'status':stopped}
            if not stopped['capture_saved'] or not stopped['finalization_complete']:
                item['valid']=False
            rounds.append(item)
            dump(out/'rounds.json',rounds)
            print(json.dumps({'round':index,'valid':item['valid'],'curve_ms':trace.get('elapsed_ms'),'saved':stopped['capture_saved']}),flush=True)
            if not item['valid']:
                raise RuntimeError('capture or render verification failed; evidence retained')
            time.sleep(.2)
        if args.connected:
            pointer(page,'connectDataButton')
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                result=page.evaluate('window.afpConnectionTrace')
                if result and ('completed_ms' in result or result.get('error')):
                    break
                time.sleep(.03)
            dump(out/'disconnect.json',result)
    finally:
        active=status(page)
        if active.get('save_root') and Path(active['save_root']).resolve()==capture.resolve() and active['running']:
            page.evaluate("(async()=>{const s=await postJson('/api/acquisition/stop', {acquisition_mode:'real'},{timeoutMs:90000});renderAcquisitionStatus(s);return s;})()")
        if args.connected and (not active.get('capture_uuid') or active.get('save_root') and Path(active['save_root']).resolve()==capture.resolve()):
            page.evaluate("(async()=>{window.afpCurveTrace=null;const s=await postJson('/api/acquisition/disconnect', {},{timeoutMs:90000});renderAcquisitionStatus(s);return s;})()")
        page.evaluate('if(controls.saveRoot.value==='+json.dumps(str(capture))+'){controls.saveRoot.value='+json.dumps(original['save'])+';controls.liveSpecimen.value='+json.dumps(original['specimen'])+';controls.newLayer.value='+json.dumps(original['layer'])+';}true')
        page.socket.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    parser.add_argument('--config',required=True)
    parser.add_argument('--connected',action='store_true')
    parser.add_argument('--rounds',type=int,default=5)
    parser.add_argument('--idle-seconds',type=int,default=120)
    parser.add_argument('--idle-round',type=int,default=1)
    parser.add_argument('--active-seconds',type=int,default=30)
    run(parser.parse_args())
