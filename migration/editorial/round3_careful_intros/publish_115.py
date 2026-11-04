#!/usr/bin/env python3
"""One-time, in-place edits to independently reviewed leads; never posts new articles."""
import base64,gzip,hashlib,json,os,re,time
from pathlib import Path
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession

BID='2750143140558868280';API='https://www.googleapis.com/blogger/v3/blogs/'+BID
DIR=Path('migration/editorial/round3_careful_intros')
MARK='CYBEROPLUS_EDITED_LEAD_2026_11_04'

def digest(s):return hashlib.sha256(s.encode('utf-8')).hexdigest()
def verify(x,msg):
    if not x:raise RuntimeError(msg)

def session():
    a=json.loads(os.environ['BLOGGER_TOKEN_JSON']);b=json.loads(os.environ['BLOGGER_CLIENT_SECRET_JSON']);cfg=b.get('installed') or b.get('web') or {}
    for v in ('client_id','client_secret','token_uri'):
        if not a.get(v) and cfg.get(v):a[v]=cfg[v]
    return AuthorizedSession(Credentials.from_authorized_user_info(a))

def get(s,pid):
    r=s.get(API+'/posts/'+pid,params={'view':'ADMIN','fetchBody':'true'},timeout=45);r.raise_for_status();return r.json()

def check_unchanged(old,new):
    a,b=BeautifulSoup(old,'html.parser'),BeautifulSoup(new,'html.parser')
    for tag in ('img','a','iframe','script','style','pre','code','textarea','table'):
        orig=[str(x) for x in a.find_all(tag)];dest=[str(x) for x in b.find_all(tag)]
        # Anchor and protected node HTML must survive byte-for-byte after reparsing.
        verify(orig==dest,'Content-preservation failure for '+tag)

def main():
    raw=base64.b64decode((DIR/'edit_candidates.b64').read_text('ascii'),validate=True)
    data=json.loads(gzip.decompress(raw));changes=[r for r in data['changes'] if r['new_opening']]
    verify(data['blog_id']==BID and len(data['changes'])==139 and len(changes)==115,'Unexpected article inventory')
    s=session();r=s.get(API,timeout=35);r.raise_for_status();blog=r.json()
    verify(str(blog.get('id'))==BID and urlsplit(blog.get('url','')).hostname in ['www.cyberoplus.com','cyberoplus.com'],'Wrong target blog')
    result=[]
    for i,row in enumerate(changes,1):
        pid=row['post_id'];status='BLOCKED';reason=''
        try:
            old=get(s,pid);verify(str(old.get('id'))==pid and old.get('status')=='LIVE','Not exact live post')
            verify(old.get('url')==row['expected_url'],'Permalink changed')
            verify(old.get('published')==row['expected_published'],'Publication date changed')
            source=old.get('content','')
            if MARK in source:status,reason='ALREADY_UPDATED','Marker present; skip replay'
            elif digest(source)!=row['baseline_sha256']:status,reason='SKIPPED_LIVE_DIFF','Live body differs from editorial baseline; do not overwrite'
            elif source.count(row['old_opening'])!=1:status,reason='SKIPPED_STRUCTURE','Source opening no longer unique'
            else:
                revised=source.replace(row['old_opening'],f'<!-- {MARK}:{pid} -->\n'+row['new_opening'],1)
                check_unchanged(source,revised)
                # Ensure all characters outside exactly one opening paragraph match.
                verify(revised.replace(f'<!-- {MARK}:{pid} -->\n'+row['new_opening'],row['old_opening'],1)==source,'Unexpected body diff')
                patch={'content':revised}
                t=row.get('title_candidate','')
                if t and old.get('title')==row['expected_old_title'] and len(t)<=88:patch['title']=t
                # Optimistic concurrency check right before update.
                now=get(s,pid)
                verify(now.get('content')==old.get('content') and now.get('updated')==old.get('updated'),'Concurrent edit; skip')
                p=s.patch(API+'/posts/'+pid,json=patch,timeout=75);p.raise_for_status()
                out=get(s,pid)
                for field in ('id','url','published','status','labels'):
                    verify(out.get(field)==old.get(field),'CRITICAL published metadata changed: '+field)
                verify(digest(out.get('content',''))==digest(revised),'CRITICAL body mismatch')
                verify(out.get('title')==patch.get('title',old.get('title')),'CRITICAL title mismatch')
                status,reason='PUBLISHED_AND_VERIFIED','Revised lead in existing post, original media and code preserved'
        except Exception as ex:
            reason=f'{type(ex).__name__}: {str(ex)[:180]}'
            if status not in ('SKIPPED_LIVE_DIFF','SKIPPED_STRUCTURE'):status='BLOCKED'
        result.append(dict(post_id=pid,status=status,reason=reason))
        print(f'{i}/{len(changes)} POST {pid} {status} {reason}',flush=True)
        if 'CRITICAL' in reason:print('CRITICAL error: halting writes',flush=True);break
        time.sleep(.15)
    summary={'candidates':len(changes),'by_status':{k:sum(x['status']==k for x in result) for k in set(x['status'] for x in result)}}
    Path('round3_results.json').write_text(json.dumps({'summary':summary,'results':result},indent=2),encoding='utf-8')
    print('ROUND3_RESULT='+json.dumps(summary),flush=True)
    if len(result)!=len(changes):raise RuntimeError('Incomplete batch from critical stop')

if __name__=='__main__':main()