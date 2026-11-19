# -*- coding: utf-8 -*-
"""Núcleo del Event Log de BestPay.

Registra eventos y errores en un cursor independiente para que sobrevivan a
un rollback de la transacción principal. Los errores de sistema/configuración
crean además un ``error.detail`` que dispara el aviso a Telegram.
"""
import contextlib
import json
import logging
import sys
import threading
import time
import traceback
from datetime import timedelta

from odoo import api, fields, SUPERUSER_ID

_logger = logging.getLogger(__name__)

MAX_TEXT_LEN = 4000

# Claves cuyo contenido nunca debe viajar al event log ni a Telegram.
SENSITIVE_KEY_PARTS = (
    'password', 'token', 'csrf', 'recaptcha', 'authorization', 'secret',
    'api_key', 'apikey', 'image', 'photo', 'datas', 'attachment', 'b64',
    # Datos personales del pagador (BDV / Mercantil).
    'cedula', 'telefono', 'phone', 'document', 'rif', 'account',
)

# Evita recursión si por accidente se registra un error dentro del logger.
_reentrant = threading.local()


def _caller_file(depth=2):
    try:
        return sys._getframe(depth).f_code.co_filename
    except Exception:
        return 'Unknown'


def _truncate(value, limit=MAX_TEXT_LEN):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + '...[TRUNCADO]'
    return value


def _stringify(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        return str(value)


def sanitize_payload(data, limit=MAX_TEXT_LEN):
    """Devuelve una versión segura del payload: enmascara claves sensibles,
    reemplaza archivos por un resumen y trunca cadenas muy largas."""
    if data is None:
        return None

    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            key_lower = str(key).lower()
            if any(part in key_lower for part in SENSITIVE_KEY_PARTS):
                result[key] = '[OCULTO]'
            else:
                result[key] = sanitize_payload(value, limit=limit)
        return result

    if isinstance(data, (list, tuple)):
        return [sanitize_payload(item, limit=limit) for item in data]

    if isinstance(data, (bytes, bytearray)):
        return '<binary (%d bytes)>' % len(data)

    # Objetos tipo archivo (werkzeug FileStorage / odoo Binary).
    if hasattr(data, 'filename') and hasattr(data, 'read'):
        try:
            return {
                'filename': getattr(data, 'filename', 'archivo'),
                'content_type': getattr(data, 'content_type', None),
                'content_length': getattr(data, 'content_length', None),
            }
        except Exception:
            return '<archivo>'

    if isinstance(data, str):
        return _truncate(data, limit=limit)

    if isinstance(data, (int, float, bool)):
        return data

    return _truncate(str(data), limit=limit)


def _get_new_cursor(env):
    registry = getattr(env, 'registry', None)
    if registry is not None:
        return registry.cursor()
    from odoo.sql_db import db_connect
    return db_connect(env.cr.dbname).cursor()


def resolve_instance(env):
    """Nombre visible de ESTA instalación (BESTPAY-DEV, BESTPAY-PROD...).

    Se usa la etiqueta configurada en Ajustes → BestPay → Errores y
    Notificaciones. Si no existe, se usa el nombre de la base de datos y se
    antepone ``SIN-ETIQUETA`` para que la instancia sin configurar se note.
    """
    try:
        icp = env['ir.config_parameter'].sudo()
    except Exception:
        return 'SIN-ETIQUETA'

    label = (icp.get_param('bestpay.instance_label') or '').strip()
    if label:
        return label

    try:
        dbname = env.cr.dbname
    except Exception:
        dbname = None

    detalle = dbname or icp.get_param('web.base.url') or 'sin datos'
    return 'SIN-ETIQUETA · %s' % detalle


def safe_log_event(env, *, input_data=None, output_data=None, is_error=False,
                   origin=None, function_name=None, controller='model',
                   user=None, instance=None, request_url=None, http_method=None,
                   ip=None, duration_ms=None, error_trace=None, error_type=None,
                   meta=None, dedupe_key=None, dedupe_minutes=0):
    """Crea event.log (y error.detail si aplica) usando un cursor independiente
    para que persista aunque la transacción principal haga rollback.

    ``dedupe_key`` + ``dedupe_minutes`` evitan repetir el mismo aviso (por
    ejemplo, la tasa BCV caída en cada petición). Nunca lanza excepciones.
    """
    if getattr(_reentrant, 'active', False):
        return None
    _reentrant.active = True
    try:
        return _do_log_event(
            env, input_data=input_data, output_data=output_data,
            is_error=is_error, origin=origin, function_name=function_name,
            controller=controller, user=user, instance=instance,
            request_url=request_url, http_method=http_method, ip=ip,
            duration_ms=duration_ms, error_trace=error_trace,
            error_type=error_type, meta=meta, dedupe_key=dedupe_key,
            dedupe_minutes=dedupe_minutes,
        )
    except Exception:
        _logger.exception('Error registrando event.log')
        return None
    finally:
        _reentrant.active = False


def _do_log_event(env, **kwargs):
    if env is None:
        try:
            from odoo.http import request
            if request:
                env = request.env
        except Exception:
            env = None
    if env is None:
        _logger.error(
            'No hay entorno de Odoo disponible para registrar el evento %s',
            kwargs.get('function_name'),
        )
        return None

    input_data = sanitize_payload(kwargs.get('input_data'))
    output_data = sanitize_payload(kwargs.get('output_data'))
    meta = sanitize_payload(kwargs.get('meta'))
    is_error = bool(kwargs.get('is_error'))
    origin = kwargs.get('origin') or _caller_file(3)
    function_name = kwargs.get('function_name')
    controller = kwargs.get('controller') or 'model'
    user = kwargs.get('user')
    instance = kwargs.get('instance')
    request_url = kwargs.get('request_url')
    http_method = kwargs.get('http_method')
    ip = kwargs.get('ip')
    duration_ms = kwargs.get('duration_ms')
    error_trace = kwargs.get('error_trace')
    error_type = kwargs.get('error_type')
    dedupe_key = kwargs.get('dedupe_key')
    dedupe_minutes = int(kwargs.get('dedupe_minutes') or 0)

    try:
        company_id = env.company.id
    except Exception:
        company_id = False

    reference = None
    with _get_new_cursor(env) as cr:
        log_env = api.Environment(cr, SUPERUSER_ID, {})

        if instance is None:
            instance = resolve_instance(log_env)

        if user is None:
            try:
                user = env.user.name
            except Exception:
                user = 'Desconocido'

        # Anti-spam: si el mismo fallo ya se registró hace poco, no se repite
        # ni el evento ni el aviso. Se deja constancia solo en el log de Odoo.
        if is_error and dedupe_key and dedupe_minutes > 0:
            cutoff = fields.Datetime.now() - timedelta(minutes=dedupe_minutes)
            repeated = log_env['event.log'].search_count([
                ('dedupe_key', '=', dedupe_key),
                ('is_error', '=', True),
                ('timestamp', '>=', cutoff),
            ], limit=1)
            if repeated:
                _logger.info(
                    '[EVENT LOG] Aviso %s omitido por dedupe (%s min).',
                    dedupe_key, dedupe_minutes,
                )
                return False

        event_log = log_env['event.log'].create({
            'input': _stringify(input_data) if input_data is not None else '',
            'output': _stringify(output_data) if output_data is not None else '',
            'meta': _stringify(meta) if meta is not None else '',
            'is_error': is_error,
            'origin': origin,
            'function_name': function_name,
            'controller': controller,
            'user': user,
            'instance': instance,
            'request_url': request_url,
            'http_method': http_method,
            'ip': ip,
            'duration_ms': duration_ms,
            'company_id': company_id,
            'dedupe_key': dedupe_key,
        })
        reference = event_log.reference

        error_detail = None
        if is_error:
            error_detail = log_env['error.detail'].create({
                'event_log_id': event_log.id,
                'function_name': function_name,
                'error_type': error_type,
                'error_trace': error_trace,
            })

        cr.commit()

        if is_error and error_detail:
            try:
                error_detail._notify_telegram()
                cr.commit()
            except Exception:
                _logger.exception('Error notificando por Telegram')
                try:
                    cr.rollback()
                except Exception:
                    pass

    return reference


@contextlib.contextmanager
def log_errors(model_or_env, function_name, input_data=None):
    """Context manager para capturar y registrar cualquier excepción,
    volviéndola a lanzar después."""
    env = getattr(model_or_env, 'env', None) or model_or_env
    start = time.time()
    try:
        yield
    except Exception as exc:
        ref = safe_log_event(
            env,
            input_data=input_data,
            output_data=str(exc),
            is_error=True,
            origin=_caller_file(2),
            function_name=function_name,
            controller='model',
            duration_ms=int((time.time() - start) * 1000),
            error_trace=traceback.format_exc(),
            error_type=type(exc).__name__,
        )
        if ref:
            try:
                exc._bestpay_event_logged = True
            except Exception:
                pass
        raise
