"""快照/许可签名金样例守护：contracts/snapshot-vectors.json ↔ 服务端规范化实现 ↔ 文档示例。

为什么补这一条：签名字节序是跨端验签的生死线，而它此前只是"文档里一段示例 + 两端各自手抄的断言"。
服务端有两份独立实现（entitlement 快照、points 阶段许可），客户端还有第三份 TS 实现——任何一边
改了 sort_keys / separators / ensure_ascii / 整数写法，签名就整体失效，现网表现为
"离线宽限全部验签失败"，属于最难定位的一类故障。金样例把「载荷 → 规范化字节 → 签名」
三元组钉成一份共享文件，两侧各读同一份、各自断言。

同一份文件的客户端守护见 client/tests/contract-snapshot-vectors.test.ts。
"""

import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import load_der_public_key

from app.domain.entitlement.service import _canonical as snapshot_canonical
from app.domain.points.service import PointsService

_ROOT = Path(__file__).resolve().parents[3]
CONTRACT = _ROOT / "contracts" / "snapshot-vectors.json"
DOC = _ROOT / "contracts" / "snapshot.md"


def _spec() -> dict[str, Any]:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _public_key() -> Any:
    return load_der_public_key(bytes.fromhex(_spec()["key"]["public_key_spki_der_hex"]))


def _verifies(vector: dict[str, Any]) -> bool:
    try:
        _public_key().verify(
            bytes.fromhex(vector["signature"]), vector["canonical"].encode("ascii")
        )
        return True
    except InvalidSignature:
        return False


def test_canonical_bytes_match_every_vector() -> None:
    """两侧字节序的唯一判据：规范化结果必须与金样例逐字相等。"""
    for vector in _spec()["vectors"]:
        payload = vector["payload"]
        assert snapshot_canonical(payload).decode("ascii") == vector["canonical"], vector["id"]


def test_two_server_implementations_agree() -> None:
    """快照与许可各有一份 _canonical，任何一份跑偏都会让对应链路静默失效。"""
    for vector in _spec()["vectors"]:
        payload = vector["payload"]
        assert snapshot_canonical(payload) == PointsService._canonical(payload), vector["id"]


def test_signatures_verify_and_tampering_does_not() -> None:
    """正向量必须验签通过，篡改向量必须失败——否则金样例本身是坏的。"""
    for vector in _spec()["vectors"]:
        expect_valid = vector.get("expect_valid", True)
        assert _verifies(vector) is expect_valid, vector["id"]


def test_vectors_actually_exercise_key_sorting() -> None:
    """金样例里必须至少有一个载荷是乱序书写的，否则"键排序"这条规则没被测到。"""
    unsorted = [
        v["id"]
        for v in _spec()["vectors"]
        if list(v["payload"].keys()) != sorted(v["payload"].keys())
    ]
    assert unsorted, "所有向量都已按键序书写，键排序规则失去守护"


def test_ensure_ascii_is_not_turned_off() -> None:
    """非 ASCII 必须转 \\uXXXX（客户端按此实现）：改成 ensure_ascii=False 即跨端验签全红。"""
    assert snapshot_canonical({"k": "中文"}) == b'{"k":"\\u4e2d\\u6587"}'
    assert snapshot_canonical({"k": "\U0001F600"}) == b'{"k":"\\ud83d\\ude00"}'


def test_doc_example_matches_the_machine_vector() -> None:
    """文档第 2 节的示例串必须就是金样例，防止"只改文档不改契约"。"""
    block = DOC.read_text(encoding="utf-8").split("```json")[1].split("```")[0].strip()
    doc_example = json.loads(block)
    vector = next(v for v in _spec()["vectors"] if v["id"] == "snapshot-doc-example")
    assert block == vector["canonical"]
    assert snapshot_canonical(doc_example).decode("ascii") == vector["canonical"]
