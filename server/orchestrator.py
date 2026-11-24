"""Stateful, evidence-gated dialogue planning and controlled internal actions.

A planner proposes a JSON plan. The server validates facts, source excerpts and
permissions and performs database actions; a model never receives direct tools.
The local planner is a limited, explicitly reported fallback, not a pretend LLM.
"""
from __future__ import annotations
from copy import deepcopy
from contextlib import contextmanager, nullcontext
import threading
from weakref import WeakValueDictionary
from datetime import datetime, timezone
import re
import time
from typing import Literal
import uuid
import logging
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError
from .db import utcnow
from .knowledge import detect_models, normalize_model
from .models import AppSetting, Conversation, ConversationState, Document, Escalation

_conversation_locks = WeakValueDictionary()
_locks_guard = threading.Lock()


@contextmanager
def conversation_lock(conversation_id: str):
    with _locks_guard:
        lock = _conversation_locks.setdefault(conversation_id, threading.RLock())
    with lock:
        yield


FACT_KEYS = {'equipment_model', 'serial_number', 'company', 'contact_name', 'email', 'phone',
             'address', 'preferred_datetime', 'location_confirmed', 'problem', 'part_description',
             'requested_quantity', 'part_marking', 'client_confirmation'}
HUMAN = re.compile(r'(?:нуж[еа]н|хочу|позов|переда|свяж|поговор|приглас|подключ).{0,45}(?:человек|специалист|инженер|оператор|сотрудник)|(?:специалист|оператор)[а-я]*\s*[,!]|жив[оы]м?\s+(?:человек|специалист)', re.I)
VISIT = re.compile(r'выезд|прие(?:хать|зж|зд)|визит|на\s+мест[ое]|осмотр\s+(?:у\s+нас|на\s+площадке)', re.I)
REMOTE = re.compile(r'дистанцион|удал[её]нн|по\s+телефон|консультаци', re.I)
_HUMAN_TARGET = re.compile(r'\b(?:человек|специалист|инженер|оператор|сотрудник)[а-я]*\b', re.I)
_VISIT_TARGET = re.compile(r'\b(?:выезд[а-я]*|приех[а-я]*|приез[а-я]*|приед[а-я]*|визит[а-я]*|осмотр\s+(?:у\s+нас|на\s+площадке))\b', re.I)
_REMOTE_TARGET = re.compile(r'\b(?:дистанционн[а-я]*|удал[её]нн[а-я]*|консультаци[а-я]*)\b|по\s+телефону', re.I)
_REQUEST_CUE = re.compile(r'\b(?:хочу|хотим|нуж(?:ен|на|но|ны)|прошу|просим|позовите|вызовите|пригласите|подключите|передайте|свяжите|согласовать|организуйте|закажите|запланируйте|направьте|пришлите|предпочитаю|давайте|не\s+против)\b', re.I)
_DECLINE_CUE = re.compile(r'\b(?:не\s+(?:хочу|хотим|нуж(?:ен|на|но|ны)|желаю|планирую|требуется|надо|нужно|зовите|вызывайте|приглашайте|подключайте|направляйте|присылайте|приезжайте)|без|отказ[а-я]*|отмен[а-я]*)\b', re.I)
_POST_DECLINE = re.compile(r'^\s*(?:(?:мне|нам|пока|сейчас|вообще|совсем|больше|уже|теперь|точно|совершенно)\s+){0,3}(?:не\s+(?:нуж(?:ен|на|но|ны)|требуется|надо|нужно|хочу)|отмен[а-я]*|исключ[а-я]*)\b', re.I)
_POST_REQUEST = re.compile(r'^\s*(?:(?:мне|нам|пока|сейчас|уже|теперь|точно)\s+){0,3}(?:нуж(?:ен|на|но|ны)|хочу|хотим|пригласите|вызовите|прошу)\b', re.I)
_VISIT_IMPERATIVE = re.compile(r'\b(?:приезжайте|приедьте|приезжай|приходите)\b', re.I)


def service_preferences(message: str) -> dict:
    """Conservative explicit wishes, including local negation and corrections.

    Mentions and quoted old messages do not authorize a physical visit. The
    planner still interprets the full dialogue, but cannot override a client's
    explicit refusal or turn a bare mention into consent.
    """
    plain = re.sub(r'«[^»]*»|“[^”]*”|"[^"\n]*"', ' ', message)
    clauses = re.split(r'[.!?;,\n]+|\b(?:но|а|однако|зато|и)\b', plain, flags=re.I)
    def preference(target, imperative=None):
        decisions = []
        for clause in clauses:
            for match in target.finditer(clause):
                before, after = clause[:match.start()][-90:], clause[match.end():]
                negative = list(_DECLINE_CUE.finditer(before))
                positive = [cue for cue in _REQUEST_CUE.finditer(before)
                            if not any(deny.start() <= cue.start() and cue.end() <= deny.end() for deny in negative)]
                cues = [(cue.end(), False) for cue in negative] + [(cue.end(), True) for cue in positive]
                if _POST_DECLINE.search(after) or re.search(r'\bне\s*$', before, re.I):
                    decisions.append(False)
                elif _POST_REQUEST.search(after):
                    decisions.append(True)
                elif cues:
                    # The most recent request/decline in this clause wins.
                    decisions.append(max(cues, key=lambda cue: cue[0])[1])
                elif imperative and imperative.search(match.group(0)):
                    decisions.append(True)
        return decisions[-1] if decisions else None
    human = preference(_HUMAN_TARGET)
    remote = preference(_REMOTE_TARGET)
    visit = preference(_VISIT_TARGET, _VISIT_IMPERATIVE)
    if human is None and remote is not None and not (remote is False and visit is True):
        human = remote
    return {'human': human, 'visit': visit}
FAILURE = re.compile(r'не\s+помог|не\s+получил|не\s+получается|без\s+результат|не\s+сработал|вс[её]\s+ещ[её]\s+не', re.I)
CONFUSION = re.compile(r'не\s+(?:знаю|понимаю|умею)|как\s+(?:определить|понять|узнать)|где\s+(?:посмотреть|найти)', re.I)
CONFIRM = re.compile(r'подтверждаю|согласен|согласна|вс[её]\s+верно|да[, .].*(?:адрес|место|время)', re.I)
DATE = re.compile(r'\b(?:\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?|\d{1,2}:\d{2}|завтра|послезавтра|понедельник[а-я]*|вторник[а-я]*|сред[ауы]|четверг[а-я]*|пятниц[ауы]|суббот[ауы]|воскресень[ея]|\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-я]*)\b', re.I)
SUCCESS = re.compile(r'вс[её]\s+(?:заработало|работает|исправлено)|проблем[ау]\s+(?:решена|устрани)|удалось\s+(?:исправить|наладить)|помогл[оа]\b|заработал[оа]?\b', re.I)
PROHIBITED_PROMISE = re.compile(r'(?:визит|выезд|встреча|бронировани[ея])\s+(?:уже\s+)?подтвержд[её]н|специалист\s+(?:точно\s+)?приедет|гарантиру(?:ю|ем)|(?:сч[её]т|письмо|документ|заказ|отгрузка)\s+(?:уже\s+)?(?:отправлен[аоы]?|оформлен[аоы]?|оплачен[аоы]?)|\b(?:отправлю|вышлю|забронирую)\b', re.I)
UNSOURCED_SPEC = re.compile(r'\d+(?:[.,]\d+)?\s*(?:мм|см|кВт|об/мин|вольт|бар)\b|(?:аналог|ремень|деталь)\s+(?:точно\s+)?(?:совместим|подойд[её]т)', re.I)
_BUSINESS = re.compile(r'\b(?:сч[её]т(?:а|у|ом|е|ы|ов)?|оплат\w*|накладн\w*|спецификац\w*|коммерческ\w*|заказ(?:а|у|ом|е|ы|ов)?|доставк\w*|отгруз\w*|цен(?:а|у|ы|ой|е)|стоимост\w*|сколько\s+сто(?:ит|ят)|поч[её]м|прайс\w*)\b', re.I)
_TECHNICAL = re.compile(r'\b(?:машин\w*|оборудован\w*|упаков\w*|формиров\w*|заклей\w*|конвейер\w*|датчик\w*|рем[её]н\w*|привод\w*|транспортер\w*|короб\w*|лот(?:ок|к\w*))\b', re.I)
_DRIVE_MODEL = re.compile(r'\b(?:VEICHI|ВЕЙЧИ|ВЭЙЧИ)\s+SD\s*[-–]?\s*(\d{3,4})(?![\w-])', re.I)
_DRIVE_ALARM = re.compile(r'\bER\s*[.:-]?\s*(\d{3,4})\b', re.I)
_DRIVE = re.compile(r'\b(?:привод\w*|сервоусилител\w*|сервопривод\w*)\b', re.I)
_STOP = re.compile(r'\b(?:останов\w*|останавлив\w*|замира\w*|завис\w*|вста[её]\w*)\b', re.I)
_STOP_TIMING = re.compile(r'\b(?:периодическ\w*|иногда|нерегулярно|каждый\s+раз\s+после|после\s+захвата|до\s+сброса)\b', re.I)
_SCHOOL = re.compile(r'\b(?:дз|домашн\w*\s+(?:задани\w*|работ\w*)|homework|schoolwork)\b', re.I)
_UNRELATED = re.compile(r'\b(?:математик\w*|алгебр\w*|геометри\w*|тригонометри\w*|сочинени\w*|реферат\w*|стих(?:отворени\w*|и|а|у|ом|е|ов)?|сказк\w*|анекдот\w*|гороскоп\w*|астролог\w*|рецепт\w*|кулинар\w*|приготов(?:ить|лени\w*)|блюд\w*|пицц\w*|борщ\w*)\b', re.I)
_UNAVAILABLE = re.compile(r'не\s+(?:знаю|понимаю|умею)|не\s+могу.{0,35}(?:определ|сфотограф|прочита)|(?:надпис\w*|маркировк\w*).{0,20}(?:ст[её]рт|не\s+вид|отсутств)', re.I)
_MOVING_OPERATION = re.compile(r'на\s+ходу|не\s+(?:выключ\w*|останавлив\w*|останавлива\w*|отключ\w*)|(?:при|во\s+время)\s+работ\w*|работающ\w*|вращающ\w*|движущ\w*|включ[её]нн\w*', re.I)
_PHYSICAL_INTERVENTION = re.compile(r'\b(?:поправ\w*|подправ\w*|подтолк\w*|извле\w*|вытащ\w*|достать|очист\w*|разобра\w*|разбир\w*|смаз\w*|отрегулир\w*|замен\w*|лезть|косну\w*|касаться|касайтесь|трог\w*)\b', re.I)
_BYPASS = re.compile(r'\b(?:обой\w*|обход\w*|закорот\w*|замкн\w*|обман\w*)\b.{0,65}\b(?:защит\w*|блокиров\w*|датчик\w*|концевик\w*|аварийн\w*)\b|\b(?:заблокир\w*|отключ\w*)\b.{0,65}\b(?:защит\w*|блокиров\w*|аварийн\w*)\b', re.I)


def dangerous_operation(text: str) -> bool:
    """Block physical work while energized and defeating safety mechanisms.

    Mere descriptions of a running machine or a faulty sensor are not enough.
    Negated safety instructions are not treated as a request to do the action.
    """
    text = str(text or '')[:6000]
    def positive(matches):
        return any(not re.search(r'(?:\bне|нельзя|запрещено)\s*$', text[max(0, match.start() - 18):match.start()], re.I)
                   and not re.search(r'^\s*(?:строго\s*)?(?:запрещ\w*|недопустим\w*|не\s+(?:допуска\w*|разреш\w*))', text[match.end():match.end() + 45], re.I)
                   for match in matches)
    if positive(_BYPASS.finditer(text)):
        return True
    if not _MOVING_OPERATION.search(text):
        return False
    return positive(_PHYSICAL_INTERVENTION.finditer(text)) or bool(re.search(r'можно.{0,35}рук(?:ой|ами)|рук(?:ой|ами).{0,35}можно', text, re.I))


def _safe_operation_response() -> str:
    return ('Нет. Не поправляйте, не извлекайте и не регулируйте детали на работающем оборудовании и не обходите защиту. '
            'Остановите машину штатным безопасным способом и держитесь вне опасной зоны. '
            'Перед вмешательством необходимы полная остановка и отключение/блокировка источников энергии уполномоченным сотрудником '
            'по правилам предприятия и руководству точной модели. Если безопасно устранить причину нельзя, обратитесь к ответственному специалисту.')


def _business_kind(message: str) -> str:
    if re.search(r'\bсч[её]т(?:а|у|ом|е|ы|ов)?\b', message, re.I):
        return 'invoice'
    if re.search(r'оплат', message, re.I):
        return 'payment'
    if re.search(r'достав|отгруз|накладн', message, re.I):
        return 'shipping'
    if re.search(r'цен[ауыое]|стоимост|коммерческ|сколько\s+сто(?:ит|ят)|поч[её]м|прайс', message, re.I):
        return 'pricing'
    return 'order'


def route_turn(message: str, state: dict, equipment_model=None, attachments=None) -> dict:
    """Route the current request before stale goals can influence retrieval.

    These are conservative topic boundaries, not a free-form moderation model.
    Technical calculations remain in scope; clear school/homework requests do
    not become service work because an equipment model was selected earlier.
    """
    current = str(message or '')[:6000]
    attached = '\n'.join((str(item.get('filename', ''))[:240] + '\n' + str(item.get('text', ''))[:4000])
                         for item in (attachments or [])[:12] if isinstance(item, dict))
    technical = bool(_TECHNICAL.search(current) or detect_models(current)
                     or _DRIVE_MODEL.search(current) or _DRIVE_ALARM.search(current))
    attached_technical = bool(_TECHNICAL.search(attached) or detect_models(attached))
    task = bool(re.search(r'реши\w*|сдела\w*|выполни\w*|напиши\w*|помоги|solve|answer', current, re.I))
    equation = bool(re.search(r'(?:\b[xyz]\b|[xyz][²³^])[^\n]{0,80}=|\b(?:уравнени\w*|equation)\b', current + '\n' + attached, re.I))
    arithmetic = bool(re.fullmatch(r'\s*(?:сколько\s+(?:будет|получится)\s*)?\d+(?:[.,]\d+)?\s*[+*/×÷-]\s*\d+(?:[.,]\d+)?\s*[?=]?\s*', current, re.I))
    generic_coding = bool(re.search(r'\b(?:напиши\w*|создай\w*|сделай\w*|write|build).{0,70}\b(?:код\w*|скрипт\w*|приложени\w*|python|javascript|html|сайт\w*)\b', current, re.I))
    outside = bool(_SCHOOL.search(current) or arithmetic or (generic_coding and not technical) or
                   (_UNRELATED.search(current) and not technical) or
                   (task and not technical and not attached_technical and (_SCHOOL.search(attached) or _UNRELATED.search(attached) or equation)))
    if outside:
        return {'domain': 'outside', 'topic_change': False, 'business_kind': None,
                'unknown_response_count': 0}
    previous_domain = state.get('active_domain')
    if previous_domain not in {'commercial', 'technical'}:
        previous_domain = ('commercial' if _BUSINESS.search(state.get('goal', ''))
                           else 'technical' if _TECHNICAL.search(state.get('goal', ''))
                           else 'commercial' if state.get('intent') == 'commercial'
                           else 'technical' if state.get('goal') else None)
    topic_text = re.sub(r'\bне\s+(?:о|про)\s+(?:сч[её]т\w*|оплат\w*|заказ\w*|цен\w*)', '', current, flags=re.I)
    explicit_domain = 'commercial' if _BUSINESS.search(topic_text) else 'technical' if technical else None
    domain = explicit_domain or previous_domain or 'technical'
    models = detect_models(current)
    old_model = state.get('known_facts', {}).get('equipment_model')
    model_change = bool(domain == 'technical' and old_model and len(models) == 1 and normalize_model(old_model) != models[0])
    explicit_change = bool(re.search(r'\b(?:теперь|друг(?:ой|ая)|нов(?:ый|ая))\s+(?:вопрос|тема|задача)\b', current, re.I))
    changed = bool(state.get('goal') and (model_change or explicit_change or
                   explicit_domain and previous_domain and explicit_domain != previous_domain))
    unknown = bool(_UNAVAILABLE.search(current))
    return {'domain': domain, 'topic_change': changed,
            'business_kind': _business_kind(current) if explicit_domain == 'commercial' else state.get('business_kind') if domain == 'commercial' else None,
            'unknown_response_count': ((0 if changed else state.get('unknown_response_count', 0)) + 1) if unknown else 0}


def _retire_topic(state: dict, message: str):
    # The conversation transcript and real cases are untouched. Keep a bounded
    # private snapshot so changing subject does not erase earlier client facts.
    archived = {key: deepcopy(state.get(key)) for key in
                ('goal', 'known_facts', 'unconfirmed_facts', 'requested_action', 'task_status',
                 'escalation_id', 'attempted_actions', 'source_refs', 'attachments')}
    state['past_topics'] = (list(state.get('past_topics') or []) + [archived])[-8:]
    contacts = {key: value for key, value in state.get('known_facts', {}).items()
                if key in {'company', 'contact_name', 'email', 'phone'}}
    state.update(goal=message[:500], known_facts=contacts, unconfirmed_facts={},
                 fact_provenance={key: value for key, value in state.get('fact_provenance', {}).items() if key in contacts},
                 missing_information=[], requested_action='none', requires_human=False,
                 client_confirmed=False, task_status='', attempted_actions=[], source_refs=[],
                 attachments=[], failed_attempts=0, escalation_id=None, last_response='', unknown_response_count=0)
    state.pop('solution_candidate_id', None)


def _commercial_response(message: str, kind: str | None) -> str:
    if kind == 'invoice':
        match = re.search(r'\bсч[её]т(?:а|у|ом|е)?\s*(?:№|N(?:o)?\.?|#)?\s*([A-Za-zА-Яа-я]*[-/]?\d[\w./-]{0,70})', message, re.I)
        reference = f'счёте №{match.group(1)}' if match else 'счёте'
        next_step = ('Можно приложить сам счёт или попросить проверить его у сотрудника.' if match
                     else 'Для проверки укажите номер и дату счёта или приложите сам документ.')
        return (f'Вопрос о {reference} понял. Подтвердить его актуальность, срок действия или оплату по этому чату пока не могу: '
                'у меня нет подключённого источника актуальных данных по счетам. ' + next_step)
    if kind == 'payment':
        return ('Статус оплаты требует актуальных данных бухгалтерии. По текущему чату я не могу подтвердить поступление денег. '
                'Для проверки подойдут номер счёта и платёжный документ; при необходимости можно запросить ответ сотрудника.')
    if kind == 'shipping':
        return ('Вопрос по доставке или отгрузке понял. Подтверждённого актуального статуса заказа у меня сейчас нет. '
                'Укажите номер заказа, счёта или накладной для проверки; дату отправки и доставки подтвердит ответственный сотрудник.')
    if kind == 'pricing':
        return ('Цена и наличие требуют актуального предложения отдела продаж. По руководству оборудования нельзя подтвердить стоимость или остаток. '
                'Опишите нужную позицию и количество, если они ещё не указаны. Запросить проверку у сотрудника можно отдельно.')
    return ('Вопрос по заказу понял. Для подтверждения его статуса нужны актуальные данные по заказу, которых сейчас нет в этом чате. '
            'Можно указать номер заказа или приложить документ и попросить сотрудника проверить сведения.')


def _unknown_alternative(count: int, message: str) -> str:
    if re.search(r'не\s+могу.{0,35}(?:сфотограф|сним)|нет.{0,25}(?:фото|сним)', message, re.I):
        return ('Снимок сейчас получить нельзя — повторно просить его не буду. Опишите, где находится узел и что именно происходит при работе; '
                'можно переписать доступное обозначение со шильдика. Не разбирайте машину и не угадывайте материал или совместимость.')
    if count >= 3:
        return ('Эти параметры остаются неизвестными, поэтому повторять тот же вопрос не будем. Опишите сам симптом и что изменилось перед его появлением. '
                'Характеристики детали нужно сверять с руководством точной модели или маркировкой; если проверить их самостоятельно не получается, '
                'можно запросить дистанционную консультацию сотрудника.')
    return ('Не нужно угадывать название, материал или цвет детали. Начнём с доступных сведений: общий вид узла и читаемая маркировка, если она есть. '
            'Остановите оборудование, не снимайте защитные кожухи. Если надпись стёрта или снимок сделать нельзя, так и сообщите — выберем другой способ проверки.')


def _repeats_unknown_question(plan, state: dict, message: str) -> bool:
    if not _UNAVAILABLE.search(message) or plan.response_kind == 'technical' or plan.technical_claims:
        return False
    if plan.action not in {'request_information', 'request_photo', 'explain'}:
        return False
    text = plan.response
    repeated_parameter = bool(re.search(r'(?:сообщите|укажите|определите|уточните).{0,140}(?:тип|материал|цвет|маркиров|надпис|открыт|замкнут)', text, re.I))
    tokens = lambda value: set(re.findall(r'\w+', str(value).casefold()))
    now, before = tokens(text), tokens(state.get('last_response', ''))
    near_duplicate = bool(len(before) >= 8 and len(now & before) / max(1, len(now | before)) > .8)
    unavailable_photo = bool(re.search(r'не\s+могу.{0,35}(?:сфотограф|сним)|нет.{0,25}(?:фото|сним)', message, re.I)
                             and re.search(r'(?:пришлите|сделайте|отправьте).{0,50}(?:фото|сним)', text, re.I))
    return repeated_parameter or near_duplicate or unavailable_photo


def _drive_value(pattern, text: str, prefix: str) -> tuple[str, str] | None:
    matches = list(pattern.finditer(text))
    if len({match.group(1) for match in matches}) != 1:
        return None
    match = matches[0]
    if re.search(r'\bне\s*$', text[:match.start()], re.I):
        return None
    return prefix + match.group(1), match.group(0)


def _remember_drive_details(state: dict, message: str) -> dict:
    reported = {}
    for key, pattern, prefix in (('component_model', _DRIVE_MODEL, 'Veichi SD'),
                                 ('alarm_code', _DRIVE_ALARM, 'Er.')):
        finding = _drive_value(pattern, message, prefix)
        if finding:
            value, quote = finding
            reported[key] = value
            state['known_facts'][key] = value
            state['fact_provenance'][key] = {
                'quote': quote, 'origin': 'customer_report', 'turn': state['turn_count'] + 1,
            }
    return reported


def _intermittent_drive_stop(text: str) -> bool:
    return bool(_DRIVE.search(text) and _STOP.search(text) and _STOP_TIMING.search(text))


def _drive_observation(turn: dict) -> str:
    if turn.get('intermittent_stop'):
        return ('При такой остановке полезно различить два состояния: на дисплее привода есть аварийный код '
                'или код отсутствует, а панель машины показывает ожидание шага. Если уже видели показания до сброса, '
                'напишите, что было на обоих экранах. Если остановка повторится сама, снимите показания только с безопасного места. '
                'Не сдвигайте механизм руками, не открывайте шкаф и не запускайте машину специально для проверки.')
    if turn.get('drive_alarm_followup'):
        model, code = turn.get('component_model'), turn['alarm_code']
        intro = f'Модель привода {model} зафиксирована. ' if model else ''
        return (intro + f'Уточните по уже произошедшему эпизоду: {code} появился при включении, при пуске движения '
                'или во время рабочего цикла? Если на штатной панели было дополнительное сообщение, пришлите его. '
                'До сверки с руководством и специалистом не меняйте ноль энкодера и параметры привода.')
    if turn.get('component_model_supplied'):
        return (f'Модель привода {turn["component_model"]} зафиксирована. Опишите симптом и в какой момент рабочего цикла '
                'он проявляется. Если на штатной панели уже был аварийный код или сообщение, пришлите его целиком.')
    return ''


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class FactPatch(StrictModel):
    key: str
    value: str
    quote: str


class TechnicalClaim(StrictModel):
    text: str
    source_index: int
    quote: str


class SafetyPlan(StrictModel):
    stop_remote: bool
    reason: str


class ActionPlan(StrictModel):
    intent: Literal['technical_question', 'identification', 'parts_request', 'human_help', 'remote_consultation', 'onsite_visit', 'scheduling', 'commercial', 'general']
    goal: str
    topic_change: bool
    stage: Literal['diagnosing', 'collecting_details', 'awaiting_staff', 'awaiting_approval', 'waiting_client', 'resolved_candidate']
    facts: list[FactPatch]
    unconfirmed_facts: list[FactPatch]
    missing_information: list[str]
    requested_action: Literal['none', 'remote_consultation', 'onsite_visit', 'commercial']
    requires_human: bool
    client_confirmed: bool
    action: Literal['answer', 'explain', 'request_information', 'request_photo', 'escalate', 'record_details', 'propose_solution_candidate']
    response_kind: Literal['technical', 'identification', 'organizational', 'conversation', 'clarification']
    response: str
    technical_claims: list[TechnicalClaim]
    attempted_actions: list[str]
    department: Literal['service', 'engineering', 'warehouse', 'sales']
    priority: Literal['normal', 'high']
    safety: SafetyPlan
    explanation: str


def initial_state() -> dict:
    return {'schema_version': 1, 'intent': 'general', 'goal': '', 'stage': 'diagnosing',
            'known_facts': {}, 'unconfirmed_facts': {}, 'fact_provenance': {},
            'missing_information': [], 'requested_action': 'none', 'requires_human': False,
            'client_confirmed': False, 'task_status': '', 'turn_count': 0,
            'attempted_actions': [], 'source_refs': [], 'attachments': [],
            'failed_attempts': 0, 'escalation_id': None}


def get_state(session: Session, conversation: Conversation | str) -> ConversationState:
    conversation_id = conversation if isinstance(conversation, str) else conversation.id
    row = session.get(ConversationState, conversation_id)
    if row is None:
        row = ConversationState(conversation_id=conversation_id, state=initial_state())
        session.add(row)
        session.flush()
    return row


def public_state(value: dict | ConversationState) -> dict:
    state = value.state if isinstance(value, ConversationState) else value
    keys = ('intent', 'goal', 'stage', 'known_facts', 'unconfirmed_facts', 'missing_information',
            'requested_action', 'requires_human', 'client_confirmed', 'escalation_id', 'task_status', 'turn_count')
    return {key: deepcopy(state.get(key, initial_state().get(key))) for key in keys}


def _local_facts(message: str) -> list[dict]:
    facts = []
    def add(key, value, quote=None):
        facts.append({'key': key, 'value': value.strip()[:600], 'quote': (quote or value).strip()[:1000]})
    models = detect_models(message)
    if len(models) == 1:
        add('equipment_model', models[0], message)
    for key, pattern in [('email', r'[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}'),
                         ('phone', r'(?<!\d)\+?\d[\d ()-]{8,20}\d(?!\d)'),
                         ('serial_number', r'(?:серийн\w*\s*(?:номер)?|s/n|serial)\s*[:№#]?\s*([A-Za-zА-Яа-я0-9_/-]{3,80})')]:
        match = re.search(pattern, message, re.I)
        if match:
            value = match.group(1) if key == 'serial_number' else match.group(0)
            if key == 'serial_number' and value.casefold() in {'номер', 'знаю', 'неизвестен', 'отсутствует'}:
                continue
            add(key, value, match.group(0))
    if re.search(r'адрес\s*[:—-]\s*\S|\bул\.|улиц[аеы]|проспект|переулок|\bдом\s+\d', message, re.I):
        address = re.sub(r'^.*?адрес\s*[:—-]\s*', '', message, flags=re.I)
        add('address', address[:600], address[:600])
    if DATE.search(message):
        add('preferred_datetime', message[:600], message[:600])
    match = re.search(r'(?:ООО|АО|ИП)\s+[«"A-Za-zА-Яа-я][^,;\n]{1,100}', message)
    if match:
        add('company', match.group(0))
    if CONFIRM.search(message):
        add('client_confirmation', message[:600])
        if re.search('адрес|место', message, re.I):
            add('location_confirmed', message[:600])
    return facts


def local_plan(context: dict) -> dict:
    """Conservative offline routing. It makes no machine-specific inferences."""
    message, state, sources = context['message'], context['state'], context['sources']
    facts = _local_facts(message)
    known = dict(state['known_facts']) | {p['key']: p['value'] for p in facts}
    terminal_handoff = state.get('task_status') in {'resolved', 'cancelled'}
    requested = state.get('requested_action', 'none') if not terminal_handoff else 'none'
    intent, action, kind = 'technical_question', 'request_information', 'clarification'
    stage = 'diagnosing' if terminal_handoff else state.get('stage', 'diagnosing')
    needs_human = False if terminal_handoff else state.get('requires_human', False)
    preferences = service_preferences(message)
    if preferences['human'] is False:
        requested, needs_human = 'none', False
    elif preferences['visit'] is False and requested == 'onsite_visit':
        requested = 'remote_consultation'
    if preferences['visit'] is True:
        requested, intent, needs_human = 'onsite_visit', 'onsite_visit', True
    elif preferences['human'] is True:
        requested, intent, needs_human = 'remote_consultation', 'human_help', True
    elif re.search(r'сч[её]т|заказ|купить|стоимость|цен[ау]|коммерческ', message, re.I):
        # A question about an invoice is not consent to open a staff task.
        requested, intent, kind = 'none', 'commercial', 'organizational'
    elif requested != 'none':
        intent = 'scheduling' if requested == 'onsite_visit' else ('commercial' if requested == 'commercial' else 'remote_consultation')
    elif re.search(r'запчаст|зип|детал|рем[её]н|вал[ыа]?\b|присоск', message, re.I):
        intent = 'parts_request'
    elif SUCCESS.search(message) and not FAILURE.search(message):
        intent, kind, action = 'general', 'conversation', 'propose_solution_candidate'
    elif CONFUSION.search(message):
        intent = 'identification'
    elif re.fullmatch(r'\s*(?:здравствуй\w*|привет|добрый\s+(?:день|вечер|утро))[!. ]*', message, re.I):
        intent, kind, action = 'general', 'conversation', 'answer'
    previous_failures = 0 if terminal_handoff else state.get('failed_attempts', 0)
    if previous_failures + bool(FAILURE.search(message)) >= 2:
        needs_human, requested = True, requested if requested != 'none' else 'remote_consultation'
    missing = []
    if requested == 'onsite_visit':
        missing = [key for key in ('equipment_model', 'address', 'preferred_datetime') if not known.get(key)]
    elif requested in {'remote_consultation', 'commercial'}:
        missing = [key for key in ('contact_name',) if not known.get('contact_name') and not known.get('email') and not known.get('phone')]
    elif not known.get('equipment_model') and intent not in {'general', 'commercial'}:
        missing = ['equipment_model']
    claims = []
    if dangerous_operation(message):
        # A question about an unsafe intervention needs a clear stop response,
        # not a random maintenance excerpt or an invented parameter.
        kind, action, stage = 'conversation', 'answer', 'diagnosing'
        response = _safe_operation_response()
    elif requested != 'none' or needs_human:
        needs_human, kind, action = True, 'organizational', 'escalate'
        stage = 'awaiting_approval' if requested == 'onsite_visit' else 'awaiting_staff'
        response = 'Сохраню сведения в обращении для сотрудника. Время и условия визита требуют отдельного согласования.' if requested == 'onsite_visit' else 'Передам вопрос сотруднику для дистанционной консультации. Повторять историю не потребуется.'
    elif intent == 'commercial':
        action, kind, stage = 'answer', 'organizational', 'diagnosing'
        response = _commercial_response(message, context.get('current_turn', {}).get('business_kind') or _business_kind(message))
    elif action == 'propose_solution_candidate':
        response = 'Хорошо, что удалось добиться результата. Уточните, какое именно действие помогло: сохраним его для проверки сотрудником.'
        stage = 'resolved_candidate'
    elif kind == 'conversation':
        response = 'Здравствуйте! Расскажите, какая помощь нужна с оборудованием ПОСТЭКС.'
    elif CONFUSION.search(message):
        action, kind = 'request_photo', 'identification'
        stage = 'collecting_details'
        response = _unknown_alternative(context.get('current_turn', {}).get('unknown_response_count', 1), message)
    elif sources:
        kind, action, stage = 'technical', 'answer', 'diagnosing'
        for n, source in enumerate(sources[:3], 1):
            excerpt = source.get('text', '')[:700].strip()
            if excerpt:
                claims.append({'text': excerpt, 'quote': excerpt, 'source_index': n})
        response = ''
    else:
        action, stage = 'request_photo', 'collecting_details'
        if known.get('equipment_model'):
            response = 'Модель сохранил. Чтобы предметно разобраться в вашем вопросе, пришлите фото узла или надписи на детали и опишите, что именно не получается. Пока не будем угадывать характеристики или подбирать замену.'
        else:
            response = 'Начнём с определения оборудования: пришлите фото шильдика или перепишите обозначение модели. Не разбирайте работающую машину. Затем проверим ваш вопрос по доступным материалам; при необходимости подключим сотрудника.'
    return {'intent': intent, 'goal': state.get('goal') or message[:500], 'topic_change': False,
            'stage': stage, 'facts': facts, 'unconfirmed_facts': [], 'missing_information': missing,
            'requested_action': requested, 'requires_human': needs_human,
            'client_confirmed': bool(CONFIRM.search(message)), 'action': action,
            'response_kind': kind, 'response': response, 'technical_claims': claims,
            'attempted_actions': [message[:600]] if FAILURE.search(message) else [],
            'department': 'sales' if requested == 'commercial' else 'service', 'priority': 'normal',
            'safety': {'stop_remote': False, 'reason': ''},
            'explanation': 'Локальный ограниченный режим: сохранение фактов, поиск выдержек и внутренняя маршрутизация без догадок о параметрах.'}


def _validate_fact(patch: FactPatch, message: str) -> bool:
    if patch.key not in FACT_KEYS or not patch.quote.strip() or len(patch.value) > 600:
        return False
    if patch.quote.casefold() not in message.casefold():
        return False
    if patch.key == 'address' and CONFIRM.search(patch.value) and not re.search(r'\d|ул\.|улица|проспект|переулок', patch.value, re.I):
        return False
    if patch.key == 'equipment_model':
        return normalize_model(patch.value) in detect_models(patch.quote)
    return patch.value.casefold() in patch.quote.casefold()


def _reported_checks(message):
    """Keep literal customer reports; never treat AI advice as completed work."""
    match = re.search(r'\b(?:проверил[аи]?|осмотрел[аи]?|очистил[аи]?|заменил[аи]?|переподключил[аи]?)\b', message, re.I)
    if match and not re.search(r'\bне\s*$', message[:match.start()], re.I):
        return [message[:600]]
    return []


def _source_refs(sources: list[dict]) -> list[dict]:
    return [{k: source[k] for k in ('document_id', 'solution_id', 'equipment_id', 'page', 'title', 'model', 'url', 'source_type') if k in source} for source in sources[:12]]


def _retrieve(session: Session, message: str, state: dict, equipment_model, document_id, include_internal, inspection_out=None) -> tuple[list, str | None, bool]:
    from .ai import _verified
    from .knowledge import search_knowledge
    inferred = detect_models(message)
    machine = normalize_model(equipment_model) if equipment_model else state['known_facts'].get('equipment_model')
    if not machine and len(inferred) == 1:
        machine = inferred[0]
    conflict = len(inferred) > 1 or bool(machine and inferred and any(m != machine for m in inferred))
    if conflict:
        return [], machine, True
    query = message + ' ' + state.get('goal', '')[:600]
    from .manual_context import inspect_manuals
    inspection = inspect_manuals(session, query, model=machine, document_id=document_id,
        include_internal=include_internal, limit=6)
    if inspection_out is not None:
        inspection_out.update({k: v for k, v in inspection.items() if k != 'sources'})
    from .semantic_search import search_semantic
    sources = list(inspection['sources'])
    sources += search_semantic(session, query, include_internal=include_internal,
        equipment_model=machine, document_id=document_id, limit=6)
    sources += search_knowledge(session, query, model=machine, document_id=document_id, include_internal=include_internal, limit=6)
    sources += [s for s in _verified(session, query, public_only=not include_internal, semantic=False)
                if not machine or (s.get('model') and normalize_model(s['model']) == machine)]
    if include_internal and document_id is None:
        from .equipment_context import registry_sources
        sources += registry_sources(session, message, facts=state.get('known_facts'), model=machine)
    sources = list({(source.get('document_id'), source.get('solution_id'), source.get('equipment_id'), source.get('page')): source
                    for source in reversed(sources)}.values())[::-1]
    trusted = []
    for source in sources:
        if source.get('document_id'):
            document = session.get(Document, source['document_id'])
            from .source_access import can_use_for_answer
            if not can_use_for_answer(document, include_internal):
                continue
            if 'непроверен' in document.category.casefold() or 'история переписки' in document.category.casefold():
                continue
            # Category is editable display metadata. Trust follows the original
            # import provenance even after rename/reindex or an incomplete old
            # index that does not yet retain source_kind.
            if (document.indexing_details or {}).get('source_kind') == 'unreviewed_history':
                continue
            history_origin = session.scalar(select(AppSetting.key).where(
                AppSetting.key.startswith('history_import:'),
                AppSetting.value['document_id'].as_integer() == document.id))
            if history_origin:
                continue
        trusted.append(source)
    models = {m for s in trusted for m in (s.get('models') or detect_models(s.get('model', '')))}
    if not machine and len(models) > 1:
        return [], None, True
    return trusted[:8], machine, False


def _respond(session: Session, message: str, *, conversation: Conversation | None = None,
            equipment_model: str | None = None, document_id: int | None = None,
            include_internal: bool = False, ai_model: str | None = None, history: list | None = None,
            attachments: list | None = None, planner=None, use_knowledge: bool = True, allow_actions: bool = True,
            require_model: bool = False) -> dict:
    """Process one turn. Only a persisted, owned conversation may create actions.

    The API must check conversation ownership before calling this function.
    A missing conversation means Playground/dry run: proposals have no effects.
    """
    started, request_id = time.monotonic(), str(uuid.uuid4())
    row = get_state(session, conversation) if conversation is not None else None
    expected_revision = None
    conversation_id = conversation.id if conversation is not None else None
    if row is not None:
        session.refresh(row)
        expected_revision = row.revision
    state = initial_state() | deepcopy(row.state if row is not None else {})
    state['known_facts'] = dict(state.get('known_facts') or {})
    routing = route_turn(message, state, equipment_model, attachments)
    unsafe_operation = dangerous_operation(message)
    routing['unsafe_operation'] = unsafe_operation
    if routing['domain'] == 'outside':
        content = ('Я помощник компании ПОСТЭКС: помогаю с упаковочным оборудованием, запчастями и сервисом. '
                   'Школьные задания и другие задачи вне этой области не выполняю. '
                   'Если нужна помощь по оборудованию компании, опишите вопрос.')
        state['turn_count'] += 1
        state['last_request_id'] = request_id
        if row is not None:
            row.state = deepcopy(state)
            row.updated_at = utcnow()
            session.commit()
        return {'content': content, 'sources': [], 'engine': 'search_only', 'planning_mode': 'company_policy',
                'action': 'answer', 'proposed_action': 'answer', 'missing_information': [],
                'trace': [{'step': 'company_scope', 'status': 'outside_scope',
                           'detail': 'Запрос вне обслуживания оборудования; поиск, модель и рабочие действия не запускались'}],
                'equipment_model': state['known_facts'].get('equipment_model'), 'state': public_state(state),
                'escalation': None, 'request_id': request_id, 'latency_ms': round((time.monotonic() - started) * 1000),
                'usage': None, 'model': None, 'provider_response_id': None}
    if routing['topic_change']:
        _retire_topic(state, message)
    state['active_domain'] = routing['domain']
    state['unknown_response_count'] = routing['unknown_response_count']
    state['business_kind'] = routing['business_kind']
    if routing['domain'] == 'commercial':
        state['failed_attempts'] = 0
        if state.get('requested_action') not in {'none', 'commercial'}:
            state['requested_action'], state['requires_human'] = 'none', False
    # An explicit UI model is a user choice, never a verified machine parameter.
    if routing['domain'] != 'commercial' and equipment_model and len(detect_models(equipment_model)) == 1:
        state['known_facts'].setdefault('equipment_model', normalize_model(equipment_model))
    drive_report = _remember_drive_details(state, message[:6000]) if routing['domain'] == 'technical' else {}
    for report in _reported_checks(message):
        if report not in state['attempted_actions']:
            state['attempted_actions'] = (state['attempted_actions'] + [report])[-30:]
    manual_inspection = {}
    sources, machine, conflict = _retrieve(session, message, state, equipment_model, document_id, include_internal, inspection_out=manual_inspection) if use_knowledge and routing['domain'] != 'commercial' else ([], state['known_facts'].get('equipment_model'), False)
    from .attachment_context import conversation_attachment_context
    attachment_memory = conversation_attachment_context(session, conversation, attachments)
    attachment_context = []
    attachment_budget = 12000
    for item in attachment_memory[:12]:
        if not isinstance(item, dict):
            continue
        bounded = {k: item[k] for k in ('id', 'filename', 'kind', 'engine', 'warnings', 'url',
            'received','processing_status','content_processed','analysis_kind','video','current_turn','source_message_id') if k in item}
        bounded['text'] = str(item.get('text', ''))[:min(attachment_budget, 4000)]
        attachment_budget -= len(bounded['text'])
        attachment_context.append(bounded)
    if routing['domain'] == 'technical':
        routing['web_lookup_allowed'] = bool(use_knowledge and document_id is None and not conflict and not unsafe_operation)
        prior_user_turns = [str(item.get('content', ''))[:3000] for item in (history or [])[-10:]
                            if isinstance(item, dict) and item.get('role') == 'user'] if not routing['topic_change'] else []
        used_attachments = [item['text'] for item in attachment_context if item.get('content_processed') and
                            (not routing['topic_change'] or item.get('current_turn'))]
        known = state['known_facts']
        inferred_alarm = next((finding[0] for text in [*reversed(prior_user_turns), *used_attachments]
                               if (finding := _drive_value(_DRIVE_ALARM, text, 'Er.'))), None)
        if component_model := known.get('component_model'):
            routing['component_model'] = component_model
            routing['component_model_supplied'] = 'component_model' in drive_report
        if alarm_code := known.get('alarm_code') or inferred_alarm:
            routing['alarm_code'] = alarm_code
            routing['drive_alarm_followup'] = bool(drive_report or re.search(r'энкодер\w*', message, re.I))
        if _intermittent_drive_stop(message) or ('component_model' in drive_report and any(
                _intermittent_drive_stop(text) for text in [state.get('goal', ''), *prior_user_turns])):
            routing['intermittent_stop'] = True
    observation = _drive_observation(routing)
    planner_state = deepcopy(state)
    planner_state.pop('past_topics', None)
    from .response_integrity import source_conflicts, reply_violation
    conflicts = source_conflicts(sources)
    context = {'message': message[:6000], 'state': planner_state, 'sources': sources, 'current_turn': routing,
               'manual_inspection': manual_inspection,
               'source_conflicts': conflicts,
               'recent_history': [] if routing['topic_change'] else list(history or [])[-10:], 'attachments': attachment_context,
               'now_utc': datetime.now(timezone.utc).isoformat(),
               'capabilities': {'internal_escalation': conversation is not None and allow_actions, 'outbound_mail': False, 'booking': False}}
    # A first turn must survive provider rollback. It must also release its
    # SQLite INSERT/write lock before an external call can take many seconds.
    # The context above is a bounded snapshot; model inference needs no open
    # database transaction. API callers commit the owned user turn beforehand.
    session.commit()
    engine, meta, ai_error = 'search_only', {}, None
    try:
        if planner is not None:
            raw = planner.plan(context) if hasattr(planner, 'plan') else planner(context)
            meta = {'provider': 'injected'}
        else:
            from .ai import generate_plan
            raw, meta = generate_plan(session, context, ai_model)
            if raw is None:
                ai_error = meta.get('error')
                if not require_model:
                    raw = local_plan(context)
            else:
                engine = 'openai'
                sources = meta.pop('answer_sources', sources)
                conflicts = source_conflicts(sources)
        plan = ActionPlan.model_validate(raw) if raw is not None else None
    except Exception as exc:
        from .ai import safe_api_error
        ai_error = safe_api_error(exc)
        plan = None if require_model else ActionPlan.model_validate(local_plan(context))
        engine = 'search_only'
    def model_failure(code, explanation, *, counted=False):
        nonlocal row
        diagnostic = dict(meta.get('provider_diagnostics') or {'provider_called': False})
        if counted and engine == 'openai':
            diagnostic.update(status=code, stage='reply_validation', failure_kind=code)
        logging.getLogger('uvicorn.error.posteks.ai').warning(
            'ai_reply_failed request_id=%s invocation_id=%s stage=%s code=%s',
            request_id, diagnostic.get('invocation_id', '-'), diagnostic.get('stage', 'unknown'), code)
        if conversation_id is not None:
            session.rollback()
            fresh = session.get(ConversationState, conversation_id, populate_existing=True)
            if fresh is None or fresh.revision != expected_revision:
                raise StaleDataError('Conversation changed while the response was being prepared')
            row = fresh
        if not counted:
            for patch in _local_facts(message):
                if patch['key'] in FACT_KEYS:
                    state['known_facts'][patch['key']] = patch['value']
            state['turn_count'] += 1
        state['goal'] = state.get('goal') or message[:500]
        state['last_request_id'] = request_id
        if row is not None:
            row.state = deepcopy(state)
            row.updated_at = utcnow()
            session.commit()
        content = explanation + ' Ответ языковой модели не получен или не прошёл проверку; техническую консультацию я не подменяю. Сообщение и вложения сохранены в этом диалоге.'
        if unsafe_operation:
            content += '\n\n' + _safe_operation_response()
        elif observation:
            content += '\n\n' + observation
        return {'content': content, 'sources': [], 'engine': 'unavailable', 'planning_mode': 'provider_error',
            'action': 'retry', 'proposed_action': 'retry', 'missing_information': [], 'state': public_state(state),
            'equipment_model': state['known_facts'].get('equipment_model'), 'escalation': None,
            'ai_error': explanation, 'error': {'code': code, 'message': explanation, 'retryable': True},
            'model': meta.get('model') or ai_model, 'provider_response_id': meta.get('response_id'),
            'provider_diagnostics': diagnostic,
            'request_id': request_id, 'usage': meta.get('usage'),
            'latency_ms': round((time.monotonic() - started) * 1000),
            'trace': [{'step': 'model', 'status': 'failed', 'detail': code}], 'source_conflicts': conflicts}
    if plan is None:
        return model_failure(meta.get('error_code', 'provider_error'), ai_error or 'Модель сейчас недоступна.')
    if routing['domain'] == 'commercial':
        # Current subject takes precedence even if the model repeats a previous
        # technical answer or invents that a staff request was already made.
        preferences_now = service_preferences(message)
        prior_commercial_handoff = bool(state.get('requires_human') and state.get('requested_action') == 'commercial')
        explicit_staff = bool(preferences_now['human'] is True or re.search(
            r'\b(?:передайте|соедините|позовите|запросите).{0,45}(?:отдел\w*|менеджер\w*|бухгалтер\w*)', message, re.I))
        handoff = explicit_staff or prior_commercial_handoff
        plan = plan.model_copy(update={'intent': 'commercial', 'goal': message[:500] if routing['topic_change'] else state.get('goal') or message[:500],
            'topic_change': routing['topic_change'], 'response_kind': 'organizational', 'technical_claims': [],
            'response': plan.response if engine == 'openai' else _commercial_response(message, routing['business_kind']), 'requires_human': handoff,
            'requested_action': 'commercial' if handoff else 'none', 'action': 'escalate' if handoff else 'answer',
            'stage': 'awaiting_staff' if handoff else 'diagnosing', 'missing_information': [], 'department': 'sales',
            'facts': [patch for patch in plan.facts if patch.key not in {'address', 'preferred_datetime', 'location_confirmed', 'client_confirmation'}],
            'unconfirmed_facts': [], 'attempted_actions': [], 'safety': SafetyPlan(stop_remote=False, reason='')})
    elif engine != 'openai' and _repeats_unknown_question(plan, state, message) and not plan.safety.stop_remote:
        plan = plan.model_copy(update={'response': _unknown_alternative(routing['unknown_response_count'], message),
            'action': 'explain', 'response_kind': 'identification', 'technical_claims': [],
            'missing_information': [key for key in plan.missing_information if key not in (state.get('missing_information') or [])]})
    unsafe_operation = unsafe_operation or dangerous_operation(plan.response) or any(dangerous_operation(c.text) for c in plan.technical_claims)
    if (observation and not unsafe_operation and not plan.technical_claims and not plan.safety.stop_remote
            and not plan.requires_human and plan.requested_action == 'none'
            and plan.response_kind != 'organizational'
            and plan.action in {'answer', 'explain', 'request_information', 'request_photo'}):
        plan = plan.model_copy(update={'response': observation, 'stage': 'collecting_details',
            'response_kind': 'clarification', 'action': 'request_information', 'missing_information': []})
    if unsafe_operation:
        # This gate applies to injected/provider plans as well as the local
        # planner. A source citation must not authorize reaching into a running
        # machine or defeating a protective mechanism.
        plan = plan.model_copy(update={'response': _safe_operation_response(),
            'response_kind': 'conversation', 'action': 'answer', 'technical_claims': [],
            'attempted_actions': [], 'missing_information': [],
            'safety': SafetyPlan(stop_remote=False, reason='Недопустимое вмешательство в работающую машину или обход защиты')})
    if conversation_id is not None:
        # Provider bookkeeping may commit or roll back its own transaction.
        # End any remaining read transaction, then compare a fresh state with
        # the revision whose snapshot was sent to the planner. This conflict
        # must reach the API as 409, never become a local fallback overwrite.
        session.rollback()
        row = session.get(ConversationState, conversation_id, populate_existing=True)
        if row is None or row.revision != expected_revision:
            raise StaleDataError('Conversation changed while the response was being prepared')
    rejected_facts = []
    for patch in plan.facts[:24]:
        if _validate_fact(patch, message):
            value = normalize_model(patch.value) if patch.key == 'equipment_model' else patch.value
            state['known_facts'][patch.key] = value
            state.setdefault('fact_provenance', {})[patch.key] = {'quote': patch.quote[:1000], 'origin': 'customer_report', 'turn': state['turn_count'] + 1}
        else:
            rejected_facts.append(patch.key)
    # Model suggestions stay explicitly unconfirmed and cannot authorize actions.
    unconfirmed = dict(state.get('unconfirmed_facts') or {})
    for patch in plan.unconfirmed_facts[:16]:
        if patch.key in FACT_KEYS and patch.value and len(patch.value) <= 600 and patch.quote and any(patch.quote.casefold() in text.casefold() for text in [message] + [a.get('text', '') for a in attachment_context]):
            unconfirmed[patch.key] = patch.value
    state['unconfirmed_facts'] = unconfirmed
    if not state.get('goal'):
        state['goal'] = message[:500]
    elif plan.topic_change and plan.goal.strip() and plan.goal.casefold() in message.casefold():
        state['goal'] = plan.goal[:500]
    preferences = service_preferences(message)
    human_declined = preferences['human'] is False
    visit_declined = preferences['visit'] is False
    contradictory_wishes = human_declined and preferences['visit'] is True
    requested = plan.requested_action
    if requested == 'onsite_visit' and not (preferences['visit'] is True or state.get('requested_action') == 'onsite_visit'):
        requested = 'remote_consultation'
    terminal_handoff = state.get('task_status') in {'resolved', 'cancelled'}
    if requested == 'none' and state.get('requested_action') != 'none' and not plan.topic_change and not terminal_handoff:
        requested = state['requested_action']
    if preferences['human'] is True and requested == 'none':
        requested = 'remote_consultation'
    if preferences['visit'] is True:
        requested = 'onsite_visit'
    state['failed_attempts'] = (0 if terminal_handoff else state.get('failed_attempts', 0)) + bool(FAILURE.search(message))
    needs_human = plan.requires_human or preferences['human'] is True or plan.safety.stop_remote or state['failed_attempts'] >= 2
    if requested != 'none' and state.get('task_status') not in {'resolved', 'cancelled'}:
        needs_human = True
    existing_escalation = session.get(Escalation, state['escalation_id']) if state.get('escalation_id') else None
    if visit_declined:
        if state.get('requested_action') == 'onsite_visit' and existing_escalation:
            requested = 'remote_consultation'
        elif plan.requested_action == 'onsite_visit' and preferences['human'] is not True:
            requested, needs_human = 'none', bool(plan.safety.stop_remote)
        elif requested == 'onsite_visit':
            requested = 'remote_consultation' if preferences['human'] is True else 'none'
    if human_declined or contradictory_wishes:
        requested, needs_human = 'none', False
    terminal_followup = bool(existing_escalation and existing_escalation.status in {'resolved', 'cancelled'} and not
        (preferences['human'] is True or preferences['visit'] is True or (plan.requires_human and plan.action == 'escalate')))
    if terminal_followup:
        requested = 'none'
        needs_human = False
    state.update(intent=plan.intent, stage=plan.stage, requested_action=requested,
                 requires_human=needs_human, client_confirmed=bool(state.get('client_confirmed') or (plan.client_confirmed and CONFIRM.search(message))),
                 turn_count=state['turn_count'] + 1)
    state['missing_information'] = [key[:160] for key in plan.missing_information[:6] if not state['known_facts'].get(key)]
    if terminal_followup and not plan.topic_change:
        # A completed task has no pending scheduling/contact form. Keep its
        # history and facts, but require a new goal/action before collecting
        # those details again; this applies to every follow-up, not a phrase.
        state['stage'], state['missing_information'] = 'diagnosing', []
    if requested != 'onsite_visit' and (human_declined or visit_declined):
        state['missing_information'] = [key for key in state['missing_information']
                                        if key not in {'address', 'preferred_datetime', 'location_confirmed'}]
    if human_declined:
        state['stage'] = 'collecting_details' if contradictory_wishes else 'diagnosing'
        state['missing_information'] = ['consultation_format'] if contradictory_wishes else []
    elif visit_declined and not needs_human:
        state['stage'] = 'diagnosing'
    for attempt in plan.attempted_actions[:8]:
        if attempt and attempt.casefold() in message.casefold() and attempt not in state['attempted_actions'] and not dangerous_operation(attempt):
            state['attempted_actions'] = (state['attempted_actions'] + [attempt[:600]])[-30:]
    new_refs = _source_refs(sources)
    # Retain identifiers across long conversations, never private source excerpts.
    refs = state.get('source_refs', []) + new_refs
    state['source_refs'] = list({(r.get('document_id'), r.get('solution_id'), r.get('equipment_id'), r.get('page'),
                                 r.get('url') if r.get('source_type') == 'official_web' else None): r
                                 for r in refs}.values())[-30:]
    remembered_files = {a['id']: a for a in state.get('attachments', []) if a.get('id')}
    for attachment in attachment_context:
        if isinstance(attachment, dict) and attachment.get('id'):
            remembered_files[attachment['id']] = {k: deepcopy(v) for k, v in attachment.items()
                if k not in {'text', 'current_turn', 'source_message_id'}}
    state['attachments'] = list(remembered_files.values())[-30:]
    claims, invalid_evidence, ambiguous_table = [], False, False
    for claim in plan.technical_claims[:8]:
        index = claim.source_index - 1
        if (not 0 <= index < len(sources) or not claim.quote.strip()
            or claim.quote not in sources[index].get('text', '') or claim.text not in claim.quote):
            invalid_evidence = True
            continue
        source_models = sources[index].get('models') or detect_models(sources[index].get('model', ''))
        if (machine and len(set(source_models)) > 1
            and sources[index].get('table_kind') != 'parts_inventory'
            and re.search(r'\d+\s*(?:мм|см|кВт|бар|м/мин|шт)', claim.text, re.I)
            and set(detect_models(claim.quote)) != {machine}):
            invalid_evidence, ambiguous_table = True, True
            continue
        claims.append(f'«{claim.text[:1500]}» [{claim.source_index}]')
    content, action = plan.response.strip()[:6000], plan.action
    if require_model and engine == 'openai' and not unsafe_operation:
        violation = reply_violation(plan, sources, bool(claims) and not invalid_evidence, conflicts, message)
        if violation:
            return model_failure(*violation, counted=True)
        if PROHIBITED_PROMISE.search(content):
            return model_failure('unperformed_action', 'Модель заявила о действии, которого система не выполняла.', counted=True)
        if plan.technical_claims and not re.search(r'\[\d+\]', content):
            content += '\n\nИсточники: ' + ', '.join(f'[{c.source_index}]' for c in plan.technical_claims)
    organizational = (plan.response_kind == 'organizational' or plan.intent in
                      {'human_help', 'remote_consultation', 'onsite_visit', 'scheduling', 'commercial'})
    if organizational and engine != 'openai':
        # There is no booking, pricing or outgoing-mail tool. Compose this part
        # from actual workflow state rather than trusting free-form model claims.
        content, action = ((_commercial_response(message, routing['business_kind']), 'answer') if routing['domain'] == 'commercial'
                           else ('Сведения сохранены в этом диалоге. Пожелания по встрече и условиям работ можно передать сотруднику на согласование.', 'record_details'))
    if engine != 'openai' and (plan.response_kind == 'technical' or plan.technical_claims):
        if claims and not invalid_evidence and not conflict:
            content, action = 'В источниках по вашему вопросу указано:\n\n' + '\n\n'.join(claims), 'answer'
            if any(s.get('table_binding') == 'shared_manual' for s in sources):
                content = ('В общем руководстве нескольких модификаций есть следующие позиции. '
                    'Это перечень документа, а не подтверждение комплектации конкретной машины:\n\n' + '\n\n'.join(claims))
            absent = manual_inspection.get('parts_check', {}).get('unmatched_markings', [])
            if manual_inspection.get('available') and absent:
                labels = ', '.join(str(value) for value in absent)[:400]
                content = (f'В доступном тексте руководства выбранной модели маркировка «{labels}» не найдена. '
                    'Поэтому совместимость предложенной детали пока не подтверждена. Само отсутствие маркировки '
                    'в тексте не доказывает, что деталь нигде не используется.\n\n' + content)
        else:
            content, action = 'Точных подтверждённых данных для такой рекомендации пока недостаточно. Пришлите чёткое фото маркировки или нужного узла; проверим сведения перед выбором детали или регулировкой.', 'request_photo'
    elif engine != 'openai' and (PROHIBITED_PROMISE.search(content) or UNSOURCED_SPEC.search(content)):
        content, action = 'Я сохранил сведения. Технические параметры и условия работ необходимо подтвердить; пока не буду предлагать неподтверждённое решение.', 'request_information'
    if conflict and requested == 'none':
        content, action = 'В сообщении или выбранной модели указаны разные модификации. Уточните одну точную модель, чтобы не смешать характеристики оборудования.', 'request_information'
        state['missing_information'] = ['equipment_model']
    if ambiguous_table:
        content, action = 'На странице есть данные нескольких модификаций, и связь значения с нужным столбцом неоднозначна. Откройте исходную страницу: перед рекомендацией нужно подтвердить, к какой модели относится значение.', 'request_information'
    if engine != 'openai' and terminal_followup and not plan.topic_change and routing['domain'] != 'commercial':
        status = 'закрыт' if existing_escalation.status == 'resolved' else 'отменён'
        content, action = (f'Предыдущий запрос №{existing_escalation.id} {status}. Сообщение сохранено в этом диалоге. Если появилась новая задача, опишите её; повторно передать вопрос сотруднику можно по вашей просьбе.', 'answer')
        claims, invalid_evidence, ambiguous_table = [], False, False
    escalation = None
    if human_declined and not contradictory_wishes:
        content, action = 'Сотрудника подключать не будем. Продолжим помощь в этом диалоге; при необходимости вы сможете запросить консультацию позже.', 'answer'
        if conversation is not None and allow_actions and existing_escalation and existing_escalation.status not in {'resolved', 'cancelled'}:
            from .escalations import _synchronize_workflow
            from .workflows import record_event
            existing_escalation.status = 'cancelled'
            _synchronize_workflow(session, existing_escalation)
            state['task_status'], state['stage'] = 'cancelled', 'diagnosing'
            record_event(session, 'CUSTOMER', 'escalation.cancelled', 'escalation', existing_escalation.id)
            content = f'Запрос №{existing_escalation.id} отменён по вашему сообщению. Продолжим помощь в этом диалоге.'
    elif contradictory_wishes:
        content, action = 'Уточните пожелание: нужен выезд сотрудника или хотите продолжить без подключения специалиста? Пока заявку на выезд не оформляю.', 'request_information'
    elif visit_declined and not needs_human:
        content, action = 'Выезд не оформляю. Продолжим дистанционно в этом диалоге; если понадобится сотрудник, скажите об этом.', 'answer'
    if conversation is not None and allow_actions and needs_human:
        from .escalations import create_or_update_escalation, public_escalation
        reason = 'Клиент запросил сотрудника' if preferences['human'] is True else (plan.safety.reason or 'Требуется согласование или проверка сотрудником')
        escalation = create_or_update_escalation(session, conversation, state, reason=reason,
            requested_action=requested if requested != 'none' else 'remote_consultation',
            department={'service': 'Сервис', 'engineering': 'КБ', 'warehouse': 'Склад', 'sales': 'Продажи'}[plan.department], priority=plan.priority, client_message=True)
        session.flush()
        state['escalation_id'], state['task_status'] = escalation.id, escalation.status
        state['stage'] = 'awaiting_approval' if requested == 'onsite_visit' else 'awaiting_staff'
        action = 'escalate'
        content = f'Запрос №{escalation.id} передан сотруднику. История и полученные сведения сохранены в этом диалоге.'
        if requested == 'onsite_visit':
            content += ' Пожелания по месту и времени зафиксированы для согласования. Выезд пока не подтверждён.'
        elif requested == 'commercial':
            content += ' Стоимость и коммерческие условия подтвердит ответственный сотрудник.'
        else:
            content += ' Начнём с дистанционной консультации; это не заявка на обязательный выезд.'
        missing_labels = {'equipment_model': 'модель оборудования', 'address': 'адрес', 'preferred_datetime': 'удобные дату и время', 'contact_name': 'имя и способ связи'}
        if state['missing_information']:
            content += ' Для продолжения уточните: ' + ', '.join(missing_labels.get(k, k) for k in state['missing_information'][:2]) + '.'
    elif (conversation is None or not allow_actions) and needs_human:
        content += '\n\nЭто проверка в Playground: реальная заявка не создана.'
    if not content:
        content = 'Расскажите, что нужно уточнить. Я сохраню сведения и помогу выбрать следующий шаг.'
    # A reported success can create a review candidate, never a verified rule.
    if conversation is not None and allow_actions and SUCCESS.search(message) and not FAILURE.search(message) and not unsafe_operation:
        if state.get('attempted_actions') and state['known_facts'].get('equipment_model'):
            from .workflows import VerifiedSolution, record_event
            if not state.get('solution_candidate_id'):
                candidate = VerifiedSolution(title=state['goal'][:240], equipment_model=state['known_facts']['equipment_model'],
                    symptoms=state['goal'], actions='\n'.join(state['attempted_actions'])[:8000],
                    source=f'Диалог {conversation.id}; результат сообщён клиентом, требуется проверка',
                    customer_confirmed=True, status='CANDIDATE', visibility='internal')
                session.add(candidate)
                session.flush()
                state['solution_candidate_id'] = candidate.id
                record_event(session, 'AI_ORCHESTRATOR', 'solution.candidate_created', 'solution', candidate.id)
        elif not escalation and engine != 'openai':
            content = 'Хорошо, что удалось добиться результата. Уточните, какое именно действие помогло — передадим описание на проверку сотруднику.'
    if unsafe_operation:
        content = _safe_operation_response() + ('\n\n' + content if escalation is not None else '')
        claims, ambiguous_table = [], False
        if escalation is None:
            action = 'answer'
    state['last_request_id'] = request_id
    state['last_response'] = content[:2000]
    if row is not None:
        row.state = deepcopy(state)
        row.updated_at = utcnow()
        session.commit()
    trace = [{'step': 'intent', 'status': 'planned', 'detail': plan.intent},
             {'step': 'topic', 'status': 'changed' if routing['topic_change'] else 'current', 'detail': routing['domain']},
             {'step': 'context', 'status': 'persisted' if row else 'dry_run', 'detail': f"Шагов диалога: {state['turn_count']}; сохранённых фактов: {len(state['known_facts'])}"},
             {'step': 'sources', 'status': 'found' if sources else 'missing', 'detail': f'Подтверждённых источников: {len(sources)}'},
             {'step': 'safety', 'status': 'blocked' if invalid_evidence or rejected_facts or unsafe_operation else 'checked', 'detail': 'Проверены происхождение фактов, точные выдержки и разрешённые внутренние действия'},
             {'step': 'action', 'status': 'created' if escalation else 'proposed', 'detail': action}]
    if manual_inspection:
        coverage = manual_inspection.get('coverage', {})
        trace.append({'step': 'manual_inspection',
            'status': 'available' if manual_inspection.get('available') else 'missing',
            'detail': (f"Просмотрен индекс страниц: {coverage.get('pages_scanned', 0)}; "
                f"страниц с совпадениями: {coverage.get('matching_pages', 0)}. "
                'Это проверка извлечённого текста; схемы и пропуски OCR требуют отдельного просмотра.')})
    if organizational or human_declined or visit_declined or terminal_followup:
        trace.append({'step': 'composition', 'status': 'server_composed', 'detail': 'Организационный ответ составлен по сохранённому статусу; бронирование и отправка писем недоступны'})
    result = {'content': content, 'sources': sources if claims and not invalid_evidence else ([{**ref, 'text': ''} for ref in _source_refs(sources)] if ambiguous_table else []), 'engine': engine,
              'planning_mode': meta.get('provider', 'openai' if engine == 'openai' else 'local_fallback'),
              'action': 'request_information' if action == 'request_photo' else action, 'proposed_action': action, 'missing_information': state['missing_information'], 'trace': trace,
              'equipment_model': state['known_facts'].get('equipment_model', machine),
              'state': public_state(state), 'escalation': None, 'request_id': request_id,
              'latency_ms': round((time.monotonic() - started) * 1000), 'usage': meta.get('usage'),
              'model': meta.get('model'), 'provider_response_id': meta.get('response_id')}
    result['provider_diagnostics'] = meta.get('provider_diagnostics', {'provider_called': False, 'status': meta.get('provider', 'reference_only')})
    result['source_conflicts'] = conflicts
    if manual_inspection:
        result['manual_inspection'] = manual_inspection
    if escalation is not None:
        from .escalations import public_escalation
        result['escalation'] = public_escalation(escalation)
    if ai_error:
        result['ai_error'] = ai_error
    return result


def respond(session: Session, message: str, **kwargs) -> dict:
    conversation = kwargs.get('conversation')
    with conversation_lock(conversation.id) if conversation is not None else nullcontext():
        return _respond(session, message, **kwargs)
