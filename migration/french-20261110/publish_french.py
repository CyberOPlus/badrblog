#!/usr/bin/env python3
"""Update exactly 259 existing CyberoPlus posts from a reviewed local French payload."""
import base64, hashlib, json, os, re, time, lzma
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

BLOG_ID='2750143140558868280'
BASE='https://www.googleapis.com/blogger/v3/blogs/'+BLOG_ID
HOSTS={'cyberoplus.com','www.cyberoplus.com'}
ROOT=Path('migration/french-20261110')
EXPECTED_PAYLOAD_SHA256='025586637b201afc98d469c2e26f74dcc9c16ca7aa3889d4bab6bbda83101ac2'

def need(value,message):
    if not value:raise RuntimeError(message)

def digest(text):return hashlib.sha256(text.encode('utf-8')).hexdigest()

def session():
    token=json.loads(os.environ['BLOGGER_TOKEN_JSON'])
    root=json.loads(os.environ['BLOGGER_CLIENT_SECRET_JSON'])
    config=root.get('installed') or root.get('web') or {}
    for key in ('client_id','client_secret','token_uri'):
        if not token.get(key) and config.get(key):token[key]=config[key]
    return AuthorizedSession(Credentials.from_authorized_user_info(token))

def request(s,method,path,**kwargs):
    for attempt in range(7):
        r=s.request(method,BASE+path,timeout=80,**kwargs)
        if r.status_code in (429,500,502,503,504) or (r.status_code==403 and 'rateLimitExceeded' in r.text):
            if attempt==6:break
            time.sleep(min(2**attempt,30));continue
        need(r.status_code<400,'Blogger request failed: '+method+' '+path+' HTTP '+str(r.status_code))
        return r.json()
    raise RuntimeError('Blogger request did not succeed after bounded retries')

def all_live(s,resource='posts'):
    output=[];cursor=None;seen=set()
    for _ in range(30):
        q={'maxResults':100,'view':'ADMIN'}
        if resource=='posts':q.update(status='live',fetchBodies='true')
        if cursor:q['pageToken']=cursor
        d=request(s,'GET','/'+resource,params=q)
        output.extend(d.get('items') or [])
        cursor=d.get('nextPageToken')
        if not cursor:return output
        need(cursor not in seen,'Pagination loop');seen.add(cursor)
    raise RuntimeError('Unexpected inventory volume')

def identity(live,p):
    return str(live.get('id'))==p['id'] and live.get('status')=='LIVE' and live.get('url')==p['url'] and live.get('published')==p['published'] and sorted(live.get('labels') or [])==sorted(p['labels'])

def html_integrity(source,target,pid):
    a,b=BeautifulSoup(source,'html.parser'),BeautifulSoup(target,'html.parser')
    need(Counter(x.name for x in a.find_all())==Counter(x.name for x in b.find_all()),pid+': HTML element inventory changed')
    for tag,attr in (('a','href'),('img','src'),('img','srcset'),('iframe','src'),('script','src'),('source','src'),('source','srcset')):
        need([x.get(attr) for x in a.find_all(tag)]==[x.get(attr) for x in b.find_all(tag)],pid+': resource URL changed')
    for tag in ('pre','code','kbd','script','style','svg','math','iframe','textarea'):
        need([str(x) for x in a.find_all(tag)]==[str(x) for x in b.find_all(tag)],pid+': protected content changed: '+tag)
    for aa,bb in zip(a.find_all(),b.find_all()):
        need({k:v for k,v in aa.attrs.items() if k not in ('alt','title','aria-label','placeholder','lang','dir')}=={k:v for k,v in bb.attrs.items() if k not in ('alt','title','aria-label','placeholder','lang','dir')},pid+': layout or functional attributes changed')
    need(not re.search(r'ZXQ\d+QXZ',target),pid+': unresolved translation marker')

def main():
    need(os.environ.get('BLOGGER_BLOG_ID')==BLOG_ID,'Wrong Blogger blog secret')
    names=sorted(ROOT.glob('payload-part-*.b64'))
    need(bool(names),'Missing French payload')
    raw=lzma.decompress(base64.b64decode(''.join(p.read_text() for p in names),validate=True))
    need(hashlib.sha256(raw).hexdigest()==EXPECTED_PAYLOAD_SHA256,'French payload checksum differs')
    data=json.loads(raw)
    need(data['blog_id']==BLOG_ID and data['language']=='fr','Wrong payload destination')
    rows=data['articles']
    need(len(rows)==259 and len({x['id'] for x in rows})==259,'Expected exactly 259 distinct posts')
    need(all(x['title'].strip() and x['content'].strip() and 60<=len(x['search_description'])<=150 for x in rows),'Incomplete French article fields')
    s=session();blog=request(s,'GET','')
    need(str(blog.get('id'))==BLOG_ID and urlsplit(blog.get('url','')).hostname in HOSTS,'Blog/domain mismatch')
    live=all_live(s)
    need(len(live)==259 and {str(p['id']) for p in live}=={p['id'] for p in rows},'Published inventory changed')
    originals={str(p['id']):p for p in live}
    plans={}
    for p in rows:
        old=originals[p['id']]
        need(identity(old,p),p['id']+': URL, publication date, status or labels changed')
        done=old.get('title')==p['title'] and digest(old.get('content',''))==digest(p['content'])
        source=old.get('title')==p['source_title'] and digest(old.get('content',''))==p['source_content_sha256']
        need(done or source,p['id']+': a newer edit differs from the verified source; refusing overwrite')
        if source:html_integrity(old['content'],p['content'],p['id'])
        plans[p['id']]='SKIP' if done else 'UPDATE'
    print('ALL_259_PREFLIGHT_OK: exact blog, source hashes, URLs, dates, labels, HTML, images, links and code verified',flush=True)
    changed=0;skipped=0
    for p in rows:
        if plans[p['id']]=='SKIP':skipped+=1;continue
        current=request(s,'GET','/posts/'+p['id'],params={'view':'ADMIN'})
        need(identity(current,p) and current.get('title')==p['source_title'] and digest(current.get('content',''))==p['source_content_sha256'],p['id']+': concurrent edit detected')
        # PATCH keeps the existing identity, permalink, labels, comments and publication time.
        request(s,'PATCH','/posts/'+p['id'],json={'title':p['title'],'content':p['content']})
        saved=request(s,'GET','/posts/'+p['id'],params={'view':'ADMIN'})
        need(identity(saved,p) and saved.get('title')==p['title'] and digest(saved.get('content',''))==digest(p['content']),p['id']+': saved result differs')
        changed+=1
        print('FRENCH_POST_VERIFIED: '+p['id']+' '+p['title'],flush=True)
        time.sleep(.35)
    final=all_live(s)
    need(len(final)==259,'Final published count changed')
    by_id={str(p['id']):p for p in final}
    for p in rows:
        item=by_id[p['id']]
        need(identity(item,p) and item.get('title')==p['title'] and digest(item.get('content',''))==digest(p['content']),p['id']+': final verification failed')
    report=dict(blog_id=BLOG_ID,language='fr',articles_verified=259,updated=changed,already_translated=skipped,permalinks_preserved=True,publication_dates_preserved=True,labels_preserved=True,pages_modified=False,template_modified=False)
    Path('french-publication-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('ALL_259_FRENCH_ARTICLES_PUBLISHED_AND_REREAD '+json.dumps(report,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
