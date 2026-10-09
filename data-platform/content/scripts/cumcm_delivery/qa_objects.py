"""Actual object corruption/conflict checks use an isolated synthetic digest and clean up it only."""
import uuid
from . import core,services

def main():
    body=('Erdos synthetic QA object '+str(uuid.uuid4())).encode()
    sha=core.sha256_bytes(body);key=services.object_key(sha);client,bucket=services.s3()
    path=services.RUNTIME/'local/qa-object.bin';core.write_bytes(path,body)
    try:
        client.get_object(Bucket=bucket,Key=key)
        raise ValueError('unexpected collision with existing QA digest; will not overwrite')
    except client.exceptions.NoSuchKey:pass
    checks=[]
    try:
        client.put_object(Bucket=bucket,Key=key,Body=b'deliberately corrupted synthetic QA bytes')
        try:services.get_verified(sha);rejected=False
        except ValueError:rejected=True
        checks.append({'name':'corrupt_real_object_rejected','passed':rejected})
        try:services.put_verified(path,sha);rejected=False
        except ValueError:rejected=True
        checks.append({'name':'immutable_object_conflict_rejected','passed':rejected})
    finally:client.delete_object(Bucket=bucket,Key=key)
    services.put_verified(path,sha)
    checks.append({'name':'real_upload_download_roundtrip','passed':services.get_verified(sha)==body})
    client.delete_object(Bucket=bucket,Key=key)
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_codex_object_negative.json',{'checks':checks,'fixture_sha256':sha,'fixture_removed':True,'production_objects_modified':False})
    if not all(c['passed'] for c in checks):raise ValueError('object negative QA failed')
    print('3 actual object-service integrity checks passed')

if __name__=='__main__':main()
