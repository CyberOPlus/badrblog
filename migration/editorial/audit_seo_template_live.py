#!/usr/bin/env python3
"""Read-only live Blogger SEO/Plus UI audit. No post, page or template writes."""
import os,json,re,collections
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
BID="2750143140558868280"
t=json.loads(os.environ["BLOGGER_TOKEN_JSON"])
c=json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"])
cf=c.get("installed") or c.get("web") or {}
for k in ("client_id","client_secret","token_uri"):
    if not t.get(k) and cf.get(k):t[k]=cf[k]
s=AuthorizedSession(Credentials.from_authorized_user_info(t))
base="https://www.googleapis.com/blogger/v3/blogs/"+BID
r=s.get(base,timeout=35);r.raise_for_status();b=r.json()
assert str(b["id"])==BID and (urlsplit(b["url"]).hostname or "").lower() in ("cyberoplus.com","www.cyberoplus.com")
allp=[];stats=collections.Counter()
for status in ("live","draft","scheduled"):
    token=None
    while True:
        params={"status":status,"maxResults":100,"fetchBodies":"true","view":"ADMIN"}
        if token:params["pageToken"]=token
        r=s.get(base+"/posts",params=params,timeout=90);r.raise_for_status()
        page=r.json();allp+=page.get("items",[])
        token=page.get("nextPageToken")
        if not token:break
assert len({str(p["id"]) for p in allp})==len(allp)
for p in allp:
    d=BeautifulSoup(p.get("content",""),"html.parser")
    words=d.get_text(" ",strip=True)
    stats["total"]+=1
    stats["status_"+p["status"]]+=1
    stats["arabic_in_title_or_visible"]+=bool(re.search(r"[\u0600-\u06ff]",p["title"]+words))
    stats["malformed_h2"]+=bool([n for n in d.find_all("h2") if len(n.get_text(strip=True))<4])
    stats["missing_h2"]+=d.find("h2") is None
    stats["images_without_alt"]+=sum(not x.get("alt","").strip() for x in d.find_all("img"))
    stats["tables"]+=len([x for x in d.find_all("table") if "tr-caption-container" not in x.get("class",[])])
    stats["table_wrapped_without_table_class"]+=len([x for x in d.find_all("table") if "tr-caption-container" not in x.get("class",[]) and x.parent and x.parent.name not in ("[document]","body") and not (x.parent.has_attr("class") and "table" in x.parent["class"])])
    stats["headings_with_inline_style"]+=len(d.select("h2[style],h3[style],h4[style]"))
    stats["steps_ol"]+=len(d.select("ol[data-steps],ol.steps"))
    stats["ordinary_ol"]+=len(d.select("ol:not([data-steps]):not(.steps)"))
    stats["heading_warnings"]+=len([x for x in d.find_all(["h2","h3"]) if not x.get_text(" ",strip=True)])
    stats["questionable_title"]+=bool(re.search(r"\bwhat does he really do\b|solemn loading|pivotal|shat script|are you really|the website does not exist|what are you doing now|\bwhat's nmap's tool\b",p["title"],re.I))
    stats["inline_styled_text"]+=len(d.select("p[style],h2[style],h3[style],li[style]"))
    stats["unexpected_article_h1"]+=len(d.find_all("h1"))
    stats["not_one_of_4_categories"]+=p.get("labels") not in [["technology"],["guides"],["cybersecurity"],["artificial-intelligence"]]
print("CURRENT_LIVE_BLOGGER_AUDIT="+json.dumps(dict(sorted(stats.items())),ensure_ascii=False),flush=True)
print("POSTS_SAMPLE="+json.dumps([{"id":p["id"],"status":p["status"],"title":p["title"],"labels":p.get("labels")} for p in allp[:5]]),flush=True)
