"""Private mail views. Classification is a display rule, never sender authentication."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime,time,timedelta,timezone,date
from email.utils import getaddresses
import hashlib,os,re
from typing import Literal
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select,func,or_
from sqlalchemy.orm import Session,defer,object_session,selectinload
from .auth import require_staff
from .db import get_session,utcnow
from .models import AppSetting

router=APIRouter(prefix='/api',dependencies=[Depends(require_staff)])
PUBLIC_DOMAINS={'mail.ru','bk.ru','list.ru','inbox.ru','yandex.ru','yandex.com','ya.ru','gmail.com','googlemail.com','outlook.com','hotmail.com','icloud.com','rambler.ru'}

def addresses(value):
 return {address.strip().casefold() for _,address in getaddresses([str(value or '')]) if '@' in address}

def rules():
 own=set().union(*(addresses(os.getenv(k,'')) for k in ('MAIL_USERNAME','MAIL_ADDRESS','IMAP_USER','IMAP_USERNAME')))
 domains={v.strip().casefold().lstrip('@') for v in re.split('[,;\\s]+',os.getenv('CORPORATE_EMAIL_DOMAINS') or os.getenv('CORPORATE_EMAIL_DOMAIN','posteks.ru')) if v.strip()}
 departments={}
 for label,keys in [('Конструкторское бюро',['DEPARTMENT_KB_EMAIL','KB_RESPONSE_SENDERS']),('Продажи',['DEPARTMENT_MANAGER_EMAIL','DEPARTMENT_SALES_EMAIL']),('АСУ',['DEPARTMENT_ASU_EMAIL']),('Производство',['DEPARTMENT_PRODUCTION_EMAIL'])]:
  for key in keys:
   for email in addresses(os.getenv(key,'')): departments[email]=label
 return own,domains,departments

def internal(email,domains,departments):
 email=str(email or '').casefold()
 domain=email.rpartition('@')[2]
 return email in departments or any(domain==d or domain.endswith('.'+d) for d in domains)

def base_metadata(item):
 own,domains,departments=rules();sender=item.sender.casefold()
 locations=getattr(item,'mail_locations',[])
 outgoing=sender in own or bool(locations and all(p.direction=='outgoing' for p in locations))
 recipients={r.casefold() for r in item.recipients or []}
 category=('internal' if recipients and all(internal(r,domains,departments) or r in own for r in recipients) else 'outgoing') if outgoing else ('internal' if internal(sender,domains,departments) else 'clients')
 return {'category':category,'direction':'outgoing' if outgoing else 'incoming','department':departments.get(sender,''),'is_new':bool(not outgoing and not item.local_read_at and item.source!='IMAP_HISTORY'),'sort_at':(item.sent_at or item.created_at).isoformat()+'Z','local_read_at':item.local_read_at.isoformat()+'Z' if item.local_read_at else None}

def _company_hint(item):
 text=str(item.sender_name or '')+'\n'+getattr(item,'_org_hint','')
 # A forwarded/quoted signature does not identify the current sender's firm.
 text=re.split(r'(?im)^\s*(?:>|От:|From:|Кому:|To:|Пересылаемое сообщение|[-_]{5,})',text, maxsplit=1)[0]
 for match in re.finditer(r'\b(?:ООО|АО|ПАО|ЗАО|ОАО|ИП)\s*(?:[«"“][^»"”\n]{2,90}[»"”]|[А-ЯЁA-Z][^\n;<>]{1,65})',text):
  label=match.group(0).strip(' ,.');fold=label.casefold()
  if not any(own in fold for own in ('постэкс','постекс','posteks')): return label
 return ''

def _external_addresses(item):
 own,domains,depts=rules()
 return [r.casefold() for r in item.recipients or [] if r.casefold() not in own and not internal(r,domains,depts)]

def thread_records(session,item):
 from .workflows import InboxMessage
 where=InboxMessage.case_id==item.case_id if item.case_id else InboxMessage.id==item.id
 return list(session.scalars(select(InboxMessage).options(defer(InboxMessage.raw_mime),defer(InboxMessage.body)).where(where).order_by(func.coalesce(InboxMessage.sent_at,InboxMessage.created_at),InboxMessage.id)))

def external_reply_target(session,item):
 rows=thread_records(session,item)
 incoming=[r for r in rows if base_metadata(r)['category']=='clients']
 if incoming: return incoming[-1].sender
 candidates=_external_addresses(item)
 return candidates[0] if len(candidates)==1 else None

def mail_metadata(item,context=None):
 meta=base_metadata(item);own,domains,depts=rules();candidate=item
 if meta['category']!='clients':
  rows=(context or {}).get(item.case_id,[]) if context is not None and item.case_id else (thread_records(object_session(item),item) if object_session(item) else [])
  candidate=next((r for r in rows if base_metadata(r)['category']=='clients'),item)
  if candidate is item and meta['category']!='clients' and object_session(item):
   candidate=next((r for r in thread_records(object_session(item),item) if base_metadata(r)['category']=='clients'),item)
 email=candidate.sender.casefold() if base_metadata(candidate)['category']=='clients' else ''
 if not email:
  recipients=_external_addresses(candidate)
  email=recipients[0] if len(recipients)==1 else ''
 domain=email.rpartition('@')[2]
 key=('domain:'+domain if domain and domain not in PUBLIC_DOMAINS else 'email:'+email) if email else ('internal:'+item.sender.casefold() if item.sender else 'unknown:'+str(item.id))
 company=_company_hint(candidate)
 label=company or email or depts.get(item.sender.casefold()) or item.sender
 session=object_session(item)
 if session:
  override=session.get(AppSetting,'mail_customer:'+hashlib.sha256(key.encode()).hexdigest())
  if override and override.value.get('name'): label=override.value['name'];company=label
  from .workflows import ServiceCase
  case=session.get(ServiceCase,item.case_id) if item.case_id else None
 else: case=None
 return {**meta,'customer_key':key,'customer_label':label,'customer_email':email,'organization_suggested':bool(company and not (session and override)),'company_name':company,'case_kind':case.kind if case else None}

def _load(session,q='',date_from=None,date_to=None,folder=''):
 from .workflows import InboxMessage
 query=select(InboxMessage,func.substr(InboxMessage.body,1,1000),func.substr(InboxMessage.body,-2000)).options(defer(InboxMessage.raw_mime),defer(InboxMessage.body))
 if q.strip():
  term=q.strip()[:300];query=query.where(or_(InboxMessage.subject.contains(term,autoescape=True),InboxMessage.body.contains(term,autoescape=True),InboxMessage.sender.contains(term,autoescape=True),InboxMessage.sender_name.contains(term,autoescape=True)))
 if folder:
  from .mail_models import MailRemoteMessage
  query=query.where(InboxMessage.id.in_(select(MailRemoteMessage.inbox_id).where(MailRemoteMessage.folder==folder)))
 try: tz=ZoneInfo(os.getenv('BUSINESS_TIMEZONE','Asia/Yekaterinburg'))
 except ZoneInfoNotFoundError: raise HTTPException(422,'Неизвестный BUSINESS_TIMEZONE в конфигурации')
 for value,upper in [(date_from,False),(date_to,True)]:
  if value:
   try: boundary=datetime.combine(date.fromisoformat(value),time.min,tz)+(timedelta(days=1) if upper else timedelta())
   except ValueError: raise HTTPException(422,'Дата должна иметь вид ГГГГ-ММ-ДД')
   boundary=boundary.astimezone(timezone.utc).replace(tzinfo=None)
   query=query.where(func.coalesce(InboxMessage.sent_at,InboxMessage.created_at)<boundary if upper else func.coalesce(InboxMessage.sent_at,InboxMessage.created_at)>=boundary)
 rows=[];context=defaultdict(list)
 for item,intro,tail in session.execute(query):
  item._org_hint=(intro or '')+'\n'+(tail or '');rows.append(item);context[item.case_id].append(item)
 return rows,context

def view(session,category='clients',sort='newest',q='',customer_key='',date_from=None,date_to=None,folder=''):
 rows,context=_load(session,q,date_from,date_to,folder)
 selected=[]
 for item in rows:
  meta=mail_metadata(item,context)
  if category!='all' and meta['category']!=category: continue
  if customer_key and meta['customer_key']!=customer_key: continue
  selected.append((item,meta))
 selected.sort(key=lambda p:(p[0].sent_at or p[0].created_at,p[0].id),reverse=sort=='newest')
 return selected

@router.get('/mail/inbox')
def inbox_view(category:Literal['clients','internal','outgoing','all']='clients',sort:Literal['newest','oldest']='newest',q:str='',customer_key:str='',date_from:str|None=None,date_to:str|None=None,folder:str='',limit:int=50,offset:int=0,session:Session=Depends(get_session)):
 from .workflows import serialize_mail
 if not 1<=limit<=200 or offset<0: raise HTTPException(422,'Некорректная страница писем')
 selected=view(session,category,sort,q,customer_key,date_from,date_to,folder)
 items=[{**serialize_mail(row),**meta} for row,meta in selected[offset:offset+limit]]
 return {'items':items,'total':len(selected),'unread_total':sum(meta['is_new'] for _,meta in selected),'offset':offset,'limit':limit,'has_more':offset+len(items)<len(selected),'category':category,'sort':sort}

@router.get('/mail/customers')
def customers(category:Literal['clients','internal','outgoing','all']='clients',q:str='',date_from:str|None=None,date_to:str|None=None,session:Session=Depends(get_session)):
 grouped={}
 for row,meta in view(session,category,'newest',q,date_from=date_from,date_to=date_to):
  key=meta['customer_key']
  group=grouped.setdefault(key,{'key':key,'label':meta['customer_label'],'email':meta['customer_email'],'company_name':meta['company_name'],'company_suggested':meta['organization_suggested'],'new_count':0,'message_count':0,'thread_count':0,'last_at':meta['sort_at'],'_threads':set()})
  group['message_count']+=1;group['new_count']+=meta['is_new'];group['_threads'].add(row.case_id or -row.id)
 for group in grouped.values(): group['thread_count']=len(group.pop('_threads'))
 return list(grouped.values())

class ReadBody(BaseModel):
 read:bool=True
 message_ids:list[int]|None=Field(default=None,max_length=2000)

@router.post('/inbox/{message_id}/read')
def mark_read(message_id:int,body:ReadBody,session:Session=Depends(get_session),actor=Depends(require_staff)):
 from .workflows import InboxMessage,record_event
 item=session.get(InboxMessage,message_id)
 if not item: raise HTTPException(404,'Письмо не найдено')
 rows=thread_records(session,item);allowed={r.id:r for r in rows};ids=body.message_ids if body.message_ids is not None else [message_id]
 if any(i not in allowed for i in ids): raise HTTPException(422,'Сообщение не принадлежит открытой цепочке')
 for i in ids: allowed[i].local_read_at=utcnow() if body.read else None
 record_event(session,actor,'mail.local_read','inbox',message_id,{'count':len(ids),'read':body.read,'imap_flags_changed':False});session.commit()
 return {'updated':len(ids),'read':body.read,'imap_flags_changed':False}

@router.post('/inbox/{message_id}/promote')
def promote(message_id:int,session:Session=Depends(get_session),actor=Depends(require_staff)):
 from .workflows import InboxMessage,ServiceCase,record_event,serialize_case
 item=session.get(InboxMessage,message_id)
 if not item: raise HTTPException(404,'Письмо не найдено')
 case=session.get(ServiceCase,item.case_id) if item.case_id else None
 if not case:
  case=ServiceCase(title=item.subject[:240],source='MAIL_OPERATOR',kind='service');session.add(case);session.flush();item.case_id=case.id
 case.kind='service';meta=mail_metadata(item)
 if meta['customer_email']: case.email=meta['customer_email']
 record_event(session,actor,'mail.promoted_to_case','case',case.id,{'inbox_id':item.id});session.commit()
 return serialize_case(case)
