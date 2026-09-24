# -*- coding: utf-8 -*-
"""临时工具：把本地某个脚本上传到 NAS run/ 并执行，回显 stdout+stderr。

用法: NAS_PASS=... python _nas_run.py <local_script.py> [remote_args]
"""
import os
import sys
import paramiko

HOST = os.environ.get('NAS_HOST', '192.168.0.31')
USER = os.environ.get('NAS_USER', 'admin2')
PASS = os.environ.get('NAS_PASS', '')
RUN = '/share/nas02/xueqiu-watch-v2/run'
PY = os.environ.get(
    'NAS_PY', '/share/CACHEDEV1_DATA/.qpkg/Python3/opt/python3/bin/python3.12')

if not PASS:
    sys.exit('need NAS_PASS')
if len(sys.argv) < 2:
    sys.exit('usage: _nas_run.py <local_script.py> [args...]')

local = os.path.abspath(sys.argv[1])
name = os.path.basename(local)
args = ' '.join(sys.argv[2:])

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, port=22, username=USER, password=PASS, timeout=10)
sftp = c.open_sftp()
sftp.put(local, '%s/%s' % (RUN, name))
sftp.close()
print('uploaded %s -> %s' % (name, RUN))

_, o, e = c.exec_command(
    'cd %s && %s -u %s %s 2>&1' % (RUN, PY, name, args), timeout=180)
print(o.read().decode('utf-8', 'ignore'))
print(e.read().decode('utf-8', 'ignore'))
c.close()
