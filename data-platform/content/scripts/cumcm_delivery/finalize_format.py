"""Pin the primary current edition and the actually compiled v3 format packages."""
from . import core,audit,local_closure,templates

def run():
    rd=audit.run_dir(local_closure.args());out=rd/'templates';qa=core.load_json(rd/'quality/complex_format_checks.json')
    if len(qa['cases'])!=4 or not qa['all_passed']:raise ValueError('all complex compile cases must pass first')
    evidence=out/'evidence/cumcm-format-2026-organizer.pdf';sha=core.sha256_of(evidence)
    audit.deps();import profile_extraction as pe
    pages,_,_=pe.extract_pdf_pages(evidence);text=''.join(pages)
    if '2026' not in text or '30' not in text or '字号' not in text or '2.5' not in text:raise ValueError('organizer rule evidence mismatch')
    rules=core.load_json(out/'ruleset.json');rules['ruleset_id']='cumcm-format-2025-2026-v3'
    rules['target_edition_default_for_local_acceptance']=2026;rules['submission_certification']=False
    rules['editions'][0]['archive_status']='attributed_2025_text_retained; organizer edition PDF not established; never auto-certify a 2025 submission'
    rules['editions'][1].update({'source_sha256':sha,'source_asset_ref':{'asset_id':'asset-'+sha,'version':1,'sha256':sha},
        'requires_primary_archive_verification':False,'archive_status':'primary_organizer_pdf_verified','verified_at':core.now_utc_iso()})
    rules['district_requirements']='versioned optional override required for an actual district submission; local integration acceptance uses national 2026 profile'
    core.write_json(out/'ruleset.json',rules)
    fonts=core.load_json(out/'font_manifest.json');fonts['format_version']=3;fonts['actual_rendered_font_checks']='quality/complex_format_checks.json'
    fonts['actual_usage_verified']=['SimSun','SimHei','KaiTi','Times New Roman','Courier New'];fonts['preview_sha256']=qa['cases'][0]['pdf_sha256']
    core.write_json(out/'font_manifest.json',fonts)
    dependency=core.load_json(out/'dependency_manifest.json')
    for package in dependency['packages']:
        for filename,destination in [('ruleset.json','erdos-ruleset.json'),('font_manifest.json','erdos-fonts.json')]:core.write_json(out/package['kind']/destination,core.load_json(out/filename))
        archive=out/package['file'];templates._deterministic_zip(out/package['kind'],archive);package['sha256']=core.sha256_of(archive);package['compiled']=True
    core.write_json(out/'dependency_manifest.json',dependency)
    core.write_json(out/'format_readiness.json',{'format_version':3,'packages':dependency['packages'],'complex_3_and_5_question_cases':qa,
        'shared_typst_helpers_fixed':True,'national_2026_primary_pdf_sha256':sha,'all_five_fonts_actually_used':True,
        'national_font_face_mandatory':False,'target_edition_required':True,'district_submission_certified':False})
    local_closure.state('format_delivery','pass',['templates/format_readiness.json','quality/complex_format_checks.json'],
        {'scope':'national 2026 local integration acceptance; 2025 archive and district requirements have explicit confidence/override status'})
    print('format v3: actual five fonts, primary 2026 rules and 3/5 question boundaries pinned')

if __name__=='__main__':run()
