#!/usr/bin/python3
"""Real graphical Ctrl+Space through console, optionally through the IDE.

Uses a private Xvfb display and state directory. The recorder/transcriber are
stand-ins: proves the physical input/recorder lifecycle without recording the
owner or sending audio to a model. --client names the staged/installed client/.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from Xlib import X, XK, display
from Xlib.ext import xtest

CHILD = r'''
import sys,time,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import screen
log=Path(sys.argv[2])
def note(event):
    with log.open('a') as f:f.write(json.dumps(event)+'\n')
class Recorder:
    process=None
    @classmethod
    def available(cls):return True
    def start(self):self.process=True;self.started=time.monotonic();note('start');return True
    def seconds(self):return int(time.monotonic()-self.started) if self.process else 0
    def stop(self):self.process=None;note('stop');return b'RIFF'
class Terminal:
    owner,device,workspace='voice-test@localhost','test','/scratch'
    turn,finished,view,local,waiting,status,started='',False,{},None,[],{},0
    def __init__(self):self.service=self
    def running(self):return False
    def doing(self):return ''
    def poll(self):return None
    def command(self,line):return None
    def call(self,route,body,timeout=20):
        assert route=='/v1/chat/hear';note('hear');return {'text':'heard words '}
screen.Recorder=Recorder
with screen.Screen(Terminal(),None) as ui:
    draw=ui.draw
    last=['']
    def render(*a,**k):
        if ui.editor.text!=last[0]:last[0]=ui.editor.text;note({'draft':ui.editor.text})
        return draw(*a,**k)
    ui.draw=render
    note('ready')
    ui.loop(600)
note('exit')
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--console',type=Path,required=True)
    parser.add_argument('--client',type=Path,required=True)
    parser.add_argument('--ide',type=Path)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='mike-hold-ui-') as temp:
        root=Path(temp);log=root/'events';child=root/'child.py';child.write_text(CHILD)
        readfd,writefd=os.pipe()
        xvfb=subprocess.Popen(['Xvfb','-displayfd',str(writefd),'-screen','0','1280x800x24','-nolisten','tcp'],pass_fds=(writefd,),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        os.close(writefd)
        with os.fdopen(readfd) as f:number=f.readline().strip()
        env=dict(os.environ,DISPLAY=':'+number,WAYLAND_DISPLAY='',WINIT_UNIX_BACKEND='x11',XDG_CONFIG_HOME=str(root/'config'),XDG_STATE_HOME=str(root/'state'))
        client=[str(Path('/usr/bin/python3')),str(child),str(args.client.resolve()),str(log)]
        argv=[str(args.console.resolve()),'-e',*client]
        if args.ide:
            import shlex
            shell=root/'shell';shell.write_text('#!/bin/sh\nexec '+shlex.join(client)+'\n');shell.chmod(0o700)
            config=root/'config/codelovesme-ide';config.mkdir(parents=True)
            (config/'settings.json').write_text(json.dumps({'terminal.integrated.shell':str(shell),'terminal.location':'c'}))
            argv=[str(args.console.resolve()),'-e',str(args.ide.resolve()),str(root)]
        output=(root/'app.log').open('w')
        app=subprocess.Popen(argv,env=env,stdout=output,stderr=subprocess.STDOUT)
        d=display.Display(':'+number)
        def events():return [json.loads(s) for s in log.read_text().splitlines()] if log.exists() else []
        def until(check,what,timeout=15):
            end=time.monotonic()+timeout
            while time.monotonic()<end:
                if check():return
                if app.poll() is not None:raise AssertionError('app exited: '+(root/'app.log').read_text()[-2000:])
                time.sleep(.05)
            raise AssertionError(what+': '+repr(events())+'\n'+(root/'app.log').read_text()[-2000:])
        def key(name,pressed):
            code=d.keysym_to_keycode(XK.string_to_keysym(name))
            xtest.fake_input(d,X.KeyPress if pressed else X.KeyRelease,code);d.sync()
        def tap(name):key(name,True);key(name,False)
        try:
            windows=[]
            def appeared():
                windows[:]=[w for w in d.screen().root.query_tree().children if w.get_attributes().map_state==X.IsViewable]
                return bool(windows)
            until(appeared,'no window')
            window=windows[0];window.set_input_focus(X.RevertToParent,X.CurrentTime);d.sync();time.sleep(.5)
            if args.ide:
                key('Control_L',True);tap('j');key('Control_L',False)
            until(lambda:'ready' in events(),'CLI did not start')
            for char in 'draft ':tap('space' if char==' ' else char)
            for count,release in ((1,'space'),(2,'Control_L')):
                key('Control_L',True);key('space',True)
                until(lambda:events().count('start')==count,'hold did not start')
                time.sleep(1.0)  # Includes the platform's auto-repeat interval.
                assert events().count('stop')==count-1,events()
                assert events().count('start')==count,events()
                key(release,False)
                until(lambda:events().count('hear')==count,'release did not transcribe')
                key('Control_L' if release=='space' else 'space',False)
                until(lambda:any(isinstance(e,dict) and e['draft'].startswith('draft ') and e['draft'].count('heard words')==count for e in events()),'draft not preserved')
            key('Control_L',True);key('space',True)
            until(lambda:events().count('start')==3,'third hold did not start')
            other=d.screen().root.create_window(0,0,50,50,0,d.screen().root_depth,X.InputOutput,X.CopyFromParent)
            other.map();other.set_input_focus(X.RevertToParent,X.CurrentTime);d.sync()
            until(lambda:events().count('hear')==3,'focus loss did not stop')
            key('space',False);key('Control_L',False)
            window.set_input_focus(X.RevertToParent,X.CurrentTime);d.sync();other.destroy()
            key('Control_L',True);key('space',True)
            until(lambda:events().count('start')==4,'final hold did not start')
            tap('u');tap('d');key('space',False);key('Control_L',False)
            end=time.monotonic()+10
            while time.monotonic()<end and 'exit' not in events():time.sleep(.05)
            assert 'exit' in events(),events()
            assert events().count('stop')==4 and events().count('hear')==3,events()
            print('PASS real Ctrl+Space: hold/repeat, Space-first and Ctrl-first release, draft, focus loss, exit'+(' through IDE' if args.ide else ' in console'))
        finally:
            d.close()
            if app.poll() is None:app.terminate()
            try:app.wait(timeout=5)
            except subprocess.TimeoutExpired:app.kill();app.wait()
            output.close();xvfb.terminate();xvfb.wait(timeout=5)

if __name__=='__main__':main()
