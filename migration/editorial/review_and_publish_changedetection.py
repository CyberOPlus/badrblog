#!/usr/bin/env python3
"""Safely review and publish the existing changedetection.io draft once; never recreate it."""
import os,json,hashlib,re
from bs4 import BeautifulSoup
from urllib.parse import urlsplit
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
BLOG="2750143140558868280"
PID="6824696513272823472"
API="https://www.googleapis.com/blogger/v3/blogs/"+BLOG+"/posts/"+PID
OLD_SHA="79fe62b1343a6e12060752f147a46fd25125377cb384b7a19e0ff384f1ce8cb6"
OLD_TITLE="changedetection.io: Website Change Detection and Automatic Alerts"
TITLE="changedetection.io Guide: Monitor Websites and Get Change Alerts"
URL="https://www.cyberoplus.com/2026/04/changedetection-io-self-hosted-website-change-monitoring.html"
PUBLISHED="2026-04-18T18:16:00+01:00"
def check(v,m):
    if not v:raise RuntimeError(m)
def sha(s):return hashlib.sha256(s.encode("utf-8")).hexdigest()
t=json.loads(os.environ["BLOGGER_TOKEN_JSON"])
c=json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"])
co=c.get("installed") or c.get("web") or {}
for k in ("client_id","client_secret","token_uri"):
    if not t.get(k) and co.get(k):t[k]=co[k]
session=AuthorizedSession(Credentials.from_authorized_user_info(t))
b=session.get("https://www.googleapis.com/blogger/v3/blogs/"+BLOG,timeout=30);b.raise_for_status()
check(str(b.json().get("id"))==BLOG and (urlsplit(b.json().get("url","")).hostname or "").lower() in ("cyberoplus.com","www.cyberoplus.com"),"Wrong blog")
def read():
    r=session.get(API,params={"view":"ADMIN","fetchBody":"true"},timeout=40);r.raise_for_status();return r.json()
source=read()
check(str(source.get("id"))==PID and source.get("status")=="DRAFT" and source.get("url")==URL and source.get("published")==PUBLISHED,"Unexpected post identity/state")
check(source.get("title")==OLD_TITLE and sha(source.get("content",""))==OLD_SHA,"Manual edit or concurrent modification; no write")
check(source.get("labels")==["guides"],"Current labels changed, no write")
old=source["content"]
new=old
pairs=[
("Self-hosting gives you full control over your data and your schedule at no license cost, and it suits anyone who already runs a server. The hosted service removes all maintenance: the project team runs the backend and the browsers while you only configure the watches. Both options offer the same core features.",
 "Self-hosting offers direct control over deployment and stored watch data, but you must maintain the server, updates, backups and security. The hosted service handles most infrastructure for you, with pricing, quotas and feature availability governed by its current plan. Check the official website for up-to-date differences rather than assuming both options have identical features."),
("Updating is not an optional step","Keep your installation updated"),
("The dashboard has no authentication by default. If the installation must be reachable from outside your machine, enable the <a href=\"https://github.com/dgtlmoon/changedetection.io/wiki/Password-protection\">password protection</a> provided by the tool and keep it behind a reverse proxy with HTTPS.",
 "The application can start without a login password. <strong>Keep the example Docker port bound to <code>127.0.0.1</code> while configuring it.</strong> Before allowing access from another device, follow the <a href=\"https://github.com/dgtlmoon/changedetection.io/wiki/Password-protection\">official password-protection instructions</a>, use HTTPS through a carefully configured reverse proxy, limit network exposure and update promptly. An HTTPS proxy alone does not replace authentication or access controls."),
("Some targets resist monitoring: aggressive anti-bot systems, heavy JavaScript, geographic restrictions or CAPTCHAs can block a fetch. The interface shows these failures, and you can retry, change the fetch method, or accept that a particular page is unreliable.",
 "Some sites restrict automated requests or require JavaScript and interactive sessions. Confirm you have permission to monitor the target, check its terms and documented rate limits, and use an official API or RSS feed when available. If the website blocks monitoring or presents a CAPTCHA, do not treat the block as permission to evade its access controls. Review errors and use a supported fetching method only where authorized."),
("Indirectly, yes: it can watch security advisories and release notes so you learn about a vulnerability the day the fix is delivered. The tool itself is open-source, and documented advisories such as <a href=\"https://nvd.nist.gov/vuln/detail/CVE-2026-35490\">CVE-2026-35490</a> are yet another reason to keep a self-hosted instance up to date.",
 "It can monitor public vendor security advisories and software release notes, but it does not assess, verify or remediate vulnerabilities for you. In April 2026, the project disclosed the authentication-bypass vulnerability <a href=\"https://github.com/advisories/GHSA-jmrh-xmgh-x9j4\" rel=\"noopener noreferrer\">CVE-2026-35490</a>, affecting changedetection.io versions up to and including <strong>0.54.7</strong>; the advisory lists <strong>0.54.8</strong> as the first patched version. Use a currently supported release and consult newer project advisories before publishing a dashboard to a network."),
("Between a self-hosted Docker deployment for full control and the hosted service for zero maintenance, it adapts to individuals and small teams alike.",
 "A self-hosted instance offers control with ongoing maintenance responsibilities; a hosted plan reduces infrastructure work but has its own terms and limits.")
]
for a,b in pairs:
    check(a in new,"Expected paragraph/headline not found; no write: "+a[:45])
    new=new.replace(a,b,1)
# Add a concise practical framework and primary sources, using existing article semantic HTML only.
addition='''<h2>Monitor websites responsibly</h2>
<aside data-type="warning"><strong>Respect access rules and sensitive data.</strong> Monitor public pages or systems for which you have authorization. Avoid sending account passwords, private tokens, personal data or restricted page contents to untrusted notification channels. Keep watch intervals reasonable and use documented APIs or feeds when available.</aside>
<p>For professional change monitoring, distinguish <strong>page updates</strong> from <strong>service availability</strong>. A changed HTML element does not prove that a product is available, that a security patch has been installed, or that a job posting is still accepting applications. Always open the official source and verify the change before acting on an alert.</p>
<h2>Security checklist for a self-hosted instance</h2>
<ol data-steps>
<li>Use the official maintained image and update it regularly. Review release notes and current security advisories.</li>
<li>Keep the initial web dashboard bound to localhost; do not publish an unauthenticated management interface.</li>
<li>Configure a strong login password before enabling remote access. Use TLS and an access restriction at your reverse proxy or network edge.</li>
<li>Protect the persistent datastore volume and exported notification credentials; restrict who can add monitoring targets.</li>
<li>Back up the datastore before upgrades, and verify you can restore a watch and its settings.</li>
<li>Test one alert with a non-sensitive page you control and check the resulting change history.</li>
</ol>
<h2>Official documentation and source verification</h2>
<ul>
<li><a href="https://github.com/dgtlmoon/changedetection.io" rel="noopener noreferrer">changedetection.io official source code and README</a></li>
<li><a href="https://github.com/dgtlmoon/changedetection.io/blob/master/docker-compose.yml" rel="noopener noreferrer">Official Docker Compose example and optional browser configuration</a></li>
<li><a href="https://changedetection.io/docs/api_v1/" rel="noopener noreferrer">changedetection.io API documentation</a></li>
<li><a href="https://github.com/dgtlmoon/changedetection.io/releases" rel="noopener noreferrer">Current releases and changelogs</a></li>
<li><a href="https://github.com/advisories/GHSA-jmrh-xmgh-x9j4" rel="noopener noreferrer">CVE-2026-35490 advisory and patched versions</a></li>
</ul>
'''
idx=new.rfind('<h2>Conclusion</h2>')
check(idx>0,"Conclusion marker not found")
new=new[:idx]+addition+new[idx:]
dom0=BeautifulSoup(old,"html.parser")
dom1=BeautifulSoup(new,"html.parser")
check(len(dom0.find_all("img"))==3 and [x.get("src") for x in dom0.find_all("img")]==[x.get("src") for x in dom1.find_all("img")],"Images altered")
check(len(dom1.find_all("h2"))>=27 and len(new)>len(old),"Incomplete improved article")
check(not re.search(r"[\u0600-\u06ff]",new),"Article contains Arabic text")
check("0.54.8" in new and "CVE-2026-35490" in new and "localhost" in new and "permission" in new,"Missing compliance or version safeguards")
check(not re.search(r'(?i)we tested|our tests show|we personally verified',new),"Unverified hands-on claims")
check(len(dom1.find_all("a",href=lambda x:x and "cyberoplus.com" in x))>=1,"Existing internal link not preserved")
print("DRAFT_EDITORIAL_VALIDATED: 3 images preserved, English reviewed, primary security advisory and responsible-use cautions included",flush=True)
# Confirm the original draft has not changed between preparing and writing.
latest=read()
check(sha(latest["content"])==OLD_SHA and latest.get("status")=="DRAFT" and latest.get("title")==OLD_TITLE and latest.get("labels")==["guides"],"Concurrent change detected, no write")
r=session.patch(API,json={"title":TITLE,"content":new,"labels":["technology"]},timeout=70)
r.raise_for_status()
saved=read()
check(str(saved.get("id"))==PID and saved.get("url")==URL and saved.get("published")==PUBLISHED and saved.get("status")=="DRAFT" and saved.get("title")==TITLE and saved.get("labels")==["technology"],"CRITICAL: draft identity/metadata changed")
check(sha(saved.get("content",""))==sha(new),"CRITICAL: edited HTML not saved as expected")
print("DRAFT_UPDATED_AND_VERIFIED: "+URL,flush=True)
# Only one official publish attempt; never circumvent policy restrictions or create an alternate post.
# Specify original publication date to avoid a misleading new-publish date if supported by Blogger.
r=session.post(API+"/publish",params={"publishDate":PUBLISHED},timeout=70)
if r.status_code>=400:
    print("PUBLICATION_REJECTED_BY_BLOGGER: HTTP="+str(r.status_code)+" body="+r.text[:500],flush=True)
    raise RuntimeError("Blogger did not permit draft publication. Do not retry by reposting; check Blogger warning / Request review.")
after=read()
check(str(after.get("id"))==PID and after.get("url")==URL and after.get("status")=="LIVE" and after.get("title")==TITLE and after.get("labels")==["technology"],"Publication returned but final state differs")
check(sha(after.get("content",""))==sha(new),"Published content differs")
if after.get("published") != PUBLISHED:
    print("ORIGINAL_PUBLISHED_DATE_CHANGED_BY_BLOGGER: was="+PUBLISHED+" now="+str(after.get("published")),flush=True)
else:
    print("ORIGINAL_PUBLISHED_DATE_PRESERVED: "+PUBLISHED,flush=True)
print("PUBLISHED_AND_VERIFIED: "+URL,flush=True)
