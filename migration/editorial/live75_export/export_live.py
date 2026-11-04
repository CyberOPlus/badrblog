import os,json,zipfile,hashlib
from pathlib import Path
from urllib.parse import urlsplit
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
BID='2750143140558868280';API=f'https://www.googleapis.com/blogger/v3/blogs/{BID}'
t=json.loads(os.environ['BLOGGER_TOKEN_JSON']); c=json.loads(os.environ['BLOGGER_CLIENT_SECRET_JSON']); cfg=c.get('installed') or c.get('web') or {}
for k in ['client_id','client_secret','token_uri']:
 if not t.get(k) and cfg.get(k):t[k]=cfg[k]
s=AuthorizedSession(Credentials.from_authorized_user_info(t))
res=s.get(API,timeout=40);res.raise_for_status();blog=res.json()
assert str(blog['id'])==BID and urlsplit(blog.get('url','')).hostname in ['cyberoplus.com','www.cyberoplus.com']
ids=json.loads(Path('migration/editorial/live75_export/IDS_75.json').read_text())
rows=[]
with zipfile.ZipFile('cyberoplus_live_75_readonly.zip','w',zipfile.ZIP_DEFLATED,compresslevel=8) as z:
 for i,pid in enumerate(ids):
  try:
   r=s.get(API+'/posts/'+pid,params={'view':'ADMIN','fetchBody':'true'},timeout=50);r.raise_for_status();p=r.json()
   rows.append({k:p.get(k) for k in ['id','url','published','updated','status','labels','title']})
   z.writestr(f'LIVE_HTML/{pid}.html',p.get('content',''))
   print(f'{i+1}/{len(ids)} READ {pid} {p.get("title","")[:55]}',flush=True)
  except Exception as e:rows.append({'id':pid,'error':type(e).__name__+': '+str(e)[:240]});print(f'READ_FAILURE {pid} {e}',flush=True)
 z.writestr('LIVE_METADATA_75.json',json.dumps(rows,ensure_ascii=False,indent=2))
 print('LIVE_READ_SUMMARY '+json.dumps({'total':len(ids),'read':sum('error' not in x for x in rows),'failure':sum('error' in x for x in rows)}),flush=True)