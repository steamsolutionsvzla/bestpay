# -*- coding: utf-8 -*-
import logging
import secrets
import json
from odoo import models, fields, api
    
_logger = logging.getLogger(__name__)

class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    is_bestpay_provider = fields.Boolean(
        string="Es Proveedor BestPay", 
        default=False,
        help="Marque esta casilla si este proveedor/banco es utilizado por la API de BestPay."
    )

class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_api_client = fields.Boolean(
        string="Es Cliente BestPay", 
        help="Indica si este tercero está autorizado para consumir la API de BestPay.",
        default=False
    )

    bestpay_client_id = fields.Char(
        string="BestPay Client ID",
        help="Identificador público para autenticación M2M.",
        copy=False,
    )
    bestpay_client_secret = fields.Char(
        string="BestPay Client Secret",
        help="Credencial privada para autenticación M2M.",
        copy=False,
    )

    allowed_provider_ids = fields.Many2many(
        'payment.provider',
        'res_partner_payment_provider_rel',
        'partner_id',
        'provider_id',
        string="Bancos/Pasarelas Permitidas",
        domain="[('is_bestpay_provider', '=', True)]"
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

    uuid_hash = fields.Char(
        string="Hash Único de Transacción",
        copy=False,
        readonly=True,
        index=True,
        help="Hash seguro y único utilizado para exponer la transacción en las URLs de redirección sin revelar el ID."
    )

    @api.model_create_multi
    def create(self, vals_list):
        """
        Sobrescribimos el create para generar un hash seguro antes de guardar en la BD.
        Odoo 19 utiliza obligatoriamente create_multi (recibe una lista de diccionarios).
        """
        for vals in vals_list:
            # secrets.token_urlsafe(32) genera una cadena aleatoria y segura de ~43 caracteres,
            # perfecta para URLs (usa caracteres A-Z, a-z, 0-9, -, y _).
            if not vals.get('uuid_hash'):
                vals['uuid_hash'] = secrets.token_urlsafe(32)

            if 'payment_request_payload' in vals and isinstance(vals['payment_request_payload'], (dict, list)):
                try:
                    vals['payment_request_payload'] = json.dumps(vals['payment_request_payload'], ensure_ascii=False, indent=4)
                except Exception as e:
                    _logger.error("[BESTPAY] No se pudo serializar el payload del tercero: %s", e)
                
        return super(PaymentTransaction, self).create(vals_list)

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
    
    def _bestpay_action_recalculate_jit_ves(self):
        """
        [MÓDULO PRINCIPAL]
        Recálculo JIT corregido: Multiplica el monto original por la tasa directa 
        de la divisa correspondiente a la transacción.
        """
        self.ensure_one()
        if not self.is_recalculable_ves:
            return

        _logger.info("[BESTPAY CORE] Ejecutando recálculo JIT adaptado a divisa origen para Tx: %s", self.reference)
        
        today = fields.Date.today()
        currency_ves = self.env['res.currency'].search([('name', '=', 'VES')], limit=1)
        
        # 1. Validar/Actualizar la tasa en BD local
        latest_rate_ves = self.env['res.currency.rate'].search([
            ('currency_id', '=', currency_ves.id), ('name', '=', today), ('company_id', '=', self.env.company.id)
        ], limit=1)

        if not latest_rate_ves:
            self.env['res.currency']._update_bcv_rate()
            latest_rate_ves = self.env['res.currency.rate'].search([
                ('currency_id', '=', currency_ves.id), ('name', '=', today), ('company_id', '=', self.env.company.id)
            ], limit=1)

        if not latest_rate_ves:
            latest_rate_ves = self.env['res.currency.rate'].search([('currency_id', '=', currency_ves.id)], order='name desc', limit=1)

        tasa_bcv_ves = latest_rate_ves.rate if latest_rate_ves else 0.0

        if tasa_bcv_ves <= 0:
            return

        monto_original = self.amount
        nuevo_monto_ves = 0.0
        tasa_final_guardar = tasa_bcv_ves

        # CASO USD
        if self.currency_id.name == 'USD':
            nuevo_monto_ves = round(monto_original * tasa_bcv_ves, 2)
            tasa_final_guardar = tasa_bcv_ves

        # CASO EUR
        elif self.currency_id.name == 'EUR':
            currency_eur = self.env['res.currency'].search([('name', '=', 'EUR')], limit=1)
            latest_rate_eur = self.env['res.currency.rate'].search([
                ('currency_id', '=', currency_eur.id)
            ], order='name desc', limit=1)
            
            tasa_eur_en_usd = latest_rate_eur.rate if latest_rate_eur else 1.0
            
            # Re-calculamos pasando por la simulación estricta de Odoo
            monto_usd_simulado = round(monto_original / tasa_eur_en_usd, 2)
            nuevo_monto_ves = round(monto_usd_simulado * tasa_bcv_ves, 2)
            
            # Forzamos la tasa al despeje real de lo que se va a guardar
            tasa_final_guardar = round(nuevo_monto_ves / monto_original, 4)

        # 4. Guardar datos respetando la correlación de la divisa original
        if nuevo_monto_ves > 0:
            self.write({
                'amount_ves': nuevo_monto_ves,
                'exchange_rate_bcv': tasa_final_guardar
            })
            _logger.info("[BESTPAY CORE] Recálculo exitoso. Moneda: %s. Tasa Directa: %s. Monto: %s VES", self.currency_id.name, tasa_final_guardar, nuevo_monto_ves)

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

    payment_request_payload = fields.Text(
        string="Payload de Solicitud API",
        readonly=True,
        help="Cuerpo completo (JSON/Dict) enviado por el tercero para originar la transacción."
    )