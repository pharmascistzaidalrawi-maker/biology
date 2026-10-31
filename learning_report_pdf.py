"""Detailed activity reports using the approved Amiri/brand report assets."""
from io import BytesIO
import re
from weekly_report_design import Report, INK, MUTED, GOLD, weekly_filename, week_label


def safe_name(name):
    return ' '.join(re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', ' ', str(name or 'طالب')).split())[:100] or 'طالب'


def activity_lines(activity, entries=None):
    from zoneinfo import ZoneInfo
    zone=ZoneInfo('Asia/Baghdad')
    rows=[]
    for at,text in entries or []:
        # PDF font does not contain emoji; the Arabic labels carry the meaning.
        text=re.sub(r'[^\w\s\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff/():،؛.\-—|]', '', text)
        rows.append(f"{at.astimezone(zone):%d/%m %H:%M} | {text}")
    if entries is None:
        for r in activity['lectures']:
            rows.append(f"محاضرة مكتملة: الفصل {r['chapter']}، المحاضرة {r['lecture']} | {r['at'].astimezone(zone):%d/%m %H:%M}")
        for r in activity['tasks']:
            noun='واجب' if r['kind']=='homework' else 'امتحان'
            grade=f"{r['grade']}/100" if r['grade'] is not None else 'بانتظار التصحيح'
            rows.append(f"{noun}: {r['title']} | {r['at'].astimezone(zone):%d/%m %H:%M} | {grade}")
        for r in activity['reviews']:
            rows.append(f"مراجعة مكتملة: الفصل {r['chapter']}، المحاضرة {r['lecture']}، المرحلة {r['stage']} | {r['at'].astimezone(zone):%d/%m %H:%M}")
        for r in activity.get('school_reviews',[]):
            rows.append(f"مراجعة مدرسة مكتملة: {r['title']} | {r['at'].astimezone(zone):%d/%m %H:%M}")
        for r in activity['weaknesses']:
            rows.append(f"نقطة ضعف تمت معالجتها: {r['weakness_text']} | {r['at'].astimezone(zone):%d/%m %H:%M}")
    return rows


def append_activity(report, activity, entries=None):
    summary=(f"محاضرات مكتملة: {len(activity['lectures'])} | "
             f"واجبات مسلمة: {sum(r['kind']=='homework' for r in activity['tasks'])} | "
             f"امتحانات مسلمة: {sum(r['kind']=='exam' for r in activity['tasks'])}")
    items=[summary,f"مراجعات مكتملة: {len(activity['reviews'])} | نقاط ضعف تمت معالجتها: {len(activity['weaknesses'])} | صافي XP الدراسة: {activity['earned']}"]
    details=activity_lines(activity,entries)
    items.append(f"مراجعات مدرسة مكتملة: {len(activity.get('school_reviews',[]))}")
    items += details or ['لا يوجد إنجاز مكتمل مسجل خلال هذه الفترة.']
    items += ['يعتمد هذا السجل على تاريخ الإكمال أو إنهاء التسليم، حتى لو نُشر الواجب أو الامتحان قبل بداية الفترة.']
    title='شنو قرأت وأنجزت فعليًا؟'
    def heading():
        if report.y+75>770:report.next_page()
        report.text(title,561,report.y,18,INK,True)
        report.y+=26
    heading()
    for index,item in enumerate(items):
        lines=report.lines(item,493,12,index<2)
        while lines:
            if report.y+42>770:
                report.next_page();heading()
            count=max(1,int((770-report.y-18)//21))
            part,lines=lines[:count],lines[count:]
            height=18+21*len(part)
            report.rect(34,report.y,527,height,'#FFFFFF' if index%2==0 else '#F0F2EC',5)
            for j,line in enumerate(part):report.text(line,545,report.y+23+j*21,12,INK if index<2 else MUTED,index<2)
            report.y+=height+6
    report.y+=15


def build_activity_pdf(student,activity,period,entries,subject='',number=1):
    buffer=BytesIO()
    report=Report(buffer)
    report.period_title={'day':'التقرير اليومي','week':'التقرير الأسبوعي','month':'التقرير الشهري'}[period]
    report.period_label=activity['start'].strftime('%d/%m/%Y') if period=='day' else 'الأسبوع '+week_label(number) if period=='week' else 'إنجاز الفترة'
    report.header(activity['start'],activity['end'],1)
    report.c.setTitle(report.period_title+' | منصة المجتهد التعليمية')
    report.rect(34,197,527,93,'#FFFFFF',9)
    report.rect(552,213,3,61,GOLD,1)
    name=report.lines(student['full_name'],490,19,True)
    # Long names flow before the details instead of colliding with them.
    h=max(93,42+len(name)*25)
    if h>93:report.rect(34,197,527,h,'#FFFFFF',9)
    for i,line in enumerate(name):report.text(line,535,227+i*25,19,INK,True)
    report.text(subject or 'منصة المجتهد التعليمية',535,197+h-14,11,MUTED)
    report.y=197+h+35
    append_activity(report,activity,entries)
    report.finish()
    suffix=activity['start'].strftime('%Y-%m-%d')
    buffer.name=weekly_filename(student['full_name'],number) if period=='week' else f"تقرير الطالب {safe_name(student['full_name'])} {report.period_title} {suffix}.pdf"
    buffer.seek(0)
    return buffer
