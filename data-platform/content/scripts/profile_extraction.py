"""Source-bound local PDF extraction; candidates are distinct from visually checked facts."""
import hashlib,importlib.metadata,re,sys
from pathlib import Path
SITE=Path('C:/Users/ltf02/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/Lib/site-packages')
def load_pdfium():
 sys.path.append(str(SITE));prev=Path.resolve
 try:
  Path.resolve=lambda self,strict=False:self.absolute()
  import pypdfium2
 finally:Path.resolve=prev
 return pypdfium2
def extract_pdf_pages(path):
 pdf=load_pdfium();data=Path(path).read_bytes();doc=pdf.PdfDocument(data);pages=[]
 try:
  for i in range(len(doc)):
   page=doc[i];tp=page.get_textpage()
   try:pages.append(tp.get_text_range())
   finally:tp.close();page.close()
 finally:doc.close()
 return pages,hashlib.sha256(data).hexdigest(),importlib.metadata.version('pypdfium2')
def section_type(title):
 for key,words in [('toc',['目录','contents']),('abstract',['摘要','abstract','summary']),('restatement',['重述','提出','introduction']),('assumptions',['假设','assumption']),('symbols',['符号','变量说明','notation']),('analysis',['问题分析','问题的分析','数据分析']),('sensitivity',['敏感','灵敏','sensitivity']),('validation',['检验','验证','validation']),('discussion',['讨论','评价','改进','discussion','strength','weakness']),('solve',['求解','算法','solution']),('model',['模型','建模','model']),('conclusion',['结论','总结','conclusion']),('references',['参考文献','references']),('appendix',['附录','appendix'])]:
  if any(w in title.lower() for w in words):return key
 return 'other'
def detect_structure(pages,sha,pid=None):
 # Explicit numbering plus textual titles, not every short sentence mentioning a model.
 pattern=re.compile(r'^\s*(?:(?P<cn>[一二三四五六七八九十]+)[、．.]\s*|(?P<num>\d+(?:\.\d+)*)[.]?\s+)(?P<title>[^\d].{1,65})$')
 out=[];toc_pages=[]
 for pi,text in enumerate(pages,1):
  lines=text.splitlines();toc=any(re.fullmatch(r'\s*(目录|contents)\s*',s,re.I) for s in lines)
  if toc:toc_pages.append(pi);continue
  for line in lines:
   line=line.strip();m=pattern.fullmatch(line);special=re.fullmatch(r'(摘要|参考文献|附录(?:[A-Z一二三四五六七八九十])?|Abstract|References|Appendix)',line,re.I)
   if not m and not special:continue
   title=m.group('title').strip() if m else line
   if not re.search(r'[\u3400-\u9fffA-Za-z]{2}',title) or re.search(r'[。；;=<>]|^月|\bfor\b|\bif\b',title):continue
   if m and m.group('num') and not re.search(r'[\u3400-\u9fff]',title) and not re.fullmatch(r'[A-Z][A-Za-z ,():/\-]{1,64}',title):continue
   if re.search(r'\.{3,}|…',title):continue
   level=1 if not m or m.group('cn') else m.group('num').count('.')+1
   kind=section_type(title);sub=None;match=re.search(r'(问题|任务)\s*([一二三四1234])',title)
   if pid and match:sub=pid+':q'+str({'一':1,'二':2,'三':3,'四':4}.get(match[2],match[2]))
   out.append(dict(section_title=title,section_type=kind,level=level,subproblem_ref=sub,page_range=[pi,pi],page_evidence=line,evidence=dict(source_sha256=sha,locator_type='page',start=pi,end=pi)))
 for i,s in enumerate(out):
  following=next((n for n in out[i+1:] if n['level']<=s['level']),None);s['page_range'][1]=max(s['page_range'][0],following['page_range'][0] if following else len(pages))
 return out,toc_pages
def empty_length():
 return dict(zh_chars=None,en_words=None,zh_parts=dict(body=None,abstract=None,toc=None,appendix=None),en_parts=dict(body=None,abstract=None,toc=None,appendix=None),section_ratios=[],statistical_basis='Original length not established; recognized text counts stored separately, not assigned as body length',missing=['text layer may omit digits/formulas; reliable disjoint section boundaries require review'])
def text_metrics(pages):
 text='\n'.join(pages)
 return dict(recognized_cjk_chars=sum('\u3400'<=c<='\u9fff' for c in text),recognized_en_words=len(re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)*",text)),recognized_nonspace_chars=sum(not c.isspace() for c in text),scope='all extracted pages, includes abstract/appendix/references',is_original_length=False)
def figure_candidates(pages):
 out=[]
 for pi,text in enumerate(pages,1):
  for line in text.splitlines():
   m=re.fullmatch(r'\s*图\s*(\d+)\s*[：:.．]?\s+(.{2,65})\s*',line)
   if m and '。' not in m[2]:out.append(dict(page=pi,number=m[1],caption_text=m[2],status='needs_visual_review'))
 return out
