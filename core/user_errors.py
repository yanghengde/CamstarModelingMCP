"""Short business messages with separate, sanitized diagnostic evidence."""
import json
import re


def diagnostic_text(value):
    text = str(value)
    text = re.sub(r'(?i)(password|authorization|api[_-]?key|token|otp)(["\s]*[:=]["\s]*)[^\s,;\}\"]+',
                  r'\1\2<redacted>', text)
    text = re.sub(r'(?i)Bearer\s+[^\s\"\']+', 'Bearer <redacted>', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '<redacted>', text)
    text = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1<redacted>@', text)
    return text[:4000]


def error_event(message, error):
    return {'type': 'error', 'message': message,
            'details': diagnostic_text(f'{type(error).__name__}: {error}')}


def tool_diagnostic(result):
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and parsed.get('error'):
            return diagnostic_text(parsed['error'])
        if result.startswith(('❌', 'Error')):
            return diagnostic_text(result)
    return None
