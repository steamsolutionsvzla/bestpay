# -*- coding: utf-8 -*-
"""Utilidades de identidad del pagador y del aviso bancario.

El webhook del BDV entrega el teléfono y la cédula del pagador en un formato
distinto al que puede usar el tercero al crear la transacción:

    banco:   04129391309 / V29676304
    tercero: 4129391309  / 29.676.304 / +58 412-939-1309

Estas funciones llevan ambos lados a una misma clave para poder compararlos
sin depender del formato. No dependen de Odoo: se pueden probar de forma
aislada.
"""
import re
from datetime import datetime

try:  # Odoo siempre trae pytz, pero la utilidad debe poder importarse sola.
    import pytz
except ImportError:  # pragma: no cover
    pytz = None


# Campos donde el tercero puede enviar el teléfono del pagador.
PHONE_FIELDS = (
    'customer_phone',
    'customer_phone_number',
    'customer_telephone',
    'customer_mobile',
    'payer_phone',
    'payer_phone_number',
    'buyer_phone',
    'phone',
    'mobile',
    'telefono',
    'telefono_pagador',
    'cliente_telefono',
)

# Campos donde el tercero puede enviar la cédula/documento del pagador.
DOCUMENT_FIELDS = (
    'customer_document',
    'customer_document_id',
    'customer_document_number',
    'customer_documento',
    'customer_dni',
    'document',
    'document_id',
    'document_number',
    'documento',
    'cedula',
    'cedula_pagador',
    'id_cliente',
    'customer_id',
    'dni',
    'buyer_document',
    'cliente_cedula',
    'cliente_documento',
)

# Igual que DOCUMENT_FIELDS pero sin los campos que solo traen el número.
DOCUMENT_FULL_FIELDS = tuple(
    field for field in DOCUMENT_FIELDS
    if field not in (
        'customer_document_number',
        'document_number',
        'numero_documento',
    )
)

DOCUMENT_NUMBER_FIELDS = (
    'customer_document_number',
    'document_number',
    'numero_documento',
)

# Contenedores habituales: {"customer": {"phone": ..., "document": ...}}
NESTED_CONTAINERS = (
    'customer',
    'payer',
    'cliente',
    'pagador',
    'buyer',
    'user',
)


def only_digits(value):
    """Devuelve solo los dígitos de un valor ('' si no hay ninguno)."""
    return re.sub(r'\D', '', str(value or ''))


def parse_amount(value):
    """Convierte montos del banco a float, tolerando coma o punto decimal.

    Ejemplos:
        "5.00"      -> 5.0
        "5,00"      -> 5.0
        "1.234,56"  -> 1234.56
        "1,234.56"  -> 1234.56
    """
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value or '').strip()
    if not text:
        return 0.0

    text = re.sub(r'[^\d.,-]', '', text)
    if not any(char.isdigit() for char in text):
        return 0.0

    if ',' in text and '.' in text:
        decimal = ',' if text.rfind(',') > text.rfind('.') else '.'
        thousands = '.' if decimal == ',' else ','
        text = text.replace(thousands, '').replace(decimal, '.')
    elif ',' in text:
        text = text.replace('.', '').replace(',', '.')
    elif text.count('.') > 1:
        parts = text.split('.')
        if all(len(part) == 3 for part in parts[1:]):
            text = ''.join(parts)
        else:
            text = ''.join(parts[:-1]) + '.' + parts[-1]

    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def normalize_phone(value):
    """Normaliza un teléfono venezolano a su clave nacional de 10 dígitos.

    Ejemplos:
        04129391309  -> 4129391309
        +58 412-939-1309 -> 4129391309
        4129391309   -> 4129391309
    """
    digits = only_digits(value)
    if not digits:
        return ''

    # Quita ceros iniciales (0 nacional o 00 internacional).
    digits = digits.lstrip('0')

    # Quita el código de país 58 cuando viene pegado.
    if len(digits) > 10 and digits.startswith('58'):
        digits = digits[2:]

    # Si quedó basura a la izquierda, nos quedamos con los últimos 10.
    if len(digits) > 10:
        digits = digits[-10:]

    return digits


def phone_variants(value):
    """Variantes equivalentes de un teléfono para comparar por SQL."""
    key = normalize_phone(value)
    if not key:
        return []
    return list({key, '0' + key, '58' + key})


def normalize_document(value):
    """Normaliza una cédula/RIF a letra + dígitos, sin separadores.

    Ejemplos:
        V-29.676.304 -> V29676304
        29676304     -> 29676304
        29676304     -> 29676304
    """
    raw = str(value or '').strip().upper()
    raw = re.sub(r'[^A-Z0-9]', '', raw)
    if not raw:
        return ''

    match = re.match(r'^([VEJGP])?0*(\d+)$', raw)
    if match:
        letter = match.group(1) or ''
        digits = match.group(2).lstrip('0') or '0'
        return letter + digits

    return raw


def document_variants(value):
    """Variantes de un documento: con letra y solo dígitos."""
    key = normalize_document(value)
    if not key:
        return []
    digits = only_digits(key)
    return list({key, digits}) if digits else [key]


def reference_matches(bank_reference, stored_reference, min_digits=4):
    """True si la referencia del banco termina con la que escribió el pagador.

    La conciliación del BDV acepta desde los últimos 4 dígitos hasta la
    referencia completa. El aviso del banco siempre trae la referencia
    completa. Esta comparación cubre los tres casos con una sola regla:

        banco: 001199999989
        pagador: 001199999989  -> exacta
        pagador: 1199999989    -> sin ceros a la izquierda
        pagador: 9989          -> últimos 4 dígitos

    El llamador debe validar además monto y unicidad: un sufijo de 4 dígitos
    por sí solo no identifica un pago.
    """
    bank = only_digits(bank_reference)
    stored = only_digits(stored_reference)
    if not bank or not stored or len(stored) < min_digits:
        return False
    return bank.endswith(stored)


def _first_value(payload, field_names):
    for field_name in field_names:
        value = payload.get(field_name)
        if value not in (None, '', False):
            return value
    return ''


def extract_payer_identity(payload):
    """Extrae (teléfono, documento) normalizados del payload del tercero.

    Acepta los campos planos y también un objeto anidado ``customer``/
    ``payer``/``cliente``. Si el tercero envía tipo y número de documento por
    separado, los combina.
    """
    if not isinstance(payload, dict):
        return '', ''

    containers = [payload]
    for container_name in NESTED_CONTAINERS:
        nested = payload.get(container_name)
        if isinstance(nested, dict):
            containers.append(nested)

    phone_raw = ''
    document_raw = ''

    for container in containers:
        if not phone_raw:
            phone_raw = _first_value(container, PHONE_FIELDS)
        if not document_raw:
            # 1) Documento completo (ej. "V-29.676.304").
            document_raw = _first_value(container, DOCUMENT_FULL_FIELDS)
        if not document_raw:
            # 2) Tipo + número por separado (ej. "V" + "29676304").
            doc_type = (
                container.get('customer_document_type')
                or container.get('document_type')
                or container.get('tipo_documento')
                or ''
            )
            doc_number = _first_value(container, DOCUMENT_NUMBER_FIELDS)
            if doc_number:
                document_raw = '%s%s' % (doc_type, doc_number)
        if not document_raw:
            # 3) Solo número (sin tipo).
            document_raw = _first_value(container, DOCUMENT_NUMBER_FIELDS)
        if phone_raw and document_raw:
            break

    return normalize_phone(phone_raw), normalize_document(document_raw)


def parse_bank_datetime(fecha, hora, tz_name='America/Caracas'):
    """Convierte fecha/hora del aviso del BDV a datetime UTC naive.

    El banco envía ``fecha`` como ``YYYYMMDD`` y ``hora`` como ``HHMM`` (a
    veces ``HHMMSS``), en hora local de Venezuela. Odoo guarda ``create_date``
    en UTC, así que la comparación debe hacerse en UTC.

    Devuelve ``None`` si la fecha no se puede interpretar.
    """
    fecha = str(fecha or '').strip()
    hora = str(hora or '').strip()
    if not fecha:
        return None

    day = None
    for date_format in ('%Y%m%d', '%Y-%m-%d'):
        try:
            day = datetime.strptime(fecha, date_format)
            break
        except ValueError:
            continue
    if day is None:
        return None

    digits = only_digits(hora)
    if digits:
        digits = digits.ljust(6, '0')[:6]
        try:
            hour = int(digits[0:2])
            minute = int(digits[2:4])
            second = int(digits[4:6])
        except (TypeError, ValueError):
            hour = minute = second = 0
    else:
        hour = minute = second = 0

    local_dt = day.replace(hour=hour, minute=minute, second=second)
    if pytz is None:
        return local_dt

    try:
        local_tz = pytz.timezone(tz_name)
        localized = local_tz.localize(local_dt)
        return localized.astimezone(pytz.UTC).replace(tzinfo=None)
    except Exception:
        return local_dt
