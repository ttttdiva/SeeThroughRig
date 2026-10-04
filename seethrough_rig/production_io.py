"""Project-safe IO, crash-releasing locks and cancellable child processes."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from .package_validate import MAX_FILE, ValidationError, json_bytes

class ProductionError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class Cancelled(ProductionError):
    def __init__(self):
        super().__init__('cancelled', 'Production cancelled; durable inputs retained')

def no_links(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for p in [path, *path.parents]:
        if p.exists() or p.is_symlink():
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400:
                raise ProductionError('unsafe_path', 'Symlink/reparse path prohibited: ' + str(p))
    return path

def inside(root: Path, path: Path) -> Path:
    root = no_links(root).resolve()
    path = no_links(path).resolve()
    if path != root and root not in path.parents:
        raise ProductionError('unsafe_path', 'Path escapes project scope')
    return path

def read_file(path: Path, limit=MAX_FILE) -> bytes:
    path = no_links(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ProductionError('invalid_file', 'Not a bounded regular file: ' + str(path))
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ProductionError('invalid_file', 'File changed beyond size limit')
    return raw

def read_json(path):
    return json_bytes(read_file(path))

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def atomic_bytes(path: Path, raw: bytes, exclusive=False):
    path = no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / ('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        if exclusive:
            # Hard link atomically refuses overwrite; no symlink/external references.
            os.link(temp, path)
        else:
            os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def atomic_json(path, obj, exclusive=False):
    atomic_bytes(path, (json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8'), exclusive)

@contextlib.contextmanager
def lock(path: Path):
    path = no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as fh:
        if path.stat().st_size == 0:
            fh.write(b'0')
            fh.flush()
        fh.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ProductionError('busy', 'Project or GPU operation already running') from exc
        try:
            yield
        finally:
            fh.seek(0)
            if os.name == 'nt':
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh, fcntl.LOCK_UN)

def child_env(extra=None):
    # Host secrets, PYTHONPATH, tokens, proxy credentials and plugin env are omitted.
    allowed = ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'LANG', 'LC_ALL',
               'CUDA_PATH', 'CUDA_VISIBLE_DEVICES')
    env = {k: os.environ[k] for k in allowed if k in os.environ}
    if os.name == 'nt':
        system = os.environ.get('SYSTEMROOT', r'C:\Windows')
        env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + str(Path(system) / 'System32')
    else:
        env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + '/usr/local/bin:/usr/bin:/bin'
    env.update({'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'PIP_NO_INDEX': '1'})
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env

def checkpoint(project):
    if no_links(project / 'cancel.request').exists():
        raise Cancelled()

def stop_child(proc):
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        killer = Path(os.environ.get('SYSTEMROOT', r'C:\Windows')) / 'System32' / 'taskkill.exe'
        subprocess.run([str(killer), '/PID', str(proc.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, env=child_env())
    else:
        import signal
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            proc.kill()
        else:
            os.killpg(proc.pid, 9)
        proc.wait(timeout=10)

def run_child(command, cwd, log, project=None, env=None):
    cwd, log = no_links(cwd), no_links(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    if project:
        checkpoint(project)
    with log.open('ab') as fh:
        kwargs = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
        proc = subprocess.Popen([str(x) for x in command], cwd=cwd, env=child_env(env), stdin=subprocess.DEVNULL,
                                stdout=fh, stderr=subprocess.STDOUT, **kwargs)
        try:
            while proc.poll() is None:
                if project:
                    checkpoint(project)
                time.sleep(0.15)
            if project:
                checkpoint(project)
            if proc.returncode:
                # Do not echo process log: may include private input data.
                raise ProductionError('stage_failed', f'Child process exited {proc.returncode}; see project log {log.name}')
        finally:
            if proc.poll() is None:
                stop_child(proc)

def git_output(root, args):
    try:
        result = subprocess.run(['git', '-C', str(root), *args], check=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=30, env=child_env())
        return result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProductionError('provenance_unverified', 'Git commit cannot be verified locally') from exc
