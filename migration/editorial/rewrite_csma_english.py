#!/usr/bin/env python3
import os,json,hashlib,re
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
BID="2750143140558868280"
PID="8037245016776914431"
URL="https://www.cyberoplus.com/2026/03/network-access-methods-csma-cd-ca-token.html"
ORIG_TITLE="طرق الوصول إلى الشبكة — CSMA/CD وCSMA/CA وToken Passing"
TITLE="CSMA/CD vs CSMA/CA vs Token Passing: How Network Access Works"
DATE="2026-03-15T23:23:00Z"
SOURCE_HASH="b7f5d5b3108d2fdce3e846038d8bad23bc2d46ef8ff5f8581ca57fa0fb22a993"
MARK="<!-- CYBEROPLUS_INTERNAL_LINKS_2026_11 -->"
def sha(s):return hashlib.sha256(s.encode("utf-8")).hexdigest()
def check(x,m):
    if not x:raise RuntimeError(m)
t=json.loads(os.environ["BLOGGER_TOKEN_JSON"]);c=json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"]);conf=c.get("installed") or c.get("web") or {}
for k in ("client_id","client_secret","token_uri"):
    if not t.get(k) and conf.get(k):t[k]=conf[k]
s=AuthorizedSession(Credentials.from_authorized_user_info(t));api="https://www.googleapis.com/blogger/v3/blogs/"+BID
r=s.get(api,timeout=30);r.raise_for_status()
check(str(r.json().get("id"))==BID and (urlsplit(r.json().get("url","")).hostname or "").lower() in ("cyberoplus.com","www.cyberoplus.com"),"Wrong blog")
endpoint=api+"/posts/"+PID
def read():
    r=s.get(endpoint,params={"view":"ADMIN","fetchBody":"true"},timeout=40);r.raise_for_status();return r.json()
old=read()
check(str(old.get("id"))==PID and old.get("url")==URL and old.get("title")==ORIG_TITLE and old.get("published")==DATE and old.get("status")=="LIVE" and old.get("labels")==["technology"],"Original metadata changed")
body=old["content"];core=body.split("\n"+MARK)[0]
check(sha(core)==SOURCE_HASH,"Source HTML changed, no write")
parsed=BeautifulSoup(core,"html.parser")
images=parsed.find_all("img")
check(len(images)==4,"Four original images expected")
img_html=[]
for i,img in enumerate(images):
    wrap=img.find_parent("div",class_="separator") or img.find_parent("figure") or img.find_parent("table",class_="tr-caption-container")
    check(wrap is not None,"Original media wrapper absent")
    node=BeautifulSoup(str(wrap),"html.parser")
    descriptive_alt=[
      "Shared Ethernet collision detection using CSMA CD",
      "Half duplex Ethernet backoff sequence",
      "WiFi CSMA CA contention and randomized backoff",
      "Token Ring token passing network example"][i]
    node.find("img")["alt"]=descriptive_alt
    node.find("img")["title"]=descriptive_alt
    captions=[
      "Comparing CSMA CD, CSMA CA and token passing",
      "Shared half-duplex Ethernet collision detection and backoff",
      "WiFi contention using CSMA CA randomized backoff",
      "Token passing among stations on a historical Token Ring network"]
    for cell in node.select("td.tr-caption"):cell.string=captions[i]
    img_html.append(str(node))
new=r'''<p><strong>CSMA/CD, CSMA/CA and token passing</strong> are methods for coordinating access to a shared network medium. They were developed for different network designs and should not be described as interchangeable ways that every modern network sends data. In particular, <strong>CSMA/CD belongs to shared, half-duplex Ethernet</strong>; it is not used on ordinary switched, full-duplex Ethernet links today. Wi-Fi commonly uses contention and collision-avoidance mechanisms, while token passing is associated with historical technologies such as Token Ring.</p>
{{IMAGE1}}
<h2>Why is a media access method needed?</h2>
<p>When multiple stations can transmit on the same medium, they need rules for deciding when to send. If two stations attempt overlapping transmissions, the receiver may not decode the expected data. A medium-access-control (MAC) method determines how stations contend, defer, detect problems or receive permission to send.</p>
<p>It helps to distinguish the <strong>physical medium</strong> from the <strong>logical protocol</strong>: a wireless channel may be shared by many devices; a dedicated Ethernet link to a switch can operate full-duplex without the same collision behavior as a legacy hub.</p>
<h2>CSMA/CD: Carrier Sense Multiple Access with Collision Detection</h2>
<p><strong>CSMA/CD</strong> is the classic medium-access procedure for Ethernet when stations share a half-duplex collision domain. <em>Carrier Sense</em> means checking whether the medium is busy before starting. <em>Multiple Access</em> means several stations can try to use it. <em>Collision Detection</em> means a transmitting station can detect overlapping transmission and react.</p>
<ol data-steps>
<li>A station has an Ethernet frame ready to send.</li>
<li>It listens and defers while the medium is busy.</li>
<li>When allowed, it begins transmitting and monitors for a collision.</li>
<li>If a collision occurs, it stops the affected transmission and follows the specified backoff-and-retry behavior.</li>
<li>After a successful transmission, other stations may again contend for access.</li>
</ol>
{{IMAGE2}}
<p>Collisions are expected events under shared half-duplex Ethernet, not evidence that every cable is damaged. The probability of collision and the efficiency of the network depend on load, propagation delays, collision-domain constraints and protocol timing.</p>
<aside data-type="warning"><strong>Critical modern distinction:</strong> a normal full-duplex connection between a PC and an Ethernet switch can transmit and receive simultaneously and has no shared collision domain in the classic CSMA/CD sense. Such links <strong>do not use CSMA/CD</strong>. Statements claiming that every current Ethernet packet waits for CSMA/CD are misleading. Cisco's <a href="https://www.cisco.com/en/US/docs/internetworking/troubleshooting/guide/tr1904.html" rel="noopener noreferrer">Ethernet troubleshooting reference</a> explicitly distinguishes half-duplex CSMA/CD from full-duplex operation.</aside>
<h2>Why full-duplex Ethernet does not need collision detection</h2>
<p>A traditional hub repeats traffic and shares the transmission environment across attached half-duplex stations. A switch separates links, and a full-duplex point-to-point Ethernet connection uses separate transmission and reception paths or compatible signaling to support simultaneous communication. There is no need to contend with multiple unrelated stations for the same half-duplex wire in the legacy way.</p>
<p>Do not confuse Ethernet <strong>frame errors, packet drops, queue congestion or duplex mismatch</strong> with collisions. An incorrectly negotiated half/full-duplex pair can lead to poor performance on applicable older equipment; diagnosing the issue requires checking real interface counters and configuration.</p>
<h2>CSMA/CA: Carrier Sense Multiple Access with Collision Avoidance</h2>
<p><strong>CSMA/CA</strong> is associated with contention-based Wi-Fi access. A station senses whether the channel appears available, waits for appropriate interframe periods and uses a randomized backoff before transmitting. The objective is to <strong>reduce the probability of collisions</strong>, not to guarantee that collisions never occur.</p>
<ol data-steps>
<li>A wireless station has a frame ready to send.</li>
<li>It checks the channel's physical and virtual carrier-sensing conditions.</li>
<li>If the medium is busy, the station defers; it uses an appropriate contention backoff procedure.</li>
<li>When the conditions permit, it transmits the frame.</li>
<li>Where the protocol requires an acknowledgement, lack of an acknowledgement can lead to retry handling.</li>
</ol>
{{IMAGE3}}
<p>Wireless devices generally cannot rely on listening for a collision while transmitting in the same way as legacy wired Ethernet: transmitted and received signal strengths, half-duplex constraints and radio propagation complicate detection.</p>
<h3>What is the hidden-node problem?</h3>
<p>Two wireless stations may both reach an access point while being unable to hear each other. Each may consider the channel idle and start transmission, creating interference at the receiver. This is one example of why channel sensing does not guarantee exclusive access.</p>
<h3>Does RTS/CTS solve every collision?</h3>
<p>Request-to-Send/Clear-to-Send (RTS/CTS) is an optional coordination mechanism that can help with certain contention situations and the hidden-node problem. It adds overhead and is not necessarily used for every frame. Its usefulness depends on deployment conditions and protocol settings.</p>
<h2>Token passing: permission before transmission</h2>
<p>With <strong>token passing</strong>, the right to transmit is controlled using a token passed between stations. A station waits until it receives the appropriate token, uses its permitted transmission opportunity and then passes access onward according to protocol rules.</p>
<p>Token-passing methods can provide predictable access properties in suitable designs, but they add their own control and management requirements. Classic <strong>IEEE 802.5 Token Ring</strong> is a historical example rather than a standard you should expect in a modern office Ethernet installation. IEEE lists its relevant Token Ring standard as <strong>withdrawn</strong>.</p>
{{IMAGE4}}
<h2>CSMA/CD vs CSMA/CA vs token passing</h2>
<table><thead><tr><th>Method</th><th>How access is organized</th><th>Typical context</th><th>Important limitation</th></tr></thead><tbody>
<tr><td>CSMA/CD</td><td>Listen; transmit; detect collision and retry</td><td>Legacy shared half-duplex Ethernet</td><td>Not used on full-duplex switched Ethernet links</td></tr>
<tr><td>CSMA/CA</td><td>Listen, defer and use contention backoff to avoid collisions</td><td>Contention-based Wi-Fi access</td><td>Cannot eliminate every collision or radio interference problem</td></tr>
<tr><td>Token passing</td><td>Transmit when granted the token</td><td>Historical Token Ring and related designs</td><td>Not a typical access method in modern switched Ethernet</td></tr>
</tbody></table>
<h2>How are these methods related to the OSI model?</h2>
<p>Media-access control is mainly associated with the <strong>Data Link layer</strong>, while physical signaling belongs to the Physical layer. However, deployed standards define the detailed behavior, and teaching diagrams are only approximations. For additional context, see our <a href="https://www.cyberoplus.com/2026/03/osi-model-7-layers-explained.html">seven-layer OSI guide</a> and the <a href="https://www.cyberoplus.com/2026/03/llc-mac-sublayers-data-link-layer.html">LLC versus MAC sublayers comparison</a>.</p>
<h2>Do MAC access rules make a network secure?</h2>
<p>No. CSMA/CD, CSMA/CA and token passing primarily organize access to a medium; they are not replacements for encryption, network access control, authentication, segmentation or monitoring. Wi-Fi security protocols address different concerns from medium contention.</p>
<p>For example, successful Wi-Fi channel access does not establish that the application is trusted. Similarly, a full-duplex Ethernet link avoids classic shared-medium collisions, but it does not automatically protect data from unauthorized access.</p>
<h2>Practical questions to test your understanding</h2>
<ol data-steps>
<li><strong>A PC connects to a modern managed Ethernet switch using full duplex.</strong> Is it normally relying on CSMA/CD? <strong>No.</strong> Its switched point-to-point link does not require legacy half-duplex collision detection.</li>
<li><strong>Two Wi-Fi stations can reach an access point but cannot hear each other.</strong> What problem may appear? <strong>Hidden nodes</strong> can produce contention and collisions at the receiver.</li>
<li><strong>Which design assigns permission via a circulating control token?</strong> <strong>Token passing.</strong> Classic Token Ring is a historical example.</li>
<li><strong>Does an Ethernet frame's FCS prevent collisions?</strong> <strong>No.</strong> The FCS is an error-detection mechanism, not a method for reserving transmission time.</li>
</ol>
<h2>Where should you go next?</h2>
<p>Understanding MAC access methods becomes easier after learning <a href="https://www.cyberoplus.com/2026/03/network-topology-types-explained.html">network topologies</a>, <a href="https://www.cyberoplus.com/2026/04/ethernet-protocol-how-it-works.html">how Ethernet works</a> and the distinction between MAC addresses and IP routes. Our <a href="https://www.cyberoplus.com/2026/03/what-is-network-protocol.html">network protocols introduction</a> explains how standards define communication rules beyond physical access.</p>
<h2>Technical references</h2>
<ul>
<li><a href="https://www.ieee802.org/3/" rel="noopener noreferrer">IEEE 802.3 Ethernet working group</a></li>
<li><a href="https://www.ieee802.org/11/" rel="noopener noreferrer">IEEE 802.11 Wireless LAN working group</a></li>
<li><a href="https://www.cisco.com/en/US/docs/internetworking/troubleshooting/guide/tr1904.html" rel="noopener noreferrer">Cisco: half-duplex Ethernet versus full-duplex Ethernet</a></li>
<li><a href="https://standards.ieee.org/ieee/802.5/1102/" rel="noopener noreferrer">IEEE 802.5 Token Ring standard and withdrawn status</a></li>
</ul>'''
for i,node in enumerate(img_html,1):new=new.replace("{{IMAGE"+str(i)+"}}",node)
if MARK in body:new+="\n"+MARK+body.split(MARK,1)[1]
result=BeautifulSoup(new,"html.parser")
check(len(new)>9000 and len(result.find_all("h2"))>=10,"Rewrite incomplete")
check([x.get("src") for x in result.find_all("img")]==[x.get("src") for x in images],"Images or original links lost")
check(not re.search(r"[\u0600-\u06ff]",new),"Untranslated Arabic remains")
check("full-duplex" in new and "not used" in new and "withdrawn" in new,"Critical corrections absent")
old_targets={a["href"] for a in parsed.find_all("a",href=True) if a.get("href","").startswith("https://www.cyberoplus.com/")}
updated_targets={a["href"] for a in result.find_all("a",href=True)}
check(old_targets.issubset(updated_targets),"Previously published relevant links lost")
print("CSMA_EDITORIAL_VALIDATION_OK: IEEE/Cisco sources, modern Ethernet distinction, four images",flush=True)
now=read()
check(now.get("content")==body and now.get("title")==ORIG_TITLE,"Concurrent manual edit")
r=s.patch(endpoint,json={"title":TITLE,"content":new},timeout=70);r.raise_for_status()
after=read()
check(str(after.get("id"))==PID and after.get("url")==URL and after.get("published")==DATE and after.get("status")=="LIVE" and after.get("labels")==["technology"] and after.get("title")==TITLE,"Blogger resource identity changed")
check(sha(after.get("content",""))==sha(new),"Published HTML differs")
print("CSMA_ENGLISH_ARTICLE_PUBLISHED_AND_VERIFIED: "+URL,flush=True)
