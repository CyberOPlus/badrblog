#!/usr/bin/env python3
"""Receive one authorized French payload part in a one-day Actions artifact, never Git."""
import hashlib,json,os,re
from pathlib import Path
ROOT=Path('migration/french-20261110')
def need(v,m):
 if not v:raise RuntimeError(m)
def main():
 m=json.loads((ROOT/'manifest.json').read_text())
 e=json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text());inputs=e.get('inputs') or {}
 need(e.get('repository',{}).get('full_name')=='CyberOPlus/badrblog','Wrong repository')
 need(inputs.get('package_id')==m['package_id'],'Wrong package')
 part=int(inputs.get('part_number','0'));need(1<=part<=len(m['parts']),'Invalid part number')
 item=m['parts'][part-1];data=inputs.get('payload_b64','')
 need(len(data)==item['characters'] and bool(re.fullmatch(r'[A-Za-z0-9+/]+={0,2}',data)),'Invalid payload part')
 need(hashlib.sha256(data.encode('ascii')).hexdigest()==item['sha256'],'Payload part checksum differs')
 out=Path('migration-inbox');out.mkdir(exist_ok=True);(out/item['file']).write_text(data,encoding='ascii')
 with open(os.environ['GITHUB_OUTPUT'],'a') as f:f.write('artifact_name='+item['artifact']+'\n')
 print('PART_ACCEPTED '+str(part)+'/'+str(len(m['parts']))+'; retention=1 day')
if __name__=='__main__':main()
