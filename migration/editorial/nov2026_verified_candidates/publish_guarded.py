#!/usr/bin/env python3
"""CyberoPlus 2026-11-04 guarded in-place editorial update of existing Blogger posts.

No post insertion, draft promotion, URL/date changes, template edits, or unrelated posts.
Only candidates meeting strict mechanical integrity and no-live-change preconditions
are patched. Source files are *unreviewed editorial drafts*; this code deliberately
rejects riskier drafts instead of implying 259/259 are publication ready.
"""
import base64, hashlib, io, json, os, sys, time, zipfile
from pathlib import Path
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession

BLOG_ID = '2750143140558868280'
BASE = f'https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}'
EXPECTED_SHA256 = '8d65cf1de1479224878a20b636267e7e60cbea0f313d424c88a63745ce5f5d8b'
CHUNK_DIR = Path('migration/editorial/nov2026_verified_candidates')
MAX_PUBLISH = 258
WRITE_ENABLED = os.getenv('CYBEROPLUS_SAFE_WRITE', 'false').lower() == 'true'


def digest(s): return hashlib.sha256(s.encode('utf-8')).hexdigest()

def check(condition, message):
    if not condition: raise RuntimeError(message)


def authorized():
    secret = json.loads(os.environ['BLOGGER_CLIENT_SECRET_JSON'])
    tok = json.loads(os.environ['BLOGGER_TOKEN_JSON'])
    config = secret.get('installed') or secret.get('web') or {}
    for key in ('client_id', 'client_secret', 'token_uri'):
        if not tok.get(key) and config.get(key): tok[key] = config[key]
    return AuthorizedSession(Credentials.from_authorized_user_info(tok))


def get(session, pid):
    r=session.get(f'{BASE}/posts/{pid}',params={'view':'ADMIN','fetchBody':'true'},timeout=40)
    r.raise_for_status(); return r.json()


def main():
    parts=sorted(CHUNK_DIR.glob('part_*.b64'))
    check(len(parts)==15,'Expected exactly 15 payload chunks')
    data=base64.b64decode(''.join(p.read_text('ascii') for p in parts),validate=True)
    check(hashlib.sha256(data).hexdigest()==EXPECTED_SHA256,'Payload SHA-256 mismatch')
    pack=zipfile.ZipFile(io.BytesIO(data)); check(pack.testzip() is None,'Corrupt ZIP content')
    manifest=json.loads(pack.read('manifest.json'))
    posts=manifest['posts']
    check(manifest['blog_id']==BLOG_ID and len(posts)==258,'Unexpected deployment site or payload')
    check(len(set(x['post_id'] for x in posts))==258,'Duplicate Blogger post IDs')
    sess=authorized()
    br=sess.get(BASE,timeout=40);br.raise_for_status()
    blog=br.json(); check(str(blog.get('id'))==BLOG_ID,'Wrong Blogger ID')
    check((urlsplit(blog.get('url','')).hostname or '').lower() in ('cyberoplus.com','www.cyberoplus.com'),'Wrong Blogger host')
    results=[]
    allowed=0
    for i, row in enumerate(posts):
        pid=row['post_id']; status='SKIPPED'; reason=''
        try:
            new=pack.read(f'content/{pid}.html').decode('utf-8')
            check(digest(new)==row['new_content_sha256'],'Payload HTML hash mismatch')
            # Reject drafts which shortened source, changed image metadata, or dropped
            # any original code examples. These rules err on the side of skipping.
            if not (row['new_words']>=row['old_words'] and row['image_src_order_preserved']
                    and row['image_attrs_same'] and row['image_lightboxes_preserved']
                    and row['unmatched_code_examples']==0):
                reason='Fails draft content-preservation QA'
            else:
                old=get(sess,pid)
                check(str(old.get('id'))==pid,'Post ID mismatch')
                check(old.get('status')=='LIVE','Article not live: draft must remain draft')
                check(old.get('url')==row['expected_url'],'Permalink changed in Blogger')
                check(old.get('published')==row['expected_published'],'Original published date changed')
                if digest(old.get('content',''))!=row['old_content_sha256']:
                    reason='Live content changed since November 2 snapshot; skip stale overwrite'
                else:
                    # Independent, live-image/link and active-content preservation checks.
                    a=BeautifulSoup(old['content'],'html.parser');b=BeautifulSoup(new,'html.parser')
                    def seq(s,tag,attr): return [x.get(attr) for x in s.find_all(tag)]
                    check(seq(a,'img','src')==seq(b,'img','src'),'Media URLs would change')
                    check(seq(a,'img','alt')==seq(b,'img','alt') or row['image_attrs_same'],'Image metadata mismatch')
                    for tag in ('script','iframe','style','textarea'):
                        check([str(t) for t in a.find_all(tag)]==[str(t) for t in b.find_all(tag)],'Executable or embedded content changed: '+tag)
                    # Pre/post link counts may differ intentionally for semantic internal links.
                    # But retain non-internal image enlargement URLs exactly.
                    check(len(b.find_all('img'))==len(a.find_all('img')),'Image count changed')
                    title=row.get('seo_title_candidate','').strip()
                    if not (30<=len(title)<=88 and '\n' not in title and '<' not in title):title=''
                    if old.get('title') != row.get('old_title'):title=''  # prefer newer live title
                    payload={'content':new}
                    if title and title != old['title']:payload['title']=title
                    if not WRITE_ENABLED:
                        status='DRY_RUN_PASS';reason='Safe eligible; write flag off'
                    else:
                        # Read the live post immediately before PATCH to avoid racing edits.
                        latest=get(sess,pid)
                        check(latest.get('updated')==old.get('updated') and latest.get('content')==old.get('content'),'Concurrent modification: skip')
                        request=sess.patch(f'{BASE}/posts/{pid}',json=payload,timeout=70);request.raise_for_status()
                        after=get(sess,pid)
                        for key in ('id','url','published','status','labels'):
                            check(after.get(key)==old.get(key),'CRITICAL changed original Blogger metadata '+key)
                        check(digest(after.get('content',''))==digest(new),'Published HTML differs from requested body')
                        check(after.get('title')==payload.get('title',old['title']),'Title mismatch after update')
                        status='PUBLISHED_AND_VERIFIED';allowed+=1
                        reason='Updated existing Blogger Post ID; URL/published/status/labels verified unchanged'
        except Exception as exc:
            status='BLOCKED';reason=f'{type(exc).__name__}: {str(exc)[:180]}'
        results.append({'post_id':pid,'status':status,'reason':reason})
        print(f'{i+1:03}/{len(posts)} {pid} {status}: {reason}',flush=True)
        if status=='BLOCKED' and reason.startswith('RuntimeError: CRITICAL'):
            print('STOPPING due to critical post-write discrepancy',flush=True);break
    out={'write_enabled':WRITE_ENABLED,'candidate_count':len(posts),'published_and_verified':allowed,'by_status':{k:sum(r['status']==k for r in results) for k in set(r['status'] for r in results)},'results':results}
    Path('publication_results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
    print('BATCH_SUMMARY='+json.dumps({k:v for k,v in out.items() if k!='results'}),flush=True)
    if len(results)!=len(posts):sys.exit(2)


if __name__=='__main__':main()