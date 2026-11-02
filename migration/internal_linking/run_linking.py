#!/usr/bin/env python3
"""Connect CyberoPlus published posts using verified relevant links.
Only appends contextual links to Blogger post HTML. Never alters draft status,
permalink, title, publication date, images, labels, XML, CSS, or JS.
"""
import hashlib,html,io,json,os,re,sys,zipfile
from collections import Counter
from urllib.parse import urlsplit,urljoin
import requests
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession

BLOG_ID="2750143140558868280"
ARTIFACT_ID="13165675858"
REPO="CyberOPlus/badrblog"
BASE="https://www.googleapis.com/blogger/v3/blogs/"+BLOG_ID
PLAN="0:248,14;1:253;2:212;3:227;4:11;7:151;8:170;9:84;10:125;11:208;18:19;21:22,88;22:89;27:15;32:75;39:78;40:142;45:47;49:50;50:46;55:56;56:51;60:45;63:64;65:201;67:29;73:71;74:141,243;75:32;76:162,236;81:67;82:195;83:178,187;84:239,9;90:222;91:197;93:189;94:231;96:70;100:214;101:205;102:126;104:110;106:111;107:112;108:135;110:104;111:106;112:107;113:130;115:223;118:210;121:163;125:10,149;126:102;128:34;130:113;132:13;133:177;134:160;135:108;136:199;137:198;140:219;142:161;143:244;148:0;160:134,156;162:76;165:166;170:8,183;171:114;173:238,179;176:196;181:171;182:229;185:119;196:176;197:91;204:7;209:28;211:12;212:2,82;213:105,98;218:184;224:131;225:92;229:41;230:188;235:139;239:158;242:193;244:143;245:144;247:148;249:254;250:147;253:1,5;254:249;256:241,127"
CUSTOM={43:"LLC and MAC sublayers of the Data Link layer",58:"simplex, half-duplex and full-duplex transmission",177:"legal ways to download music for offline listening",184:"Adobe Photoshop installation guide",192:"risks of unofficial WhatsApp applications",129:"what botnets are and how they work",155:"Continuous Threat Exposure Management (CTEM)",167:"Delinea privileged access security",26:"the Scrapling web-scraping framework",196:"official Moroccan sports TV programming",201:"Cloudflare outages and service resilience",211:"detecting deepfake images and videos",251:"NIST CSF 2.0 implementation",202:"AES, RSA and hashing in cryptography",8:"the Caroline Ellison and Manifund report",115:"offline educational apps for children",166:"Warframe on Xbox and cross-play",178:"Roblox access restrictions in Qatar",135:"Canadian farm worker recruitment",123:"the Moroccan teacher recruitment exam",113:"Hungary scholarships for Moroccan students",258:"changedetection.io website monitoring"}
MARKER="<!-- CYBEROPLUS_INTERNAL_LINKS_2026_11 -->"

def require(value, reason):
    if not value: raise RuntimeError(reason)
def sha(s): return hashlib.sha256(s.encode("utf-8")).hexdigest()
def normalize(u):
    try:
        q=urlsplit(urljoin("https://www.cyberoplus.com/",u))
        if (q.hostname or "").lower() not in {"www.cyberoplus.com","cyberoplus.com"}:return None
        return re.sub(r"/+","/",q.path).rstrip("/") or "/"
    except Exception:return None

def get_snapshot():
    h={"Authorization":"Bearer "+os.environ["GITHUB_TOKEN"],"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}
    u="https://api.github.com/repos/"+REPO+"/actions/artifacts/"+ARTIFACT_ID+"/zip"
    r=requests.get(u,headers=h,timeout=85);r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        data=json.loads(z.read("blogger-originals.json"))
    require(str(data["blog_id"])==BLOG_ID and len(data["posts"])==259,"Snapshot identity or inventory mismatch")
    return data["posts"]

def session():
    token=json.loads(os.environ["BLOGGER_TOKEN_JSON"])
    creds=json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"])
    c=creds.get("installed") or creds.get("web") or {}
    for field in ("client_id","client_secret","token_uri"):
        if not token.get(field) and c.get(field): token[field]=c[field]
    return AuthorizedSession(Credentials.from_authorized_user_info(token))

def all_posts(sess, status, bodies=False):
    acc=[];tok=None;seen=set()
    while True:
        query={"status":status,"maxResults":100,"fetchBodies":"true" if bodies else "false","view":"ADMIN"}
        if tok:query["pageToken"]=tok
        r=sess.get(BASE+"/posts",params=query,timeout=65);r.raise_for_status()
        acc+=r.json().get("items",[])
        tok=r.json().get("nextPageToken")
        if not tok:break
        require(tok not in seen,"Repeated pagination");seen.add(tok)
    return acc

def read(sess,pid):
    r=sess.get(BASE+"/posts/"+pid,params={"view":"ADMIN","fetchBody":"true"},timeout=45)
    r.raise_for_status()
    return r.json()

def anchor(p,index):
    if index in CUSTOM:return CUSTOM[index]
    title=BeautifulSoup(p["title"],"html.parser").get_text(" ",strip=True)
    if len(title)>105 or len(title)<16 or re.search(r"[\u0600-\u06ff]|Feller|Solemn|pivotal|what does he really|what does that mean|Shat script|best playground",title,re.I):
        title=urlsplit(p["url"]).path.rsplit("/",1)[-1].replace(".html","").replace("-"," ").strip().title()
    if len(title)>105:title=title.split(":")[0].split(" — ")[0]
    return title[:105].strip(" .:—-")

def edges(items):
    ix={normalize(x["url"]):i for i,x in enumerate(items) if x["status"]=="LIVE"}
    outbound={i:set() for i in range(len(items))}
    for i,p in enumerate(items):
        for a in BeautifulSoup(p.get("content",""),"html.parser").find_all("a",href=True):
            j=ix.get(normalize(a["href"]))
            if j is not None and i!=j:outbound[i].add(j)
    return outbound

def main():
    require(os.environ.get("BLOGGER_BLOG_ID")==BLOG_ID,"Wrong Blogger blog secret")
    snapshot=get_snapshot()
    require(len(snapshot)==259 and sum(x["status"]=="LIVE" for x in snapshot)==258,"Expected 258 live and one draft at snapshot")
    edits={}
    for part in PLAN.split(";"):
        a,b=part.split(":")
        i=int(a)
        to=[int(x) for x in b.split(",")]
        require(i not in edits and len(to)==len(set(to)) and 1<=len(to)<=3,"Duplicate or oversized edit")
        require(snapshot[i]["status"]=="LIVE" and all(snapshot[j]["status"]=="LIVE" and i!=j for j in to),"Draft/nonlive/self-link in plan")
        edits[i]=to
    require(len(edits)==100 and sum(map(len,edits.values()))==114,"Unexpected planned scope")
    original_graph=edges(snapshot)
    expected={i:set(js) for i,js in original_graph.items()}
    for i,js in edits.items():
        require(not set(js)&expected[i],"Plan would duplicate an existing internal link")
        expected[i].update(js)
    live_indices=[i for i,p in enumerate(snapshot) if p["status"]=="LIVE"]
    incoming=Counter(j for i in live_indices for j in expected[i])
    require(all(expected[i] and incoming[i]>0 for i in live_indices),"Plan failed graph coverage")
    print("PLAN_VERIFIED: 258 published articles have inbound and outbound links; 100 articles targeted; 114 new contextual links",flush=True)

    sess=session()
    r=sess.get(BASE,timeout=30);r.raise_for_status()
    blog=r.json()
    require(str(blog.get("id"))==BLOG_ID and normalize(blog.get("url","")) is not None,"Wrong Blogger target")
    live=all_posts(sess,"live",False)
    drafts=all_posts(sess,"draft",False)
    require(len(live)==258 and len(drafts)==1 and len({str(p["id"]) for p in live+drafts})==259,"Current post count/status changed; abort")
    current={str(p["id"]):p for p in live}
    require(all(str(p["id"]) in current and current[str(p["id"])]["url"]==p["url"] for p in snapshot if p["status"]=="LIVE"),"Published post/URL changed; abort")

    complete=0;skipped=0;already=0;added_links=0;bad=[]
    for i,js in edits.items():
        original=snapshot[i]
        pid=str(original["id"])
        livepost=read(sess,pid)
        expected_status=original["status"]
        require(str(livepost.get("id"))==pid and livepost.get("url")==original["url"] and livepost.get("status")==expected_status,"Source URL/status/ID changed "+pid)
        if MARKER in livepost.get("content",""):
            already+=1;print("ALREADY_HAS_LINK_MARKER: "+pid,flush=True);continue
        if sha(livepost["content"])!=sha(original["content"]) or livepost.get("title")!=original["title"] or livepost.get("published")!=original["published"]:
            skipped+=1;bad.append(pid)
            print("SKIPPED_MANUAL_EDIT: "+pid+" "+original["url"],flush=True)
            continue
        existing_html=BeautifulSoup(original["content"],"html.parser")
        existing_src=[x.get("src") for x in existing_html.find_all("img")]
        refs=[]
        for j in js:
            target=snapshot[j]
            require(str(target["id"]) in current and current[str(target["id"])]["url"]==target["url"],"Target disappeared or changed status")
            href=html.escape(target["url"],quote=True)
            name=html.escape(anchor(target,j),quote=False)
            refs.append('<a href="'+href+'">'+name+'</a>')
        if len(refs)==1:
            sentence="For related background, see "+refs[0]+"."
        else:
            sentence="For related background, see "+", ".join(refs[:-1])+" and "+refs[-1]+"."
        addition="\n"+MARKER+"\n<p><strong>Related reading:</strong> "+sentence+"</p>\n"
        new_html=original["content"]+addition
        parsed=BeautifulSoup(new_html,"html.parser")
        require([x.get("src") for x in parsed.find_all("img")]==existing_src,"Changed existing images")
        require(len(parsed.find_all("a",href=True))==len(existing_html.find_all("a",href=True))+len(js),"Link count mismatch")
        require(new_html.startswith(original["content"]),"Existing body accidentally modified")
        payload={"content":new_html}
        r=sess.patch(BASE+"/posts/"+pid,json=payload,timeout=65);r.raise_for_status()
        after=read(sess,pid)
        for field in ("id","title","url","published","status"):
            require(after.get(field)==original.get(field),"CRITICAL field altered: "+field+" "+pid)
        require(sha(after.get("content",""))==sha(new_html),"CRITICAL content mismatch "+pid)
        require(after.get("labels")==livepost.get("labels"),"CRITICAL label changed "+pid)
        complete+=1;added_links+=len(js)
        print("PUBLISHED_VERIFIED: "+str(complete)+"/"+str(len(edits))+" source="+pid+" links="+str(len(js)),flush=True)

    final=all_posts(sess,"live",True)
    final_drafts=all_posts(sess,"draft",False)
    require(len(final)==258 and len(final_drafts)==1,"Final published/draft count changed")
    order={str(p["id"]):i for i,p in enumerate(snapshot)}
    result_graph={i:set() for i in range(len(snapshot))}
    index_by_path={normalize(p["url"]):i for i,p in enumerate(snapshot) if p["status"]=="LIVE"}
    for p in final:
        i=order[str(p["id"])]
        for a in BeautifulSoup(p["content"],"html.parser").find_all("a",href=True):
            j=index_by_path.get(normalize(a["href"]))
            if j is not None and j!=i:result_graph[i].add(j)
    indegree=Counter(j for i in live_indices for j in result_graph[i])
    orphan_in=[str(snapshot[i]["id"]) for i in live_indices if indegree[i]==0]
    orphan_out=[str(snapshot[i]["id"]) for i in live_indices if not result_graph[i]]
    print("FINAL_SUMMARY: published=258 draft=1 updated="+str(complete)+" new_links="+str(added_links)+" skipped_manual_edits="+str(skipped)+" already_marked="+str(already),flush=True)
    print("FINAL_GRAPH: incoming_zero="+str(len(orphan_in))+" outgoing_zero="+str(len(orphan_out)),flush=True)
    if orphan_in:print("NEED_FOLLOWUP_INCOMING: "+",".join(orphan_in),flush=True)
    if orphan_out:print("NEED_FOLLOWUP_OUTGOING: "+",".join(orphan_out),flush=True)
    if bad:print("NEED_MANUAL_REVIEW: "+",".join(bad),flush=True)
    require(not orphan_in and not orphan_out and skipped==0,"Partial linking verified; review skipped sources and remaining orphan IDs")
    print("ALL_258_PUBLISHED_POSTS_LINKED_BOTH_DIRECTIONS_AND_VERIFIED",flush=True)

if __name__=="__main__":
    main()
