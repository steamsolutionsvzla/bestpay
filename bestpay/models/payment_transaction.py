# -*- coding: utf-8 -*-
import logging
from odoo import models, fields
    
_logger = logging.getLogger(__name__)

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

    amount_ves = fields.Monetary(
        string="Importe VES",
        currency_field='currency_ves_id', # Necesitamos apuntar al ID de VES de forma auxiliar
        help="Monto equivalente o complementario en Bolívares."
    )
    exchange_rate_bcv = fields.Float(
        string="Tasa BCV Aplicada",
        digits=(12, 4),
        help="La tasa oficial del BCV utilizada en el momento exacto de crear esta transacción."
    )

    is_recalculable_ves = fields.Boolean(
        string="Recalcular VES al pagar",
        default=False,
        help="Si es True, el monto en VES se actualizará con la tasa del BCV del día en que se procese el pago."
    )
    
    # Campo técnico para que la UI de Odoo sepa cómo formatear el campo amount_ves
    currency_ves_id = fields.Many2one(
        'res.currency', 
        string="Moneda VES Auxiliar", 
        default=lambda self: self.env['res.currency'].search([('name', '=', 'VES')], limit=1)
    )

    def _get_bcv_rate_or_fetch(self):
        """
        Busca la tasa de hoy. Si no existe, ejecuta el scraping 
        en tiempo real para traerla y guardarla antes de proceder.
        """
        self.ensure_one()
        today = fields.Date.today()
        currency_ves = self.env['res.currency'].search([('name', '=', 'VES')], limit=1)
        
        # Intentar buscar la tasa de hoy
        rate_row = self.env['res.currency.rate'].search([
            ('currency_id', '=', currency_ves.id),
            ('name', '=', today)
        ], limit=1)
        
        if not rate_row:
            _logger.info("[BESTPAY] Tasa de hoy no encontrada. Ejecutando actualización en tiempo real...")
            # Llamamos a tu función del Cron bajo demanda
            self.env['res.currency']._update_bcv_rate()
            # Volvemos a buscar
            rate_row = self.env['res.currency.rate'].search([
                ('currency_id', '=', currency_ves.id),
                ('name', '=', today)
            ], limit=1)
            
        # Si aun así no hay (ej. caída de la web del BCV), usamos la última disponible históricamente
        if not rate_row:
            rate_row = self.env['res.currency.rate'].search([
                ('currency_id', '=', currency_ves.id)
            ], order='name desc', limit=1)
            
        return rate_row.rate if rate_row else 1.0

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