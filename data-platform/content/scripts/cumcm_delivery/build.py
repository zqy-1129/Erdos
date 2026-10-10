"""build/update：编排数据生产步骤（复用各阶段模块，不复制逻辑）。"""
import argparse

from . import inventory, expand, parse, relate, recipes, templates, pack


def _make_args(args):
    return argparse.Namespace(
        run_id=args.run_id, release_id=args.release_id,
        scope_config=args.scope_config, data_root=args.data_root,
        max_documents=0, target="local-test",
    )


def cmd_build(args):
    a = _make_args(args)
    inventory.main(a)
    expand.main(a)
    parse.main(a)
    relate.main(a)
    recipes.main(a)
    templates.main(a)
    pack.main(a)
    print("build 完成：run_id={} release_id={}".format(args.run_id, args.release_id))
    return 0


def cmd_update(args):
    # 增量：只重跑会受输入变化影响的步骤（inventory/expand/parse/relate/pack）
    a = _make_args(args)
    inventory.main(a)
    expand.main(a)
    parse.main(a)
    relate.main(a)
    pack.main(a)
    print("update 完成（新资料已接入）")
    return 0


main = cmd_build
