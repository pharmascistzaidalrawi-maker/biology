"""Central text formatting: UTF-16 bold entities, retaining clickable links."""
from html.parser import HTMLParser
from copy import copy
from telegram import MessageEntity
from telegram.ext import ExtBot


def width(text):return len(text.encode('utf-16-le'))//2

class RichText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.parts=[];self.links=[];self.stack=[];self.offset=0
    def handle_data(self,data):self.parts.append(data);self.offset+=width(data)
    def handle_starttag(self,tag,attrs):
        if tag=='a':self.stack.append((self.offset,dict(attrs).get('href')))
        elif tag=='br':self.handle_data('\n')
    def handle_endtag(self,tag):
        if tag=='a' and self.stack:
            start,url=self.stack.pop()
            if url and self.offset>start:self.links.append(MessageEntity('text_link',start,self.offset-start,url=url))


def format_text(data,key='text',entities_key='entities'):
    text=data.get(key)
    if not isinstance(text,str) or not text:return
    mode=str(data.get('parse_mode') or '').lower()
    entities=list(data.get(entities_key) or [])
    if mode=='html':
        parser=RichText();parser.feed(text);parser.close()
        text=''.join(parser.parts);entities=parser.links
    elif mode and mode not in ('none',):
        # Existing Markdown is passed through without changing its semantics.
        return
    entities=[e for e in entities if (e.get('type') if isinstance(e,dict) else e.type) not in ('bold','code','pre')]
    data[key]=text;data['parse_mode']=None
    data[entities_key]=[MessageEntity('bold',0,width(text)),*entities]


class BoldBot(ExtBot):
    async def _do_post(self,endpoint,data,**kwargs):
        data=dict(data)
        if endpoint not in ('answerCallbackQuery','setMyCommands','createForumTopic','editForumTopic'):
            format_text(data)
            format_text(data,'caption','caption_entities')
            media=data.get('media')
            if media:
                def formatted(item):
                    if not hasattr(item,'caption'):return item
                    obj=copy(item);values={'caption':item.caption,'parse_mode':item.parse_mode,'caption_entities':item.caption_entities}
                    format_text(values,'caption','caption_entities')
                    for k,v in values.items():object.__setattr__(obj,k,v)
                    return obj
                data['media']=[formatted(item) for item in media] if isinstance(media,(list,tuple)) else formatted(media)
        return await super()._do_post(endpoint,data,**kwargs)
