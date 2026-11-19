# -*- coding: utf-8 -*-
"""Instrumentación de los flujos de BestPay con el Event Log.

Regla anti-ruido:
    ``is_error=True``  -> crea error.detail y dispara el aviso de Telegram.
    ``is_error=False`` -> solo deja el registro en el menú Errores.

Los errores esperados del cliente (datos inválidos, transacción no
encontrada) no se registran aquí: ya se responden en la API y llenarían la
base de ruido. Solo se registran fallos de sistema/configuración y rechazos
anómalos que alguien deba revisar.
"""
import functools
import logging
import sys
import time
import traceback

from odoo.exceptions import UserError, ValidationError

from .event_logger import safe_log_event

_logger = logging.getLogger(__name__)

# Clave de contexto para silenciar el aviso puntual cuando el proceso que
# llama ya va a mandar su propio aviso agregado (por ejemplo, un cron).
SILENT_CONTEXT_KEY = 'bestpay_event_log_silent'

SILENT_TYPES = (UserError, ValidationError)


def _caller_file(depth=3):
    try:
        return sys._getframe(depth).f_code.co_filename
    except Exception:
        return 'bestpay'


def _resolve_user(env):
    try:
        return env.user.name or 'Desconocido'
    except Exception:
        return 'Desconocido'


def _request_info():
    """URL, método e IP si el fallo ocurrió dentro de una petición HTTP."""
    info = {}
    try:
        from odoo.http import request
        if not request:
            return info
        httprequest = request.httprequest
        info = {
            'request_url': httprequest.url,
            'http_method': httprequest.method,
            'ip': httprequest.remote_addr,
        }
    except Exception:
        pass
    return info


def is_silenced(env):
    """True si el proceso que llama ya reportará el fallo de forma agregada."""
    try:
        return bool(env.context.get(SILENT_CONTEXT_KEY))
    except Exception:
        return False


def is_logged(exc):
    """True si la excepción ya fue registrada por una capa inferior."""
    return bool(getattr(exc, '_bestpay_event_logged', False))


def log_business_error(env, function_name, input_data=None, output_data=None,
                       exc=None, is_error=True, controller='model',
                       error_type=None, error_trace=None, duration_ms=None,
                       http=False, meta=None, dedupe_key=None,
                       dedupe_minutes=0):
    """Registra un fallo de un flujo de negocio. Nunca lanza.

    :param env: entorno de Odoo del flujo que falla.
    :param function_name: nombre estable para poder filtrar/agrupar.
    :param input_data: dict con contexto del registro (sin datos personales).
    :param output_data: mensaje legible del fallo.
    :param exc: excepción capturada; de ella salen el tipo y la traza.
    :param is_error: ``True`` dispara Telegram, ``False`` solo registra.
    :param http: ``True`` adjunta URL, método e IP de la petición actual.
    :param meta: datos de contexto que se muestran en el aviso (proveedor,
                 referencia de la transacción, cliente, etc.).
    :param dedupe_key: identificador para no repetir el mismo aviso.
    :param dedupe_minutes: ventana de silencio para ``dedupe_key``.
    """
    if exc is not None:
        if error_trace is None:
            error_trace = traceback.format_exc()
        if error_type is None:
            error_type = type(exc).__name__

    # Con is_error=False no se crea 'error.detail' (y por tanto la traza no
    # tendría dónde guardarse). Para no perderla, se pliega en la salida.
    if not is_error and error_trace:
        output_data = '%s\n\n%s' % (output_data or '', error_trace)

    extra = _request_info() if http else {}

    try:
        ref = safe_log_event(
            env,
            input_data=input_data,
            output_data=output_data,
            is_error=is_error,
            origin=_caller_file(2),
            function_name=function_name,
            controller='app' if http else controller,
            user=_resolve_user(env),
            duration_ms=duration_ms,
            error_trace=error_trace,
            error_type=error_type,
            meta=meta,
            dedupe_key=dedupe_key,
            dedupe_minutes=dedupe_minutes,
            **extra
        )
        if exc is not None:
            try:
                # Se marca también cuando el dedupe suprimió el aviso, para que
                # las capas superiores no lo registren otra vez.
                exc._bestpay_event_logged = True
            except Exception:
                pass
        return ref
    except Exception:  # pragma: no cover - salvaguarda
        _logger.exception(
            "No se pudo registrar el evento '%s' en el Event Log", function_name)
        return None


def log_model_errors(input_builder=None, silent_types=SILENT_TYPES):
    """Decorador para métodos de modelo: registra y vuelve a lanzar.

    Los errores de negocio (``UserError``/``ValidationError``) se registran
    como aviso suave: el usuario ya los ve en pantalla y no deben despertar a
    nadie. Cualquier otra excepción va como error real.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            env = getattr(self, 'env', None)
            data = None
            if input_builder is not None:
                try:
                    data = input_builder(self)
                except Exception:
                    data = None

            start = time.perf_counter()
            try:
                return func(self, *args, **kwargs)
            except silent_types as exc:
                if not is_logged(exc):
                    log_business_error(
                        env, func.__name__,
                        input_data=data,
                        output_data='Validación de negocio: %s' % exc,
                        exc=exc,
                        is_error=False,
                        controller='model',
                        duration_ms=int((time.perf_counter() - start) * 1000),
                    )
                raise
            except Exception as exc:
                if not is_logged(exc):
                    log_business_error(
                        env, func.__name__,
                        input_data=data,
                        output_data=str(exc) or type(exc).__name__,
                        exc=exc,
                        is_error=not is_silenced(env),
                        controller='model',
                        duration_ms=int((time.perf_counter() - start) * 1000),
                    )
                raise
        return wrapper
    return decorator
