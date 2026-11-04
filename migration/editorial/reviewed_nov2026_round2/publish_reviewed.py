"""Publish individually reviewed CyberoPlus HTML to same Blogger post IDs only.
Never changes original publication date, URL, labels or status; skips changed live bodies.
"""
import hashlib, json, os, sys, time
from pathlib import Path
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession

BLOG_ID='2750143140558868280'
BASE=f'https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}'
ROOT=Path('migration/editorial/reviewed_nov2026_round2')

def need(x,msg):
    if not x: raise RuntimeError(msg)

def sha(v):return hashlib.sha256(v.encode('utf-8')).hexdigest()

def session():
    token=json.loads(os.environ['BLOGGER_TOKEN_JSON']); secret=json.loads(os.environ['BLOGGER_CLIENT_SECRET_JSON'])
    cfg=secret.get('installed') or secret.get('web') or {}
    for k in ('client_id','client_secret','token_uri'):
        if not token.get(k) and cfg.get(k):token[k]=cfg[k]
    return AuthorizedSession(Credentials.from_authorized_user_info(token))

def post(s,pid):
    r=s.get(BASE+'/posts/'+pid,params={'view':'ADMIN','fetchBody':'true'},timeout=40)
    r.raise_for_status();return r.json()

def seq(s,tag,attr=None):
    return [(str(x) if attr is None else x.get(attr)) for x in s.find_all(tag)]

def img_link(s):
    return [t.find_parent('a').get('href') if t.find_parent('a') else None for t in s.find_all('img')]

def check_media(original,edited):
    a=BeautifulSoup(original,'html.parser'); b=BeautifulSoup(edited,'html.parser')
    for attr in ('src','alt','title','data-original-height','data-original-width'):
        need(seq(a,'img',attr)==seq(b,'img',attr),'Image attribute changed: '+attr)
    need(img_link(a)==img_link(b),'Image lightbox URL changed')
    for t in ('pre','code','script','iframe','style','textarea'):
        need(seq(a,t)==seq(b,t),'Protected embedded markup modified: '+t)
    need(len(b.find_all('table'))>=len(a.find_all('table')),'Original tables lost')
    need(not b.find('h1'),'Unexpected heading H1')
    return True

def main():
    manifest=json.loads((ROOT/'reviewed_manifest.json').read_text('utf-8'))
    rows=manifest['posts']
    need(str(manifest.get('blog_id'))==BLOG_ID and len(rows)==2,'Unexpected batch')
    need(len(set(r['post_id'] for r in rows))==len(rows),'Duplicate Post ID')
    need(os.environ.get('BLOGGER_BLOG_ID')==BLOG_ID,'Wrong Blogger blog secret')
    s=session();r=s.get(BASE,timeout=40);r.raise_for_status()
    info=r.json()
    need(str(info.get('id'))==BLOG_ID,'Blogger blog mismatch')
    need((urlsplit(info.get('url','')).hostname or '').lower() in ('cyberoplus.com','www.cyberoplus.com'),'Blogger host mismatch')
    outcome=[]
    for row in rows:
        pid=row['post_id'];status='SKIPPED';reason=''
        try:
            new=(ROOT/(pid+'.html')).read_text('utf-8')
            need(sha(new)==row['html_sha256'],'Payload hash mismatch')
            old=post(s,pid)
            need(str(old.get('id'))==pid and old.get('status')=='LIVE','Unexpected status or Post ID')
            need(old.get('url')==row['url'],'Original URL changed')
            if sha(old.get('content',''))==row['html_sha256'] and old.get('title')==row['title']:
                status='ALREADY_VERIFIED';reason='Already saved; URL/date/status preserved'
            elif sha(old.get('content',''))!=row['source_sha256']:
                status='SKIPPED';reason='Live content changed since snapshot; do not overwrite'
            else:
                check_media(old['content'],new)
                need(row['new_words']>=row['source_words'],'New content unexpectedly shorter than source')
                title=row['title'];need(30<=len(title)<=88,'Invalid SEO title')
                before={k:old.get(k) for k in ('id','published','url','status','labels')}
                latest=post(s,pid)
                need(latest.get('updated')==old.get('updated') and latest.get('content')==old.get('content'),'Concurrent edit; skip')
                response=s.patch(BASE+'/posts/'+pid,json={'content':new,'title':title},timeout=70)
                response.raise_for_status()
                verify=post(s,pid)
                for k,v in before.items():need(verify.get(k)==v,'CRITICAL preserved metadata changed: '+k)
                need(sha(verify.get('content',''))==sha(new),'CRITICAL content mismatch')
                need(verify.get('title')==title,'CRITICAL title mismatch')
                status='PUBLISHED_AND_VERIFIED';reason='Existing live Post ID updated; URL, published, status, labels unchanged'
        except Exception as e:
            status='BLOCKED';reason=f'{type(e).__name__}: {str(e)[:200]}'
        print(json.dumps({'post_id':pid,'status':status,'reason':reason},ensure_ascii=False),flush=True)
        outcome.append({'post_id':pid,'url':row['url'],'status':status,'reason':reason})
        if status=='BLOCKED' and 'CRITICAL' in reason:break
    doc={'posts':outcome,'counts':{k:sum(p['status']==k for p in outcome) for k in set(p['status'] for p in outcome)}}
    Path('reviewed_publication_result.json').write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding='utf-8')
    print('REVIEWED_BATCH_RESULTS='+json.dumps(doc['counts']),flush=True)
    if len(outcome)<len(rows) or any(p['status']=='BLOCKED' for p in outcome):sys.exit(2)

if __name__=='__main__':main()