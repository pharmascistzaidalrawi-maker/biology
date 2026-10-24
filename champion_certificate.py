"""Deterministic personalized artwork rendered locally, without external APIs."""
from io import BytesIO
from pathlib import Path
import re
from PIL import Image, ImageDraw, ImageFont
import arabic_reshaper
from bidi.algorithm import get_display

ROOT=Path(__file__).resolve().parent

def render_certificate(name,grade,kind='exam',title='',maximum=100):
    image=Image.open(ROOT/'assets/certificates/champion-template.png').convert('RGB')
    w,h=image.size
    draw=ImageDraw.Draw(image)
    fontpath=ROOT/'assets/reports/Amiri-Bold.ttf'
    noun='الواجب' if kind=='homework' else 'الامتحان'
    suffix=re.sub(r'^(?:📚|📝|\s|\[تراكمي\])*','',str(title)).strip()
    heading='بطل '+suffix if suffix.startswith(noun) else 'بطل '+noun
    def put(value,cy,width,size,color):
        text=get_display(arabic_reshaper.reshape(str(value)))
        while size>15:
            font=ImageFont.truetype(str(fontpath),size,layout_engine=ImageFont.Layout.BASIC)
            box=draw.textbbox((0,0),text,font=font)
            if box[2]-box[0]<=width:break
            size-=1
        x=(w-(box[2]-box[0]))/2-box[0]
        y=cy-(box[3]-box[1])/2-box[1]
        draw.text((x,y),text,font=font,fill=color)
    put(heading,h*.525,w*.74,110,'#080D10')
    put(name,h*.655,w*.72,127,'#06101E')
    def number(n):return f'{float(n):g}'
    put(f'{number(grade)} / {number(maximum)}',h*.788,w*.27,58,'#080D10')
    result=BytesIO();image.save(result,format='JPEG',quality=95,subsampling=0)
    result.name='شهادة التميز.jpg';result.seek(0)
    return result
