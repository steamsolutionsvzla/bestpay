# -*- coding: utf-8 -*-
import json

from odoo import fields, models
from odoo.exceptions import UserError


class BestpayBDVAssignNotificationWizard(models.TransientModel):
    _name = 'bestpay.bdv.assign.notification.wizard'
    _description = 'Asignar manualmente una notificación BDV a una transacción'

    notification_id = fields.Many2one(
        'bestpay.bank.notification',
        string='Notificación',
        required=True,
        readonly=True,
        ondelete='cascade',
    )
    external_ref = fields.Char(
        related='notification_id.external_ref', readonly=True)
    amount = fields.Float(
        related='notification_id.amount', readonly=True)
    payer_phone = fields.Char(
        related='notification_id.payer_phone', readonly=True)
    payer_document = fields.Char(
        related='notification_id.payer_document', readonly=True)
    merchant_phone = fields.Char(
        related='notification_id.merchant_phone', readonly=True)

    transaction_id = fields.Many2one(
        'payment.transaction',
        string='Transacción',
        required=True,
        domain="[('provider_id.code', '=', 'bdv'), ('state', 'not in', ('cancel', 'error'))]",
        help="Transacción abierta (o ya pagada) a la que pertenece este aviso.",
    )
    note = fields.Text(
        string='Observación',
        help="Motivo de la asignación manual, para auditoría.",
    )

    def action_assign(self):
        self.ensure_one()
        notification = self.notification_id
        tx = self.transaction_id

        if not tx or tx.provider_id.code != 'bdv':
            raise UserError('La transacción seleccionada no es del proveedor BDV.')
        if tx.state in ('cancel', 'error'):
            raise UserError(
                'La transacción seleccionada está cancelada o con error; no se puede asignar.')
        if notification.transaction_id and notification.transaction_id != tx:
            raise UserError(
                'Esta notificación ya está asignada a la transacción %s.'
                % notification.transaction_id.reference)

        # Confirmar una transacción abierta con un monto distinto marcaría la
        # orden como pagada sin serlo. Solo se permite el enlace tardío a una
        # transacción ya pagada (queda como auditoría).
        if tx.state != 'done':
            expected_amount = tx.amount_ves or tx.amount or 0.0
            if abs((notification.amount or 0.0) - expected_amount) > 0.01:
                raise UserError(
                    'El monto del aviso (%s) no coincide con el de la '
                    'transacción %s (%s). Si es un pago parcial, no se puede '
                    'confirmar automáticamente.'
                    % (notification.amount, tx.reference, expected_amount))

        payload = {}
        try:
            payload = json.loads(notification.raw_payload or '{}')
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict) or not payload:
            payload = {
                'referenciaBancoOrdenante': notification.external_ref or '',
                'numeroCliente': notification.payer_phone or '',
                'idCliente': notification.payer_document or '',
                'monto': str(notification.amount or 0.0),
                'numeroComercio': notification.merchant_phone or '',
                'idComercio': notification.merchant_rif or '',
                'fecha': '',
                'hora': '',
            }

        result = tx._bdv_apply_notification_match(
            notification, tx, payload, 'manual', 1)

        if result.get('late'):
            message = (
                'La transacción ya estaba pagada. El aviso quedó enlazado '
                'como pago tardío para auditoría.')
            notif_type = 'warning'
        else:
            message = 'La transacción %s quedó confirmada con este aviso.' % tx.reference
            notif_type = 'success'

        if self.note:
            notification.write({
                'match_note': self.note,
            })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Notificación asignada',
                'message': message,
                'type': notif_type,
                'sticky': False,
            },
        }
