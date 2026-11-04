import os,json,base64,zipfile,io,hashlib,time
from pathlib import Path
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
BID='2750143140558868280';API='https://www.googleapis.com/blogger/v3/blogs/'+BID;DIR=Path('migration/editorial/nov2026_reviewed_12')
def sha(s):return hashlib.sha256(s.encode('utf-8')).hexdigest()
def check(x,m):
 if not x:raise RuntimeError(m)
a=json.loads(os.environ['BLOGGER_TOKEN_JSON']); b=json.loads(os.environ['BLOGGER_CLIENT_SECRET_JSON']); cfg=b.get('installed') or b.get('web') or {}
for k in ('client_id','client_secret','token_uri'):
 if not a.get(k) and cfg.get(k):a[k]=cfg[k]
s=AuthorizedSession(Credentials.from_authorized_user_info(a))
def get(pid):
 r=s.get(API+'/posts/'+pid,params={'view':'ADMIN','fetchBody':'true'},timeout=45);r.raise_for_status();return r.json()
blog=s.get(API,timeout=30);blog.raise_for_status();o=blog.json();check(str(o['id'])==BID and urlsplit(o['url']).hostname in ('cyberoplus.com','www.cyberoplus.com'),'Wrong Blogger blog')
z=zipfile.ZipFile(io.BytesIO(base64.b64decode((DIR/'reviewed_payload.b64').read_text('ascii'),validate=True)));check(z.testzip() is None,'Corrupt editorial archive')
rows=json.loads(z.read('MANIFEST.json'));check(len(rows)==12 and len(set(x['post_id'] for x in rows))==12,'Unexpected inventory')
results=[]
for r in rows:
 pid=r['post_id'];status='BLOCKED';reason=''
 try:
  p=get(pid);new=z.read('HTML/'+pid+'.html').decode();old=p.get('content','')
  check(p.get('status')=='LIVE' and str(p.get('id'))==pid,'Not the existing published post')
  check(p.get('url')==r['url'] and p.get('published')==r['published'],'URL or published date drift')
  if sha(old)==sha(new):status='ALREADY_SAME';reason='Desired body is already live'
  elif sha(old)!=r['current_sha256']:status='SKIPPED_LIVE_DIFF';reason='Live body changed; do not overwrite'
  else:
   check(sha(new)==r['new_sha256'],'Payload mismatch')
   a=BeautifulSoup(old,'html.parser');b=BeautifulSoup(new,'html.parser')
   for tag in ('img','a','pre','code','table','script','iframe','style','textarea'):
    check([str(x) for x in a.find_all(tag)]==[str(x) for x in b.find_all(tag)],'Protected markup drift: '+tag)
   before=get(pid);check(before.get('updated')==p.get('updated') and before.get('content')==old,'Concurrent live change')
   rr=s.patch(API+'/posts/'+pid,json={'content':new},timeout=75);rr.raise_for_status()
   after=get(pid)
   for k in ('id','url','published','status','labels','title'):
    check(after.get(k)==p.get(k),'CRITICAL post metadata changed: '+k)
   check(sha(after.get('content',''))==sha(new),'CRITICAL live body mismatch')
   status='PUBLISHED_AND_VERIFIED';reason='In-place article lead rewritten; all original media and code preserved'
 except Exception as e:status='BLOCKED';reason=f'{type(e).__name__}: {str(e)[:175]}'
 results.append(dict(post_id=pid,status=status,reason=reason))
 print(json.dumps(results[-1]),flush=True)
 if reason.startswith('RuntimeError: CRITICAL'):break
 time.sleep(.15)
summary={k:sum(x['status']==k for x in results) for k in set(x['status'] for x in results)}
Path('results_12.json').write_text(json.dumps({'summary':summary,'results':results},indent=2));print('REVIEWED_12_RESULT='+json.dumps(summary),flush=True)