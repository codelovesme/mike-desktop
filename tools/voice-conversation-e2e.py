#!/usr/bin/env python3
"""Two real STT/model/TTS rounds using synthetic input and a private null sink.

Creates a disposable account and scratch service. No owner account, microphone,
or default audio device is used. Requires the local host/model, ffmpeg and pactl.
"""
import base64, io, json, os, queue, runpy, secrets, shlex, subprocess, sys, tempfile, time, wave
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];APPS=ROOT.parent/'my-euglena-apps'
os.environ.setdefault('CODE_BIN',str(Path.home()/'.local/share/cdlvsm/packages/code/v2.14.5/code'))
helpers=runpy.run_path(str(ROOT/'tools/service-e2e.py'))
api,until,Service=(helpers[k] for k in ('api','until','Service'))
sys.path.insert(0,str(ROOT/'client'))
import screen
from cli import Terminal


def main():
    mongo=next(line.split('=',1)[1].strip().strip('\"\'') for line in (APPS/'auth/.env').read_text().splitlines() if line.startswith('MONGO_URI='))
    email='qa-voice-'+secrets.token_hex(6)+'@localhost';password=secrets.token_urlsafe(24)+'!A1'
    token=None;client=None;service=None;sink_id=None;ui=None;old_path=os.environ.get('PATH','')
    with tempfile.TemporaryDirectory(prefix='mike-voice-e2e-') as temporary:
        area=Path(temporary)
        try:
            assert api('auth',{'_class':'SignUp','email':email,'password':password})['_class']=='SignedUp'
            query='const d=db.getSiblingDB("auth");print(d.state.findOne({_id:'+json.dumps('verify_'+email)+'}).value.code);'
            result=subprocess.run(['docker','exec','mongodb','mongosh',mongo,'--quiet','--eval',query],capture_output=True,text=True,check=True)
            assert api('auth',{'_class':'VerifyEmail','email':email,'code':result.stdout.strip()})['_class']=='Verified'
            token=api('auth',{'_class':'Authenticate','email':email,'password':password})['token']
            service=Service(area)
            package=os.environ.get('MIKE_E2E_PACKAGE_DIR')
            if package:
                service.process=subprocess.Popen([str(Path(package)/'mike-service'),'run'],cwd=area,env=service.env,stdin=subprocess.DEVNULL,stdout=service.log,stderr=subprocess.STDOUT)
                until('packaged service',lambda:helpers['mike_service'].reachable(service.run_dir),20,.1)
            else:service.start()
            client=service.client('voice-test')
            assert client.attach(token,connect='auto')['voice_chat']
            clips=[]
            for index,word in enumerate(('hello','goodbye')):
                reading=client.call('/v1/chat/read',{'text':'Please reply with just the word '+word+'.'})
                def ready():
                    answer=client.call('/v1/chat/spoken',{'reading_id':reading['reading_id']})
                    assert answer['stage'] in ('ready','speaking'),answer.get('reason')
                    return answer if answer['stage']=='ready' else None
                sound=until('synthetic spoken input',ready,120,.3)
                raw=area/f'input-{index}.{sound["format"]}';raw.write_bytes(base64.b64decode(sound['audio_base64']))
                clip=area/f'clip-{index}.wav'
                subprocess.run(['ffmpeg','-v','error','-i',str(raw),'-ar','16000','-ac','1','-c:a','pcm_s16le',str(clip)],check=True)
                clips.append(clip)
            sink='mike_voice_qa_'+secrets.token_hex(4)
            sink_id=subprocess.run(['pactl','load-module','module-null-sink','sink_name='+sink],capture_output=True,text=True,check=True).stdout.strip()
            wrapper=area/'bin';wrapper.mkdir();player=wrapper/'pw-play'
            player.write_text('#!/bin/sh\nexec /usr/bin/pw-play --target '+shlex.quote(sink)+' "$@"\n');player.chmod(0o700)
            os.environ['PATH']=str(wrapper)+os.pathsep+old_path
            starts=[]
            class Capture:
                process=None
                @classmethod
                def available(cls):return True
                def start(self):
                    self.started=time.monotonic();self.process=True;index=len(starts)
                    self.path=clips[index] if index<len(clips) else area/'silence.wav'
                    if index>=len(clips):
                        with wave.open(str(self.path),'wb') as w:w.setparams((1,2,16000,0,'NONE','none'));w.writeframes(b'\0'*3200)
                    starts.append(index);return True
                def seconds(self):return int(time.monotonic()-self.started) if self.process else 0
                def stop(self):self.process=None;return self.path.read_bytes()
            terminal=Terminal(client,str(area),True,io.StringIO());terminal.defer=True;terminal.attach()
            ui=screen.Screen(terminal,fd_in=0,out=io.StringIO());ui.size=lambda:(100,35);terminal.prompt=ui;ui.key_releases=True
            heard=[];replies=[];original=ui.audio_event
            def event(kind,payload):
                if kind=='transcript' and payload.get('conversation'):heard.append(payload['text'])
                original(kind,payload)
            ui.audio_event=event
            with patch.object(screen,'Recorder',Capture):
                ui.recorder=Capture();ui.handle('key','voice-chat');end=time.monotonic()+360;next_poll=0
                while time.monotonic()<end and len(starts)<3:
                    now=time.monotonic()
                    if now>=next_poll:
                        terminal.poll();next_poll=now+.5
                        if terminal.finished and terminal.turn not in [item[0] for item in replies]:
                            assert terminal.view.get('stage')=='done',terminal.view.get('reply')
                            replies.append((terminal.turn,terminal.view.get('reply','')))
                    ui.audio_tick(now)
                    try:kind,payload=ui.heard.get_nowait();ui.audio_event(kind,payload)
                    except queue.Empty:pass
                    assert ui.conversation,'conversation stopped: '+ui.flash+' '+ui.out.getvalue()[-1000:]
                    time.sleep(.05)
                assert len(starts)>=3 and len(replies)==2 and len(heard)==2,(len(starts),len(replies),heard)
                ui.handle('key','ctrl-space');assert not ui.conversation and not ui.recorder.process and not ui.player.process
                ui.audio_tick();assert len(starts)==3
            print('PASS two real STT → model → TTS → null-sink playback → listen rounds',flush=True)
            print('PASS Ctrl+Space closes conversation without restarting capture',flush=True)
        finally:
            if ui:ui.stop_conversation(quiet=True)
            os.environ['PATH']=old_path
            if sink_id:subprocess.run(['pactl','unload-module',sink_id],capture_output=True)
            if client:
                try:client.revoke()
                except Exception:pass
                client.detach()
            if service:service.stop();service.log.close()
            if token:
                try:api('mike',{'_class':'ForgetAllMemories'},token)
                except Exception:pass
            subprocess.run(['node','--input-type=module','-e',"import {forgetAccount} from './tools/browser/provision.mjs'; await forgetAccount(process.argv[1]);",email],cwd=APPS,env=dict(os.environ,MONGO_URI=mongo),capture_output=True,timeout=30)

if __name__=='__main__':main()
