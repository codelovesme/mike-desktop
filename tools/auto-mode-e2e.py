#!/usr/bin/env python3
"""Mobile Mike UI → real model → no-window service, Auto/Ask/Plan without grants.

Disposable account/service only. No owner token, profile or filesystem mutation
is used. Set MIKE_E2E_PACKAGE_DIR for packaged execution. Playwright receives QA
credentials on stdin and prints only outcome labels, never tokens.
"""
import json,os
from pathlib import Path
import runpy,secrets,subprocess,sys,tempfile
ROOT=Path(__file__).resolve().parents[1];APPS=ROOT.parent/'my-euglena-apps'
os.environ.setdefault('CODE_BIN',str(Path.home()/'.local/share/cdlvsm/packages/code/v2.14.5/code'))
h=runpy.run_path(str(ROOT/'tools/service-e2e.py'));api,until,Service=(h[k] for k in ('api','until','Service'))

JS=r'''
import {chromium} from 'playwright';
let raw='';for await(const chunk of process.stdin)raw+=chunk;const c=JSON.parse(raw);
const browser=await chromium.launch({headless:true});
try{
 const page=await browser.newPage({viewport:{width:390,height:844},isMobile:true});
 page.setDefaultTimeout(20000);
 await page.goto(c.host+'/desktop/mike');
 await page.locator('#guest-panel label:text-is("Email") + input').fill(c.email);
 await page.locator('#guest-panel label:text-is("Password") + input').fill(c.password);
 await page.locator('#guest-panel .card button',{hasText:'Sign in'}).click();
 await page.locator('#guest-panel .mike').first().waitFor();
 async function call(app,particle){return page.evaluate(async ({app,particle})=>{
   const token=localStorage.getItem('id:token');
   const r=await fetch('/api/'+app,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({_class:'Impulse',token,particle})});return r.json();
 },{app,particle});}
 for(const mode of ['auto','ask','plan']){
   await page.getByRole('button',{name:'Model settings',exact:true}).click();
   await page.getByRole('combobox',{name:'Mode',exact:true}).selectOption(mode);
   if(mode==='auto')await page.getByText('Commands run on your selected device as its service user, without another approval.',{exact:true}).waitFor();
   await page.getByRole('button',{name:'Close model settings',exact:true}).click();
   const marker='mode198-'+mode;
   const text=`On device id=${c.device}, run exactly /usr/bin/printf with one argument ${marker}, in directory /tmp, using RunTerminalStep once. Then report its actual returned stdout. Do not change files or use another tool. This is an execution request; follow the selected mode.`;
   const askResponse=page.waitForResponse(r=>{
      if(!r.url().endsWith('/api/mike')||r.request().method()!=='POST')return false;
      try{return JSON.parse(r.request().postData()).particle._class==='Ask';}catch{return false;}
   });
   await page.getByRole('textbox',{name:'Ask Mike',exact:true}).fill(text);
   await page.getByRole('textbox',{name:'Ask Mike',exact:true}).press('Enter');
   const response=await askResponse;const payload=JSON.parse(response.request().postData());
   if(payload.particle.mode!==mode)throw Error('UI did not send selected mode');
   const begun=await response.json();if(!begun.turn_id)throw Error('no model turn');
   let turn,answered=false,finished=false;const end=Date.now()+180000;
   while(Date.now()<end){
      turn=await call('mike',{_class:'Progress',turn_id:begun.turn_id});
      if(mode==='auto'&&turn.stage==='confirm')throw Error('Auto unexpectedly requested confirmation');
      if(mode==='ask'&&turn.stage==='confirm'){
        if(answered||turn.pending?.class!=='AnswerTerminalInput')throw Error('unexpected Ask confirmation');
        await page.getByRole('button',{name:'Yes, run it',exact:true}).waitFor();
        const asked=await call('mike-interface',{_class:'InvocationProgress',id:turn.pending.fields.id});
        if(asked.input?.version!==turn.pending.fields.version)throw Error('Ask version mismatch');
        console.log('PASS Ask holds the no-grant command until its exact question is approved');
        await page.getByRole('button',{name:'Yes, run it',exact:true}).click();answered=true;
      }
      if(mode==='plan'&&turn.stage==='confirm'){
        if(turn.pending?.app!=='mike-plan'||turn.steps?.length)throw Error('Plan reached a tool');
        await call('mike',{_class:'Confirm',turn_id:begun.turn_id,yes:false});
        console.log('PASS Plan proposes only and dispatches no command');finished=true;break;
      }
      if(turn.stage==='done'){
        if(mode==='plan'){
          if(turn.steps?.length)throw Error('Plan reached tools');
          console.log('PASS Plan replies without executing');finished=true;break;
        }
        if(mode==='ask'&&!answered)throw Error('Ask completed without confirmation');
        if(!turn.reply?.includes(marker)||!turn.steps?.some(s=>s.class==='RunTerminalStep'))throw Error('missing actual terminal result');
        console.log('PASS mobile UI '+mode+' → real model → Linux service returns exact stdout');finished=true;break;
      }
      if(turn.stage==='failed')throw Error('model turn failed');
      await page.waitForTimeout(400);
   }
   if(!finished)throw Error('timed out in '+mode+' at '+turn?.stage);
   if(mode!=='plan')await page.getByRole('button',{name:'Model settings',exact:true}).waitFor({state:'visible'});
 }
}finally{await browser.close();}
'''

def main():
 mongo=next(x.split('=',1)[1].strip().strip('\"\'') for x in (APPS/'auth/.env').read_text().splitlines() if x.startswith('MONGO_URI='))
 email='qa-auto198-'+secrets.token_hex(6)+'@localhost';password=secrets.token_urlsafe(24)+'!A1';token=None;service=None
 with tempfile.TemporaryDirectory(prefix='mike-auto198-') as t:
  try:
   assert api('auth',{'_class':'SignUp','email':email,'password':password})['_class']=='SignedUp'
   q='print(db.getSiblingDB("auth").state.findOne({_id:'+json.dumps('verify_'+email)+'}).value.code)'
   v=subprocess.run(['docker','exec','mongodb','mongosh',mongo,'--quiet','--eval',q],capture_output=True,text=True,check=True).stdout.strip()
   assert api('auth',{'_class':'VerifyEmail','email':email,'code':v})['_class']=='Verified'
   token=api('auth',{'_class':'Authenticate','email':email,'password':password})['token']
   service=Service(Path(t));package=os.environ.get('MIKE_E2E_PACKAGE_DIR')
   if package:
    service.process=subprocess.Popen([str(Path(package)/'mike-service'),'run'],env=dict(service.env,MIKE_SERVICE_CODE_BIN=os.environ['CODE_BIN']),
                                     stdin=subprocess.DEVNULL,stdout=service.log,stderr=subprocess.STDOUT)
    until('packaged service',lambda:h['mike_service'].reachable(service.run_dir),20,.1)
   else:service.start()
   client=service.client('setup');state=client.attach(token,'auto');device=state['device_id'];client.detach()
   assert not state.get('grants')
   until('mode capability',lambda:any(d['device_id']==device and 'terminal_modes_v1' in d['capabilities'] for d in api('mike-interface',{'_class':'MyDevices'},token).get('items',[])),20)
   run=subprocess.run(['node','--input-type=module','-e',JS],cwd=APPS/'tools/browser',input=json.dumps({'host':h['HOST'],'email':email,'password':password,'device':device}),capture_output=True,text=True,timeout=600)
   print(run.stdout,end='',flush=True)
   if run.returncode:raise RuntimeError('browser/model check failed: '+run.stderr[-1300:])
   check=service.client('check');final=check.attach();check.detach()
   assert not final.get('grants')
   results=[a for a in final.get('activity',[]) if a.get('kind')=='result' and a.get('state')=='completed']
   assert len(results)==2 and {x['stdout'] for x in results}=={'mode198-auto','mode198-ask'}
   assert all(x['exit_code']==0 for x in results)
   print('PASS exactly two harmless commands executed with no grants; Plan executed none',flush=True)
  finally:
   if service:service.stop();service.log.close()
   if token:
    try:
     for d in api('mike-interface',{'_class':'MyDevices'},token).get('items',[]):api('mike-interface',{'_class':'RevokeDevice','device_id':d['device_id']},token)
     api('mike',{'_class':'ForgetAllMemories'},token)
    except Exception:pass
   subprocess.run(['node','--input-type=module','-e',"import {forgetAccount} from './tools/browser/provision.mjs'; await forgetAccount(process.argv[1]);",email],cwd=APPS,env=dict(os.environ,MONGO_URI=mongo),capture_output=True,timeout=30)

if __name__=='__main__':main()
