"""Check tracked text for credential formats and private deployment artifacts.
Heuristic defense in depth, not a complete secret scanner.
"""
import pathlib,re,subprocess,sys
PATTERNS={
 'github_token':r'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}',
 'cloud_access_id':r'AKID[A-Za-z0-9]{16,}|AKIA[A-Z0-9]{16}',
 'private_key':r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
 'credential_url':r'https?://[^\s/]+:[^\s/]+@',
}
root=pathlib.Path(__file__).resolve().parents[1];errors=[]
files=subprocess.check_output(['git','ls-files','-z'],cwd=root).decode().split('\0')
for name in filter(None,files):
 p=root/name
 if not p.is_file():continue
 if name.endswith(('.dump','.pem','.key')) or name.startswith(('audit-snapshots/','private-export/','production-config/')):errors.append((name,'private_artifact'))
 try:s=p.read_text()
 except UnicodeDecodeError:continue
 for label,pattern in PATTERNS.items():
  if re.search(pattern,s):errors.append((name,label))
for path,kind in errors:print(f'{path}: {kind}')
print(f'Public export checks: {len(errors)} finding(s)')
sys.exit(bool(errors))
