# -*- coding: utf-8 -*-
import json
import secrets
import string

from odoo import api, fields, models

from ..utils.identity import normalize_document, normalize_phone, parse_amount


class BestpayBankNotification(models.Model):
    _name = 'bestpay.bank.notification'
    _description = 'Registro de Notificaciones Bancarias (Webhooks)'
    _order = 'received_at DESC'

    # 1. Identificación del origen
    provider_code = fields.Char(string="Proveedor", help="Ej: 'bdv', 'mercantil'")
    partner_id = fields.Many2one(
        'res.partner',
        string="Comercio Receptor",
        help="Cliente de BestPay que recibió el dinero. Se asigna cuando la "
             "notificación se casa con una transacción; el teléfono del "
             "comercio no alcanza cuando varios clientes comparten la cuenta.",
    )

    # 2. Relación con la transacción (si el motor de casado tuvo éxito)
    transaction_id = fields.Many2one('payment.transaction', string="Transacción Casada")

    # 3. Datos extraídos para vista rápida (sin tener que abrir el JSON)
    external_ref = fields.Char(
        string="Referencia Externa",
        help="Referencia que manda el banco",
        index=True,
    )
    amount = fields.Float(string="Monto Notificado", digits=(12, 2))
    payer_phone = fields.Char(
        string="Teléfono Pagador",
        index=True,
        help="Teléfono del pagador tal como lo manda el banco (con 0 inicial).",
    )
    payer_phone_key = fields.Char(
        string="Teléfono Pagador (Clave)",
        index=True,
        copy=False,
        help="Teléfono del pagador normalizado a 10 dígitos para comparar.",
    )
    payer_document = fields.Char(
        string="Documento Pagador",
        help="Cédula/documento del pagador tal como lo manda el banco.",
    )
    payer_document_key = fields.Char(
        string="Documento Pagador (Clave)",
        index=True,
        copy=False,
        help="Documento del pagador normalizado para comparar.",
    )
    merchant_phone = fields.Char(
        string="Teléfono Comercio",
        index=True,
        help="Teléfono del comercio (numeroComercio) que envió el banco.",
    )
    merchant_rif = fields.Char(
        string="RIF Comercio",
        help="RIF del comercio (idComercio) que envió el banco.",
    )

    # 4. Estado del procesamiento
    status = fields.Selection([
        ('received', 'Recibido (Pendiente)'),
        ('matched', 'Casado Exitosamente'),
        ('matched_late', 'Casado (Llegó Tarde)'),
        ('ambiguous', 'Ambiguo (Revisión)'),
        ('orphan', 'Huérfana (Sin Transacción)'),
        ('error', 'Error / No se pudo casar'),
    ], string="Estado", default='received', index=True)

    match_method = fields.Selection([
        ('reference', 'Referencia bancaria'),
        ('payer_identity', 'Teléfono/Cédula del pagador'),
        ('amount', 'Monto único'),
        ('checkout_polling', 'Consulta del checkout'),
        ('manual', 'Manual'),
    ], string="Método de Casado", copy=False, readonly=True)
    candidate_count = fields.Integer(
        string="Candidatos Evaluados",
        default=0,
        copy=False,
        readonly=True,
        help="Cantidad de transacciones abiertas que coincidían con el monto.",
    )
    matched_at = fields.Datetime(
        string="Fecha de Casado",
        copy=False,
        readonly=True,
    )
    match_note = fields.Text(
        string="Nota de Casado",
        copy=False,
        readonly=True,
        help="Observación de una asignación manual o de una revisión.",
    )

    # 5. El dato más importante para debugging
    raw_payload = fields.Text(string="Payload Crudo (JSON)", help="JSON exacto recibido del banco")

    # 6. Metadatos
    received_at = fields.Datetime(string="Fecha de Recepción", default=fields.Datetime.now, index=True)
    error_message = fields.Text(string="Motivo de Fallo", readonly=True)

    def action_view_transaction(self):
        """Acción para ir a la transacción desde la notificación."""
        self.ensure_one()
        if self.transaction_id:
            return {
                'type': 'ir.actions.act_window',
                'name': 'Transacción de Pago',
                'res_model': 'payment.transaction',
                'res_id': self.transaction_id.id,
                'view_mode': 'form',
                'target': 'current',
            }
        return {'type': 'ir.actions.act_window_close'}

    @api.model
    def _bestpay_get_or_create_from_bdv_payload(self, payload):
        """Crea o actualiza la notificación a partir del JSON del BDV.

        Devuelve ``(notification, created)``. La notificación nace sin partner:
        el partner se asigna cuando el motor de casado encuentra la
        transacción. Así, con una cuenta compartida, no se queda pegada al
        primer partner que devolvía el teléfono del comercio.
        """
        payload = payload or {}
        external_ref = str(payload.get('referenciaBancoOrdenante') or '').strip()
        if not external_ref:
            # Sin referencia del banco no se puede deduplicar por referencia.
            # Se usa una clave sintética sin dígitos para que dos avisos
            # distintos no se pisen y para que no pueda coincidir por sufijo
            # numérico con una referencia real.
            external_ref = 'SIN-REF-%s' % ''.join(
                secrets.choice(string.ascii_uppercase) for _ in range(12))

        payer_phone_raw = str(payload.get('numeroCliente') or '').strip()
        payer_phone_key = normalize_phone(payer_phone_raw)
        if payer_phone_key:
            # Formato de visualización consistente: 0 + 10 dígitos.
            payer_phone_raw = '0' + payer_phone_key

        payer_document_raw = str(payload.get('idCliente') or '').strip()

        try:
            amount = parse_amount(payload.get('monto'))
        except (TypeError, ValueError):
            amount = 0.0

        vals = {
            'provider_code': 'bdv',
            'partner_id': False,
            'payer_phone': payer_phone_raw,
            'payer_phone_key': payer_phone_key,
            'payer_document': payer_document_raw,
            'payer_document_key': normalize_document(payer_document_raw),
            'merchant_phone': str(payload.get('numeroComercio') or '').strip(),
            'merchant_rif': str(payload.get('idComercio') or '').strip(),
            'external_ref': external_ref,
            'amount': amount,
            'status': 'received',
            'match_method': False,
            'candidate_count': 0,
            'matched_at': False,
            'match_note': False,
            'error_message': False,
            'raw_payload': json.dumps(payload, ensure_ascii=False, indent=2),
        }

        existing = self.sudo().search([
            ('external_ref', '=', external_ref),
            ('provider_code', '=', 'bdv'),
        ], limit=1, order='received_at DESC')

        if existing:
            # Re-notificación de un pago ya casado: no se reprocesa.
            if existing.transaction_id or existing.status in ('matched', 'matched_late'):
                return existing, False

            # Aviso pendiente/ambiguo/huérfano: se actualiza y se reintenta.
            existing.write(vals)
            return existing, False

        return self.sudo().create(vals), True
