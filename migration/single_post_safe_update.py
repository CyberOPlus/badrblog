#!/usr/bin/env python3
"""One-off guarded Blogger update for ONE preapproved article.

No translation provider, new-post insert, delete, schedule or background writes.
Only posts.get, blogs.get, posts.patch, posts.get; fail closed on any mismatch.
"""
import hashlib
import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

BLOG_ID = '2750143140558868280'
POST_ID = '9195875967606348644'
ORIGINAL_TITLE = 'كيف تعرف من فتح الكمبيوتر في غيابك على Windows 11؟ شرح سجل الدخول'
ORIGINAL_SHA256 = 'f50b411f87d780501eeb626715e02fc58aa694bf3f2e1a1e0568e5a3f63f7c2d'
ORIGINAL_UPDATED = '2026-10-31T16:32:02Z'
ORIGINAL_URL = 'https://www.cyberoplus.com/2026/10/windows-11-login-history-event-viewer.html'
NEW_TITLE = 'How to Find Out Who Accessed Your Windows 11 PC While You Were Away: Sign-in Log Guide'
NEW_LABELS = ['Tutorials']
STAGING_PATH = 'migration/staging/windows-11-login-history-event-viewer.en.html'
BASE = 'https://www.googleapis.com/blogger/v3'

def sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

def get(s, path, params=None):
    r=s.get(BASE+path,params=params or {},timeout=40)
    if r.status_code!=200:
        raise RuntimeError('Read verification failed: HTTP '+str(r.status_code))
    return r.json()

class Shape(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.elements=[];self.links=[];self.media=[];self.protected=[];self.depth=0
    def handle_starttag(self,tag,attrs):
        self.elements.append(('START',tag,tuple(k for k,v in attrs)))
        a=dict(attrs)
        if tag=='a':self.links.append(a.get('href'))
        if tag in ('img','iframe','source','video','audio'):
            self.media.append((tag, a.get('src'),a.get('srcset')))
        if tag in ('script','style','pre','code','kbd','samp'):
            self.depth+=1
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        self.handle_endtag(tag)
    def handle_endtag(self,tag):
        self.elements.append(('END',tag))
        if tag in ('script','style','pre','code','kbd','samp'):
            self.depth=max(0,self.depth-1)
    def handle_data(self,data):
        if self.depth:self.protected.append(data)

def validate(old,new):
    a=Shape();a.feed(old);a.close()
    b=Shape();b.feed(new);b.close()
    if a.elements!=b.elements or a.links!=b.links or a.media!=b.media or a.protected!=b.protected:
        raise RuntimeError('HTML structure, link, media or code block changed: refusing update')
    if re.search(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]',new):
        raise RuntimeError('Arabic text remains in the staged English content')
    if len(new)<7000 or len(new)>30000:
        raise RuntimeError('Unexpected English article length')
    if new.count('Get-WinEvent -FilterHashtable')!=1:
        raise RuntimeError('PowerShell example is missing or duplicated')

def creds():
    token=json.loads(os.environ['BLOGGER_TOKEN_JSON'])
    client=json.loads(os.environ['BLOGGER_CLIENT_SECRET_JSON'])
    conf=client.get('installed') or client.get('web') or {}
    for k in ('client_id','client_secret','token_uri'):
        if not token.get(k) and conf.get(k):token[k]=conf[k]
    return Credentials.from_authorized_user_info(token)

def main():
    if os.environ.get('BLOGGER_BLOG_ID')!=BLOG_ID:
        raise RuntimeError('Blog ID secret differs from verified CyberoPlus destination')
    staged=Path(STAGING_PATH).read_text(encoding='utf-8')
    sess=AuthorizedSession(creds())
    blog=get(sess,'/blogs/'+BLOG_ID)
    if str(blog.get('id'))!=BLOG_ID or (urlsplit(blog.get('url') or '').hostname or '').lower() not in ('cyberoplus.com','www.cyberoplus.com'):
        raise RuntimeError('Blog identity mismatch')
    print('TARGET_DOMAIN_VERIFIED: cyberoplus.com')
    path='/blogs/'+BLOG_ID+'/posts/'+POST_ID
    source=get(sess,path,{'view':'ADMIN','fetchBody':'true'})
    if (str(source.get('id'))!=POST_ID or source.get('title')!=ORIGINAL_TITLE or
        source.get('status')!='LIVE' and str(source.get('status','')).lower()!='live' or
        str(source.get('updated'))!=ORIGINAL_UPDATED or
        source.get('url')!=ORIGINAL_URL or
        source.get('labels')!=['guides'] or
        sha(source.get('content',''))!=ORIGINAL_SHA256):
        raise RuntimeError('Original post identity/content/status changed. Refusing update.')
    validate(source['content'],staged)
    print('SOURCE_VERIFIED: exact original title, ID, URL, label, timestamp, and content SHA-256')
    print('HTML_VERIFIED: tags, links, images, embedded code, PowerShell content preserved')
    if os.environ.get('SINGLE_ARTICLE_APPLY')!='I_CONFIRM_THIS_ONE_POST_ONLY':
        print('DRY_RUN_PASS: no write was made')
        return
    print('PATCHING_ONE_EXISTING_POST: no insert, deletion, or republish')
    response=sess.patch(BASE+path,json={'title':NEW_TITLE,'content':staged,'labels':NEW_LABELS},timeout=60)
    if response.status_code!=200:
        raise RuntimeError('Blogger PATCH returned HTTP '+str(response.status_code))
    updated=get(sess,path,{'view':'ADMIN','fetchBody':'true'})
    if str(updated.get('id'))!=POST_ID or updated.get('url')!=ORIGINAL_URL or updated.get('title')!=NEW_TITLE or updated.get('labels')!=NEW_LABELS:
        raise RuntimeError('Blogger PATCH accepted, but title/URL/labels verification FAILED; inspect the post immediately')
    if updated.get('published')!=source.get('published'):
        raise RuntimeError('Post publish timestamp changed; inspect immediately')
    if sha(updated.get('content',''))!=sha(staged):
        raise RuntimeError('Content returned by Blogger differs from staged bytes; inspect for Blogger normalization')
    print('SINGLE_POST_UPDATE_SUCCESS: post ID, URL and published timestamp preserved; title, HTML, and labels verified')
    print('VERIFIED_POST_URL: '+ORIGINAL_URL)

if __name__=='__main__':
    try:
        main()
    except Exception as e:
        print('SINGLE_POST_UPDATE_FAIL: '+type(e).__name__+': '+str(e),file=sys.stderr)
        sys.exit(1)
