"""国赛 2010—2025 数据交付统一 CLI。

工作目录 D:/Erdos/data-platform/content。命令：
  preflight inventory expand parse relate recipes templates pack import
  retrieve fetch check evaluate update build

详见 handoff/cumcm/README.md 与 reports/trae/cumcm_delivery_final.md。
"""
import argparse
import sys
import os
from pathlib import Path
if hasattr(sys.stdout,'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

sys.path.insert(0, str(Path(os.path.abspath(__file__)).parent))

from cumcm_delivery import core, preflight  # noqa: E402

# 延迟导入其余模块（避免未实现阶段阻塞 preflight 等基础命令）
_lazy = {}


_MOD_NAME = {"import": "importdb"}


def _mod(name):
    if name not in _lazy:
        import importlib
        _lazy[name] = importlib.import_module("cumcm_delivery." + _MOD_NAME.get(name, name))
    return _lazy[name]


def add_common(p):
    p.add_argument("--run-id", default="cumcm-2010-2025-codex-r003")
    p.add_argument("--release-id", default="cumcm-2010-2025-codex-v3")
    p.add_argument("--scope-config", default="config/cumcm_delivery/scope.json")
    p.add_argument("--data-root", default="D:/Erdos_data")


def build_parser():
    p = argparse.ArgumentParser(prog="cumcm_delivery", description="国赛 2010—2025 数据交付")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("preflight"); add_common(sp)
    sp.add_argument("--scope", default="config/cumcm_delivery/scope.json")

    sp = sub.add_parser("inventory"); add_common(sp)
    sp = sub.add_parser("expand"); add_common(sp)
    sp = sub.add_parser("parse"); add_common(sp); sp.add_argument("--max-documents", type=int, default=0)
    sp = sub.add_parser("relate"); add_common(sp)
    sp = sub.add_parser("recipes"); add_common(sp)
    sp = sub.add_parser("templates"); add_common(sp)
    sp = sub.add_parser("pack"); add_common(sp)
    sp = sub.add_parser("import"); add_common(sp)
    sp.add_argument("--target", default="local-audit")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--apply", action="store_true")
    sp.add_argument("--activate", action="store_true")
    sp = sub.add_parser("retrieve"); add_common(sp)
    sp.add_argument("--request")
    sp.add_argument("--target", default="local-audit")
    sp.add_argument("--internal-audit", action="store_true")
    sp.add_argument("--output")
    sp = sub.add_parser("fetch"); add_common(sp)
    sp.add_argument("asset_id"); sp.add_argument("--version", type=int); sp.add_argument("--output")
    sp.add_argument("--internal-audit", action="store_true")
    sp = sub.add_parser("check"); add_common(sp)
    sp = sub.add_parser("evaluate"); add_common(sp); sp.add_argument("--target", default="local-audit")
    sp = sub.add_parser("update"); add_common(sp); sp.add_argument("--previous")
    sp = sub.add_parser("build"); add_common(sp)
    sp = sub.add_parser("activate"); add_common(sp)
    sp = sub.add_parser("rollback"); add_common(sp)
    sp = sub.add_parser("audit-build"); add_common(sp)
    sp.add_argument("--phase", choices=("expand", "parse", "catalog", "pack", "import", "evaluate", "templates"), required=True)
    sp.add_argument("--ocr", action="store_true")
    sp.add_argument("--max-documents", type=int, default=0)
    return p


def p_inv(p):
    p.add_argument("--data-root", default="D:/Erdos_data")


def main():
    args = build_parser().parse_args()
    cmd = args.cmd
    for value in (args.run_id, args.release_id):
        if not __import__('re').fullmatch(r'cumcm-2010-2025-[a-z0-9-]+', value):
            raise ValueError('invalid scoped identifier')
    if args.run_id == 'cumcm-2010-2025-r001' or args.release_id == 'cumcm-2010-2025-v1':
        if cmd not in ('check', 'retrieve', 'fetch'):
            raise ValueError('prior Trae delivery is protected; choose a new version')
    if cmd == "audit-build":
        return _mod("audit").main(args)
    if cmd != 'inventory':
        return _mod('audit').dispatch(args)
    if cmd == "preflight":
        return preflight.run_preflight(args)

    mod = _mod(cmd)
    return mod.main(args)


if __name__ == "__main__":
    sys.exit(main())
