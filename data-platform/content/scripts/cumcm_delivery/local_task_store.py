"""Owner-scoped reference task access. The gateway supplies the trusted owner."""
from . import core,services

async def fetch_asset(trusted_owner_id,task_id,asset_id,version):
    if not trusted_owner_id or type(version)!=int or version<1:raise ValueError('trusted owner and exact version required')
    conn=await services.pg()
    try:
        row=await conn.fetchrow('SELECT * FROM content_tasks_local.assets WHERE owner_id=$1 AND task_id=$2 AND asset_id=$3 AND version=$4',trusted_owner_id,task_id,asset_id,version)
        if not row:raise LookupError('asset not found in trusted owner/task scope')
        payload=core.json_loads(row['payload']);raw=services.get_verified(row['sha256'])
        if len(raw)!=payload['size'] or core.sha256_bytes(raw)!=row['sha256']:raise ValueError('task asset content drift')
        return {'bytes':raw,'sha256':row['sha256'],'asset_id':asset_id,'version':version,'filename':payload['filename']}
    finally:await conn.close()
