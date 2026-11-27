# -*- coding: utf-8 -*-
import json
import logging

from odoo import models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class BestpayBankNotificationBDV(models.Model):
    _inherit = 'bestpay.bank.notification'

    def action_bdv_retry_match(self):
        """Reintenta el casado de un aviso pendiente/ambiguo/huérfano.

        Se usa desde la ficha de la notificación cuando el pago se quedó sin
        casar (por ejemplo, porque la transacción se creó después del aviso).
        No requiere que el banco reenvíe nada.
        """
        self.ensure_one()
        if self.provider_code != 'bdv':
            raise UserError('Esta acción solo aplica a notificaciones del BDV.')

        payload = {}
        try:
            payload = json.loads(self.raw_payload or '{}')
        except (TypeError, ValueError):
            payload = {}

        provider = self.env['payment.provider'].sudo().search([
            ('code', '=', 'bdv'),
            ('is_bestpay_provider', '=', True),
        ], limit=1)
        env_type = (provider.bdv_environment if provider else 'qa') or 'qa'

        partners = self.env['res.partner'].sudo()._bdv_find_notification_partners(
            self.merchant_phone or payload.get('numeroComercio') or '',
            '',
            env_type,
        )

        result = self.env['payment.transaction'].sudo().bdv_process_notification_webhook(
            payload, partners, notification=self)

        if result.get('matched'):
            title = 'Pago casado'
            message = 'La notificación quedó enlazada a %s.' % (
                self.transaction_id.reference or self.transaction_id.id)
            notif_type = 'success'
        elif result.get('status') == 'ambiguous':
            title = 'Sigue ambiguo'
            message = 'Hay varias transacciones abiertas que coinciden. Revisa manualmente.'
            notif_type = 'warning'
        else:
            title = 'Sin coincidencias'
            message = 'No se encontró una transacción abierta que coincida con este aviso.'
            notif_type = 'warning'

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': notif_type,
                'sticky': False,
            },
        }

    def action_bdv_open_assign_wizard(self):
        """Abre el wizard para asignar el aviso a una transacción concreta."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Asignar notificación del BDV',
            'res_model': 'bestpay.bdv.assign.notification.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_notification_id': self.id,
            },
        }
