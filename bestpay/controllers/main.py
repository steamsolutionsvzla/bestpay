# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
import json

class BestPayApiController(http.Controller):

    @http.route('/api/v1/transaction/create', type='json', auth='none', methods=['POST'], csrf=False)
    def create_transaction_api(self, **kwargs):
        # 1. Obtener el Token desde las cabeceras (Headers)
        token = request.httprequest.headers.get('Authorization')
        if not token:
            return {
                'estado': 'error', 
                'mensaje': 'No se proporcionó el token de autenticación en las cabeceras (Authorization).'
            }
            
        # 2. Validar que el cliente (tercero) exista y esté activo
        partner = request.env['res.partner'].sudo().search([
            ('is_api_client', '=', True),
            ('bestpay_token', '=', token)
        ], limit=1)
        
        if not partner:
            return {
                'estado': 'error', 
                'mensaje': 'Token inválido o el cliente no está autorizado en BestPay.'
            }

        # 3. Leer y validar los datos del JSON enviado por el tercero
        try:
            data = request.get_json_data()
        except Exception:
            return {
                'estado': 'error', 
                'mensaje': 'El cuerpo de la petición no es un JSON válido.'
            }

        provider_code = data.get('provider')
        currency_code = data.get('currency') # Recibimos 'USD', 'VES', etc.
        amount = data.get('amount')
        external_reference = data.get('external_reference')

        # Validaciones básicas de campos obligatorios
        if not all([provider_code, currency_code, amount, external_reference]):
            return {
                'estado': 'error', 
                'mensaje': 'Faltan campos obligatorios en la petición (provider, currency, amount, external_reference).'
            }

        # 4. Validar si el banco/proveedor está permitido para este cliente específico
        provider = request.env['payment.provider'].sudo().search([('code', '=', provider_code)], limit=1)
        if not provider:
            return {
                'estado': 'error', 
                'mensaje': f'El proveedor de pago "{provider_code}" no existe en el sistema.'
            }
        
        if provider.id not in partner.allowed_provider_ids.ids:
            return {
                'estado': 'error', 
                'mensaje': f'El proveedor de pago "{provider_code}" no está permitido para este cliente.'
            }

        # 5. Buscar la moneda en Odoo por su código ISO (USD, VES)
        currency = request.env['res.currency'].sudo().search([('name', '=', str(currency_code).upper())], limit=1)
        if not currency:
            return {
                'estado': 'error', 
                'mensaje': f'La moneda "{currency_code}" no está configurada o no existe en el sistema.'
            }

        # 6. Crear la transacción en Odoo utilizando los modelos nativos herederos
        try:
            tx = request.env['payment.transaction'].sudo().create({
                'amount': float(amount),
                'currency_id': currency.id,       # Aquí asignamos el ID interno de Odoo encontrado
                'provider_id': provider.id,
                'partner_id': partner.id,          # El cliente en Odoo para la contabilidad
                'bestpay_client_id': partner.id,   # Tu campo personalizado
                'external_reference': external_reference,
                'client_note': data.get('note', ''),
            })
        except Exception as e:
            return {
                'estado': 'error', 
                'mensaje': f'Error interno al registrar la transacción en Odoo: {str(e)}'
            }

        # 7. Delegar el procesamiento real al método que heredarán los módulos de los bancos
        # Este método devolverá el diccionario con los datos que el banco necesita (URLs, QR, etc.)
        try:
            # datos_banco = tx._bestpay_process_transaction_with_bank(data)
            datos_banco = {}
        except Exception as e:
            return {
                'estado': 'error', 
                'mensaje': f'Error al comunicar con el banco/proveedor: {str(e)}'
            }

        # 8. Respuesta exitosa estructurada para el tercero
        return {
            'estado': 'exitoso',
            'transaccion_id': tx.id,
            'referencia_externa': tx.external_reference,
            'tipo_flujo': tx.bestpay_flow_type,
            'datos_pago': datos_banco  # JSON procesado que viene del módulo satélite del banco
        }