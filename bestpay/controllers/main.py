# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request
import json
import logging

_logger = logging.getLogger(__name__)


class BestPayApiController(http.Controller):

    @http.route('/api/v1/transaction/create', type='json', auth='none', methods=['POST'], csrf=False)
    def create_transaction_api(self, **kwargs):
        # 1. Obtener y validar el Token desde las cabeceras
        auth_header = request.httprequest.headers.get('Authorization')
        if not auth_header:
            return {'estado': 'error', 'mensaje': 'Falta cabecera Authorization.'}
        
        parts = auth_header.split(' ')
        if len(parts) == 2 and parts[0].lower() == 'bearer':
            token = parts[1].strip()
        else:
            token = auth_header.strip()
        
        # Al usar auth='none', necesitamos forzar el uso de una base de datos si hay varias en el sistema
        # Odoo 19 requiere que request.env esté asociado a un registro válido.
        if not request.db:
            return {'estado': 'error', 'mensaje': 'Base de datos no especificada en la petición.'}

        # 2. Validar que el cliente exista utilizando .sudo() de manera segura
        partner = request.env['res.partner'].sudo().search([
            ('is_api_client', '=', True),
            ('bestpay_token', '=', token)
        ], limit=1)
        
        if not partner:
            return {'estado': 'error', 'mensaje': 'Token inválido o cliente no autorizado.'}

        # 3. Leer los datos directamente de kwargs (inyectados por type='json')
        provider_code = kwargs.get('provider')
        currency_code = kwargs.get('currency')
        amount = kwargs.get('amount')
        external_reference = kwargs.get('external_reference')
        note = kwargs.get('note', '')

        # Validaciones de campos obligatorios
        if not all([provider_code, currency_code, amount, external_reference]):
            return {
                'estado': 'error', 
                'mensaje': 'Faltan campos obligatorios: (provider, currency, amount, external_reference).'
            }

        # 4. Validar Proveedor de pagos y permisos
        provider = request.env['payment.provider'].sudo().search([('code', '=', provider_code),('is_bestpay_provider', '=', True)], limit=1)
        if not provider:
            return {'estado': 'error', 'mensaje': f'El proveedor "{provider_code}" no existe.'}
        
        if provider.id not in partner.allowed_provider_ids.ids:
            return {'estado': 'error', 'mensaje': f'El proveedor "{provider_code}" no está permitido para este cliente.'}

        # 5. Validar Moneda
        currency = request.env['res.currency'].sudo().search([('name', '=', str(currency_code).upper())], limit=1)
        if not currency:
            return {'estado': 'error', 'mensaje': f'La moneda "{currency_code}" no existe.'}
        
        # Forzamos la verificación/búsqueda de la tasa actual
        # Creamos un registro dummy o usamos el entorno para invocar el método auxiliar
        tx_dummy = request.env['payment.transaction'].sudo().new()
        tasa_bcv = tx_dummy._get_bcv_rate_or_fetch()

        try:
            monto_recibido = float(amount)
            if monto_recibido <= 0:
                return {'estado': 'error', 'mensaje': 'El monto debe ser un valor mayor a cero.'}
        except (ValueError, TypeError):
            return {'estado': 'error', 'mensaje': 'El formato del campo "amount" es inválido.'}
        monto_odoo_usd = 0.0
        monto_calculado_ves = 0.0
        recalcular = False

        if currency_code.upper() == 'VES':
            # CASO 1: Viene en VES. Monto VES queda FIJO. Recalculamos el equivalente USD base.
            monto_calculado_ves = monto_recibido
            monto_odoo_usd = monto_recibido / tasa_bcv
            recalcular = False  # NUNCA se recalcula, se respeta el monto VES de origen
            
        elif currency_code.upper() == 'USD':
            # CASO 2: Viene en USD. El monto VES es una foto de HOY, pero puede variar mañana.
            monto_odoo_usd = monto_recibido
            monto_calculado_ves = monto_recibido * tasa_bcv
            recalcular = True   # SE MARCA para recalcular en caliente antes de pagar
            
        elif currency_code.upper() == 'EUR':
            # CASO 3: Viene en EUR. Buscamos tasa de Euro para llevarlo a la base USD de Odoo
            currency_eur = request.env['res.currency'].sudo().search([('name', '=', 'EUR')], limit=1)
            latest_rate_eur = request.env['res.currency.rate'].sudo().search([
                ('currency_id', '=', currency_eur.id)
            ], order='name desc', limit=1)
            tasa_eur_en_usd = latest_rate_eur.rate if latest_rate_eur else 1.0
            
            monto_odoo_usd = monto_recibido / tasa_eur_en_usd
            monto_calculado_ves = monto_odoo_usd * tasa_bcv
            recalcular = True   # SE MARCA para recalcular en caliente

        # [Paso 6 - Creación del registro]
        try:
            internal_reference = f"BPAY-{external_reference}-{fields.Datetime.now().strftime('%Y%m%d%H%M%S')}"
            
            tx = request.env['payment.transaction'].sudo().create({
                'reference': internal_reference,
                'amount': monto_recibido, # Odoo guarda el importe en la moneda original del documento
                'currency_id': currency.id,
                'provider_id': provider.id,
                'partner_id': partner.id,                  
                'bestpay_client_id': partner.id,           
                'external_reference': external_reference,
                'client_note': note,
                # Campos de control BestPay
                'amount_ves': monto_calculado_ves,
                'exchange_rate_bcv': tasa_bcv,
                'is_recalculable_ves': recalcular, # Aquí queda la bandera guardada
            })
        except Exception as e:
            return {'estado': 'error', 'mensaje': f'Error en Odoo: {str(e)}'}

        # 7. Delegar el procesamiento al banco
        try:
            # datos_banco = tx._bestpay_process_transaction_with_bank(kwargs)
            datos_banco = {}
        except Exception as e:
            return {'estado': 'error', 'mensaje': f'Error al comunicar con el banco: {str(e)}'}

        # 8. Respuesta exitosa
        return {
            'estado': 'exitoso',
            'transaccion_id': tx.id,
            'odoo_reference': tx.reference,
            'referencia_externa': tx.external_reference,
            'tipo_flujo': tx.bestpay_flow_type,
            'datos_pago': datos_banco
        }