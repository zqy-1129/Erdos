"""阶段8：fetch 按资产 ID/版本取回真实字节并校验 SHA（本地 binding；对象存储待接）。"""
from . import core


def main(args):
    rel = core.CONTENT_DIR / "out" / "cumcm_delivery" / "releases" / args.release_id
    assets = core.load_json(rel / "entities" / "assets.json")
    bindings = core.load_json(rel / "assets" / "local_bindings.json")

    hit = [a for a in assets if a["asset_id"] == args.asset_id]
    if not hit:
        print("资产不存在: " + args.asset_id)
        return 2
    asset = hit[0]
    if args.version and asset["version"] != args.version:
        print("版本不符: 要求 {}，实为 {}".format(args.version, asset["version"]))
        return 2

    bind = bindings.get(args.asset_id)
    if not bind:
        print("无本地 binding（对象存储未接入）: " + args.asset_id)
        return 2
    src = core.no_links(core.CONTENT_DIR.parent.parent / bind["r"])
    data = src.read_bytes()
    actual = core.sha256_bytes(data)
    if actual != asset["sha256"]:
        print("校验失败：内容 SHA {} != 期望 {}".format(actual[:16], asset["sha256"][:16]))
        return 1

    if args.output:
        out = core.CONTENT_DIR / args.output
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    else:
        json_fetch(asset, len(data), "local_binding")
    return 0


def json_fetch(a, size, source):
    import json
    print(json.dumps({
        "asset_id": a["asset_id"], "version": a["version"], "role": a["role"],
        "sha256": a["sha256"], "size_bytes": size, "media_type": a["media_type"],
        "verified": True, "source": source, "object_key": a["object_key"],
    }, ensure_ascii=False, indent=2))
