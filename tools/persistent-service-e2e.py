#!/usr/bin/env python3
"""Ticket 197: packaged service restarts, no-window mobile command and shared login.

Uses disposable verified accounts, a harmless printf grant and private profiles.
No owner login or credential is read. MIKE_E2E_PACKAGE_DIR optionally selects a
staged service; MIKE_E2E_DESKTOP_DIR optionally selects its staged Desktop.
Run under xvfb-run to include the actual WebKit account adoption check.
"""
import importlib.util
import json
import os
from pathlib import Path
import runpy
import secrets
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
APPS=ROOT.parent/'my-euglena-apps'
os.environ.setdefault('CODE_BIN',str(Path.home()/'.local/share/cdlvsm/packages/code/v2.14.5/code'))
helpers=runpy.run_path(str(ROOT/'tools/service-e2e.py'))
api,until,Service=(helpers[k] for k in ('api','until','Service'))


def main():
    mongo=next(line.split('=',1)[1].strip().strip('\"\'') for line in (APPS/'auth/.env').read_text().splitlines() if line.startswith('MONGO_URI='))
    email='qa-persistent-'+secrets.token_hex(6)+'@localhost'; password=secrets.token_urlsafe(24)+'!A1'
    token=None; service=None; client=None
    with tempfile.TemporaryDirectory(prefix='mike-persistent-e2e-') as temporary:
        area=Path(temporary)
        try:
            assert api('auth',{'_class':'SignUp','email':email,'password':password})['_class']=='SignedUp'
            query='const d=db.getSiblingDB("auth");print(d.state.findOne({_id:'+json.dumps('verify_'+email)+'}).value.code);'
            got=subprocess.run(['docker','exec','mongodb','mongosh',mongo,'--quiet','--eval',query],capture_output=True,text=True,check=True)
            assert api('auth',{'_class':'VerifyEmail','email':email,'code':got.stdout.strip()})['_class']=='Verified'
            token=api('auth',{'_class':'Authenticate','email':email,'password':password})['token']
            service=Service(area)
            package=os.environ.get('MIKE_E2E_PACKAGE_DIR')
            def start():
                if not package:service.start();return
                env=dict(service.env,MIKE_SERVICE_CODE_BIN=os.environ['CODE_BIN'])
                service.process=subprocess.Popen([str(Path(package)/'mike-service'),'run'],cwd=area,env=env,
                                                  stdin=subprocess.DEVNULL,stdout=service.log,stderr=subprocess.STDOUT)
                until('packaged service',lambda:helpers['mike_service'].reachable(service.run_dir),20,.1)
            start(); client=service.client('cli')
            first=client.attach(token,'auto'); device=first['device_id']
            grant=client.call('/v1/grants/add',{'root':str(area),'run':['/usr/bin/printf'],'ask':[], 'minutes':10})
            grant_id=grant['grant']['id'];client.detach();client=None
            service.kill(); start()
            def online():
                devices=api('mike-interface',{'_class':'MyDevices'},token).get('items',[])
                return next((d for d in devices if d['device_id']==device and d['online'] and any(g['id']==grant_id for g in d['grants'])),None)
            until('device and grant online without any UI clients',online,30)
            print('PASS same device and saved grant reconnect after process kill without CLI/Desktop',flush=True)
            # A mobile browser, using only the remote web backend, sends a real task.
            js='''import {chromium} from 'playwright';
let raw='';for await(const x of process.stdin)raw+=x;const c=JSON.parse(raw);
const browser=await chromium.launch({headless:true});
try{const page=await browser.newPage({viewport:{width:390,height:844},isMobile:true});
await page.goto(c.host+'/');
const result=await page.evaluate(async c=>{
 const particle={_class:'RunTerminalStep',argv:['/usr/bin/printf','mobile-after-restart'],cwd:c.cwd,device_id:c.device};
 const r=await fetch('/api/mike-interface',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({_class:'Impulse',token:c.token,particle})});return r.json();
},c);if(result._class!=='InvocationAccepted')throw Error(result._class);
console.log(JSON.stringify({id:result.id}));}finally{await browser.close();}'''
            remote=subprocess.run(['node','--input-type=module','-e',js],cwd=APPS/'tools/browser',
                                  input=json.dumps({'host':helpers['HOST'],'token':token,'device':device,'cwd':str(area)}),capture_output=True,text=True,timeout=45)
            assert remote.returncode==0,'mobile browser request failed: '+remote.stderr[-300:]
            invocation=json.loads(remote.stdout)['id']
            def done():
                progress=api('mike-interface',{'_class':'InvocationProgress','id':invocation},token)
                return progress if progress.get('state') in ('completed','failed','rejected','done') else None
            progress=until('remote printf result',done,40)
            assert progress.get('result',{}).get('state')=='completed' and progress['result']['exit_code']==0
            assert progress['result']['stdout']=='mobile-after-restart'
            print('PASS mobile web → backend → no-window Linux service → verified printf result',flush=True)
            # Only now attach a CLI-like client, with no account token.
            client=service.client('cli');shared=client.attach()
            assert shared['account']==email and shared['chat_authenticated'] and shared['device_id']==device
            print('PASS CLI reuses restored shared account without another login',flush=True)
            desktop_dir=Path(os.environ.get('MIKE_E2E_DESKTOP_DIR',ROOT))
            profile=area/'desktop-profile';profile.mkdir()
            os.environ.update(MIKE_SERVICE_STATE_DIR=service.env['MIKE_SERVICE_STATE_DIR'],MIKE_SERVICE_RUNTIME_DIR=service.env['MIKE_SERVICE_RUNTIME_DIR'])
            spec=importlib.util.spec_from_file_location('desktop_webview_e2e',desktop_dir/'ui/desktop_webview.py')
            ui=importlib.util.module_from_spec(spec);spec.loader.exec_module(ui)
            with patch.object(ui.Path,'home',return_value=profile):
                window=ui.MikeWindow(desktop_dir,helpers['HOST'])
                result={'ok':False}
                deadline=time.monotonic()+35
                def verify():
                    if window.current_token and not window.inserting_token and window.stack.get_visible_child_name()=='conversation':
                        result['ok']=True;window.close();return False
                    if time.monotonic()>deadline:window.close();return False
                    return True
                ui.GLib.timeout_add(200,verify);ui.Gtk.main()
            assert result['ok'],'Desktop failed to adopt service account into real WebKit page'
            print('PASS Desktop WebKit adopts CLI/service login with no browser sign-in',flush=True)
            client.sign_out();assert not (area/'state/account-session.json').exists()
            service.kill();start();time.sleep(1)
            fresh=service.client('check');assert not fresh.attach()['chat_authenticated'];fresh.detach()
            print('PASS explicit sign-out survives process restart',flush=True)
        finally:
            if client:
                try:client.detach()
                except Exception:pass
            if service:service.stop();service.log.close()
            if token:
                try:
                    for d in api('mike-interface',{'_class':'MyDevices'},token).get('items',[]):
                        api('mike-interface',{'_class':'RevokeDevice','device_id':d['device_id']},token)
                except Exception:pass
            subprocess.run(['node','--input-type=module','-e',"import {forgetAccount} from './tools/browser/provision.mjs'; await forgetAccount(process.argv[1]);",email],
                           cwd=APPS,env=dict(os.environ,MONGO_URI=mongo),capture_output=True,timeout=30)


if __name__=='__main__':main()
