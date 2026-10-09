"""Render named evidence for source/layout inspection without modifying sealed inputs."""
import io
from . import core,audit,local_closure

def format_sheets():
    audit.deps();import profile_extraction as pe;pdfium=pe.load_pdfium();from PIL import Image,ImageDraw
    rd=audit.run_dir(local_closure.args());results=[]
    for fmt,name in [('typst','preview.pdf'),('latex','main.pdf')]:
        for count in (3,5):
            folder=rd/'quality/complex_formats'/(fmt+'-'+str(count));pdf=folder/name
            doc=pdfium.PdfDocument(pdf.read_bytes())
            try:
                selected=list(range(len(doc)))
                for start in range(0,len(selected),4):
                    nums=selected[start:start+4];sheet=Image.new('RGB',(1200,1720),'#dddddd');draw=ImageDraw.Draw(sheet)
                    for j,n in enumerate(nums):
                        page=doc[n]
                        try:
                            bmp=page.render(scale=.95)
                            try:im=bmp.to_pil().convert('RGB');im.thumbnail((570,815));sheet.paste(im,((j%2)*600+15,(j//2)*860+30))
                            finally:bmp.close()
                        finally:page.close()
                        draw.text(((j%2)*600+15,(j//2)*860+10),fmt+' '+str(count)+' / page '+str(n+1),fill='black')
                    out=folder/('contact-%02d.png'%(start//4+1));buf=io.BytesIO();sheet.save(buf,format='PNG');core.write_bytes(out,buf.getvalue());results.append(str(out))
            finally:doc.close()
    print('\n'.join(results))

if __name__=='__main__':format_sheets()
