# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request
import json
import logging
import base64
import binascii

_logger = logging.getLogger(__name__)


class BestPayApiController(http.Controller):

    @http.route('/api/v1/transaction/create', type='json', auth='none', methods=['POST'], csrf=False)
    def create_transaction_api(self, **kwargs):
        # 1. Validar credenciales M2M (Client Credentials)
        auth_header = request.httprequest.headers.get('Authorization', '')
        
        if not auth_header.startswith('Basic '):
            return {
                'status': 'error',
                'message': 'Autenticación requerida. Use Basic Auth con Client ID y Client Secret.'
            }

        try:
            # Validamos que sea un token Basic bien formado y luego buscamos por el token codificado.
            encoded_token = auth_header[6:].strip()
            decoded_credentials = base64.b64decode(encoded_token, validate=True).decode('utf-8')
            if ':' not in decoded_credentials:
                raise ValueError('Formato inválido')
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return {'status': 'error', 'message': 'Credenciales Basic malformadas.'}

        # Al usar auth='none', necesitamos forzar el uso de una base de datos
        if not request.db:
            return {'status': 'error', 'message': 'Base de datos no especificada en la petición.'}

        # 2. Validar que el cliente exista
        partner = request.env['res.partner'].sudo().search([
            ('is_api_client', '=', True),
            ('bestpay_basic_token', '=', encoded_token),
        ], limit=1)
        
        if not partner:
            return {'status': 'error', 'message': 'Credenciales inválidas o cliente no autorizado.'}

        # 3. Leer los datos directamente de kwargs (inyectados por type='json')
        provider_code = kwargs.get('provider')
        currency_code = kwargs.get('currency')
        amount = kwargs.get('amount')
        external_reference = kwargs.get('external_reference')
        note = kwargs.get('note', '')

        # Validaciones de campos obligatorios
        if not all([provider_code, currency_code, amount, external_reference]):
            return {
                'status': 'error', 
                'message': 'Faltan campos obligatorios: (provider, currency, amount, external_reference).'
            }
        
        if external_reference:
            # Buscamos si ya existe una transacción aprobada, pendiente o en borrador 
            # para este mismo cliente con esa misma referencia externa.
            transaccion_duplicada = request.env['payment.transaction'].sudo().search([
                ('bestpay_client_id', '=', partner.id),
                ('external_reference', '=', external_reference),
                # Dependiendo de tu lógica, puedes filtrar por estados no fallidos:
                ('state', 'not in', ['cancel', 'error']) 
            ], limit=1)

            if transaccion_duplicada:
                return {
                    'status': 'error',
                    'message': f'Transacción duplicada. La referencia externa "{external_reference}" ya está registrada para este cliente.',
                    # Opcional: Le devuelves los datos de la que ya existe por si necesita recuperarla
                    'transaction_id': transaccion_duplicada.id,
                    'odoo_reference': transaccion_duplicada.reference,
                    'uuid_hash': transaccion_duplicada.uuid_hash,
                    'transaction_status': dict(transaccion_duplicada._fields['state']._description_selection(request.env)).get(transaccion_duplicada.state),
                }

        # 4. Validar Proveedor de pagos y permisos
        provider = request.env['payment.provider'].sudo().search([('code', '=', provider_code),('is_bestpay_provider', '=', True)], limit=1)
        if not provider:
            return {'status': 'error', 'message': f'El proveedor "{provider_code}" no existe.'}
        
        if provider.id not in partner.allowed_provider_ids.ids:
            return {'status': 'error', 'message': f'El proveedor "{provider_code}" no está permitido para este cliente.'}
        
        method_code = kwargs.get('payment_method')
        payment_method = False
        
        # Intento 1: Si el tercero envió un método específico, lo buscamos y verificamos que pertenezca al proveedor
        if method_code:
            _logger.info(f"🔍 DEBUG: Buscando método '{method_code}' para proveedor ID: {provider.id if provider else 'NO HAY PROVEEDOR'}")
            
            payment_method = request.env['payment.method'].sudo().search([
                ('code', '=', str(method_code).lower()),
                ('provider_ids', 'in', provider.id)
            ], limit=1)
            
            _logger.info(f"🔍 DEBUG: Resultado de la búsqueda: {payment_method}")

         # Intento 2 (Fallback): Si no lo envió o el código no era válido, tomamos el primero del proveedor
        if not payment_method:
            payment_method = provider.payment_method_ids[:1]

        # Fallback BestPay: Buscar cualquier método activo si es proveedor BestPay
        # NOTA: Esto es temporal solo para desarrollo local
        # if not payment_method and getattr(provider, 'is_bestpay_provider', False):
        #     payment_method = request.env['payment.method'].sudo().search([('active', '=', True)], limit=1)

        # VALIDACIÓN ESTRICTA: Si sigue sin haber método, ¡ERROR REAL! (No más silent fails)
        if not payment_method:
            return {
                'status': 'error', 
                'message': f'No se encontró el método de pago "{method_code or "por defecto"}" vinculado al proveedor "{provider_code}". Verifica la configuración en Odoo.'
            }
        
        # 5. Validar Moneda
        currency = request.env['res.currency'].sudo().search([('name', '=', str(currency_code).upper())], limit=1)
        if not currency:
            return {'status': 'error', 'message': f'La moneda "{currency_code}" no existe.'}
        
        # Forzamos la verificación/búsqueda de la tasa actual
        # Creamos un registro dummy o usamos el entorno para invocar el método auxiliar
        tx_dummy = request.env['payment.transaction'].sudo().new()
        tasa_bcv = tx_dummy._get_bcv_rate_or_fetch()

        try:
            monto_recibido = float(amount)
            if monto_recibido <= 0:
                return {'status': 'error', 'message': 'El monto debe ser un valor mayor a cero.'}
        except (ValueError, TypeError):
            return {'status': 'error', 'message': 'El formato del campo "amount" es inválido.'}
        monto_odoo_usd = 0.0
        monto_calculado_ves = 0.0
        recalcular = False
        tasa_para_guardar = tasa_bcv # Por defecto es la tasa USD -> VES

        # Buscamos la moneda USD en Odoo solo para el caso de reconversión de VES
        currency_usd = request.env['res.currency'].sudo().search([('name', '=', 'USD')], limit=1)

        # Variables temporales para el .create() que por defecto toman lo que viene de la API
        monto_final_odoo = monto_recibido
        currency_final = currency

        if currency_code.upper() == 'VES':
            monto_calculado_ves = monto_recibido
            monto_odoo_usd = round(monto_recibido / tasa_bcv, 2)
            recalcular = False
            tasa_para_guardar = tasa_bcv 
            
            # CAMBIO CLAVE: Si es VES, el monto principal de Odoo será USD
            monto_final_odoo = monto_odoo_usd
            currency_final = currency_usd
            
        elif currency_code.upper() == 'USD':
            monto_calculado_ves = round(monto_recibido * tasa_bcv, 2)
            recalcular = True
            tasa_para_guardar = tasa_bcv # Guarda la tasa USD -> VES
            
        elif currency_code.upper() == 'EUR':
            # CASO 3: Viene en EUR (Mantenemos intacta tu lógica original)
            currency_eur = request.env['res.currency'].sudo().search([('name', '=', 'EUR')], limit=1)
            latest_rate_eur = request.env['res.currency.rate'].sudo().search([
                ('currency_id', '=', currency_eur.id)
            ], order='name desc', limit=1)
            
            tasa_eur_en_usd = latest_rate_eur.rate if latest_rate_eur else 1.0
            
            # 1. Calculamos el USD base redondeado 
            monto_odoo_usd = round(monto_recibido / tasa_eur_en_usd, 2)
            
            # 2. Calculamos el monto final en VES usando el USD ya normalizado
            monto_calculado_ves = round(monto_odoo_usd * tasa_bcv, 2)
            recalcular = True
            
            # 3. La tasa guardada es el resultado real y neto de la operación
            tasa_para_guardar = round(monto_calculado_ves / monto_recibido, 4)

        payload_recibido = kwargs
        # [Paso 6 - Creación del registro]
        try:
            internal_reference = f"BPAY-{external_reference}-{fields.Datetime.now().strftime('%Y%m%d%H%M%S')}"
            
            tx = request.env['payment.transaction'].sudo().create({
                'reference': internal_reference,
                'amount': monto_final_odoo, # Dinámico: Guarda USD si vino VES, o el original si vino USD/EUR
                'currency_id': currency_final.id, # Dinámico: Moneda USD si vino VES, o la original si vino USD/EUR
                'provider_id': provider.id,
                'payment_method_id': payment_method.id,
                'partner_id': partner.id,                  
                'bestpay_client_id': partner.id,           
                'external_reference': external_reference,
                'client_note': note,
                'payment_request_payload': payload_recibido,
                # Campos de control BestPay (Ahora todos los escenarios los aprovechan correctamente)
                'amount_ves': monto_calculado_ves,
                'exchange_rate_bcv': tasa_para_guardar,
                'is_recalculable_ves': recalcular, 
            })
        except Exception as e:
            return {'status': 'error', 'message': f'Error en Odoo: {str(e)}'}

        # 7. Delegar el procesamiento al banco
        try:
            datos_banco = tx._bestpay_process_transaction_with_bank(kwargs)
        except Exception as e:
            error_msg = f'Error al comunicar con el banco: {str(e)}'
            
            # --- SOLUCIÓN: Marcamos la transacción como fallida en Odoo ---
            try:
                tx._set_error(error_msg)
            except Exception as tx_err:
                # Respaldo por si _set_error falla debido a alguna restricción interna de Odoo
                tx.write({'state': 'error', 'payment_request_response': f"Fallo crítico: {error_msg}. Error interno: {str(tx_err)}"})
            
            return {
                'status': 'error', 
                'message': error_msg,
                'transaction_status': 'error'  # Le avisamos al tercero que el registro quedó en error
            }
        
        if datos_banco and 'error' in datos_banco:
            try:
                tx._set_error(datos_banco['error'])
            except Exception as tx_err:
                tx.write({'state': 'error', 'payment_request_response': f"Fallo crítico: {datos_banco['error']}. Error interno: {str(tx_err)}"})
            
            return {
                'status': 'error', 
                'message': datos_banco['error'],
                'transaction_status': 'error'  # Le avisamos al tercero que el registro quedó en error
            }

        # 8. Respuesta exitosa
        return {
            'status': 'success',
            'transaction_id': tx.id,
            'odoo_reference': tx.reference,
            'external_reference': tx.external_reference,
            'uuid_hash': tx.uuid_hash,
            'flow_type': tx.bestpay_flow_type,
            'payment_details': datos_banco
        }