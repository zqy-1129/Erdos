"""Exact residual-page remediation queue from the sealed release, not a readability claim."""
from . import core

def main():
    release='cumcm-2010-2025-codex-v2'
    root=core.CONTENT_DIR/'out/cumcm_delivery/releases'/release
    report=core.load_json(root/'metadata/quality/parse_report.json');rows=[]
    for document in report['documents']:
        if not document['needs_review_pages']:continue
        folder=root/'metadata/parsed'/document['sha256']
        for path in sorted(folder.glob('page-*.json')):
            page=core.load_json(path)
            if page['quality']=='readable':continue
            rows.append({'release_id':release,'source_ref':{'asset_id':'asset-'+document['sha256'],'version':1,'sha256':document['sha256'],'page':page['page']},
              'year':document['year'],'root_kind':document['root_kind'],'method':page['method'],'recognized_text':page['text'],
              'block_count':len(page['blocks']),'quality':'needs_review','reviewer_type':None,'status':'pending',
              'remediation':'render original page, determine blank/image-only/formula-only or OCR failure; retain source evidence and use specialized recognition if needed; do not mark readable without evidence'})
    expected=sum(d['needs_review_pages'] for d in report['documents'])
    if len(rows)!=expected:raise ValueError('residual page queue does not match complete parse report')
    core.write_jsonl(core.CONTENT_DIR/'reports/trae/cumcm_codex_sparse_pages.jsonl',rows)
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_codex_sparse_summary.json',{'release_id':release,'actual_sparse_pages':len(rows),'all_have_source_sha_page_and_remediation':True,'pages_independently_reviewed':0,'uses_sealed_package_only':True})
    print('{} exact residual source-page review items prepared'.format(len(rows)))

if __name__=='__main__':main()
