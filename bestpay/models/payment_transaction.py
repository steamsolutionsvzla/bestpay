# -*- coding: utf-8 -*-
from odoo import models, fields

class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_api_client = fields.Boolean(
        string="Es Cliente BestPay", 
        help="Indica si este tercero está autorizado para consumir la API de BestPay.",
        default=False
    )
    bestpay_token = fields.Char(
        string="Token API BestPay", 
        help="Token único para autenticar las peticiones de este cliente."
    )
    allowed_provider_ids = fields.Many2many(
        'payment.provider',
        'res_partner_payment_provider_rel',
        'partner_id',
        'provider_id',
        string="Bancos/Pasarelas Permitidas"
    )

class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    # Datos de origen del tercero
    bestpay_client_id = fields.Many2one(
        'res.partner', 
        string="Cliente API (BestPay)",
        domain="[('is_api_client', '=', True)]"
    )
    external_reference = fields.Char(
        string="Referencia Externa (Tercero)", 
        help="ID de la orden, factura o referencia del sistema del tercero."
    )
    client_note = fields.Text(string="Nota del Tercero")

    # Flexibilidad para el flujo del banco destino
    bestpay_flow_type = fields.Selection([
        ('redirect', 'Redirección a URL (Link)'),
        ('qr', 'Código QR'),
        ('direct_data', 'Datos Manuales (Pago Móvil / Transferencia)'),
        ('otp_required', 'Requiere Código SMS/OTP de confirmación')
    ], string="Tipo de Flujo del Banco", default='redirect')

    # Datos dinámicos devueltos procesados
    bank_response_json = fields.Text(
        string="Datos de Respuesta Estructurados",
        help="Estructura JSON que la API devolverá al tercero según el flujo del banco."
    )
    
    # Log técnico para auditoría en caso de fallos
    bank_raw_log = fields.Text(
        string="Log Crudo del Banco", 
        help="Respuesta exacta sin procesar recibida del API del banco."
    )