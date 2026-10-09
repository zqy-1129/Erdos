"""New Windows format version; source helper imports work inside dynamic sections."""
import json,re,zipfile
from . import core,audit,local_closure,templates,complex_formats

HELPERS='''// Shared helpers: import from each generated section with #import "../helpers.typ": *
#let song(body) = text(font: ("SimSun", "Times New Roman"), body)
#let hei(body) = text(font: "SimHei", weight: "bold", body)
#let kai(body) = text(font: "KaiTi", body)
#let three-line-table(caption, columns, header, body) = {
  align(center)[#text(font: "SimHei", size: 10.5pt, caption)]
  table(columns: columns, stroke: none, inset: 5pt,
    table.hline(y: 0, stroke: 0.8pt),
    table.header(..header.map(c => strong(c)), table.hline(y: 1, stroke: 0.5pt)),
    ..body, table.hline(stroke: 0.8pt))
}
'''

def run():
    rd=audit.run_dir(local_closure.args());out=rd/'templates';packages=[]
    for fmt in ('typst','latex'):
        base=out/fmt;complex_formats.unpack(out/('cumcm-'+fmt+'-v2.zip'),base)
        entry=base/('main.typ' if fmt=='typst' else 'main.tex');text=entry.read_text(encoding='utf-8')
        if fmt=='typst':
            # Installed Windows fonts are explicit. No missing macOS fallbacks or silent substitutions.
            text=re.sub(r'#let body-font = .*', '#let body-font = ("Times New Roman", "SimSun")',text)
            text=re.sub(r'#let song-font = .*', '#let song-font = ("SimSun", "Times New Roman")',text)
            text=re.sub(r'#let kai-font = .*', '#let kai-font = ("KaiTi", "SimSun")',text)
            text=text.replace('font: ("Courier New", "Menlo", "SimSun", "Songti SC")','font: ("Courier New", "SimSun")')
            core.write_bytes(base/'helpers.typ',HELPERS.encode('utf-8'))
            for section in (base/'sections').glob('*.typ'):
                value=section.read_text(encoding='utf-8')
                if section.name!='A_code.typ':value='#import "../helpers.typ": *\n'+value
                core.write_bytes(section,value.encode('utf-8'))
        else:
            text=text.replace('自动设置（Songti SC / Heiti SC / Kaiti SC）','自动设置（SimSun / SimHei / KaiTi）')
            text=text.replace('% 摘要页（不编号）','% 摘要第一页从 1 连续编号').replace('% 封面（不编号）','% 电子论文第一页：标题与摘要（不含身份信息）')
            text=text.replace('\\usepackage{booktabs}','\\usepackage{booktabs}\n\\usepackage{longtable}')
        core.write_bytes(entry,text.encode('utf-8'))
        for name in ('ruleset','font_manifest','dynamic_section_contract'):core.write_json(base/('erdos-'+{'ruleset':'ruleset','font_manifest':'fonts','dynamic_section_contract':'dynamic-sections'}[name]+'.json'),core.load_json(out/(name+'.json')))
        archive=out/('cumcm-'+fmt+'-v3.zip');templates._deterministic_zip(base,archive)
        packages.append({'kind':fmt,'version':3,'file':archive.name,'sha256':core.sha256_of(archive),'compiled':False})
    dependency=core.load_json(out/'dependency_manifest.json');dependency['packages']=packages;dependency['shared_helper_import']='each generated Typst section imports ../helpers.typ; includes do not inherit main-file bindings'
    core.write_json(out/'dependency_manifest.json',dependency)
    contract=core.load_json(out/'dynamic_section_contract.json');contract['typst_section_preamble']='#import "../helpers.typ": *';contract['long_tables']='use native Typst table with repeated table.header, or LaTeX longtable; never force multipage data into nonbreakable block'
    core.write_json(out/'dynamic_section_contract.json',contract)
    # Refresh packaged contracts after updating the entry requirement.
    for package in packages:
        core.write_json(out/package['kind']/'erdos-dynamic-sections.json',contract)
        templates._deterministic_zip(out/package['kind'],out/package['file']);package['sha256']=core.sha256_of(out/package['file'])
    core.write_json(out/'dependency_manifest.json',dependency);print('new format v3 created with explicit shared-helper imports')

if __name__=='__main__':run()
