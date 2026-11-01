#!/usr/bin/python3
"""A real clipboard image through the installed CLI in a private X11 console.

Used by my-euglena-apps/tools/mike-options-e2e.py with MIKE_TEST_UI=1.
Requires Xvfb, xclip, Python Xlib/Pillow and the installed console. The parent
checks the real model answer and creates MIKE_TEST_DONE_FILE; no token is
passed to this visual driver. The user's desktop/clipboard are untouched.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from PIL import Image
from Xlib import X, XK, display
from Xlib.ext import xtest


def main():
    with tempfile.TemporaryDirectory(prefix='m185-clipboard-ui-') as temp:
        area=Path(temp)
        readfd,writefd=os.pipe()
        xvfb=subprocess.Popen(['Xvfb','-displayfd',str(writefd),'-screen','0','1280x800x24','-nolisten','tcp'],pass_fds=(writefd,),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        os.close(writefd)
        with os.fdopen(readfd) as read: number=read.readline().strip()
        env=dict(os.environ,DISPLAY=':'+number,WAYLAND_DISPLAY='',WINIT_UNIX_BACKEND='x11',XDG_CONFIG_HOME=str(area/'config'),XDG_STATE_HOME=str(area/'state'))
        config=area/'config/codelovesme-console';config.mkdir(parents=True)
        (config/'settings.json').write_text(json.dumps({'window.cols':130,'window.rows':38,'font.size':16}))
        packages=Path.home()/'.local/share/cdlvsm/packages'
        app=subprocess.Popen([str(packages/'console/current/console'),'-e',str(packages/'mike-cli/current/mike-cli'),'--project',os.environ['MIKE_TEST_PROJECT']],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        d=display.Display(':'+number)
        try:
            deadline=time.monotonic()+30;window=None
            while time.monotonic()<deadline:
                for candidate in d.screen().root.query_tree().children:
                    if candidate.get_attributes().map_state==X.IsViewable:window=candidate;break
                if window:break
                if app.poll() is not None:raise RuntimeError('CLI console exited')
                time.sleep(.2)
            assert window is not None,'CLI console did not open'
            window.configure(x=0,y=0,width=1280,height=800)
            window.set_input_focus(X.RevertToParent,X.CurrentTime);d.sync();time.sleep(2)
            def key(name,control=False):
                code=d.keysym_to_keycode(XK.string_to_keysym(name));ctrl=d.keysym_to_keycode(XK.string_to_keysym('Control_L'))
                if control:xtest.fake_input(d,X.KeyPress,ctrl)
                xtest.fake_input(d,X.KeyPress,code);xtest.fake_input(d,X.KeyRelease,code)
                if control:xtest.fake_input(d,X.KeyRelease,ctrl)
                d.sync()
            text=os.environ['MIKE_TEST_CLIP_MARKER']+' What is the dominant color of this image? Reply in English with one word. '
            for char in text:
                sym=ord(char);code=d.keysym_to_keycode(sym);mapping=d.get_keyboard_mapping(code,1)[0]
                shift=sym!=mapping[0];sc=d.keysym_to_keycode(XK.string_to_keysym('Shift_L'))
                if shift:xtest.fake_input(d,X.KeyPress,sc)
                xtest.fake_input(d,X.KeyPress,code);xtest.fake_input(d,X.KeyRelease,code)
                if shift:xtest.fake_input(d,X.KeyRelease,sc)
            d.sync()
            subprocess.run(['xclip','-selection','clipboard','-t','image/png','-i',os.environ['MIKE_TEST_CLIP_PNG']],env=env,check=True)
            key('v',True);time.sleep(.8);key('Return')
            deadline=time.monotonic()+240
            while time.monotonic()<deadline and not Path(os.environ['MIKE_TEST_DONE_FILE']).exists():time.sleep(.2)
            assert Path(os.environ['MIKE_TEST_DONE_FILE']).exists(),'clipboard model answer was not verified'
            time.sleep(.5);d.sync();geometry=window.get_geometry()
            raw=window.get_image(0,0,geometry.width,geometry.height,X.ZPixmap,0xffffffff)
            Image.frombytes('RGB',(geometry.width,geometry.height),raw.data,'raw','BGRX').save(os.environ['MIKE_TEST_SCREENSHOT'])
            print('PASS real Ctrl+V clipboard image in installed graphical Mike CLI',flush=True)
            key('d',True);time.sleep(.3)
        finally:
            d.close()
            if app.poll() is None:app.terminate()
            try:app.wait(timeout=5)
            except subprocess.TimeoutExpired:app.kill();app.wait()
            xvfb.terminate();xvfb.wait(timeout=5)


if __name__=='__main__':main()
