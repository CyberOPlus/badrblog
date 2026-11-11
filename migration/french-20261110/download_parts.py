#!/usr/bin/env python3
"""Read only the exact authorized one-day payload artifacts; never persist corpus in Git."""
import hashlib,io,json,os,re,time,zipfile
from pathlib import Path
import requests
ROOT=Path('migration/french-20261110');API='https://api.github.com/repos/CyberOPlus/badrblog'
def need(v,m):
 if not v:raise RuntimeError(m)
def main():
 m=json.loads((ROOT/'manifest.json').read_text());wanted={x['artifact']:x for x in m['parts']}
 s=requests.Session();s.headers.update(Authorization='Bearer '+os.environ['GH_DOWNLOAD_TOKEN'],Accept='application/vnd.github+json',**{'X-GitHub-Api-Version':'2022-11-28'})
 found={}
 for page in range(1,21):
  r=s.get(API+'/actions/artifacts',params={'per_page':100,'page':page},timeout=45);need(r.status_code==200,'Artifact inventory HTTP '+str(r.status_code));items=r.json().get('artifacts',[])
  for a in items:
   name=a['name']
   if name in wanted and not a.get('expired') and (name not in found or a['created_at']>found[name]['created_at']):found[name]=a
  if len(found)==len(wanted) or len(items)<100:break
 need(len(found)==len(wanted),'Incomplete temporary payload: '+str(len(found))+'/'+str(len(wanted))+' parts')
 for item in m['parts']:
  a=found[item['artifact']];url=API+'/actions/artifacts/'+str(a['id'])+'/zip'
  r=s.get(url,timeout=90);need(r.status_code==200,'Payload download HTTP '+str(r.status_code))
  with zipfile.ZipFile(io.BytesIO(r.content)) as z:
   need(z.namelist()==[item['file']],'Unexpected artifact entry');data=z.read(item['file'])
  need(len(data)==item['characters'] and hashlib.sha256(data).hexdigest()==item['sha256'],'Artifact checksum differs')
  text=data.decode('ascii');need(bool(re.fullmatch(r'[A-Za-z0-9+/]+={0,2}',text)),'Invalid payload encoding');(ROOT/item['file']).write_bytes(data)
 print('ALL_TEMPORARY_PAYLOAD_PARTS_VERIFIED '+str(len(m['parts']))+'; no corpus files committed')
if __name__=='__main__':main()
