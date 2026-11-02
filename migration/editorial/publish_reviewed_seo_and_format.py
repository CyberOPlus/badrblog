#!/usr/bin/env python3
"""Publish only manually authored, reviewed SEO titles plus two proven HTML-template fixes.
The 18:56 Blogger snapshot is a concurrency baseline, not an instruction source.
No automated translation, no template changes, no URL/date/status/label changes.
"""
import os,json,csv,io,zipfile,hashlib,re
from pathlib import Path
from urllib.parse import urlsplit
import requests
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
BID="2750143140558868280"
BASE="https://www.googleapis.com/blogger/v3/blogs/"+BID
SNAPSHOT_ID="13170817074"
TITLE_CSV="migration/editorial/reviewed_seo_titles_2026_11.csv"
def sha(t):return hashlib.sha256(t.encode("utf-8")).hexdigest()
def require(x,message):
    if not x:raise RuntimeError(message)
def get_baseline():
    url="https://api.github.com/repos/CyberOPlus/badrblog/actions/artifacts/"+SNAPSHOT_ID+"/zip"
    h={"Authorization":"Bearer "+os.environ["GITHUB_TOKEN"],"Accept":"application/vnd.github+json"}
    r=requests.get(url,headers=h,timeout=90);r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        data=json.loads(z.read("blogger-originals.json"))
    require(str(data.get("blog_id"))==BID and len(data["posts"])==259,"Wrong baseline export")
    return {str(p["id"]):p for p in data["posts"]}
def get_session():
    t=json.loads(os.environ["BLOGGER_TOKEN_JSON"])
    c=json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"])
    cfg=c.get("installed") or c.get("web") or {}
    for key in ("client_id","client_secret","token_uri"):
        if not t.get(key) and cfg.get(key):t[key]=cfg[key]
    return AuthorizedSession(Credentials.from_authorized_user_info(t))
def get(session,pid):
    r=session.get(BASE+"/posts/"+pid,params={"view":"ADMIN","fetchBody":"true"},timeout=50)
    r.raise_for_status();return r.json()
def list_posts(session,status):
    arr=[];token=None;seen=set()
    while True:
        params={"status":status,"maxResults":100,"view":"ADMIN","fetchBodies":"false"}
        if token:params["pageToken"]=token
        r=session.get(BASE+"/posts",params=params,timeout=70);r.raise_for_status()
        data=r.json();arr+=data.get("items",[])
        token=data.get("nextPageToken")
        if not token:break
        require(token not in seen,"Pagination loop");seen.add(token)
    return arr
def unchanged(p,b):
    return str(p.get("id"))==str(b.get("id")) and p.get("title")==b.get("title") and p.get("url")==b.get("url") and p.get("published")==b.get("published") and p.get("labels")==b.get("labels") and p.get("status")=="LIVE" and sha(p.get("content",""))==sha(b.get("content",""))
def verified_after(p,b,expected_title,expected_body):
    for k in ("id","url","published","status","labels"):
        require(p.get(k)==b.get(k) if k!="status" else p.get(k)=="LIVE","CRITICAL unexpected change to "+k+" for "+str(b["id"]))
    require(p.get("title")==expected_title and sha(p.get("content",""))==sha(expected_body),"CRITICAL content/title differs from saved expected values")
def format_fix(old,pid):
    original=old
    soup=BeautifulSoup(old,"html.parser")
    if pid=="7047518870817423340":
        # Only remove 19 old explicit heading styles that override #postBody typography.
        headings=[h for h in soup.find_all(["h2","h3","h4"]) if h.has_attr("style")]
        require(len(headings)==19,"Unexpected UML inline heading count")
        for h in headings:
            before=str(h)
            open_end=before.find(">")
            require(open_end>0,"Malformed heading tag")
            prefix=before[:open_end]
            cleaned=re.sub(r'\sstyle="[^"]*"','',prefix,count=1)
            require(cleaned!=prefix,"Expected heading style attribute not found")
            old=old.replace(before,cleaned+before[open_end:],1)
    elif pid=="4893738417517427659":
        # Preserve Blogger's original image but do not put it inside an empty H2.
        broken=[h for h in soup.find_all("h2") if not h.get_text(" ",strip=True)]
        require(len(broken)==1 and broken[0].find("img"),"Unexpected algorithm heading defect")
        block=str(broken[0])
        replacement="".join(str(x) for x in broken[0].contents)
        require(len(replacement)>20 and "<img" in replacement,"Image extraction failed")
        require(block in old,"Original H2 block not found")
        old=old.replace(block,replacement,1)
    else:raise RuntimeError("Not an approved formatting target")
    first=BeautifulSoup(original,"html.parser");last=BeautifulSoup(old,"html.parser")
    for tag,attribute in (("img","src"),("a","href"),("script","src"),("iframe","src")):
        require([x.get(attribute) for x in first.find_all(tag)]==[x.get(attribute) for x in last.find_all(tag)],"Resource URLs were changed")
    for tag in ("pre","code","script","style","iframe"):
        require([str(x) for x in first.find_all(tag)]==[str(x) for x in last.find_all(tag)],"Code or embedded content changed")
    require(first.get_text(" ",strip=True)==last.get_text(" ",strip=True),"Visible wording changed")
    require(len(first.find_all("img"))==len(last.find_all("img")),"Images missing")
    return old
def main():
    require(os.environ.get("BLOGGER_BLOG_ID")==BID,"Wrong blog secret")
    snap=get_baseline()
    rows=list(csv.DictReader(Path(TITLE_CSV).open(encoding="utf-8")))
    require(len(rows)==97 and len({r["post_id"] for r in rows})==97,"Unexpected reviewed title payload")
    require(all(r["post_id"] in snap for r in rows),"Title target missing from snapshot")
    s=get_session()
    r=s.get(BASE,timeout=35);r.raise_for_status()
    b=r.json()
    require(str(b.get("id"))==BID and (urlsplit(b.get("url","")).hostname or "").lower() in ("cyberoplus.com","www.cyberoplus.com"),"Wrong Blogger site")
    live=list_posts(s,"live");draft=list_posts(s,"draft")
    require(len(live)==259 and len(draft)==0,"Expected 259 live published posts, no drafts")
    require(all(p.get("labels") in [["cybersecurity"],["artificial-intelligence"],["technology"],["guides"]] for p in live),"Unexpected labels")
    print("LIVE_INVENTORY_PRECHECK_OK: 259 LIVE, 0 DRAFT, 4 known labels",flush=True)
    written=0;already=0;blocked=[];structure=0
    bytitle={row["post_id"]:row["new_title"] for row in rows}
    structure_ids={"7047518870817423340","4893738417517427659"}
    ids=list(bytitle)
    for pid in structure_ids:
        if pid not in ids:ids.append(pid)
    for pid in ids:
        base=snap[pid];p=get(s,pid)
        desired=bytitle.get(pid,base["title"])
        if desired==base["title"] and pid not in structure_ids:continue
        if not unchanged(p,base):
            # Skip concurrent edits, but recognize already-saved title updates.
            if p.get("title")==desired and p.get("url")==base.get("url") and p.get("published")==base.get("published") and p.get("labels")==base.get("labels") and p.get("content")==base.get("content"):
                already+=1;continue
            blocked.append(pid);print("SKIPPED_CHANGED_SINCE_BASELINE: "+pid,flush=True);continue
        body=base["content"]
        if pid in structure_ids:body=format_fix(body,pid)
        # Exact reread immediately before write.
        p2=get(s,pid)
        if not unchanged(p2,base):
            blocked.append(pid);print("SKIPPED_CONCURRENT_CHANGE: "+pid,flush=True);continue
        payload={"title":desired}
        if body!=base["content"]:payload["content"]=body
        response=s.patch(BASE+"/posts/"+pid,json=payload,timeout=75);response.raise_for_status()
        after=get(s,pid);verified_after(after,base,desired,body)
        written+=1
        if body!=base["content"]:structure+=1
        print("SEO_UPDATE_AND_VERIFIED: "+pid+" title="+desired+" template_fix="+str(body!=base["content"]),flush=True)
    print("EDITORIAL_SEO_SUMMARY: published="+str(written)+" already_done="+str(already)+" template_fixes="+str(structure)+" blocked="+str(len(blocked)),flush=True)
    if blocked:print("BLOCKED_POST_IDS: "+",".join(blocked),flush=True)
    final=list_posts(s,"live")
    require(len(final)==259 and len(list_posts(s,"draft"))==0,"Final publication count changed")
    require(all(p.get("labels") in [["cybersecurity"],["artificial-intelligence"],["technology"],["guides"]] for p in final),"Final classification changed")
    print("Blogger 259-post publication and category integrity verified",flush=True)
    if blocked:raise RuntimeError("Some article edits skipped to preserve manual changes")
    print("SEO_TITLE_AND_FORMATTING_BATCH_VERIFIED",flush=True)
if __name__=="__main__":main()
