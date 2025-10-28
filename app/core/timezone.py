from __future__ import annotations
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo


# Крупные города для целочасовых смещений UTC (минуты → список городов)
OFFSET_CITIES: dict[int, str] = {
	-12*60: "Бейкер-Айленд",
	-11*60: "Паго-Паго",
	-10*60: "Гонолулу",
	-9*60: "Анкоридж",
	-8*60: "Лос-Анджелес, Ванкувер",
	-7*60: "Денвер, Финикс",
	-6*60: "Чикаго, Мехико",
	-5*60: "Нью-Йорк, Торонто, Богота",
	-4*60: "Сантьяго, Каракас",
	-3*60: "Буэнос-Айрес, Сан-Паулу",
	-2*60: "Южная Георгия",
	-1*60: "Азорские острова",
	0: "Лондон, Лиссабон, Касабланка",
	1*60: "Берлин, Париж, Рим, Мадрид",
	2*60: "Киев, Афины, Каир",
	3*60: "Москва, Минск",
	4*60: "Дубай, Баку",
	5*60: "Ташкент, Карачи",
	6*60: "Дакка, Бишкек",
	7*60: "Бангкок, Новосибирск",
	8*60: "Пекин, Сингапур, Гонконг",
	9*60: "Токио, Сеул",
	10*60: "Владивосток, Сидней",
	11*60: "Магадан",
	12*60: "Окленд",
	13*60: "Нукуалофа",
	14*60: "Киритимати",
}


def offset_minutes_from_tz(code: str | None) -> int:
	"""Преобразовать IANA или строку вида UTC±HH[:MM] в смещение минут."""
	if not code:
		return 180  # по умолчанию MSK +3
	try:
		# Попробуем IANA
		off = datetime.now(ZoneInfo(code)).utcoffset()
		if off is not None:
			return int(off.total_seconds() // 60)
	except Exception:
		pass
	# Парсинг формата UTC+HH[:MM]
	try:
		code_up = code.upper().replace("UTC", "").strip()
		if not code_up:
			return 180
		sign = 1
		if code_up.startswith("+"):
			code_up = code_up[1:]
		elif code_up.startswith("-"):
			sign = -1
			code_up = code_up[1:]
		parts = code_up.split(":")
		h = int(parts[0])
		m = int(parts[1]) if len(parts) > 1 else 0
		return sign * (h * 60 + m)
	except Exception:
		return 180


# Совместимость с существующими вызовами в старом коде
_offset_minutes_from_tz = offset_minutes_from_tz




def _tzinfo_from_code(code: str | None):
	"""Получить tzinfo по IANA или строке вида UTC±HH[:MM]. Спец‑случай: "UTC" → UTC."""
	if code and code.upper().strip() == "UTC":
		return timezone.utc
	if code:
		try:
			return ZoneInfo(code)
		except Exception:
			pass
	mins = offset_minutes_from_tz(code)
	return timezone(timedelta(minutes=mins))


def to_user_tz(dt: datetime, tz_code: str | None) -> datetime:
	"""Перевести datetime в указанный часовой пояс.

	Если dt naive — считаем его UTC и делаем aware-UTC, затем переводим.
	"""
	base = dt if (getattr(dt, 'tzinfo', None) is not None) else dt.replace(tzinfo=timezone.utc)
	tzi = _tzinfo_from_code(tz_code)
	return base.astimezone(tzi)


def localize_dt(dt_naive: datetime, tz_code: str | None) -> datetime:
	"""Придать naive времени указанный TZ без конвертации (интерпретация локального времени)."""
	return dt_naive.replace(tzinfo=_tzinfo_from_code(tz_code))


def now_tz(tz_code: str | None) -> datetime:
	"""Текущее время в указанном часовом поясе."""
	return to_user_tz(datetime.now(timezone.utc), tz_code)


def format_user_dt(dt: datetime, tz_code: str | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
	"""Отформатировать дату/время в TZ пользователя.

	Спец‑случай: tz_code == "UTC" поддерживается явно.
	"""
	return to_user_tz(dt, tz_code).strftime(fmt)


