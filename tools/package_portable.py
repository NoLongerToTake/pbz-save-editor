"""Package app and runtime, excluding personal settings/saves and QA files."""
from pathlib import Path
import hashlib,json,zipfile

ROOT=Path(__file__).resolve().parents[1]
DIRECTORIES={'pbz_editor','data','runtime','vendor','bin','docs','tests','tools'}
FILES={'main.py','PBZ存档编辑器.exe','README.md','requirements.txt','THIRD_PARTY.md'}
output=ROOT/'PBZ-Save-Editor-1.0-beta-Windows-x64.zip'
required=['runtime/pythonw.exe','vendor/Crypto/__init__.py','bin/1031_pbz_decoder_v1.exe','PBZ存档编辑器.exe']
missing=[name for name in required if not (ROOT/name).is_file()]
if missing:raise SystemExit('请先准备便携包运行依赖及启动器：'+', '.join(missing))
included=[]
for path in ROOT.rglob('*'):
    if not path.is_file():continue
    relative=path.relative_to(ROOT)
    if relative.parts[0] not in DIRECTORIES and str(relative) not in FILES:continue
    if '__pycache__' in relative.parts or path.suffix.lower() in {'.pyc','.pyo','.sav','.bak','.tmp'}:continue
    if path.name in {'settings.json','startup_error.log'}:continue
    included.append((path,relative))
with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
    for path,relative in included:
        archive.write(path,str(Path('PBZ-Save-Editor-1.0-beta')/relative))
with zipfile.ZipFile(output) as archive:
    assert archive.testzip() is None
    assert not any(name.endswith(('.sav','.bak','settings.json')) for name in archive.namelist())
manifest=dict(archive=output.name,sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
              bytes=output.stat().st_size,files=len(included),archive_crc_verified=True,
              personal_saves_and_settings_included=False)
(ROOT/'1104_交付校验_v2.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(manifest,ensure_ascii=False))
