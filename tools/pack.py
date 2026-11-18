# -*- coding: utf-8 -*-
"""把插件目录打成 WBOX 插件包（wbox-plugin-zip-v1），并生成 .sha256 侧车。

用法（本仓库根目录执行）：
    python tools/pack.py                 # 打包 plugins\\ 下全部插件 -> dist\\
    python tools/pack.py plugins\\ace     # 只打一个
    python tools/pack.py --check dist\\ace-v2.1.1.zip   # 只校验已有包

格式约定（与 WBOX 主程序 src/plugin_package.py 一一对应，改一处必须改两处）：
    压缩包根下只有一个 <id>\\ 目录，<id>\\plugin.json 是清单，入口文件由 manifest.entry 指定；
    白名单扩展名、文件数 / 单文件 / 总量上限见下面的常量；
    侧车 <包名>.sha256 记录整包哈希，安装界面会拿它核对"包有没有被换过"。

这是**独立实现**，不 import 主程序代码：插件仓库要能单独克隆、单独打包。
打完会重新打开产物逐项自检，能打出来的一定装得进去。
"""
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT, 'plugins')
SHARED_DIR = os.path.join(ROOT, 'shared')
OUT_DIR = os.path.join(ROOT, 'dist')

# 共享模块 -> 需要它的插件目录。唯一正文在 shared\，打包前逐字节刷进各插件，
# 避免"改一处忘另一处"（afterburner 与 gamepp 的关闭/ACL 逻辑曾是两份）。
SYNC_TARGETS = {'proc_util.py': ('afterburner', 'gamepp')}

FORMAT_ID = 'wbox-plugin-zip-v1'
MANIFEST = 'plugin.json'
ID_PATTERN = re.compile(r'^[a-z][a-z0-9_]{1,31}$')

ALLOWED_EXT = frozenset((
    '.py', '.json', '.md', '.txt', '.cfg', '.ini', '.yaml', '.yml',
    '.csv', '.xml', '.png', '.ico',
))
SKIP_DIRS = frozenset(('__pycache__', 'data', '.git', '.idea'))
SKIP_SUFFIX = ('.pyc', '.pyo', '.bak', '.tmp', '.log', '.zip', '.flag')
SKIP_NAMES = frozenset(('Thumbs.db', 'desktop.ini'))

MAX_FILES = 64
MAX_MEMBER_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024


class PackError(Exception):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def sync_shared(verbose=True):
    """把 shared\\ 的唯一正文刷进各插件目录（有差异才写）。"""
    written = []
    for fname, pids in sorted(SYNC_TARGETS.items()):
        src = os.path.join(SHARED_DIR, fname)
        if not os.path.isfile(src):
            raise PackError('共享模块缺正文：%s' % src)
        with open(src, 'rb') as f:
            base = f.read()
        for pid in pids:
            dst = os.path.join(SRC_DIR, pid, fname)
            try:
                with open(dst, 'rb') as f:
                    same = f.read() == base
            except OSError:
                same = False
            if not same:
                shutil.copyfile(src, dst)
                written.append('%s/%s' % (pid, fname))
    if verbose and written:
        print('同步共享模块：%s' % '、'.join(written))
    return written


def check_ext(rel):
    ext = os.path.splitext(rel)[1].lower()
    if ext not in ALLOWED_EXT:
        raise PackError('不允许的文件类型 %r（%s）' % (ext or '无扩展名', rel))


def collect(plugin_dir):
    """返回 [(相对路径, 绝对路径)]，已排序；顺带做体积/类型校验。"""
    out = []
    total = 0
    for dp, dns, fns in os.walk(plugin_dir):
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS)
        for fn in sorted(fns):
            if fn in SKIP_NAMES or fn.lower().endswith(SKIP_SUFFIX):
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, plugin_dir).replace('\\', '/')
            check_ext(rel)
            size = os.path.getsize(full)
            if size > MAX_MEMBER_BYTES:
                raise PackError('单文件超过 %d KB：%s' % (MAX_MEMBER_BYTES // 1024, rel))
            total += size
            out.append((rel, full))
    if not out:
        raise PackError('插件目录里没有可打包的文件')
    if len(out) > MAX_FILES:
        raise PackError('文件数超过上限 %d' % MAX_FILES)
    if total > MAX_TOTAL_BYTES:
        raise PackError('解压总量超过上限 %d KB' % (MAX_TOTAL_BYTES // 1024))
    return out


def read_manifest(plugin_dir):
    path = os.path.join(plugin_dir, MANIFEST)
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        raise PackError('读不到 %s：%r' % (path, e))
    pid = raw.get('id')
    if not isinstance(pid, str) or not ID_PATTERN.match(pid):
        raise PackError('非法插件 id：%r' % (pid,))
    if os.path.basename(os.path.abspath(plugin_dir)) != pid:
        raise PackError('目录名 %s 与 manifest.id %s 不一致'
                        % (os.path.basename(plugin_dir), pid))
    version = str(raw.get('version') or '0.0.0')
    if not re.match(r'^\d+\.\d+\.\d+$', version):
        raise PackError('version 必须是三段式 vMAJOR.MINOR.PATCH：%r' % version)
    entry = str(raw.get('entry') or '').split(':', 1)[0].strip()
    if not entry:
        raise PackError('manifest 缺 entry')
    return raw, pid, version, entry.replace('\\', '/')


def verify(path, pid, members):
    """打包后逐项自检：条目集合、体积、侧车哈希。"""
    if os.path.getsize(path) > MAX_ARCHIVE_BYTES:
        raise PackError('压缩包体积超限')
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        want = [pid + '/' + rel for rel, _ in members]
        if sorted(names) != sorted(want):
            raise PackError('条目集合与源目录不一致')
        bad = z.testzip()
        if bad:
            raise PackError('压缩包损坏：%s' % bad)
        total = sum(i.file_size for i in z.infolist())
        if total > MAX_TOTAL_BYTES:
            raise PackError('解压总量超限')
    return True


def pack(plugin_dir, out_dir=OUT_DIR):
    raw, pid, version, entry = read_manifest(plugin_dir)
    members = collect(plugin_dir)
    if entry not in [rel for rel, _ in members]:
        raise PackError('入口文件不在包里：%s' % entry)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, '%s-v%s.zip' % (pid, version))
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel, full in members:
            z.write(full, pid + '/' + rel)
    verify(out, pid, members)
    sha = sha256_file(out)
    with open(out + '.sha256', 'w', encoding='ascii') as f:
        f.write(sha + '  ' + os.path.basename(out) + '\n')
    print('OK  %-34s %2d 文件  %7d B  sha256=%s'
          % (os.path.basename(out), len(members), os.path.getsize(out), sha[:16]))
    return out


def pack_all():
    made = []
    sync_shared()
    for pid in sorted(os.listdir(SRC_DIR)):
        d = os.path.join(SRC_DIR, pid)
        if os.path.isfile(os.path.join(d, MANIFEST)):
            made.append(pack(d))
    print('共 %d 个插件包 -> %s' % (len(made), OUT_DIR))
    return made


def check(path):
    path = os.path.abspath(path)
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        roots = {n.split('/', 1)[0] for n in names}
        if len(roots) != 1:
            raise PackError('压缩包根下必须只有一个插件目录，实际 %r' % sorted(roots))
        pid = roots.pop()
        manifest = json.loads(z.read(pid + '/' + MANIFEST).decode('utf-8-sig'))
        total = sum(i.file_size for i in z.infolist())
        for i in z.infolist():
            check_ext(i.filename)
            if i.file_size > MAX_MEMBER_BYTES:
                raise PackError('单文件超限：%s' % i.filename)
        if len(names) > MAX_FILES or total > MAX_TOTAL_BYTES:
            raise PackError('文件数 / 总量超限')
        side = path + '.sha256'
        note = '无侧车'
        if os.path.isfile(side):
            want = open(side, encoding='ascii').read().split()[0]
            got = sha256_file(path)
            note = '侧车一致' if want == got else '侧车不一致！'
            if want != got:
                raise PackError('侧车哈希与包不一致')
    print('OK  %s  id=%s v%s  %d 文件 %d B   %s'
          % (os.path.basename(path), manifest.get('id'),
             manifest.get('version'), len(names), total, note))
    return True


def main(argv):
    if len(argv) >= 2 and argv[0] == '--check':
        for p in argv[1:]:
            check(p)
        return 0
    if argv:
        sync_shared()
        for p in argv:
            pack(os.path.abspath(p))
        return 0
    pack_all()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except PackError as e:
        print('打包失败：%s' % e)
        sys.exit(1)
