#!/usr/bin/env python3
import os,json,hashlib,re
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
BLOG="2750143140558868280"
PID="5596922752938621756"
URL="https://www.cyberoplus.com/2025/08/cybersecurity-operations-center.html"
DATE="2025-08-15T17:33:00+01:00"
ORIGINAL_TITLE="ما هو SOC؟ كيف يعمل مركز العمليات الأمنية من التنبيه إلى الاستجابة للحوادث"
TITLE="What Is a Security Operations Center (SOC)? Roles, Tools and Workflow"
SHA="f001eec94b06e13be84ed325b3dac64406edbaf2921f8efad40d30cbce296b57"
MARK="<!-- CYBEROPLUS_INTERNAL_LINKS_2026_11 -->"
def sha(x):return hashlib.sha256(x.encode('utf-8')).hexdigest()
def check(x,m):
    if not x:raise RuntimeError(m)
token=json.loads(os.environ["BLOGGER_TOKEN_JSON"])
client=json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"])
conf=client.get("installed") or client.get("web") or {}
for k in ("client_id","client_secret","token_uri"):
    if not token.get(k) and conf.get(k):token[k]=conf[k]
sess=AuthorizedSession(Credentials.from_authorized_user_info(token))
base="https://www.googleapis.com/blogger/v3/blogs/"+BLOG
r=sess.get(base,timeout=30);r.raise_for_status()
check(str(r.json().get('id'))==BLOG and (urlsplit(r.json().get('url','')).hostname or '').lower() in ('cyberoplus.com','www.cyberoplus.com'),"Wrong Blogger blog")
api=base+"/posts/"+PID
def read():
    q=sess.get(api,params={"view":"ADMIN","fetchBody":"true"},timeout=40);q.raise_for_status();return q.json()
old=read()
check(str(old.get("id"))==PID and old.get("status")=="LIVE" and old.get("url")==URL and old.get("published")==DATE and old.get("title")==ORIGINAL_TITLE and old.get("labels")==["cybersecurity"],"Current post metadata changed")
source=old.get("content","")
base_original=source.split("\n"+MARK)[0]
check(sha(base_original)==SHA,"Original article changed since snapshot, no write")
soup=BeautifulSoup(base_original,"html.parser")
img=soup.find("img")
check(img is not None and len(soup.find_all("img"))==1,"Unexpected image count")
wrapper=img.find_parent("div",class_="separator") or img.find_parent("figure")
check(wrapper is not None,"Cannot preserve original image")
image=BeautifulSoup(str(wrapper),"html.parser")
image.find("img")["alt"]="Security operations center monitoring alerts and investigating cybersecurity incidents"
new=r'''<p>A <strong>Security Operations Center (SOC)</strong> is a team or operating capability that monitors an organization's systems, investigates suspicious activity and coordinates an appropriate response to cyber threats. A SOC is not simply a room of dashboards, nor is buying a SIEM platform enough to establish one. Effective operations combine trained people, reliable telemetry, clear procedures and authority to act.</p>
{{IMAGE}}
<p>This guide explains how a SOC works from the first recorded event to recovery, the roles of SIEM, EDR, XDR and SOAR, how analysts triage alerts, and how teams can measure their effectiveness. It is intended for cybersecurity students and IT professionals starting with defensive operations.</p>
<h2>What does a SOC actually do?</h2>
<p>At its core, the SOC answers practical questions: What is happening on our endpoints, accounts and networks? Which suspicious activities warrant investigation? Has an attack occurred? What is affected? What is the safest next action? The goal is to reduce the likelihood, duration and impact of security incidents—not to generate the largest possible number of alerts.</p>
<p>Some organizations maintain an in-house SOC around the clock, others operate during selected hours, and some use a managed service or hybrid team. A dashboard running 24/7 is not equivalent to 24/7 qualified human monitoring.</p>
<h2>Where does a SOC obtain its data?</h2>
<p>A SOC depends on <strong>telemetry</strong>: security-relevant records from many sources. Common inputs include endpoint activity from EDR agents, Windows and Linux logs, identity-provider and MFA events, DNS queries, firewalls, VPN gateways, cloud audit logs, email security products, web proxies, application logs and critical SaaS services.</p>
<p>Collecting more logs is not always better. Analysts need trustworthy timestamps, asset and user identities, appropriate retention, consistent schemas and enough context to understand what occurred. Large volumes of poorly structured events can hide significant attacks behind noise.</p>
<aside data-type="info"><strong>Example:</strong> a suspicious sign-in alert is more useful when correlated with the user's normal login locations, device health, recent MFA events and access to sensitive systems. One anomalous location alone does not prove account compromise.</aside>
<h2>Event vs alert vs incident</h2>
<table><thead><tr><th>Term</th><th>Meaning</th><th>Example</th></tr></thead><tbody>
<tr><td>Event</td><td>Something recorded by a system; often routine</td><td>A process starts or a user logs in</td></tr>
<tr><td>Alert</td><td>An event or pattern selected for review by a detection mechanism</td><td>A sign-in from an unusual location followed by suspicious actions</td></tr>
<tr><td>Incident</td><td>A security occurrence assessed as requiring an organizational response</td><td>Confirmed unauthorized use of a privileged account</td></tr>
</tbody></table>
<p>An alert is not automatically a confirmed incident. <strong>False positives</strong> can occur when a detection rule flags legitimate activity, and <strong>false negatives</strong> occur when a real threat is not detected.</p>
<h2>How an alert moves through a SOC</h2>
<h3>1. Detection</h3>
<p>Rules, analytics or analyst-driven searches identify behavior that may indicate malicious activity. Detection logic can look for known indicators, sequences of commands, abnormal account activity or behavior consistent with documented attack techniques.</p>
<h3>2. Triage and prioritization</h3>
<p>An analyst checks the alert's source, affected assets, reliability and potential impact. The analyst asks whether the alert has credible supporting evidence, whether similar events occurred and whether an account or system is business critical. A priority should reflect risk, not simply the severity label supplied by one tool.</p>
<h3>3. Investigation and scoping</h3>
<p>The team reconstructs a timeline using relevant event logs and other evidence. It may correlate the original activity with associated hosts, accounts, network connections and cloud changes. The goal is to determine the most plausible explanation, distinguish legitimate activity from threats and identify the incident's scope.</p>
<h3>4. Response and containment</h3>
<p>If an incident is confirmed, authorized responders follow a documented plan. Possible actions include disabling compromised credentials, isolating an affected device, blocking confirmed malicious connections, preserving evidence and escalating to the incident-response team. Response actions must account for business impact and evidence preservation.</p>
<h3>5. Recovery and improvement</h3>
<p>The organization restores trusted services, verifies recovery and records what must change: detection gaps, access controls, patching, log quality, response procedures or training. NIST's current incident-response recommendations emphasize continuous improvement across cybersecurity risk management, not a rigid sequence that ends once an alert closes.</p>
<h2>Example: suspicious administrator account activity</h2>
<ol data-steps>
<li>A cloud identity service records an unfamiliar successful sign-in by an administrator account.</li>
<li>Detection logic raises an alert because the sign-in conflicts with expected device or location signals and is followed by a privileged configuration change.</li>
<li>The analyst reviews sign-in details, device registration, MFA events and the affected administrative action.</li>
<li>The analyst contacts the appropriate owner through a trusted channel and determines whether the activity was authorized.</li>
<li>If misuse is supported by evidence, the incident team initiates approved containment, preserves records and assesses other affected resources.</li>
<li>After recovery, detection and access-control weaknesses are reviewed and corrected.</li>
</ol>
<p>This is a teaching scenario, not a description of a personally conducted SOC test or a one-size-fits-all playbook. An organization should adapt the workflow to its permissions, environment, legal obligations and operational needs.</p>
<h2>What is SIEM and how does it help?</h2>
<p><strong>Security Information and Event Management (SIEM)</strong> platforms aggregate and search security-relevant events, normalize data, correlate evidence and generate alerts or dashboards. SIEM is useful for cross-system investigation, but its value depends on correct data onboarding, rule design and analyst expertise.</p>
<p>Buying a SIEM does not automatically provide incident response. Without reliable data sources, defined ownership and maintained detection rules, a tool can create more noise than usable evidence.</p>
<h2>EDR and XDR in a SOC</h2>
<p><strong>Endpoint Detection and Response (EDR)</strong> collects security telemetry from endpoints and can support investigation or containment on supported systems. <strong>Extended Detection and Response (XDR)</strong> describes products designed to correlate signals across multiple security domains, potentially including endpoints, identities, email and cloud systems.</p>
<p>The capabilities and definitions vary by vendor. An XDR label is not proof that a product covers every attack path or eliminates the need for SIEM or specialized tools. Determine the specific telemetry and response capabilities actually required.</p>
<h2>SOAR and automation</h2>
<p><strong>Security Orchestration, Automation and Response (SOAR)</strong> supports workflows that connect tools, enrich alerts and, when properly authorized, carry out repeatable response steps. A workflow might collect asset ownership data, attach related events to a case or notify the on-call responder.</p>
<aside data-type="warning"><strong>Automate cautiously.</strong> High-impact actions such as disabling production accounts or isolating servers should use appropriate approval, safeguards and rollback plans. A false positive combined with uncontrolled automation can disrupt legitimate business operations.</aside>
<h2>Threat hunting versus alert-driven monitoring</h2>
<p>Routine monitoring reacts to events or alerts produced by existing detection rules. <strong>Threat hunting</strong> proactively tests a hypothesis about possible adversary activity, often drawing on endpoint, identity and network evidence. It can reveal blind spots that an alert-driven workflow misses.</p>
<p>For example, a hunter may look for a sequence of unusual parent-child processes and outbound connections consistent with a known technique rather than starting with an alert already raised by a vendor product. Findings should feed back into better detections.</p>
<h2>How MITRE ATT&amp;CK supports SOC investigations</h2>
<p><a href="https://attack.mitre.org/" rel="noopener noreferrer">MITRE ATT&amp;CK</a> provides a knowledge base of observed adversary tactics and techniques. Analysts use it to describe relevant behavior, connect detection coverage to attack techniques and identify gaps. ATT&amp;CK is not a checklist that automatically proves a compromise, nor does mapping every rule to a technique guarantee detection.</p>
<h2>Threat intelligence and indicators</h2>
<p>Threat intelligence provides context about attacker techniques, infrastructure, campaigns and relevant vulnerabilities. Indicators such as domains and file hashes may help investigation, but they can expire or create false positives. Teams should consider source quality, dates, confidence, relevance to their environment and whether an indicator can be safely acted upon.</p>
<h2>SOC roles: L1, L2 and L3</h2>
<table><thead><tr><th>Common label</th><th>Typical responsibility</th></tr></thead><tbody>
<tr><td>L1 analyst</td><td>Initial monitoring, alert validation, triage and escalation</td></tr>
<tr><td>L2 analyst</td><td>Deeper investigation, correlation, incident scoping and response support</td></tr>
<tr><td>L3 / senior analyst</td><td>Complex investigations, detection engineering, hunting and specialist escalation</td></tr>
</tbody></table>
<p>These levels are not universal job grades. Small SOCs may share responsibilities across the same people, while larger organizations separate engineering, intelligence, forensics, incident management and compliance functions.</p>
<h2>In-house SOC versus MDR</h2>
<p><strong>Managed Detection and Response (MDR)</strong> services provide externally delivered monitoring, investigation and often response support, depending on contract and integration. An in-house team offers direct organizational context and control but requires staffing and operational investment. A hybrid approach can combine internal knowledge with external coverage.</p>
<p>Compare the scope of detection, access permissions, escalation procedures, response ownership and service hours before selecting an arrangement. See our <a href="https://www.cyberoplus.com/2026/02/mdr-services-cybersecurity-guide.html">MDR explainer</a> for a dedicated comparison.</p>
<h2>How to measure SOC effectiveness</h2>
<p>Useful measures include the share of critical assets with adequate telemetry, validated detection coverage, time to triage, time to contain confirmed incidents, the rate of actionable alerts, the quality of investigation records and the outcome of exercises. Metrics should be interpreted in context: fewer alerts can mean better tuning, but can also indicate broken collection or missing detection.</p>
<p>Metrics such as mean time to detect (MTTD) and mean time to respond (MTTR) have value only when their start points, end points and populations are defined. A small average can conceal serious outliers or missing incidents.</p>
<h2>Will AI replace SOC analysts?</h2>
<p>Machine learning and AI-assisted workflows can help summarize cases, group related events and suggest investigative steps. However, model-generated explanations may be inaccurate or omit important evidence. Human review, reliable logging, access controls and careful validation remain necessary, particularly for consequential response actions.</p>
<h2>What should a new SOC prioritize?</h2>
<ol data-steps>
<li>Inventory critical assets and identify who owns incident response decisions.</li>
<li>Establish reliable identity, endpoint and infrastructure logging, with appropriate time synchronization and retention.</li>
<li>Define escalation paths, severity criteria and safe containment authority.</li>
<li>Start with a small set of relevant, testable detections rather than a large volume of unmaintained rules.</li>
<li>Practice with authorized exercises, review outcomes and continuously improve coverage.</li>
</ol>
<p>If you are starting from the fundamentals, our <a href="https://www.cyberoplus.com/2026/01/essential-cybersecurity-terms-beginners.html">introductory cybersecurity terminology guide</a> explains many of the concepts used in SOC work.</p>
<h2>Primary sources and further reading</h2>
<ul>
<li><a href="https://csrc.nist.gov/pubs/sp/800/61/r3/final" rel="noopener noreferrer">NIST SP 800-61 Rev. 3 (2025): Incident Response Recommendations</a></li>
<li><a href="https://www.ncsc.gov.uk/collection/building-a-security-operations-centre" rel="noopener noreferrer">UK NCSC: Building a Security Operations Centre</a></li>
<li><a href="https://www.ncsc.gov.uk/collection/building-a-security-operations-centre/detection/detection-practices" rel="noopener noreferrer">UK NCSC: Detection practices and triage</a></li>
<li><a href="https://attack.mitre.org/" rel="noopener noreferrer">MITRE ATT&amp;CK framework</a></li>
<li><a href="https://csrc.nist.gov/projects/cybersecurity-framework" rel="noopener noreferrer">NIST Cybersecurity Framework project</a></li>
</ul>'''
new=new.replace("{{IMAGE}}",str(image))
# Preserve the previously published contextual link section where it was appended.
if MARK in source:
    new+="\n"+MARK+source.split(MARK,1)[1]
s=BeautifulSoup(new,"html.parser")
check(len(new)>9000 and len(s.find_all("h2"))>=15 and len(s.find_all("img"))==1,"Article draft incomplete")
check([x.get("src") for x in s.find_all("img")]==[x.get("src") for x in soup.find_all("img")],"Image source lost")
check(not re.search(r"[\u0600-\u06ff]",new),"Arabic remains in final English article")
check("NIST SP 800-61 Rev. 3" in new and "MITRE" in new and "MDR" in new and "false positives" in new.lower(),"Core SOC lessons missing")
oldlinks={a["href"] for a in soup.find_all("a",href=True) if "cyberoplus.com" in a.get("href","")}
newlinks={a["href"] for a in s.find_all("a",href=True)}
check(oldlinks.issubset(newlinks),"Original internal article links missing")
print("SOC_ENGLISH_EDITORIAL_VALIDATION_PASSED",flush=True)
fresh=read()
check(fresh.get("content")==source and fresh.get("title")==ORIGINAL_TITLE and fresh.get("labels")==["cybersecurity"],"Concurrent Blogger edit detected")
r=sess.patch(api,json={"title":TITLE,"content":new},timeout=70);r.raise_for_status()
after=read()
check(str(after.get("id"))==PID and after.get("url")==URL and after.get("published")==DATE and after.get("status")=="LIVE" and after.get("labels")==["cybersecurity"] and after.get("title")==TITLE,"Blogger metadata mismatch")
check(sha(after.get("content",""))==sha(new),"Published HTML differs")
print("SOC_TRANSLATED_REWRITTEN_PUBLISHED_AND_VERIFIED: "+URL,flush=True)
