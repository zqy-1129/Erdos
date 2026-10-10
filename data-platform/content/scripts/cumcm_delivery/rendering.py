"""Immutable self-authored rendering advice, independently versioned from historical content."""
import json
from . import core

ROOT=core.CONTENT_DIR/'config/cumcm_delivery/rendering/cumcm-figure-style-v1'

def initialize():
    if (ROOT/'SEALED.json').exists():return profile()
    body={'profile_id':'cumcm-figure-style','version':1,'kind':'self_authored_rendering_suggestion','official_requirement':False,
      'fonts':{'chinese':'SimSun','latin':'Times New Roman','axis_points':9,'legend_points':8,'caption':'supplied by document_node outside plot'},
      'canvas':{'single_column_width_mm':160,'default_height_mm':95,'raster_dpi':300,'background':'white','tight_layout':True},
      'series':{'palette':['#0072B2','#D55E00','#009E73','#CC79A7','#E69F00','#56B4E9'],'line_width_points':1.3,'marker_size_points':4,
                'markers':['o','s','^','D'],'line_styles':['solid','dashed','dashdot'],'color_is_not_the_only_encoding':True},
      'axes':{'unit_required':True,'unit_source':'current dataset/result_card; dimensionless/category must be explicit','grid':'light major grid only','scientific_notation':'declare multiplier and unit'},
      'legend':{'text':'series meaning and unit','placement':'avoid covering observations','consistent_series_order':True},
      'export':{'formats':['png','pdf','svg'],'verify_font_embedding_or_outlines':True,'verify_no_clipping':True,'plot_data_sha_required':True,'computation_run_id_required':True},
      'caption':{'placement':'below figure in assembled paper','fields':['figure number','meaningful title','units','current computation/source identity']},
      'missing_inputs':'skip plot; request real current task computation; never manufacture historical values',
      'client_pinning':'save profile_id, version and canonical SHA with current document; do not silently change rendering advice mid-task'}
    core.write_json(ROOT/'profile.json',body)
    canonical=json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    core.write_json(ROOT/'SEALED.json',{'profile_file_sha256':core.sha256_of(ROOT/'profile.json'),'canonical_sha256':core.sha256_bytes(canonical),'sealed_at':core.now_utc_iso()})
    return profile()

def profile():
    body=core.load_json(ROOT/'profile.json');seal=core.load_json(ROOT/'SEALED.json')
    canonical=json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    if core.sha256_of(ROOT/'profile.json')!=seal['profile_file_sha256'] or core.sha256_bytes(canonical)!=seal['canonical_sha256']:
        raise ValueError('rendering profile checksum mismatch')
    if body['profile_id']!='cumcm-figure-style' or body['version']!=1:raise ValueError('unsupported rendering profile identity')
    return dict(body,canonical_sha256=seal['canonical_sha256'])

if __name__=='__main__':initialize();print('Immutable rendering profile v1 initialized; product advice, not official rules')
