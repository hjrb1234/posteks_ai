"""Small, explicit checks around a natural reply; never a replacement chatbot."""
import re


def source_conflicts(sources):
    """Detect directly comparable labelled measurements, not arbitrary numbers.

    This covers explicit conflicts; semantic differences still require the model
    and staff review. Values from different parameters/units are not conflated.
    """
    facts = {}
    for index, source in enumerate(sources, 1):
        for line in str(source.get('text', '')).splitlines():
            match = re.match(r'\s*([А-Яа-яЁёA-Za-z][^:=\n]{2,70})\s*[:=]\s*'
                r'(\d+(?:[.,]\d+)?)\s*(мм|см|бар|кПа|МПа|кВт|об/мин|м/мин|°C)\b', line, re.I)
            if not match:
                continue
            label, value, unit = match.groups()
            if not re.search(r'зазор|давлен|скорост|натяж|температур|мощност|длина|ширина|высота', label, re.I):
                continue
            key = (re.sub(r'\s+', ' ', label).strip().casefold(), unit.casefold())
            facts.setdefault(key, []).append({'source_index': index,
                'document_id': source.get('document_id'), 'solution_id': source.get('solution_id'),
                'value': value.replace(',', '.'), 'unit': unit, 'quote': match.group(0).strip()})
    result = []
    for (label, _), values in facts.items():
        origins = {(v['document_id'], v['solution_id']) for v in values}
        if len({v['value'] for v in values}) > 1 and len(origins) > 1:
            result.append({'parameter': label, 'evidence': values})
    return result[:8]


def reply_violation(plan, sources, claims_valid, conflicts, message):
    text = plan.response.strip()
    if not text:
        return 'empty_model_response', 'Модель вернула пустой ответ. Повторите запрос.'
    indexes = [int(value) for value in re.findall(r'\[(\d+)\]', text)]
    if any(not 1 <= value <= len(sources) for value in indexes):
        return 'invalid_citation', 'Модель указала источник, которого нет в найденных материалах. Ответ не показан.'
    if indexes and not plan.technical_claims:
        return 'missing_evidence', 'Модель сослалась на источник без подтверждающей выдержки. Ответ не показан.'
    if plan.technical_claims and not claims_valid:
        return 'ungrounded_response', 'Ответ модели не подтверждён приведёнными источниками. Техническая рекомендация не показана.'
    if conflicts and not re.search(r'расхожд|противореч|разн.{0,25}(?:значен|редакц|источник)|согласова', text, re.I):
        return 'conflicting_sources', 'В найденных источниках есть разные значения одного параметра. Нужно уточнить редакцию и комплектацию; рекомендация не выбрана.'
    needs_facts = bool(re.search(r'\d+(?:[.,]\d+)?\s*(?:мм|см|бар|кПа|МПа|кВт|об/мин|м/мин)\b|'
        r'\b(?:отрегулируйте|натяните|замените|демонтируйте|переставьте)\b', text, re.I))
    if needs_facts and not plan.technical_claims:
        return 'missing_evidence', 'Модель предложила параметры или действия без подтверждённого источника. Техническая рекомендация не показана.'
    if plan.technical_claims:
        evidence = '\n'.join(claim.quote for claim in plan.technical_claims) + '\n' + message
        for value in re.findall(r'\d+(?:[.,]\d+)?\s*(?:мм|см|бар|кПа|МПа|кВт|об/мин|м/мин)\b', text, re.I):
            normalize = lambda v: re.sub(r'\s+', '', v.casefold().replace(',', '.'))
            if normalize(value) not in normalize(evidence):
                return 'unsupported_parameter', 'В ответе появился параметр, отсутствующий в подтверждающих выдержках. Ответ не показан.'
    return None
