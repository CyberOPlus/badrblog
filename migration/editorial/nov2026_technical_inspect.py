#!/usr/bin/env python3
"""One-time targeted inspection of current Blogger technical headings. Read-only."""
import os,json,re,hashlib
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession

BID="2750143140558868280"
targets={
"8600317456877269158":"Coaxial",
"8842376614236276048":"Scrapling",
"6784786877331689772":"ClientServer",
"4889014546407141125":"InternetIntranet",
"1464650545159304816":"NetworkComponents",
"2702754847974296063":"DefenderControl",
"8350571397770012867":"HuaweiLauncher",
"9071374544546958469":"WirelessMedia",
"1668733775309362653":"ProgrammingLanguages"
}
cred=json.loads(os.environ["BLOGGER_TOKEN_JSON"])
cfg=json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"])
cfg=cfg.get("installed") or cfg.get("web") or {}
for name in ("client_id","client_secret","token_uri"):
    if not cred.get(name) and cfg.get(name):cred[name]=cfg[name]
s=AuthorizedSession(Credentials.from_authorized_user_info(cred))
base="https://www.googleapis.com/blogger/v3/blogs/"+BID
r=s.get(base,timeout=35);r.raise_for_status();b=r.json()
assert str(b.get("id"))==BID and urlsplit(b.get("url","")).hostname in {"cyberoplus.com","www.cyberoplus.com"}
for pid,label in targets.items():
    rr=s.get(base+"/posts/"+pid,params={"view":"ADMIN","fetchBody":"true"},timeout=60);rr.raise_for_status();p=rr.json()
    html=p.get("content","")
    soup=BeautifulSoup(html,"html.parser")
    heads=soup.find_all(["h2","h3"])
    print("INSPECT_POST="+json.dumps({"id":pid,"label":label,"title":p.get("title"),
        "status":p.get("status"),"url":p.get("url"),"updated":p.get("updated"),
        "published":p.get("published"),"sha":hashlib.sha256(html.encode()).hexdigest(),
        "headings":len(heads),"links":len(soup.find_all("a")),
        "images":len(soup.find_all("img"))},ensure_ascii=False),flush=True)
    for i,h in enumerate(heads):
        # Exact HTML headings and nearby body verify context, no raw credentials or media
        context=[]
        for sib in h.next_siblings:
            if getattr(sib,"name",None) in {"h2","h3","h4"}:break
            if getattr(sib,"name",None) in {"p","ol","ul","table","aside"}:
                context.append(sib.get_text(" ",strip=True)[:240])
                if len(context)>=2:break
        print("INSPECT_HEADING="+json.dumps({"id":pid,"n":i,"tag":h.name,
            "text":h.get_text(" ",strip=True),"raw":str(h)[:650],
            "next":context},ensure_ascii=False),flush=True)
    patterns=["imams","pivotal cable","50°","75°","Feed tcher","Fee tcher",
       "F e tcher","What does Tamper","What do you mean","hackers","Power Shell",
       "Type Script","C+ and","Scraping robots","Broxy","What is it?"]
    flat=soup.get_text(" ",strip=True)
    for match in re.finditer("|".join(re.escape(k) for k in patterns),flat,re.I):
        print("INSPECT_CONTEXT="+json.dumps({"id":pid,"phrase":match.group(0),
            "surrounding":flat[max(0,match.start()-150):match.end()+180]},
            ensure_ascii=False),flush=True)
