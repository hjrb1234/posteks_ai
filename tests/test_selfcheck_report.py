"""A shareable diagnostic must not silently include the private payload."""
import json
import os
from pathlib import Path
import stat

from scripts.selfcheck_report import safe_support_payload, write_reports


def check(status='pass', **changes):
    return {'id': 'model.generation', 'status': status, 'title': 'Проверка модели',
        'details': 'Выполнен диагностический запрос.', 'resolution': '', 'evidence': {}, **changes}


def test_html_escapes_untrusted_text_and_never_loads_remote_content(tmp_path):
    injection = '<script>alert("hello")</script><img src="https://example.invalid/leak">'
    result = write_reports(tmp_path, [check(title=injection, details=injection, resolution=injection)], {})
    html = Path(result['html']).read_text(encoding='utf-8')
    assert '<script>' not in html and '<img ' not in html
    assert '&lt;script&gt;' in html and '&lt;img ' in html
    assert "default-src 'none'" in html
    assert 'charset="utf-8"' in html


def test_support_report_drops_secrets_identities_raw_answers_and_unknown_evidence(tmp_path):
    secret = 'sk-' + 'A' * 40
    email = 'private.person@company.invalid'
    payload = {
        'model': 'gpt-4.1-mini', 'openai_api_key': secret,
        'username': 'PRIVATE_EMPLOYEE', 'mail_username': email,
        'provider_diagnostics': {'stage': 'parse', 'exception_type': 'ValidationError',
            'http_status': 200, 'provider_error_code': 'invalid_json',
            'response': 'PRIVATE_MODEL_ANSWER', 'api_key': secret},
        'raw_response': 'PRIVATE_MODEL_ANSWER', 'source_text': 'PRIVATE_MANUAL_QUOTE',
        'history': [{'sender': email, 'text': 'PRIVATE_MAIL'}],
        'document_path': 'C:/Users/PRIVATE_USER/private.pdf',
    }
    result = write_reports(tmp_path, [check(details='Адрес ' + email + '; OPENAI_API_KEY=' + secret,
        evidence={**payload, 'history_turns': 4, 'document_count': 6, 'input_tokens': 80})], payload)
    raw = Path(result['support']).read_text(encoding='utf-8')
    for private in [secret, email, 'PRIVATE_EMPLOYEE', 'PRIVATE_MODEL_ANSWER', 'PRIVATE_MANUAL_QUOTE',
                    'PRIVATE_MAIL', 'PRIVATE_USER']:
        assert private not in raw
    report = json.loads(raw)
    evidence = report['checks'][0]['evidence']
    assert evidence['model'] == 'gpt-4.1-mini' and evidence['history_turns'] == 4
    assert evidence['document_count'] == 6 and evidence['input_tokens'] == 80
    assert evidence['provider_diagnostics']['stage'] == 'parse'
    assert evidence['provider_diagnostics']['exception_type'] == 'ValidationError'
    assert report['contains_private_details'] is False


def test_secret_inside_allowed_model_or_id_is_still_redacted():
    secret = 'sk-' + 'B' * 36
    report = safe_support_payload([check(evidence={'model': secret, 'request_id': secret,
        'provider_diagnostics': {'provider_request_id': 'Bearer ' + secret}})], {})
    assert secret not in json.dumps(report)
    assert report['checks'][0]['evidence']['model'] == '[СКРЫТО]'


def test_warn_and_skipped_are_counted_and_never_reported_as_all_passed(tmp_path):
    checks = [check('pass'), check('warn', id='ocr.languages'), check('skipped', id='mail.live')]
    result = write_reports(tmp_path, checks, {'live_tested': False})
    report = json.loads(Path(result['support']).read_text(encoding='utf-8'))
    assert result['overall_status'] == 'partial'
    assert report['counts'] == {'pass': 1, 'fail': 0, 'warn': 1, 'skipped': 1}
    assert report['all_checks_passed'] is False
    summary = Path(result['summary']).read_text(encoding='utf-8')
    assert 'не проверено: 1' in summary and 'выполнена не полностью' in summary


def test_runner_metadata_and_progress_fields_survive_allowlist():
    metadata = {'created_at': '2026-11-12T15:30:00+05:00', 'model': 'gpt-4.1-mini',
        'model_origin': 'AppSetting.models', 'key_configured': True, 'live_requested': True,
        'provider_calls_started': 2, 'input_tokens': 300, 'output_tokens': 60, 'source_count': 1,
        'test_material_kind': 'real', 'python_version': '3.12.7', 'openai_sdk_version': '2.8.0',
        'max_output_tokens': 4096}
    evidence = {'expected_turns': 5, 'successful_turns': 4, 'total': 22, 'checked': 20,
        'missing': 2, 'indexed': 18, 'errors': 2, 'remaining': 2, 'unresolved': 1,
        'days': 365, 'complete': False, 'uid': 2485, 'folder_kind': 'inbox', 'sample_limit': 2, 'fetched': 1}
    report = safe_support_payload([check('warn', evidence=evidence)], metadata)
    assert report['metadata'] == metadata
    assert report['checks'][0]['evidence'] == evidence


def test_empty_unknown_and_failed_checks_are_honest():
    assert safe_support_payload([], {})['overall_status'] == 'not_run'
    invalid = safe_support_payload([check('invented_status')], {})
    assert invalid['overall_status'] == 'failed' and invalid['counts']['fail'] == 1
    assert safe_support_payload([check('fail')], {})['all_checks_passed'] is False
    assert safe_support_payload([check()], {})['all_checks_passed'] is True


def test_mail_coverage_and_fetch_stage_remain_diagnosable_without_message_content():
    evidence = {'missing_variable_names': ['MAIL_APP_PASSWORD'], 'stored_messages': 22,
        'history_run_id': 5, 'remaining_in_search': 20, 'unresolved_gaps': 1,
        'full_live_coverage_verified': False, 'remote_flags_changed': False,
        'samples': [{'kind': 'inbox', 'uid': 2485, 'body_read': False, 'bytes': 0,
                    'code': 'imap_fetch_failed', 'text': 'PRIVATE_MAIL'}],
        'folders': [{'kind': 'inbox', 'year_messages_found': 123, 'stored_uids_in_year': 22,
                    'missing_uids_count': 101, 'first_missing_uids': [1, 2], 'uidvalidity': 1727441335}],
        'date_basis': 'IMAP INTERNALDATE', 'all_visible_year_uids_stored': False,
        'attachments_content_verified': False, 'model_learning_verified': False,
        'imap_status': 'NO', 'response_code': 'TOOBIG', 'transport_error_type': 'SSLError'}
    report = safe_support_payload([check('warn', evidence=evidence)], {})
    retained = report['checks'][0]['evidence']
    assert retained['missing_variable_names'] == ['MAIL_APP_PASSWORD']
    assert retained['folders'] == evidence['folders']
    assert retained['samples'][0] == {k:v for k,v in evidence['samples'][0].items() if k != 'text'}
    assert retained['unresolved_gaps'] == 1 and retained['history_run_id'] == 5
    assert retained['date_basis'] == 'IMAP INTERNALDATE'
    assert retained['all_visible_year_uids_stored'] is False
    assert 'PRIVATE_MAIL' not in json.dumps(report)


def test_repeated_runs_preserve_previous_files_and_update_only_latest_pointer(tmp_path):
    first = write_reports(tmp_path, [check('fail')], {})
    original = Path(first['html']).read_bytes()
    second = write_reports(tmp_path, [check()], {})
    assert first['directory'] != second['directory']
    assert Path(first['html']).read_bytes() == original
    assert Path(second['latest']).read_text().strip() == second['html']
    assert Path(first['support']).is_file() and Path(second['support']).is_file()


def test_private_details_are_separate_restricted_and_warned_about(tmp_path):
    secret = 'sk-' + 'C' * 40
    result = write_reports(tmp_path, [check()], {}, private_details={
        'answer': 'PRIVATE_REAL_ANSWER', 'email': 'private.person@company.invalid',
        'api_key': secret, 'nested': {'authorization': secret}})
    private = Path(result['private'])
    raw = private.read_text(encoding='utf-8')
    assert 'PRIVATE_REAL_ANSWER' in raw and secret not in raw
    data = json.loads(raw)
    assert data['private_report_do_not_share'] is True
    assert 'Не отправляйте' in data['warning']
    assert 'PRIVATE_REAL_ANSWER' not in Path(result['support']).read_text(encoding='utf-8')
    assert 'PRIVATE_REAL_ANSWER' not in Path(result['html']).read_text(encoding='utf-8')
    assert 'PRIVATE_details.json' in Path(result['html']).read_text(encoding='utf-8')
    if os.name != 'nt':
        assert stat.S_IMODE(private.stat().st_mode) == 0o600


def test_traceback_and_object_messages_are_not_forwarded_to_support():
    report = safe_support_payload([check(details='Traceback (most recent call last):\nPRIVATE_TRACE',
        evidence={'error': {'message': 'PRIVATE_ERROR'}, 'validation_errors': [
            {'location': ['technical_claims', 'quote'], 'type': 'string_type', 'input': 'PRIVATE_VALUE'}],
            'total_count': 'PRIVATE_VALUE', 'duration_seconds': float('nan')})], {})
    raw = json.dumps(report)
    assert all(value not in raw for value in ['PRIVATE_TRACE', 'PRIVATE_ERROR', 'PRIVATE_VALUE'])
    assert report['checks'][0]['evidence']['validation_errors'] == [
        {'location': ['technical_claims', 'quote'], 'type': 'string_type'}]
    assert 'total_count' not in report['checks'][0]['evidence']
