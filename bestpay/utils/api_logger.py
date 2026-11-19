# -*- coding: utf-8 -*-
import functools
import inspect
import logging
import time
import traceback

from odoo.http import request

from .event_logger import safe_log_event, sanitize_payload

_logger = logging.getLogger(__name__)


def _request_summary():
    """Resumen seguro de la petición actual (parámetros + nombres de archivos)."""
    if not request:
        return None
    summary = {}
    try:
        httprequest = request.httprequest
        params = dict(request.params or {})
        if params:
            summary['params'] = sanitize_payload(params)
        if httprequest.files:
            files = {}
            for name, fs in httprequest.files.items():
                files[name] = getattr(fs, 'filename', 'archivo')
            if files:
                summary['files'] = files
    except Exception:
        _logger.exception('No se pudo resumir la petición para el event log')
    return summary or None


def _log_api_error(func, exc, start):
    function_name = func.__name__
    try:
        origin = inspect.getfile(func)
    except Exception:
        origin = 'Unknown'

    request_url = None
    http_method = None
    ip = None
    user = 'Desconocido'
    input_data = None
    env = None

    if request:
        try:
            request_url = request.httprequest.url
            http_method = request.httprequest.method
            ip = request.httprequest.remote_addr
        except Exception:
            pass
        try:
            env = request.env
        except Exception:
            env = None
        try:
            user = request.env.user.name
        except Exception:
            user = 'Desconocido'
        input_data = _request_summary()

    safe_log_event(
        env,
        input_data=input_data,
        output_data=str(exc),
        is_error=True,
        origin=origin,
        function_name=function_name,
        controller='app',
        user=user,
        request_url=request_url,
        http_method=http_method,
        ip=ip,
        duration_ms=int((time.time() - start) * 1000),
        error_trace=traceback.format_exc(),
        error_type=type(exc).__name__,
    )


def api_call_logger(func):
    """Decorador para rutas HTTP. Solo registra errores (nunca éxitos).

    Es una red de seguridad para excepciones que no fueron capturadas dentro
    del controlador. Las que sí se capturan se registran explícitamente en su
    punto de origen (con ``log_business_error``).
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if not getattr(exc, '_bestpay_event_logged', False):
                try:
                    _log_api_error(func, exc, start)
                except Exception:
                    _logger.exception('Error registrando error de API')
            raise
    return wrapper
