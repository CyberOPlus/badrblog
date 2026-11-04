import os,json,base64,zipfile,io,hashlib,time
from pathlib import Path
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
BID='2750143140558868280';API='https://www.googleapis.com/blogger/v3/blogs/'+BID
DIR=Path('migration/editorial/nov2026_correct_2_sections')
def sha(s):return hashlib.sha256(s.encode('utf-8')).hexdigest()
def check(v,msg):
 if not v:raise RuntimeError(msg)
t=json.loads(os.environ['BLOGGER_TOKEN_JSON']);secret=json.loads(os.environ['BLOGGER_CLIENT_SECRET_JSON']);cfg=secret.get('installed') or secret.get('web') or {}
for k in ('client_id','client_secret','token_uri'):
 if not t.get(k) and cfg.get(k):t[k]=cfg[k]
s=AuthorizedSession(Credentials.from_authorized_user_info(t))
def get(pid):
 r=s.get(API+'/posts/'+pid,params={'view':'ADMIN','fetchBody':'true'},timeout=55);r.raise_for_status();return r.json()
b=s.get(API,timeout=35);b.raise_for_status();b=b.json();check(str(b['id'])==BID and urlsplit(b['url']).hostname in ('cyberoplus.com','www.cyberoplus.com'),'Wrong Blogger target')
z=zipfile.ZipFile(io.BytesIO(base64.b64decode((DIR/'reviewed_payload.b64').read_text('ascii'),validate=True)));check(z.testzip() is None,'Bad payload')
rows=json.loads(z.read('MANIFEST.json'));check(len(rows)==2 and len({r['post_id'] for r in rows})==2,'Wrong target count')
results=[]
for a in rows:
 pid=a['post_id'];status='BLOCKED';reason=''
 try:
  p=get(pid);old=p.get('content','');new=z.read('HTML/'+pid+'.html').decode('utf-8')
  check(str(p.get('id'))==pid and p.get('status')=='LIVE','Unexpected Post ID or status')
  check(p.get('url')==a['url'] and p.get('published')==a['published'] and p.get('title')==a['expected_live_title'],'Original permalink, timestamp or title changed')
  if sha(new)==sha(old):status='ALREADY_SAME';reason='Exact new HTML already published'
  elif sha(old)!=a['expected_live_sha']:status='SKIPPED_LIVE_CHANGED';reason='Current Blogger body changed since review; cannot overwrite'
  else:
   check(sha(new)==a['new_sha'],'Proposed HTML checksum mismatch')
   x=BeautifulSoup(old,'html.parser');y=BeautifulSoup(new,'html.parser')
   for tag in ('img','a','pre','code','table','script','iframe','style','textarea'):
    check([str(v) for v in x.find_all(tag)]==[str(v) for v in y.find_all(tag)],'Protected markup changed for '+tag)
   check(len(x.find_all(['h2','h3']))==len(y.find_all(['h2','h3'])),'Heading structure unexpectedly changed')
   current=get(pid);check(current.get('updated')==p.get('updated') and current.get('content')==old,'Concurrent edit detected')
   patch=s.patch(API+'/posts/'+pid,json={'content':new},timeout=90);patch.raise_for_status();after=get(pid)
   for k in ('id','url','published','status','title','labels'):
    check(after.get(k)==p.get(k),'CRITICAL original metadata changed: '+k)
   check(sha(after.get('content',''))==sha(new),'CRITICAL live body does not match')
   status='PUBLISHED_AND_VERIFIED';reason='Reviewed corrections saved; original URL, published timestamp, status, labels, title preserved'
 except Exception as e:
  reason=type(e).__name__+': '+str(e)[:180]
 results.append({'post_id':pid,'status':status,'reason':reason})
 print(json.dumps(results[-1]),flush=True)
 if 'CRITICAL' in reason:break
 time.sleep(.15)
summary={k:sum(x['status']==k for x in results) for k in set(x['status'] for x in results)}
Path('results_correct_2.json').write_text(json.dumps({'summary':summary,'results':results},indent=2))
print('CORRECT_2_RESULT='+json.dumps(summary),flush=True)