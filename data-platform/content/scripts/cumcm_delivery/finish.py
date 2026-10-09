"""Finish only after the entire real OCR job has completed; failures stop publication."""
import argparse,time
from . import core,audit,qa,patterns,review_queue

def normalize_sources(args):
    return audit.normalize_sources(args)

def main():
    args=argparse.Namespace(run_id='cumcm-2010-2025-codex-r002',release_id='cumcm-2010-2025-codex-v2',data_root='D:/Erdos_data',scope_config='config/cumcm_delivery/scope.json',target='local-audit',apply=True,activate=True)
    rd=audit.run_dir(args)
    while True:
        report=core.load_json(rd/'quality/parse_report.json')
        if report['complete']:
            if report['failures'] or report['processed']!=385:raise ValueError('full parse has failures or count drift')
            break
        print('waiting for entire OCR: {}/{}'.format(report['processed'],report['input_pdf_count']),flush=True)
        time.sleep(60)
    audit.normalize_sources(args)
    for step in (audit.catalog,):step(args)
    patterns.main();review_queue.main();qa.structure()
    core.write_json(rd/'quality/finish_checkpoint.json',{'full_parse_complete':True,'catalog_complete':True,'structural_qa_complete':True,'next':'pack + real object/database import + evaluation'})
    audit.prepare_embeddings(args);audit.pack(args);audit.import_release(args);audit.evaluate(args)
    core.write_json(rd/'quality/finish_applied.json',{'release_id':args.release_id,'actual_import_complete':True,'executed_at':core.now_utc_iso(),'independent_content_quality':'pending'})
    print('full corpus locally sealed, imported and evaluated',flush=True)

if __name__=='__main__':main()
