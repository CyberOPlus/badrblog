#!/usr/bin/env python3
import os,json,hashlib,collections
from urllib.parse import urlsplit
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
BLOG="2750143140558868280"
BASE="https://www.googleapis.com/blogger/v3/blogs/"+BLOG
REVIEWED=[["6462241883487624632","Système d’exploitation PDF: simplified explanation and exercise with correction","Operating Systems PDF: Core Concepts, Exercises and Solutions","/2026/10/cours-systeme-exploitation-pdf.html","2026-10-15T17:24:15Z",["operating","linux"]],["2278134239424852749","What are you doing now in Manifund?","Caroline Ellison Joins Manifund: New Role and 2026 Timeline","/2026/09/caroline-ellison-new-job-manifund.html","2026-09-12T17:51:05+01:00",["manifund","caroline"]],["3172880476950116217","Security of LLM applications: Guide to Building GenAI, RG and AAI Agents in a safer manner","LLM Application Security: Protect RAG, AI Agents and Sensitive Data","/2026/05/llm-security-prompt-injection-data-leakage.html","2026-05-21T13:54:07+01:00",["rag","agent"]],["6665943173911723036","What's the difference? Thernet?","LLC vs MAC Sublayers: Data Link Layer and Ethernet Explained","/2026/03/llc-mac-sublayers-data-link-layer.html","2026-03-28T13:43:00+01:00",["ethernet","llc"]],["8254791976075725269","What's Baseband?","Baseband Transmission Explained: Signals, Examples and Uses","/2026/03/baseband-transmission-explained.html","2026-03-20T09:38:00Z",["baseband","signal"]],["1188392806047417279","Encapsulation andDecapsulation: how do data move between layers of the network?","Encapsulation vs Decapsulation: How Data Moves Through Network Layers","/2026/03/network-layered-model-encapsulation-explained.html","2026-03-16T22:29:00Z",["encapsulation","network"]],["3680625875969075712","What does he really do?","Best Ways to Earn Money From Your Phone in 2026: Apps and Freelancing","/2026/02/make-money-apps-phone.html","2026-02-23T14:14:00Z",["upwork","fiverr"]],["6607535559312677251","What does he really do?","Earn Money Watching Videos in 2026? Rewards, Apps and Scam Risks","/2025/06/make-money-watching-videos-app.html","2025-06-23T18:51:00+01:00",["kwai","swagbucks"]]]
def check(x,msg):
    if not x:raise RuntimeError(msg)
def digest(x):return hashlib.sha256(x.encode("utf-8")).hexdigest()
token=json.loads(os.environ["BLOGGER_TOKEN_JSON"])
client=json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"])
config=client.get("installed") or client.get("web") or {}
for k in ("client_id","client_secret","token_uri"):
    if not token.get(k) and config.get(k):token[k]=config[k]
sess=AuthorizedSession(Credentials.from_authorized_user_info(token))
r=sess.get(BASE,timeout=30);r.raise_for_status()
check(str(r.json().get("id"))==BLOG and (urlsplit(r.json().get("url","")).hostname or "").lower() in ("www.cyberoplus.com","cyberoplus.com"),"Wrong blog")
def get(pid):
    r=sess.get(BASE+"/posts/"+pid,params={"view":"ADMIN","fetchBody":"true"},timeout=40);r.raise_for_status();return r.json()
print("PRECHECK_ALL_REVIEWED_TITLES",flush=True)
posts=[]
for pid,old,new,path,published,terms in REVIEWED:
    p=get(pid)
    check(str(p.get("id"))==pid and p.get("status")=="LIVE" and p.get("title") in (old,new) and urlsplit(p.get("url","")).path==path and p.get("published")==published,"Post identity changed: "+pid)
    check(len(p.get("content",""))>500 and all(t in p["content"].lower() for t in terms),"Article no longer matches intended title: "+pid)
    posts.append((p,new))
print("ALL_8_PRECHECKS_PASSED",flush=True)
count=0
for p,new in posts:
    pid=str(p["id"])
    latest=get(pid)
    check(latest.get("title")==p["title"] and latest.get("content")==p["content"] and latest.get("labels")==p.get("labels"),"Concurrent update, stopped: "+pid)
    if latest["title"]==new:
        print("ALREADY_CORRECT: "+pid,flush=True);continue
    r=sess.patch(BASE+"/posts/"+pid,json={"title":new},timeout=55);r.raise_for_status()
    saved=get(pid)
    for field in ("id","url","published","status","content","labels"):
        check(saved.get(field)==p.get(field),"Unexpected Blogger change in "+field+" for "+pid)
    check(saved.get("title")==new,"New title not saved")
    count+=1
    print("SEO_TITLE_VERIFIED: "+pid+" "+new,flush=True)
print("TITLE_FIXES_COMPLETE: "+str(count)+" corrected, "+str(len(REVIEWED)-count)+" already corrected",flush=True)
