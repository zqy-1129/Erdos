"""Filled three/five-question boundary cases: formulas, multipage tables and code fonts."""
import ctypes,io,os,re,subprocess,zipfile
from . import core,audit,local_closure

def unpack(archive,destination):
    with zipfile.ZipFile(str(archive)) as z:
        names=set()
        for entry in z.infolist():
            if entry.is_dir():continue
            name=entry.filename
            if name.lower() in names:raise ValueError('template archive collision')
            names.add(name.lower());target=core.safe_relpath(destination,name)
            if entry.file_size>8*1024**2:raise ValueError('template member byte limit')
            core.write_bytes(target,z.read(entry))

def inspect(pdf,count):
    audit.deps();import profile_extraction as pe;pdfium=pe.load_pdfium();import pypdfium2.raw as raw
    doc=pdfium.PdfDocument(pdf.read_bytes());texts=[];fonts=set();dimensions=[];bounds=[]
    try:
        for page in doc:
            dimensions.append(list(page.get_size()));tp=page.get_textpage()
            try:
                texts.append(tp.get_text_range())
                for i in range(tp.count_chars()):
                    flags=ctypes.c_int();size=raw.FPDFText_GetFontInfo(tp,i,None,0,ctypes.byref(flags))
                    if size:
                        name=ctypes.create_string_buffer(size);raw.FPDFText_GetFontInfo(tp,i,name,size,ctypes.byref(flags));fonts.add(name.value.decode('utf-8','replace'))
                    try:
                        box=tp.get_charbox(i)
                        if 50<box[1]<page.get_size()[1]-40 and box[2]>box[0]:bounds.append(box)
                    except Exception:pass
            finally:tp.close();page.close()
    finally:doc.close()
    full='\n'.join(texts);compact=re.sub(r'\s','',full)
    markers=re.findall('ComplexQuestion\s*([1-9])',full)
    normalized={n.replace(' ','').lower() for n in fonts}
    font_checks={f:any(f.lower().replace(' ','') in n for n in normalized) for f in ('SimSun','SimHei','KaiTi','Times New Roman','Courier New')}
    checks={'question_count':sorted(map(int,markers))==list(range(1,count+1)),
        'a4':all(abs(w-595.28)<2 and abs(h-841.89)<2 for w,h in dimensions),'no_toc':'目录' not in full,
        'table_first_and_last_row':'Row1End' in compact and 'Row80End' in compact,
        'table_continues_across_pages':sum('MeasurementHeader' in re.sub(r'\s','',t) for t in texts)>=2,
        'code_appendix':'defcalibrate' in compact,'figure_caption':'CurrentCalibration' in compact,
        'all_five_declared_fonts_used':all(font_checks.values()),'file_under_20mb':pdf.stat().st_size<20*1024**2,
        'no_horizontal_text_overflow':not bounds or min(b[0] for b in bounds)>=68 and max(b[2] for b in bounds)<=527}
    return {'format_case':str(pdf),'pdf_sha256':core.sha256_of(pdf),'question_count':count,'pages':len(texts),'checks':checks,
        'fonts':sorted(fonts),'font_checks':font_checks,'passed':all(checks.values()),
        'scope':'filled synthetic boundary case; source and rendered-font checks; visual page review still recorded separately'}

def run():
    rd=audit.run_dir(local_closure.args());cases=rd/'quality/complex_formats';reports=[]
    current=core.CONTENT_DIR/'reports/trae/local_new_task_flow/calibration.png'
    if not current.exists():raise ValueError('real current-computation chart required')
    for fmt in ('typst','latex'):
        for count in (3,5):
            folder=cases/(fmt+'-'+str(count));archive=rd/'templates'/('cumcm-'+fmt+'-v3.zip')
            if not archive.exists():archive=rd/'templates'/('cumcm-'+fmt+'-v2.zip')
            unpack(archive,folder)
            core.write_bytes(folder/'current.png',current.read_bytes())
            if fmt=='typst':
                source='#import "../helpers.typ": *\n'+''.join('= ComplexQuestion'+str(q)+'\n本节是本次真实计算的排版边界用例。Current computed result.\n' for q in range(1,count+1))
                source+='\n#kai[楷体用于重点说明：结果必须来自当前任务。]\n$ J(beta) = sum_(i=1)^n (y_i - a x_i - b)^2 + lambda sum_(j=1)^m abs(theta_j) $\n'
                source+='\n#figure(image("../current.png", width: 90%), caption: [CurrentCalibration])\n\n#three-line-table([测量结果], (1fr,1fr,1fr), ([MeasurementHeader],[Value],[Unit]), (\n'
                for row in range(1,81):source+='[Row'+str(row)+'End],['+str(row*2)+'],[degC],\n'
                source+='))\n';core.write_bytes(folder/'sections/question_sections.typ',source.encode('utf-8'))
                core.write_bytes(folder/'sections/A_code.typ',b'```python\ndef calibrate(x, slope, intercept):\n    return slope * x + intercept\n```\n')
                pdf=folder/'preview.pdf';audit.command([audit.TYPST,'compile','--root',folder,folder/'main.typ',pdf])
            else:
                main=(folder/'main.tex').read_text(encoding='utf-8')
                if '\\usepackage{longtable}' not in main:main=main.replace('\\usepackage{booktabs}','\\usepackage{booktabs}\n\\usepackage{longtable}')
                core.write_bytes(folder/'main.tex',main.encode('utf-8'))
                source=''.join('\\section{ComplexQuestion'+str(q)+'}\n本节是本次真实计算的排版边界用例。Current computed result.\n' for q in range(1,count+1))
                source+='\n{\\kaishu 楷体用于重点说明：结果必须来自当前任务。}\n\\begin{align}J(\\beta)&=\\sum_{i=1}^n(y_i-ax_i-b)^2\\\\&\\quad+\\lambda\\sum_{j=1}^m|\\theta_j|\\end{align}\n'
                source+='\\begin{figure}[ht]\\centering\\includegraphics[width=.9\\linewidth]{current.png}\\caption{CurrentCalibration}\\end{figure}\n\\begin{longtable}{lll}\\toprule MeasurementHeader & Value & Unit\\\\\\midrule\\endfirsthead\\toprule MeasurementHeader & Value & Unit\\\\\\midrule\\endhead\n'
                for row in range(1,81):source+='Row'+str(row)+'End & '+str(row*2)+' & degC \\\\\n'
                source+='\\bottomrule\\end{longtable}\n';core.write_bytes(folder/'sections/question_sections.tex',source.encode('utf-8'))
                core.write_bytes(folder/'sections/A_code.tex',b'\\begin{lstlisting}[language=Python]\ndef calibrate(x, slope, intercept):\n    return slope * x + intercept\n\\end{lstlisting}\n')
                env=dict(os.environ,TECTONIC_CACHE_DIR=str(audit.RUNTIME/'tex-cache'),FONTCONFIG_FILE=str(audit.RUNTIME/'local/fonts.conf'))
                p=subprocess.run([str(audit.RUNTIME/'native/tectonic/tectonic.exe'),'--untrusted','--outdir',str(folder),str(folder/'main.tex')],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=300)
                core.write_bytes(folder/'compile.log',p.stdout)
                if p.returncode:raise ValueError('complex LaTeX case failed; see compile.log')
                pdf=folder/'main.pdf'
            report=inspect(pdf,count);reports.append(report)
            core.write_json(rd/'quality/complex_format_checks.json',{'cases':reports,'all_passed':all(r['passed'] for r in reports)})
            print(fmt+' '+str(count)+' question complex case: '+str(report['checks']),flush=True)
    if not all(r['passed'] for r in reports):raise ValueError('complex format verification failed')
    local_closure.state('filled_format_boundary_tests','pass',['quality/complex_format_checks.json'])

if __name__=='__main__':run()
