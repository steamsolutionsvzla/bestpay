# -*- coding: utf-8 -*-
from odoo import api, fields, models

class BestpayBankNotification(models.Model):
    _name = 'bestpay.bank.notification'
    _description = 'Registro de Notificaciones Bancarias (Webhooks)'
    _order = 'received_at DESC'

    # 1. Identificación del origen
    provider_code = fields.Char(string="Proveedor", help="Ej: 'bdv', 'mercantil'")
    partner_id = fields.Many2one('res.partner', string="Comercio Receptor", help="Cliente de BestPay que recibió el dinero")
    
    # 2. Relación con la transacción (si el motor de casado tuvo éxito)
    transaction_id = fields.Many2one('payment.transaction', string="Transacción Casada")
    
    # 3. Datos extraídos para vista rápida (sin tener que abrir el JSON)
    external_ref = fields.Char(string="Referencia Externa", help="Referencia que manda el banco")
    amount = fields.Float(string="Monto Notificado", digits=(12, 2))
    
    # 4. Estado del procesamiento
    status = fields.Selection([
        ('received', 'Recibido (Pendiente)'),
        ('matched', 'Casado Exitosamente'),
        ('error', 'Error / No se pudo casar'),
    ], string="Estado", default='received', index=True)
    
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