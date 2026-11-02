#!/usr/bin/env python3
"""One-time offline language conversion of existing CyberoPlus posts.
Strictly preserve Blogger post IDs, URLs, dates, status, category, existing links,
images, code and widgets. Translate only text/accessible labels. Not an SEO rank guarantee.
"""
import hashlib, json, os, re, sys, time
from collections import Counter
from urllib.parse import urlsplit
from bs4 import BeautifulSoup, NavigableString
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

BID="2750143140558868280"
BASE="https://www.googleapis.com/blogger/v3/blogs/"+BID
SHARD=int(os.environ.get("SHARD_ID","0"))
SHARDS=int(os.environ.get("SHARD_COUNT","8"))
CANONICAL={"cybersecurity","artificial-intelligence","technology","guides"}
AR=re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")
AR_LETTERS=re.compile(r"[\u0621-\u064a]")
SKIP_TAG={"script","style","pre","code","kbd","samp","var","math","svg","textarea"}
TEXT_ATTR=("alt","title","aria-label","placeholder")
PREFERRED_TITLES={"6874126048173066605":"Campus Connecté Maroc: Connect to University Wi-Fi on Android, iPhone, Windows and Mac","2122143739920545056":"Jordan Cybersecurity Law No. 16 of 2019: Scope and Key Provisions","972811869073396702":"Software Supply Chain Attacks: Risks From Packages to CI/CD and Production","9071374544546958469":"Wireless Transmission Media Explained: Radio, Infrared and Satellites","8037245016776914431":"CSMA/CD vs CSMA/CA vs Token Passing: How Network Access Works","3774593310021068072":"16 OSINT Tools for Cybersecurity in 2026: Domains and Infrastructure","4701336319707219667":"Academia Cyber Competition 2026 in Morocco: Results and Winners","6344153006922240831":"Online Chat Rooms Without Registration: Privacy and Safety Guide","4588432420976640507":"Computer and Internet Basics for Beginners: Hardware, Networks and Security","7288309226078856086":"Morocco vs Nigeria Semifinal: Match Result and Unsafe Streaming Apps","8709394223837169663":"Google AI Pro Free Trial: How to Check Eligibility and Activate an Offer","1176514902681097649":"Morocco Teacher Recruitment 2026/2027: How to Verify Official Listings","3227143679604396765":"Morocco Teacher Recruitment 2025: Requirements and 2026 Updates","474198384416999797":"Canada Harvest Jobs at Puneet Orchards: Pay, LMIA and Application Checks","4118057928779320275":"Microsoft Teams, MFA and Crypto Theft: October 2026 Security Developments","3784882314703336669":"Malicious postmark-mcp Package: Email Theft and 2026 Security Updates","8262491794790966193":"International Organization Jobs: Where to Find and Verify Vacancies","5596922752938621756":"What Is a Security Operations Center (SOC)? Roles, Tools and Workflow","5081674028507848664":"Meta Pixel and Localhost on Android: Privacy and Account Linking Explained","6430174930439237304":"Cloudflare Outages in 2025 and 2026: Causes and Resilience Lessons","3578618724340337930":"Meeting People Online Safely: How to Avoid Romance Scams","6939592384919727588":"Cybersecurity Jobs in Morocco: Career Paths, Skills and Where to Apply","8013642606743790511":"Fix a Slow Windows 11 PC Without Resetting It: Step-by-Step Troubleshooting","153675238469482097":"WhatsApp Advanced Chat Privacy: What It Does and How to Enable It"}

def require(ok,msg):
    if not ok:raise RuntimeError(msg)
def digest(s):return hashlib.sha256(s.encode("utf-8")).hexdigest()
def ratio(s):
    letters=sum(ch.isalpha() for ch in s)
    return sum(bool(AR.fullmatch(ch)) for ch in s)/letters if letters else 0
def session():
    t=json.loads(os.environ["BLOGGER_TOKEN_JSON"])
    c=json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"])
    data=c.get("installed") or c.get("web") or {}
    for k in ("client_id","client_secret","token_uri"):
        if not t.get(k) and data.get(k):t[k]=data[k]
    return AuthorizedSession(Credentials.from_authorized_user_info(t))
def read(sess,id):
    r=sess.get(BASE+"/posts/"+str(id),params={"view":"ADMIN","fetchBody":"true"},timeout=65)
    r.raise_for_status()
    return r.json()
def list_pages(sess,status):
    items=[];token=None;seen=set()
    while True:
        params={"maxResults":100,"status":status,"fetchBodies":"false","view":"ADMIN"}
        if token:params["pageToken"]=token
        r=sess.get(BASE+"/posts",params=params,timeout=75);r.raise_for_status()
        payload=r.json();items+=payload.get("items",[])
        token=payload.get("nextPageToken")
        if not token:break
        require(token not in seen,"Repeated Blogger pagination token")
        seen.add(token)
    return items
def init_offline_model():
    import argostranslate.package as packages
    import argostranslate.translate as translator
    import requests
    url="https://data.argosopentech.com/argospm/v1/translate-ar_en-1_0.argosmodel"
    dest="/tmp/arabic_to_english.argosmodel"
    import pathlib
    f=pathlib.Path(dest)
    if not f.exists():
        last_error=None
        for attempt in range(4):
            try:
                with requests.get(url,stream=True,timeout=(40,160)) as r:
                    r.raise_for_status()
                    with f.open("wb") as h:
                        for c in r.iter_content(chunk_size=1024*1024):
                            if c:h.write(c)
                if f.stat().st_size>50_000_000:break
                raise RuntimeError("Language package download incomplete")
            except Exception as e:
                last_error=e
                try:f.unlink(missing_ok=True)
                except Exception:pass
                time.sleep(5*(attempt+1))
        require(f.exists() and f.stat().st_size>50_000_000,"Offline model download failed: "+str(last_error))
    packages.install_from_path(f)
    languages=translator.get_installed_languages()
    a=next((x for x in languages if x.code=="ar"),None)
    en=next((x for x in languages if x.code=="en"),None)
    require(a is not None and en is not None,"Arabic-English offline model unavailable")
    model=a.get_translation(en)
    demo=model.translate("شرح حماية الحسابات من الاختراق")
    require(demo and ratio(demo)<.05,"Offline translator self-test failed")
    return model
def segments(s,maxlen=650):
    if len(s)<=maxlen:return [s]
    out=[];p=""
    for v in re.split(r"(?<=[.!؟؛])\s+",s):
        while len(v)>maxlen:
            if p:out.append(p);p=""
            cut=v.rfind(" ",0,maxlen)
            if cut<maxlen//3:cut=maxlen
            out.append(v[:cut]);v=v[cut:].lstrip()
        if len(p)+len(v)+1>maxlen:
            if p:out.append(p)
            p=v
        else:p=(p+" "+v).strip()
    if p:out.append(p)
    return out
def translate(s,model,cache):
    if not AR.search(s):return s
    if s in cache:return cache[s]
    start=len(s)-len(s.lstrip());end=len(s)-len(s.rstrip());core=s.strip()
    if not core or not AR.search(core):return s
    if core.startswith(("http:","https:","mailto:")):return s
    result=" ".join(model.translate(z).strip() for z in segments(core))
    require(len(result)>max(1,int(len(core)*.10)), "Translated text unexpectedly short")
    require(ratio(result)<.05, "Arabic remained in translated segment")
    value=s[:start]+result+(s[len(s)-end:] if end else "")
    cache[s]=value
    return value
def meaningful_text(soup):
    parts=[]
    for node in soup.find_all(string=True):
        a=node.parent;skip=False
        while getattr(a,"name",None):
            if a.name in SKIP_TAG or a.get("translate")=="no":skip=True;break
            a=a.parent
        if not skip:parts.append(str(node))
    for el in soup.find_all(True):
        for key in TEXT_ATTR:
            if el.get(key):parts.append(str(el.get(key)))
    return " ".join(parts)
def preservation(s):
    return {x: [str(v) for v in s.find_all(x)] for x in ("pre","code","script","style","iframe","video","audio","svg","math","form","textarea")}
def resources(s):
    return {x:[el.get("src") for el in s.find_all(x)] for x in ("img","iframe","video","audio","source","script")}
def rewrite(old,model,cache,pid):
    doc=BeautifulSoup(old["content"],"html.parser")
    previous=preservation(doc);sources=resources(doc)
    links=[x.get("href") for x in doc.find_all("a",href=True)]
    orig_visible=meaningful_text(doc)
    nodes=list(doc.find_all(string=True))
    for n in nodes:
        if not isinstance(n,NavigableString) or not n.parent:continue
        a=n.parent;skip=False
        while getattr(a,"name",None):
            if a.name in SKIP_TAG or a.get("translate")=="no":skip=True;break
            a=a.parent
        if not skip and AR.search(str(n)):
            n.replace_with(translate(str(n),model,cache))
    for element in doc.find_all(True):
        if element.name in SKIP_TAG:continue
        for key in TEXT_ATTR:
            value=element.get(key)
            if isinstance(value,str) and AR.search(value):
                element[key]=translate(value,model,cache)
    new_content=str(doc)
    parsed=BeautifulSoup(new_content,"html.parser")
    for key,value in previous.items():
        require(value==[str(v) for v in parsed.find_all(key)],"Code/media/widget markup changed: "+key)
    require(resources(parsed)==sources,"Image or linked media source changed")
    require([x.get("href") for x in parsed.find_all("a",href=True)]==links,"An existing URL/link changed")
    new_visible=meaningful_text(parsed)
    require(new_visible and (not orig_visible or .30<len(new_visible)/len(orig_visible)<4.0),"Translated length out of plausible range")
    require(ratio(new_visible)<.015,"Arabic remains in prose or accessible labels")
    title=old.get("title","")
    if AR.search(title):
        title=PREFERRED_TITLES.get(str(pid)) or translate(title,model,cache).strip()
    require(title and ratio(title)<.01 and len(title)<180,"Invalid translated title")
    require(str(pid) not in new_content,"Unexpected post id injected in article")
    return title,new_content,len(AR_LETTERS.findall(orig_visible))
def main():
    require(os.environ.get("BLOGGER_BLOG_ID")==BID,"Wrong Blogger target")
    require(0<=SHARD<SHARDS and SHARDS==8,"Unexpected job shard")
    s=session();resp=s.get(BASE,timeout=30);resp.raise_for_status()
    blog=resp.json()
    require(str(blog.get("id"))==BID and (urlsplit(blog.get("url","")).hostname or "").lower() in ("cyberoplus.com","www.cyberoplus.com"),"Wrong blog domain")
    published=list_pages(s,"live")
    drafts=list_pages(s,"draft")
    require(len(published)==258 and len(drafts)==1,"Unexpected Blogger inventory; do not modify")
    require(all(len(x.get("labels",[]))==1 and x["labels"][0] in CANONICAL for x in published),"Unexpected classification")
    published.sort(key=lambda p:str(p["id"]))
    subset=[p for index,p in enumerate(published) if index%SHARDS==SHARD]
    print("SHARD_INVENTORY_OK: shard="+str(SHARD)+" posts="+str(len(subset)),flush=True)
    full=[]
    for p in subset:
        actual=read(s,p["id"])
        require(actual["status"]=="LIVE" and actual["id"]==p["id"],"Target post unexpectedly changed state")
        temp=BeautifulSoup(actual["content"],"html.parser")
        if AR.search(actual.get("title","")) or AR.search(meaningful_text(temp)):
            full.append(actual)
    print("ARABIC_REQUIRES_CONVERSION: shard="+str(SHARD)+" count="+str(len(full))+" IDs="+",".join(str(p["id"]) for p in full),flush=True)
    if not full:
        print("SHARD_DONE: no Arabic content to convert",flush=True);return
    model=init_offline_model()
    cache={};stats=Counter();failed=[]
    for old in full:
        pid=str(old["id"])
        try:
            title,body,num_ar=rewrite(old,model,cache,pid)
            latest=read(s,pid)
            require(latest.get("content")==old.get("content") and latest.get("title")==old.get("title") and latest.get("labels")==old.get("labels") and latest.get("status")==old.get("status") and latest.get("updated")==old.get("updated"),"Post changed since translation began")
            payload={"title":title,"content":body}
            resp=s.patch(BASE+"/posts/"+pid,json=payload,timeout=90);resp.raise_for_status()
            saved=read(s,pid)
            for field in ("id","url","published","status","labels"):
                require(saved.get(field)==old.get(field),"CRITICAL: Blogger changed protected field "+field+" post="+pid)
            require(saved.get("title")==title and digest(saved.get("content",""))==digest(body),"CRITICAL: saved English content mismatch: "+pid)
            require(ratio(meaningful_text(BeautifulSoup(saved["content"],"html.parser")))<.015,"CRITICAL: Arabic visible after save")
            stats["published_verified"]+=1
            print("ENGLISH_POST_VERIFIED: "+pid+" arabic_letters_replaced="+str(num_ar)+" "+old["url"],flush=True)
        except Exception as e:
            stats["blocked"]+=1;failed.append(pid)
            print("BLOCKED_ARTICLE: "+pid+" "+type(e).__name__+" "+str(e)[:270],flush=True)
    print("SHARD_FINAL: id="+str(SHARD)+" "+json.dumps(stats)+" blocked_ids="+",".join(failed),flush=True)
    require(not failed,"At least one article failed; see blocked IDs; no fake completion")
if __name__=="__main__":main()
