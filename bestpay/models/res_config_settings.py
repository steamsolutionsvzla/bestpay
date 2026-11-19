# -*- coding: utf-8 -*-
import logging

import requests

from odoo import fields, models, _

from ..utils.event_logger import resolve_instance

_logger = logging.getLogger(__name__)

TELEGRAM_API = 'https://api.telegram.org/bot%s/%s'
TELEGRAM_TIMEOUT = (3, 8)
INSTANCE_LABEL_PARAM = 'bestpay.instance_label'


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    bestpay_telegram_bot_token = fields.Char(
        string='Token Bot Telegram',
        config_parameter='bestpay.telegram_bot_token',
    )
    bestpay_telegram_chat_id = fields.Char(
        string='Chat ID Telegram',
        config_parameter='bestpay.telegram_chat_id',
        help='Uno o varios chat_id separados por coma (chat privado, grupo).',
    )
    bestpay_telegram_enabled = fields.Boolean(
        string='Notificaciones activas',
        config_parameter='bestpay.telegram_enabled',
        default=True,
        help='Si se desactiva, los errores se siguen registrando pero no se '
             'envían a Telegram. Útil en desarrollo.',
    )
    bestpay_instance_label = fields.Char(
        string='Nombre de la Instancia',
        config_parameter=INSTANCE_LABEL_PARAM,
        help='Etiqueta que aparece en cada alerta de Telegram para distinguir '
             'las bases que comparten el mismo grupo (ej: BESTPAY-DEV, '
             'BESTPAY-PROD).',
    )

    # ------------------------------------------------------------------
    # Prueba de conexión con Telegram
    # ------------------------------------------------------------------
    def action_test_telegram(self):
        """Valida token + chat y envía un mensaje de prueba con la etiqueta.

        Sirve para confirmar en segundos que la instancia quedó bien
        configurada, sin esperar a que ocurra un error real.
        """
        self.ensure_one()
        icp = self.env['ir.config_parameter'].sudo()
        token = (self.bestpay_telegram_bot_token
                 or icp.get_param('bestpay.telegram_bot_token') or '').strip()
        raw_chats = (self.bestpay_telegram_chat_id
                     or icp.get_param('bestpay.telegram_chat_id') or '').strip()
        chat_ids = [chat.strip() for chat in raw_chats.split(',') if chat.strip()]
        etiqueta = ((self.bestpay_instance_label
                     or icp.get_param(INSTANCE_LABEL_PARAM) or '').strip()
                    or resolve_instance(self.env))

        if not token or not chat_ids:
            return self._telegram_notify(
                'Telegram sin configurar',
                'Faltan el token del bot o el chat id. Complétalos y vuelve a probar.',
                ok=False)

        try:
            # 1) ¿El token es válido?
            me = requests.get(
                TELEGRAM_API % (token, 'getMe'),
                timeout=TELEGRAM_TIMEOUT).json()
            if not me.get('ok'):
                return self._telegram_notify(
                    'Token inválido',
                    me.get('description') or 'Telegram rechazó el token del bot.',
                    ok=False)
            bot_username = (me.get('result') or {}).get('username') or 'bot'

            enviados = []
            fallidos = []
            for chat_id in chat_ids:
                # 2) ¿El bot ve ese chat?
                chat = requests.get(
                    TELEGRAM_API % (token, 'getChat'),
                    params={'chat_id': chat_id},
                    timeout=TELEGRAM_TIMEOUT).json()
                if not chat.get('ok'):
                    fallidos.append(
                        f'{chat_id}: {chat.get("description") or "chat no encontrado"}'
                    )
                    continue
                info = chat.get('result') or {}
                chat_name = info.get('title') or info.get('first_name') or chat_id
                chat_type = info.get('type') or '-'

                # 3) Mensaje real de prueba
                texto = (
                    f'[{etiqueta}] PRUEBA DE CONEXIÓN — BESTPAY\n'
                    '===========================\n'
                    f'[Base de datos] {self.env.cr.dbname}\n'
                    f'[URL] {icp.get_param("web.base.url") or "-"}\n'
                    f'[Bot] @{bot_username}\n'
                    f'[Chat] {chat_name} ({chat_type})\n'
                    f'[Etiqueta] {etiqueta}\n'
                    '===========================\n'
                    'Si ves este mensaje, las alertas de esta instancia están '
                    'bien configuradas.\n'
                )
                send = requests.post(
                    TELEGRAM_API % (token, 'sendMessage'),
                    data={'chat_id': chat_id, 'text': texto},
                    timeout=TELEGRAM_TIMEOUT).json()
                if send.get('ok'):
                    enviados.append(f'{chat_name} ({chat_id})')
                else:
                    fallidos.append(
                        f'{chat_id}: {send.get("description") or "envío rechazado"}'
                    )

            if not enviados:
                return self._telegram_notify(
                    'No se pudo enviar',
                    '\n'.join(fallidos) or 'Telegram rechazó el mensaje.',
                    ok=False)

            mensaje = _('Mensaje enviado a: %(chats)s con la etiqueta %(label)s.',
                        chats=', '.join(enviados), label=etiqueta)
            if fallidos:
                mensaje += '\n\nFallaron:\n' + '\n'.join(fallidos)
            return self._telegram_notify('Telegram OK', mensaje, ok=True)

        except Exception as error:
            _logger.warning('Prueba de Telegram fallida: %s', error)
            return self._telegram_notify(
                'Error de conexión',
                _('No se pudo contactar la API de Telegram: %s', error),
                ok=False)

    def _telegram_notify(self, title, message, ok=False):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': 'success' if ok else 'danger',
                'sticky': not ok,
            },
        }
