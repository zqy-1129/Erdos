"""共享工具：稳定 hash、安全路径、JSON 读写、运行标识、配置加载。"""
import hashlib
import json
import os
import sys
import stat
import re
from datetime import datetime
from pathlib import Path

CODE_CONTENT_DIR = Path(os.path.abspath(__file__)).parent.parent.parent
CONTENT_DIR = Path(os.path.abspath(os.environ.get("ERDOS_DATA_CONTENT_DIR", str(CODE_CONTENT_DIR))))
PY = os.environ.get("ERDOS_PYTHON", sys.executable)
CHUNK = 1024 * 1024
FILE_ATTR_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def sha256_bytes(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def now_utc_iso():
    return datetime.utcnow().isoformat() + "Z"


def no_links(path):
    path = Path(os.path.abspath(str(path)))
    for component in [*reversed(path.parents), path]:
        try:
            st = os.lstat(str(component))
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode) or (getattr(st, "st_file_attributes", 0) & FILE_ATTR_REPARSE):
            raise ValueError("linked/reparse path rejected: " + str(component))
    return path


def safe_relpath(root, rel):
    """拒绝目录穿越/绝对路径/反斜杠/盘符/link，返回 root 内绝对路径。"""
    if not isinstance(rel, str) or not rel or ":" in rel or "\\" in rel or any(ord(c) < 32 for c in rel):
        raise ValueError("invalid relative path: " + repr(rel))
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("path traversal rejected: " + rel)
    for p in parts:
        if p.endswith((" ", ".")) or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", p, re.I) or any(c in p for c in '<>"|?*'):
            raise ValueError("unsafe Windows component: " + p)
    base = no_links(root)
    target = no_links(base.joinpath(*parts))
    if os.path.commonpath([str(base), str(target)]) != str(base):
        raise ValueError("path escapes root: " + rel)
    return target


def _pairs(items):
    out = {}
    for key, value in items:
        if key in out:
            raise ValueError("duplicate JSON key: " + key)
        out[key] = value
    return out

def _constant(value):
    raise ValueError("nonfinite JSON: " + value)

def json_loads(text):
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)

def load_json(path):
    return json_loads(Path(path).read_text(encoding="utf-8-sig"))

def output_path(path):
    p = no_links(path)
    base = str(CONTENT_DIR).lower()
    if os.path.commonpath([str(p).lower(), base]) != base:
        raise ValueError("output must stay in content workspace")
    rel = p.relative_to(CONTENT_DIR).as_posix()
    safe_relpath(CONTENT_DIR,rel)
    protected = ("out/pack/", "normalized/stage03/", "normalized/stage04/", "normalized/stage05_codex_v3/", "normalized/competitions_codex_v6/", "out/releases/content-candidate-codex-v3/", "normalized/cumcm_delivery/cumcm-2010-2025-r001/", "out/cumcm_delivery/releases/cumcm-2010-2025-v1/")
    if any(rel.startswith(x) for x in protected):
        raise ValueError("protected prior snapshot: " + rel)
    for parent in [p.parent, *p.parents]:
        if (parent / "SEALED.json").is_file():
            raise ValueError("sealed snapshot is immutable")
        if parent == CONTENT_DIR:
            break
    return p

def write_bytes(path, data, immutable=False):
    p = output_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and p.read_bytes() == data:
        return sha256_bytes(data)
    if immutable and p.exists():
        raise ValueError("different bytes at immutable output: " + str(p))
    tmp = p.with_name(p.name + ".tmp")
    no_links(tmp)
    with open(str(tmp), "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(p))
    return sha256_bytes(data)


def write_json(path, obj):
    data = json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    return write_bytes(path, data.encode("utf-8"))


def load_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json_loads(line))
    return out


def write_jsonl(path, rows):
    write_bytes(path, ("".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in rows)).encode("utf-8"))
    return Path(path)


def load_scope(scope_path=None):
    cfg = CONTENT_DIR / "config" / "cumcm_delivery" / "scope.json"
    scope=load_json(scope_path or cfg)
    if (scope.get('competition_id'),scope.get('historical_year_min'),scope.get('historical_year_max')) != ('cumcm',2010,2025):
        raise ValueError('this delivery is restricted to CUMCM 2010 through 2025')
    expected={'problems':'problems/12_CUMCM国赛','papers':'papers_pending_auth/12_CUMCM国赛','templates':'templates/12_CUMCM国赛'}
    if scope.get('sources')!=expected:
        raise ValueError('source scope cannot silently point to other competitions')
    return scope


def load_env(kv):
    """从 key=value 行读取本地不提交的 .env（无密钥进入报告）。"""
    out = {}
    for line in kv.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def try_load_env():
    p = CONTENT_DIR / "config" / "cumcm_delivery" / ".env"
    if p.is_file():
        return load_env(p.read_text(encoding="utf-8"))
    return {}
