"""Pure admission check for mode-scoped software remote requests."""

_ALIASES={'youtube':'kiosk','youtube_tv':'kiosk','video':'mpv','cast':'mpv',
          'media':'mpv','web':'chrome','game':'games'}
_MODES={'kiosk','mpv','tv','chrome','games','off'}


class StaleMediaControl(ValueError):
    pass


def require_expected_mode(data, actual):
    """Old clients without the optional field keep their existing API contract."""
    if not isinstance(data, dict):
        raise StaleMediaControl('Некоректний запит пульта.')
    if 'expected_mode' not in data:
        return
    expected=data['expected_mode']
    if not isinstance(expected,str):
        raise StaleMediaControl('Некоректний режим пульта.')
    expected=_ALIASES.get(expected,expected)
    current=_ALIASES.get(actual,actual) if isinstance(actual,str) else None
    if expected not in _MODES or current!=expected:
        raise StaleMediaControl('Режим змінився. Оновіть пульт і повторіть команду.')
