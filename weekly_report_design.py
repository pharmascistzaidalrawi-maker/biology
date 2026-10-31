"""Approved Amiri weekly report, bundled branding, and stable Arabic filenames."""
from datetime import date
from io import BytesIO
from pathlib import Path
import re
import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor

ASSETS = Path(__file__).resolve().parent / 'assets' / 'reports'
NAVY, INK, MUTED = '#112E38', '#17343C', '#687C80'
GOLD, PAPER, LINE, GREEN, RED = '#B9904D', '#F7F5EF', '#E5E8E3', '#276653', '#9E4D43'
WIDTH, HEIGHT = 595.276, 841.89

def week_label(number):
    number = max(1, int(number))
    first = ['الأول', 'الثاني', 'الثالث', 'الرابع', 'الخامس', 'السادس', 'السابع', 'الثامن', 'التاسع', 'العاشر', 'الحادي عشر', 'الثاني عشر', 'الثالث عشر', 'الرابع عشر', 'الخامس عشر', 'السادس عشر', 'السابع عشر', 'الثامن عشر', 'التاسع عشر']
    if number <= 19:
        return first[number - 1]
    tens = {20: 'العشرون', 30: 'الثلاثون', 40: 'الأربعون', 50: 'الخمسون', 60: 'الستون', 70: 'السبعون', 80: 'الثمانون', 90: 'التسعون'}
    if number < 100:
        ten, unit = divmod(number, 10)
        return (('الحادي' if unit == 1 else first[unit - 1]) + ' و' if unit else '') + tens[ten * 10]
    return str(number)

def weekly_filename(student_name, number):
    # Telegram accepts Arabic and spaces; strip path separators/control characters.
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', ' ', str(student_name or 'طالب'))
    name = ' '.join(name.split()).strip(' .')[:100] or 'طالب'
    return f'تقرير الطالب {name} الأسبوع {week_label(number)}.pdf'

def week_number(first_week_end, current_end):
    first = date.fromisoformat(str(first_week_end))
    current = current_end.date() if hasattr(current_end, 'date') else current_end
    return max(1, (current - first).days // 7 + 1)

def _rtl(value):
    return get_display(arabic_reshaper.reshape(str(value)))

class Report:
    def __init__(self, buffer):
        for name, file in [('WeeklyAmiri', 'Amiri-Regular.ttf'), ('WeeklyAmiriBold', 'Amiri-Bold.ttf')]:
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, str(ASSETS / file)))
        self.c = canvas.Canvas(buffer, pagesize=(WIDTH, HEIGHT))
        self.c.setTitle('التقرير الأسبوعي | منصة المجتهد التعليمية')
        self.c.setAuthor('منصة المجتهد التعليمية')
        self.page = 1
        self.y = 0
        self.background()

    def rect(self, x, y, w, h, color, r=0):
        self.c.setFillColor(HexColor(color))
        if r:
            self.c.roundRect(x, HEIGHT-y-h, w, h, r, fill=1, stroke=0)
        else:
            self.c.rect(x, HEIGHT-y-h, w, h, fill=1, stroke=0)

    def text(self, value, x, y, size=12, color=INK, bold=False, align='right'):
        self.c.setFillColor(HexColor(color))
        self.c.setFont('WeeklyAmiriBold' if bold else 'WeeklyAmiri', size)
        fn = {'right': self.c.drawRightString, 'left': self.c.drawString, 'center': self.c.drawCentredString}[align]
        fn(x, HEIGHT-y, _rtl(value))

    def lines(self, text, width, size=12, bold=False):
        font = 'WeeklyAmiriBold' if bold else 'WeeklyAmiri'
        def fits(s):
            return pdfmetrics.stringWidth(_rtl(s), font, size) <= width
        result = []
        for para in str(text).split('\n'):
            line = ''
            for word in para.split():
                candidate = (line + ' ' + word).strip()
                if fits(candidate):
                    line = candidate
                    continue
                if line:
                    result.append(line)
                    line = ''
                for ch in word:
                    if line and not fits(line + ch):
                        result.append(line)
                        line = ''
                    line += ch
            result.append(line)
        return result or ['']

    def wrapped(self, text, x, y, width, size=12, color=INK, bold=False, leading=19):
        lines = self.lines(text, width, size, bold)
        for i, line in enumerate(lines):
            self.text(line, x, y+i*leading, size, color, bold)
        return len(lines)*leading

    def background(self):
        self.rect(0, 0, WIDTH, HEIGHT, PAPER)

    def footer(self):
        self.rect(34, 787, 527, .6, LINE)
        self.text('منصة المجتهد التعليمية', 561, 810, 12, INK, True)
        self.text('طريقك نحو القمة', 34, 810, 11, MUTED, align='left')
        if self.page > 1:
            self.text(str(self.page), WIDTH/2, 810, 10, MUTED, align='center')

    def next_page(self):
        self.footer()
        self.c.showPage()
        self.page += 1
        self.background()
        self.rect(0, 0, WIDTH, 76, NAVY)
        self.text('منصة المجتهد التعليمية', 561, 30, 13, '#EAD9B8', True)
        self.text(getattr(self,'period_title','التقرير الأسبوعي')+' - تتمة', 561, 58, 20, '#FFFFFF', True)
        self.text(self.label, 34, 52, 12, '#EAD9B8', align='left')
        self.y = 100

    def header(self, start, end, number):
        self.label = getattr(self,'period_label','الأسبوع ' + week_label(number))
        self.rect(0, 0, WIDTH, 180, NAVY)
        self.c.saveState()
        path = self.c.beginPath()
        path.circle(94, HEIGHT-78, 54)
        self.c.clipPath(path, stroke=0, fill=0)
        self.c.drawImage(str(ASSETS/'platform-logo.jpg'), 40, HEIGHT-132, 108, 108, preserveAspectRatio=True)
        self.c.restoreState()
        self.text('منصة المجتهد التعليمية', 561, 43, 17, '#EAD9B8', True)
        self.text(getattr(self,'period_title','التقرير الأسبوعي'), 561, 96, 35, '#FFFFFF', True)
        self.text('لأداء الطالب ومتابعة الإنجاز', 560, 124, 15, '#CDDBDA')
        self.rect(34, 147, 527, 1, '#365159')
        self.text('الفترة من', 560, 168, 10, '#CDDBDA')
        self.text(f"{start:%d/%m/%Y}  -  {end:%d/%m/%Y}", 491, 168, 12, '#FFFFFF')
        self.text(self.label, 34, 168, 11, '#EAD9B8', align='left')

    def summary(self, student, metrics, comparison, max_warnings):
        name = self.lines(student['full_name'], 280, 20, True)
        school = self.lines(student.get('school') or '-', 155, 16, True)
        h = max(75, 36+max(len(name)*27, len(school)*24))
        self.rect(34, 197, 527, h, '#FFFFFF', 9)
        self.rect(552, 213, 3, h-32, GOLD, 1)
        self.text('اسم الطالب', 534, 218, 10, MUTED)
        for i, s in enumerate(name): self.text(s, 534, 247+i*27, 20, INK, True)
        self.rect(227, 212, 1, h-30, LINE)
        self.text('المدرسة', 202, 219, 10, MUTED)
        for i, s in enumerate(school): self.text(s, 202, 247+i*24, 16, INK, True)
        shift = h-75
        self.text('ملخص الأداء', 561, 305+shift, 18, INK, True)
        self.text('مؤشرات الأسبوع', 34, 303+shift, 10, MUTED, align='left')
        cw = (527-30)/4
        average = metrics['average']
        cards = [(561-cw,'نسبة الإنجاز',f"{metrics['completion']}%",'من المهام المطلوبة',INK),
                 (561-2*cw-10,'معدل الامتحانات',f'{average}/100' if average is not None else 'لا توجد درجة','درجات الامتحانات' if average is not None else 'لم تُسجّل درجة',INK),
                 (561-3*cw-20,'مجموع النقاط',f"{student.get('xp',0)} XP",'رصيد النقاط',INK),
                 (34,'مجموع الإنذارات',f"{student.get('warnings',0)}/{max_warnings}",'الإجمالي المسجل',RED)]
        for x, title, val, sub, col in cards:
            self.rect(x, 320+shift, cw, 108, '#FFFFFF', 7)
            self.text(title, x+cw-12, 342+shift, 12, MUTED)
            size = 17 if val=='لا توجد درجة' else 24
            while pdfmetrics.stringWidth(_rtl(val),'WeeklyAmiriBold',size)>cw-18: size-=1
            self.text(val, x+cw/2, 377+shift, size, col, True, 'center')
            self.text(sub, x+cw/2, 410+shift, 9, MUTED, align='center')
            if title=='نسبة الإنجاز':
                self.rect(x+13,390+shift,cw-26,3,'#E9EEEA',1)
                if metrics['completion']:
                    bar=(cw-26)*min(100,max(0,metrics['completion']))/100
                    self.rect(x+cw-13-bar,390+shift,bar,3,GREEN,1)
        comp = self.lines(comparison, 330, 10)
        height = max(39, 18+len(comp)*17)
        self.rect(34,442+shift,527,height,'#ECEFE9',5)
        self.text('المقارنة بالأسبوع السابق',548,466+shift,11,INK,True)
        for i, s in enumerate(comp): self.text(s,385,466+shift+i*17,10,MUTED)
        self.y = 442+shift+height+34

    def table_header(self):
        if self.y+78 > 770: self.next_page()
        self.text('واجبات وامتحانات هذا الأسبوع',561,self.y,18,INK,True)
        self.y += 16
        self.rect(34,self.y,527,33,NAVY,5)
        self.rect(34,self.y+20,527,13,NAVY)
        for x, s in [(543,'العنوان'),(310,'النوع'),(218,'الدرجة'),(111,'الحالة')]:
            self.text(s,x,self.y+22,11,'#FFFFFF',True)
        self.y += 33

    def tasks(self, rows):
        self.table_header()
        rows = rows or [{'title':'لا توجد مطلوبات خلال هذا الأسبوع','kind':None,'grade':None,'submitted_at':None}]
        for index, row in enumerate(rows):
            lines = self.lines(row['title'],210,13,True)
            # Split unusually long titles over pages without dropping text.
            while lines:
                if self.y+43>770:
                    self.next_page()
                    self.table_header()
                capacity = max(1,int((770-self.y-18)//20))
                part, lines = lines[:capacity], lines[capacity:]
                h=max(43,18+len(part)*20)
                if self.y+h>770:
                    self.next_page(); self.table_header()
                    continue
                self.rect(34,self.y,527,h,'#FFFFFF' if index%2==0 else '#F0F2EC')
                for i, s in enumerate(part):self.text(s,543,self.y+27+i*20,13,INK,True)
                self.text('واجب' if row['kind']=='homework' else 'امتحان' if row['kind'] else '-',310,self.y+27,12,MUTED)
                self.text(f"{row['grade']}/100" if row['grade'] is not None else '-',218,self.y+27,11,MUTED)
                if row['kind']:
                    done = bool(row['submitted_at'])
                    self.rect(48,self.y+10,76,25,'#E5F0E9' if done else '#F6EAE3',5)
                    self.text('تم التسليم' if done else 'لم يسلم',86,self.y+27,11,GREEN if done else RED,True,'center')
                self.y+=h
        self.y+=22

    def panels(self, warnings, achievements):
        panels=[]
        for title, values, color in [('إنذارات هذا الأسبوع',warnings, MUTED),('الإنجازات الأسبوعية',achievements,GREEN)]:
            lines=[]
            for v in values:
                lines.extend(self.lines(v,226,12))
            panels.append([title,lines,color])
        while any(p[1] for p in panels):
            available = 770-self.y
            if available<91:self.next_page();available=770-self.y
            n=max(1,int((available-55)//21))
            parts=[p[1][:n] for p in panels]
            h=max(91,55+max(map(len,parts))*21)
            for x, p, part in zip([303,34],panels,parts):
                if not part:continue
                self.rect(x,self.y,258,h,'#FFFFFF',7)
                self.text(p[0],x+242,self.y+27,14,INK,True)
                for i,s in enumerate(part):self.text(s,x+242,self.y+67+i*21,12,p[2])
                del p[1][:len(part)]
            self.y+=h+18
            if any(p[1] for p in panels):self.next_page()

    def finish(self):
        self.footer()
        self.c.save()


def build_weekly_pdf(bundle, current_start, current_end, *, metrics, max_warnings=10, timezone=None, number=1):
    buffer=BytesIO()
    report=Report(buffer)
    current=metrics(bundle['current']); previous=metrics(bundle['previous'])
    if bundle['previous'] and previous['score']:
        change=round((current['score']-previous['score'])*100/previous['score'],1)
        comparison=f"ازداد الأداء بنسبة {change}% عن الأسبوع السابق" if change>=0 else f"انخفض الأداء بنسبة {abs(change)}% عن الأسبوع السابق"
    else:comparison='لا تتوفر بيانات كافية للمقارنة مع الأسبوع السابق'
    achievements=[]
    if current['total'] and current['submitted']==current['total']:achievements.append('أكمل جميع الواجبات والامتحانات المطلوبة')
    if current['average'] is not None and current['average']>=90:achievements.append('حقق معدلاً ممتازاً في الامتحانات')
    if not bundle['warnings']:achievements.append('أسبوع كامل بلا إنذارات')
    activity=bundle.get('activity')
    if activity:
        if activity['lectures']:achievements.append(f"أكمل {len(activity['lectures'])} محاضرة خلال الفترة")
        for kind,noun in [('homework','واجب'),('exam','امتحان')]:
            count=sum(r['kind']==kind for r in activity['tasks'])
            if count:achievements.append(f'سلّم {count} {noun} خلال الفترة')
        if activity['reviews']:achievements.append(f"أكمل {len(activity['reviews'])} مراجعة")
    if not achievements:achievements.append('لا توجد إنجازات خاصة مسجلة هذا الأسبوع')
    warnings=[]
    for w in bundle['warnings']:
        when=w['created_at'].astimezone(timezone) if timezone else w['created_at']
        warnings.append(f"{w['reason']} - {when:%d/%m/%Y}")
    report.header(current_start,current_end,number)
    report.summary(bundle['student'],current,comparison,max_warnings)
    report.tasks(bundle['current'])
    report.panels(warnings or ['لا توجد إنذارات خلال هذا الأسبوع'],achievements)
    if bundle.get('activity') is not None:
        from learning_report_pdf import append_activity
        append_activity(report,bundle['activity'])
    report.finish()
    buffer.name=weekly_filename(bundle['student']['full_name'],number)
    buffer.seek(0)
    return buffer
