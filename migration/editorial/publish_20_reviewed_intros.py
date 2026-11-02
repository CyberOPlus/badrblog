#!/usr/bin/env python3
"""Apply only the 20 English introductory paragraphs written in ChatGPT.
Reject any manually changed source; protect all original links, images, code,
URL, publication date, category and publication state.
"""
import os,json,io,zipfile,hashlib,csv,re
from pathlib import Path
from urllib.parse import urlsplit
import requests
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
BID="2750143140558868280"
ROOT="https://www.googleapis.com/blogger/v3/blogs/"+BID
ARTIFACT_ID="13170817074"
def check(condition,message):
    if not condition:raise RuntimeError(message)
def sha(text):return hashlib.sha256(text.encode("utf-8")).hexdigest()
def baseline():
    u="https://api.github.com/repos/CyberOPlus/badrblog/actions/artifacts/"+ARTIFACT_ID+"/zip"
    r=requests.get(u,headers={"Authorization":"Bearer "+os.environ["GITHUB_TOKEN"],"Accept":"application/vnd.github+json"},timeout=90)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:data=json.loads(z.read("blogger-originals.json"))
    check(str(data.get("blog_id"))==BID and len(data["posts"])==259,"Wrong archived Blogger baseline")
    return {str(p["id"]):p for p in data["posts"]}
def auth():
    t=json.loads(os.environ["BLOGGER_TOKEN_JSON"])
    c=json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"])
    d=c.get("installed") or c.get("web") or {}
    for k in ("client_id","client_secret","token_uri"):
        if not t.get(k) and d.get(k):t[k]=d[k]
    return AuthorizedSession(Credentials.from_authorized_user_info(t))
def get(s,id):
    r=s.get(ROOT+"/posts/"+str(id),params={"view":"ADMIN","fetchBody":"true"},timeout=45)
    r.raise_for_status();return r.json()
def check_intro(source_html,intro,pid):
    doc=BeautifulSoup(source_html,"html.parser")
    first=doc.find("p")
    check(first is not None and first.parent is not None,pid+": missing paragraph")
    check(not first.find("a"),pid+": first paragraph has existing links; do not overwrite")
    initial=str(first)
    check(initial in source_html,pid+": exact original introduction not available")
    fragment=BeautifulSoup(intro,"html.parser")
    check(len(fragment.find_all("p"))==1 and fragment.find("p") is not None,pid+": authored introduction malformed")
    check(not fragment.find(["script","style","iframe","img","form","h1","h2","h3"]),pid+": introduction contains unsupported media")
    check(len(fragment.get_text(" ",strip=True))>=140,pid+": introduction too thin")
    replacement=str(fragment.find("p"))
    edited=source_html.replace(initial,replacement,1)
    before=BeautifulSoup(source_html,"html.parser")
    after=BeautifulSoup(edited,"html.parser")
    check([x.get("src") for x in before.find_all("img")]==[x.get("src") for x in after.find_all("img")],pid+": image source changed")
    check([x.get("href") for x in before.find_all("a")]==[x.get("href") for x in after.find_all("a")],pid+": link changed")
    for tag in ("pre","script","style","iframe"):
        check([str(x) for x in before.find_all(tag)]==[str(x) for x in after.find_all(tag)],pid+": protected markup changed "+tag)
    check(len(before.find_all("h2"))==len(after.find_all("h2")),pid+": heading changed")
    check([x.get_text(" ",strip=True) for x in before.find_all("p")][1:]==[x.get_text(" ",strip=True) for x in after.find_all("p")][1:],pid+": paragraph beyond intro altered")
    check(not re.search(r"[\u0600-\u06ff]",fragment.get_text(" ",strip=True)),pid+": Arabic in rewritten introduction")
    return edited
def main():
    check(os.environ.get("BLOGGER_BLOG_ID")==BID,"Wrong Blogger blog ID")
    source=baseline()
    payload=json.loads(Path("migration/editorial/seo_reviewed_intros_2026_11.json").read_text(encoding="utf-8"))["intros"]
    titles={r["post_id"]:r["new_title"] for r in csv.DictReader(Path("migration/editorial/reviewed_seo_titles_2026_11.csv").open(encoding="utf-8"))}
    check(len(payload)==20 and all(pid in source for pid in payload),"Unexpected introduction payload")
    s=auth()
    r=s.get(ROOT,timeout=35);r.raise_for_status()
    info=r.json()
    check(str(info.get("id"))==BID and (urlsplit(info.get("url","")).hostname or "").lower() in ("cyberoplus.com","www.cyberoplus.com"),"Wrong Blogger domain")
    updates=0;skips=[]
    # Validate ALL 20 prepared replacements before any mutation.
    prepared={pid:check_intro(source[pid]["content"],html,pid) for pid,html in payload.items()}
    print("ALL_INTROS_PREVALIDATED: 20 natural English introductions with all existing resources retained",flush=True)
    for pid,body in prepared.items():
        b=source[pid];p=get(s,pid);correct_title=titles.get(pid,b["title"])
        if not (str(p["id"])==pid and p["status"]=="LIVE" and p["url"]==b["url"] and p["published"]==b["published"] and p["labels"]==b["labels"] and p["title"] in (b["title"],correct_title)):
            skips.append(pid);print("SKIPPED_METADATA_OR_MANUAL_TITLE: "+pid,flush=True);continue
        if sha(p["content"]) != sha(b["content"]):
            skips.append(pid);print("SKIPPED_MANUAL_CONTENT_EDIT: "+pid,flush=True);continue
        now=get(s,pid)
        if now.get("content")!=p.get("content") or now.get("title")!=p.get("title"):
            skips.append(pid);print("SKIPPED_CONCURRENT_EDIT: "+pid,flush=True);continue
        result=s.patch(ROOT+"/posts/"+pid,json={"content":body},timeout=75);result.raise_for_status()
        saved=get(s,pid)
        for key in ("id","url","published","status","labels","title"):
            check(saved.get(key)==p.get(key),"CRITICAL: "+key+" changed on "+pid)
        check(sha(saved["content"])==sha(body),"CRITICAL: English introduction not saved on "+pid)
        updates+=1
        print("ENGLISH_INTRO_PUBLISHED_AND_VERIFIED: "+pid+" "+p["url"],flush=True)
    print("INTRO_BATCH_SUMMARY: updated="+str(updates)+" skipped="+str(len(skips))+" total=20",flush=True)
    if skips:print("BLOCKED_POST_IDS: "+",".join(skips),flush=True)
    if skips:raise RuntimeError("Some introductions were not saved to protect concurrent manual work")
    print("ALL_20_ENGLISH_INTROS_PUBLISHED_AND_VERIFIED",flush=True)
if __name__=="__main__":main()
