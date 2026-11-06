from pathlib import Path
import sys,subprocess,shutil,json,os,hashlib,ssl
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'.build-tools'));sys.path.insert(0,str(root/'app'))
from version import VERSION
import PyInstaller.__main__
# The shared desktop tool environment also puts Poppler on PATH. Its OpenSSL
# DLLs are not the Python runtime's dependencies and must not enter this app.
python_dlls=Path(sys.base_prefix)/'DLLs'
tls_dlls=[]
for names in (('libssl-3-x64.dll','libcrypto-3-x64.dll'),('libssl-3.dll','libcrypto-3.dll')):
    candidates=[python_dlls/name for name in names]
    if all(dll.is_file() for dll in candidates):
        tls_dlls=candidates;break
if not tls_dlls:raise RuntimeError('Python OpenSSL 3 dependencies missing from '+str(python_dlls))
os.environ['PATH']=str(python_dlls)+os.pathsep+os.environ.get('PATH','')
binary_args=[]
for dll in tls_dlls:binary_args.extend(['--add-binary',str(dll)+os.pathsep+'.'])
# This Python runtime's ssl.py loads a bundled certificate policy beside itself.
# PyInstaller collects Python modules but does not discover that extra data file.
certificate_policy=Path(ssl.__file__).with_name('codex-rejected-cas.pem')
data_args=['--add-data',str(certificate_policy)+os.pathsep+'.'] if certificate_policy.is_file() else []
PyInstaller.__main__.run(['--noconfirm','--clean','--onedir','--windowed','--name','BailongPet','--distpath',str(root/'发布包'/f'build-v{VERSION}'),'--workpath',str(root/'.build'/f'work-v{VERSION}'),'--specpath',str(root/'.build'),'--exclude-module','numpy','--exclude-module','scipy','--exclude-module','matplotlib','--exclude-module','tkinter','--exclude-module','PyQt5','--exclude-module','PyQt6','--hidden-import','autostart','--hidden-import','selftest','--hidden-import','version',*binary_args,*data_args,str(root/'app/pet.py')])
for dll in tls_dlls:
    collected=root/'发布包'/f'build-v{VERSION}'/'BailongPet/_internal'/dll.name
    if hashlib.sha256(collected.read_bytes()).digest()!=hashlib.sha256(dll.read_bytes()).digest():
        raise RuntimeError('Collected an incompatible TLS dependency: '+dll.name)
if data_args:
    collected=root/'发布包'/f'build-v{VERSION}'/'BailongPet/_internal'/certificate_policy.name
    if hashlib.sha256(collected.read_bytes()).digest()!=hashlib.sha256(certificate_policy.read_bytes()).digest():
        raise RuntimeError('Collected an incompatible TLS certificate policy')
