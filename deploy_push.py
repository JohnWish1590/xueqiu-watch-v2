# -*- coding: utf-8 -*-
"""雪哨 v2 精准部署脚本（仅开发/运维用，不要直接提交真实凭据）。

作用：把本地改好的代码与字库同步到 NAS 运行目录，并重启守护进程、验证首轮推送。

⚠️ 安全：NAS 凭据一律从环境变量读取，禁止硬编码进仓库。
   export NAS_HOST=192.168.0.31
   export NAS_USER=admin2
   export NAS_PASS='你的NAS密码'
   export NAS_PY=/share/CACHEDEV1_DATA/.qpkg/Python3/opt/python3/bin/python3.12

默认 live 目录是 .../run（不是父目录！父目录只是源码副本）。
只覆盖列出的文件，绝不回退 main.py 等未变文件。
"""
import os
import sys
import time
import paramiko

NAS_HOST = os.environ.get('NAS_HOST', '192.168.0.31')
NAS_USER = os.environ.get('NAS_USER', 'admin2')
NAS_PASS = os.environ.get('NAS_PASS', '')
if not NAS_PASS:
    sys.exit('[deploy] 错误：请先 export NAS_PASS 再运行（切勿把密码写死在脚本里）。')

SRC = os.path.dirname(os.path.abspath(__file__))
RUN = '/share/nas02/xueqiu-watch-v2/run'
DST = '/share/nas02/xueqiu-watch-v2'
PY = os.environ.get(
    'NAS_PY',
    '/share/CACHEDEV1_DATA/.qpkg/Python3/opt/python3/bin/python3.12',
)

# (local_rel, remote_rel) —— 只同步改动过的文件
FILES = [
    ('xueqiu.py', 'xueqiu.py'),
    ('eink.py', 'eink.py'),
    ('bitmapfont.py', 'bitmapfont.py'),
    ('fonts/font12.xwbf', 'fonts/font12.xwbf'),
    ('fonts/font16.xwbf', 'fonts/font16.xwbf'),
    ('fonts/font18.xwbf', 'fonts/font18.xwbf'),
]


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(NAS_HOST, port=22, username=NAS_USER, password=NAS_PASS, timeout=10)
    sftp = c.open_sftp()

    for local_rel, remote_rel in FILES:
        lp = os.path.join(SRC, local_rel)
        if not os.path.exists(lp):
            print(f'  ! 跳过缺失文件: {lp}')
            continue
        for base in (RUN, DST):
            rp = f'{base}/{remote_rel}'
            sftp.put(lp, rp)
            print(f'  ↑ {rp}  ({os.path.getsize(lp)//1024} KB)')
    sftp.close()
    print('字库+代码已推送。')

    # 验证新格式字库能被新 reader 读取
    print('--- 验证新字库格式 ---')
    _, o, e = c.exec_command(
        f'cd {RUN} && {PY} -c "import bitmapfont as bf; '
        f'f=bf.load_font(18); print(\\"font18 height=\\",f.height)"')
    print((o.read().decode() + e.read().decode()).strip())

    # 停掉旧进程（按进程名匹配，不写死 PID）
    print('--- 停止旧进程 ---')
    _, o, e = c.exec_command("ps | grep '[m]ain.py' | awk '{print $1}' | xargs -r kill 2>/dev/null; sleep 1; "
                             "ps | grep '[m]ain.py' || echo 'no main.py running'")
    print(o.read().decode().strip())

    # 从 run/ 重启
    print('--- 启动新进程 (run/) ---')
    _, o, e = c.exec_command(f'sh {RUN}/start-xueqiu.sh', timeout=30)
    print(o.read().decode().strip())

    time.sleep(5)

    print('--- 验证端口 ---')
    _, o, e = c.exec_command(f'netstat -tlnp 2>/dev/null | grep 8899 || echo "PORT DOWN"')
    print(o.read().decode().strip())

    print('--- 等待首轮推送 (最多 25s) ---')
    for _ in range(5):
        time.sleep(5)
        _, o, e = c.exec_command(f'tail -3 {RUN}/xw.log')
        tail = o.read().decode().strip()
        print(tail)
        if 'code":0' in tail or 'code:0' in tail or 'code=0' in tail:
            print('>>> 推送成功 (code:0)')
            break

    # 清掉特别关注成员缓存，强制下一轮用新分页逻辑重新拉全部分组
    # （否则 main.py 会一直用本地 group_cache.json 里的旧 20 人名单，修复不生效）
    print('--- 清除 group_cache.json（强制重拉特别关注分组）---')
    _, o, e = c.exec_command(f'rm -f {RUN}/group_cache.json {DST}/group_cache.json; echo cleared')
    print(o.read().decode().strip())

    c.close()
    print('\nDone.')


if __name__ == '__main__':
    main()
