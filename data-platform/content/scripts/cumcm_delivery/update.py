"""update：增量接入（复用 build.cmd_update）。"""
from . import build


def main(args):
    return build.cmd_update(args)
