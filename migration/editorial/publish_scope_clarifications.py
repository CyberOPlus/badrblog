#!/usr/bin/env python3
"""Publish only the 26 reader-first, chat-authored intent clarifications.
GitHub is a transport layer, not an automated writer.
"""
import os,json,hashlib,re
from pathlib import Path
from urllib.parse import urlsplit,unquote
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
BLOG="2750143140558868280"
API="https://www.googleapis.com/blogger/v3/blogs/"+BLOG
MARK="<!-- CYBEROPLUS_INTENT_SCOPE_2026_11_02 -->"
def check(x,msg):
    if not x:raise RuntimeError(msg)
def sha(s):return hashlib.sha256(s.encode("utf-8")).hexdigest()
def normalize(u):
    p=urlsplit(u)
    return re.sub(r"/+","/",unquote(p.path)).lower().rstrip("/")
def session():
    t=json.loads(os.environ["BLOGGER_TOKEN_JSON"])
    c=json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"])
    x=c.get("installed") or c.get("web") or {}
    for k in ("client_id","client_secret","token_uri"):
        if not t.get(k) and x.get(k):t[k]=x[k]
    return AuthorizedSession(Credentials.from_authorized_user_info(t))
def get(s,pid):
    r=s.get(API+"/posts/"+pid,params={"view":"ADMIN","fetchBody":"true"},timeout=40)
    r.raise_for_status();return r.json()
def all_live(s):
    out=[];token=None
    while True:
        opts={"status":"live","fetchBodies":"false","maxResults":100,"view":"ADMIN"}
        if token:opts["pageToken"]=token
        r=s.get(API+"/posts",params=opts,timeout=60);r.raise_for_status()
        o=r.json();out+=o.get("items",[])
        token=o.get("nextPageToken")
        if not token:break
    return out
def prepare(source,fragment,valid_urls):
    old=BeautifulSoup(source,"html.parser")
    inc=BeautifulSoup(fragment,"html.parser")
    check(len(inc.find_all("p"))==1 and inc.find("p") is not None,"New scope must be exactly one paragraph")
    check(not inc.find(["script","style","iframe","img","h1","h2","h3","form"]),"Active/unsupported markup in scope")
    check(len(inc.get_text(" ",strip=True))>=165,"Scope not substantive")
    prior={normalize(a.get("href","")) for a in old.find_all("a",href=True)}
    for a in list(inc.find_all("a",href=True)):
        dest=a["href"]
        check(urlsplit(dest).netloc in ("cyberoplus.com","www.cyberoplus.com"),"External or malformed destination in scope")
        check(normalize(dest) in valid_urls,"Destination is not a live article")
        # No duplicate outbound URL: preserve meaningful anchor wording as plain text.
        if normalize(dest) in prior:a.unwrap()
    rendered=str(inc.find("p"))
    m=re.search(r"</p\s*>",source,re.I)
    check(m is not None,"No paragraph close tag")
    result=source[:m.end()]+"\n"+MARK+"\n"+rendered+source[m.end():]
    before=BeautifulSoup(source,"html.parser")
    after=BeautifulSoup(result,"html.parser")
    for tag,key in (("img","src"),("a","href"),("script","src"),("iframe","src")):
        x=[q.get(key) for q in before.find_all(tag)]
        y=[q.get(key) for q in after.find_all(tag)]
        if tag=="a":
            check(all(y.count(q)>=x.count(q) for q in set(x)),"Original links lost")
        else:check(x==y,"Original resources changed")
    for tag in ("pre","code","script","style","iframe","textarea"):
        check([str(q) for q in before.find_all(tag)]==[str(q) for q in after.find_all(tag)],"Code or protected markup changed")
    check(result.replace("\n"+MARK+"\n"+rendered,"",1)==source,"Original HTML changed outside intended insertion")
    return result
def main():
    r=Path("migration/editorial/cannibalization_focus_clarifications_2026_11.json")
    data=json.loads(r.read_text(encoding="utf-8"))["items"]
    check(len(data)==26,"Unexpected editorial scope batch")
    s=session()
    blog=s.get(API,timeout=30);blog.raise_for_status()
    b=blog.json()
    check(str(b.get("id"))==BLOG and (urlsplit(b.get("url","")).hostname or "").lower() in ("www.cyberoplus.com","cyberoplus.com"),"Wrong blog")
    posts=all_live(s)
    check(len(posts)==259 and len({str(p["id"]) for p in posts})==259,"Unexpected current published inventory")
    ids={str(p["id"]):p for p in posts}
    valid={normalize(p["url"]) for p in posts}
    check(set(data).issubset(ids),"Target post disappeared")
    print("INTENT_SCOPE_PRECHECK: live=259 expected_differentiations=26",flush=True)
    saved_count=0;already=0;blocked=[]
    for pid,datum in data.items():
        p=get(s,pid)
        if MARK in p.get("content",""):
            already+=1;print("ALREADY_UPDATED: "+pid,flush=True);continue
        if p.get("status")!="LIVE" or p.get("url")!=ids[pid]["url"] or p.get("labels") not in [["technology"],["guides"],["cybersecurity"],["artificial-intelligence"]]:
            blocked.append(pid);print("BLOCKED_META_OR_STATUS: "+pid,flush=True);continue
        try:
            new=prepare(p["content"],datum["html"],valid)
            latest=get(s,pid)
            check(latest.get("content")==p.get("content") and latest.get("title")==p.get("title") and latest.get("updated")==p.get("updated"),"Concurrent edit")
            result=s.patch(API+"/posts/"+pid,json={"content":new},timeout=70);result.raise_for_status()
            out=get(s,pid)
            for k in ("id","url","title","published","status","labels"):
                check(out.get(k)==p.get(k),"CRITICAL original metadata changed: "+k+" "+pid)
            check(sha(out.get("content",""))==sha(new),"CRITICAL published HTML mismatch")
            saved_count+=1
            print("INTENT_CLARIFICATION_PUBLISHED_AND_VERIFIED: "+pid+" "+datum["focus"],flush=True)
        except Exception as e:
            blocked.append(pid);print("BLOCKED_EDITORIAL: "+pid+" "+type(e).__name__+" "+str(e)[:250],flush=True)
    print("INTENT_BATCH_RESULT: published="+str(saved_count)+" already="+str(already)+" blocked="+str(len(blocked)),flush=True)
    if blocked:print("BLOCKED_IDS: "+",".join(blocked),flush=True)
    if blocked:raise RuntimeError("Some articles require manual review and were not touched")
    print("ALL_INTENT_SCOPE_DIFFERENTIATIONS_VERIFIED",flush=True)
if __name__=="__main__":main()
