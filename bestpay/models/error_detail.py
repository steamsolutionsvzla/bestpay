# -*- coding: utf-8 -*-
import json
import logging

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

TELEGRAM_TOKEN_PARAM = 'bestpay.telegram_bot_token'
TELEGRAM_CHAT_ID_PARAM = 'bestpay.telegram_chat_id'
TELEGRAM_ENABLED_PARAM = 'bestpay.telegram_enabled'
TELEGRAM_MAX_TEXT = 3500
TELEGRAM_MAX_TRACE = 3000
TELEGRAM_MAX_OUTPUT = 700
TELEGRAM_TIMEOUT = (3, 6)
TELEGRAM_MAX_ATTEMPTS = 10


class ErrorDetail(models.Model):
    _name = 'error.detail'
    _description = 'Error Detail'
    _order = 'id desc'

    event_log_id = fields.Many2one(
        'event.log', string='Event Log', required=True, ondelete='cascade', index=True
    )
    function_name = fields.Text(string='Función')
    error_type = fields.Char(string='Tipo de Error')
    error_trace = fields.Text(string='Traza del Error')
    notified = fields.Boolean(string='Notificado', default=False, index=True)
    notify_attempts = fields.Integer(string='Intentos de Notificación', default=0)
    notify_date = fields.Datetime(string='Fecha de Notificación', readonly=True)
    notify_error = fields.Text(string='Último error de envío', readonly=True)

    # ------------------------------------------------------------------
    # Configuración de Telegram
    # ------------------------------------------------------------------
    def _get_telegram_config(self):
        icp = self.env['ir.config_parameter'].sudo()
        token = (icp.get_param(TELEGRAM_TOKEN_PARAM) or '').strip()
        raw_chats = icp.get_param(TELEGRAM_CHAT_ID_PARAM) or ''
        chat_ids = [chat.strip() for chat in raw_chats.split(',') if chat.strip()]
        enabled_raw = icp.get_param(TELEGRAM_ENABLED_PARAM, 'True')
        enabled = str(enabled_raw).strip().lower() in ('1', 'true', 'yes', 'y', 'si', 'sí')
        return token, chat_ids, enabled

    # ------------------------------------------------------------------
    # Construcción del mensaje
    # ------------------------------------------------------------------
    def _build_telegram_text(self):
        self.ensure_one()
        event_log = self.event_log_id
        hora_str = 'Desconocida'
        if event_log.timestamp:
            try:
                local = fields.Datetime.context_timestamp(
                    self.env['event.log'], event_log.timestamp
                )
                hora_str = local.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                hora_str = str(event_log.timestamp)

        trace = (self.error_trace or '') or ''
        if len(trace) > TELEGRAM_MAX_TRACE:
            trace = trace[:TELEGRAM_MAX_TRACE] + '\n...[TRUNCADO]'

        output = (event_log.output or '') or ''
        if len(output) > TELEGRAM_MAX_OUTPUT:
            output = output[:TELEGRAM_MAX_OUTPUT] + '...[TRUNCADO]'

        contexto = ''
        if event_log.meta:
            try:
                meta = json.loads(event_log.meta)
            except Exception:
                meta = {}
            if isinstance(meta, dict):
                lineas = []
                for key, value in list(meta.items())[:8]:
                    if value in (None, False, ''):
                        continue
                    texto = str(value)
                    if len(texto) > 120:
                        texto = texto[:120] + '...'
                    lineas.append(f'[{key}] {texto}')
                if lineas:
                    contexto = '===========================\n' + '\n'.join(lineas) + '\n'

        texto = (
            f'[{event_log.instance or "SIN-ETIQUETA"}] ERROR DE SISTEMA — BESTPAY\n'
            '===========================\n'
            f'[Referencia] {event_log.reference or "-"}\n'
            f'[Fecha] {hora_str}\n'
            f'[Flujo] {self.function_name or "-"}\n'
            f'[Tipo] {self.error_type or "-"}\n'
            f'[Controlador] {event_log.controller or "-"}\n'
            f'[Usuario] {event_log.user or "-"}\n'
            f'[Instancia] {event_log.instance or "-"}\n'
        )
        if event_log.request_url:
            texto += (
                f'[URL] {event_log.http_method or "-"} {event_log.request_url}\n'
            )
        if event_log.ip:
            texto += f'[IP] {event_log.ip}\n'
        texto += contexto
        texto += '===========================\n'
        if output:
            texto += f'[Detalle]\n{output}\n'
        texto += '===========================\n'
        texto += f'[Traza]\n{trace}\n'

        if len(texto) > 4096:
            texto = texto[:TELEGRAM_MAX_TEXT] + '\n...[TRUNCADO]'
        return texto

    # ------------------------------------------------------------------
    # Envío a Telegram
    # ------------------------------------------------------------------
    def _notify_telegram(self):
        self.ensure_one()
        token, chat_ids, enabled = self._get_telegram_config()
        if not enabled:
            _logger.info(
                'Notificaciones de Telegram desactivadas (%s=false); '
                'se omite el aviso %s.', TELEGRAM_ENABLED_PARAM, self.event_log_id.reference,
            )
            return False
        if not token or not chat_ids:
            _logger.warning(
                'Telegram no configurado (%s / %s); se omite la notificación.',
                TELEGRAM_TOKEN_PARAM, TELEGRAM_CHAT_ID_PARAM,
            )
            return False

        url = 'https://api.telegram.org/bot%s/sendMessage' % token
        texto = self._build_telegram_text()
        enviados = 0
        errores = []

        for chat_id in chat_ids:
            try:
                response = requests.post(
                    url,
                    data={'chat_id': chat_id, 'text': texto},
                    timeout=TELEGRAM_TIMEOUT,
                )
                try:
                    data = response.json()
                except Exception:
                    data = {}
                if data.get('ok'):
                    enviados += 1
                else:
                    errores.append(
                        data.get('description') or f'HTTP {response.status_code}'
                    )
            except Exception as error:
                errores.append(str(error))

        vals = {'notify_attempts': self.notify_attempts + 1}
        if enviados:
            vals.update({
                'notified': True,
                'notify_date': fields.Datetime.now(),
                'notify_error': False,
            })
        else:
            vals['notify_error'] = ' | '.join(errores) or 'Error desconocido'
        self.write(vals)

        if not enviados:
            _logger.error(
                'Error enviando notificación a Telegram para %s: %s',
                self.event_log_id.reference, vals.get('notify_error'),
            )
        return bool(enviados)

    def action_retry_telegram(self):
        for record in self:
            record._notify_telegram()
        return True

    @api.model
    def _cron_retry_pending_notifications(self):
        pendientes = self.search([
            ('notified', '=', False),
            ('notify_attempts', '<', TELEGRAM_MAX_ATTEMPTS),
            ('event_log_id.is_error', '=', True),
        ], limit=20)
        for record in pendientes:
            record._notify_telegram()
        return True
