"""Execute one synthetic current-result plot using the published profile."""
from . import core,rendering,contracts

def main():
    from . import audit
    audit.deps()
    from validate_content_contracts import load_jsonschema
    load_jsonschema()  # Pin the contract dependency before plotting imports other packages.
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    from PIL import Image
    profile=rendering.profile();base=core.CONTENT_DIR/'reports/trae/qa_rendering';base.mkdir(parents=True,exist_ok=True)
    input_root=core.CONTENT_DIR/'reports/trae/qa_result_flow'
    dataset=input_root/'synthetic.csv';card=core.load_json(input_root/'result_card.json')
    if card['status']!='succeeded' or core.sha256_of(dataset)!=card['input_refs'][0]['sha256']:raise ValueError('verified current calculation input missing or changed')
    rows=np.genfromtxt(str(dataset),delimiter=',',skip_header=1)
    matplotlib.rcParams.update({'font.family':[profile['fonts']['latin'],profile['fonts']['chinese']],'pdf.fonttype':42,'svg.fonttype':'path'})
    fig,ax=plt.subplots(figsize=(profile['canvas']['single_column_width_mm']/25.4,profile['canvas']['default_height_mm']/25.4))
    ax.plot(rows[:,0],rows[:,1],color=profile['series']['palette'][0],marker='o',markersize=profile['series']['marker_size_points'],linewidth=profile['series']['line_width_points'],label='Synthetic y = 2x + 1')
    ax.set_xlabel('x (unit)',fontname=profile['fonts']['latin'],fontsize=profile['fonts']['axis_points']);ax.set_ylabel('y (unit)',fontname=profile['fonts']['latin'],fontsize=profile['fonts']['axis_points'])
    ax.legend(prop={'family':profile['fonts']['latin'],'size':profile['fonts']['legend_points']});ax.grid(alpha=.15);fig.tight_layout()
    outputs=[]
    for suffix in profile['export']['formats']:
        path=base/('synthetic-current-result.'+suffix)
        fig.savefig(str(path),dpi=profile['canvas']['raster_dpi'],facecolor='white')
        outputs.append({'file':str(path),'sha256':core.sha256_of(path),'bytes':path.stat().st_size})
    plt.close(fig)
    with Image.open(str(base/'synthetic-current-result.png')) as image:
        size=list(image.size);dpi=list(image.info['dpi'])
    passed=all(o['bytes']>1000 for o in outputs) and min(dpi)>299 and size[0]>=1800
    image=base/'synthetic-current-result.png';image_sha=core.sha256_of(image)
    nodes=core.load_json(input_root/'document_nodes.json')+[{'node_id':'qa-current-figure','task_id':card['task_id'],'order':2,'node_type':'figure','content':{'asset_ref':{'asset_id':'asset-'+image_sha,'version':1,'sha256':image_sha},'caption':'合成联调计算数据图，x/y 单位见当前结果，样式 cumcm-figure-style v1。','units':['x_unit','y_unit']},'result_refs':[card['result_card_id']],'release_id':'cumcm-2010-2025-codex-v2','version':1}]
    contracts.validate_document(nodes,[card]);core.write_json(base/'document_nodes.json',nodes)
    core.write_json(core.CONTENT_DIR/'reports/trae/cumcm_codex_rendering.json',{'passed':passed,'profile_id':profile['profile_id'],'version':1,'canonical_sha256':profile['canonical_sha256'],'dataset_sha256':core.sha256_of(dataset),'computation_run_id':card['run']['run_id'],'document_nodes_validated':True,'outputs':outputs,'png_pixels':size,'png_dpi':dpi,'scope':'real plot/export from verified synthetic computation input and result/document contracts; not historical graph reconstruction or a client solver'})
    if not passed:raise ValueError('rendering profile export check failed')
    print('Actual 300 dpi PNG/PDF/SVG rendering profile exports passed')

if __name__=='__main__':main()
