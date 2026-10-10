"""Read selected identity pages locally. JSON stdin/stdout; never edits PDFs or calls models."""
import json
import pathlib
import sys
import importlib.metadata
import hashlib

SITE=pathlib.Path('C:/Users/ltf02/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/Lib/site-packages')

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    inputs=json.load(sys.stdin)
    sys.path.append(str(SITE))
    # Python 3.8 cannot resolve the read-only runtime path in this sandbox.
    previous=pathlib.Path.resolve
    try:
        pathlib.Path.resolve=lambda self,strict=False:self.absolute()
        import pypdfium2 as pdfium
    finally:pathlib.Path.resolve=previous
    results={}
    for entry in inputs:
        data=pathlib.Path(entry['path']).read_bytes()
        doc=pdfium.PdfDocument(data)
        try:
            index=entry['page']-1
            if not 0<=index<len(doc):raise ValueError('identity page out of range')
            page=doc[index]
            try:
                textpage=page.get_textpage()
                try:text=textpage.get_text_range()
                finally:textpage.close()
            finally:page.close()
            results[entry['case_id']]={'page':entry['page'],'text':text,'total_pages':len(doc),'source_sha256':hashlib.sha256(data).hexdigest()}
        finally:doc.close()
    print(json.dumps({'tool':'pypdfium2','version':importlib.metadata.version('pypdfium2'),'results':results},ensure_ascii=False))
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,ImportError,KeyError) as exc:
        print('[PDF_IDENTITY_ERROR] '+str(exc),file=sys.stderr);raise SystemExit(2)
