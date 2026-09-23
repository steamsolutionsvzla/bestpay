# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request
from datetime import timedelta
import json
import logging
import base64
import binascii

_logger = logging.getLogger(__name__)


class BestPayApiController(http.Controller):

    LINK_EXPIRATION_HOURS = 24  # Regla de negocio: los links de pago vencen a las 24h

    def _bestpay_normalize_payment_details(self, payment_details):
        if not isinstance(payment_details, dict):
            return {}

        normalized = dict(payment_details)
        if not normalized.get('payment_link'):
            for key in ('checkout_url', 'redirect_url', 'url', 'payment_url', 'link'):
                value = normalized.get(key)
                if value:
                    normalized['payment_link'] = value
                    break

        if normalized.get('payment_link') and 'checkout_url' not in normalized:
            normalized['checkout_url'] = normalized['payment_link']

        return normalized

    def _bestpay_extract_payment_link(self, tx=None, payment_details=None):
        payment_details = self._bestpay_normalize_payment_details(payment_details or {})
        if payment_details.get('payment_link'):
            return payment_details['payment_link']

        if tx:
            for field_name in ('payment_link', 'payment_link_client_mer', 'payment_link_bank_mer'):
                value = getattr(tx, field_name, False)
                if value:
                    return value

        return False

    def _bestpay_standard_response(self, tx=None, status='success', message='', transaction_status=None,
                                  payment_details=None, payment_link=None):
        payload = self._bestpay_normalize_payment_details(payment_details or {})
        resolved_payment_link = payment_link or self._bestpay_extract_payment_link(tx=tx, payment_details=payload)

        if tx:
            response = {
                'status': status,
                'message': message,
                'transaction_id': tx.id,
                'odoo_reference': tx.reference,
                'external_reference': tx.external_reference,
                'uuid_hash': tx.uuid_hash,
                'transaction_status': transaction_status or tx.state,
                'payment_link': resolved_payment_link,
                'flow_type': tx.bestpay_flow_type,
                'payment_details': payload,
            }
        else:
            response = {
                'status': status,
                'message': message,
                'transaction_status': transaction_status,
                'payment_link': resolved_payment_link,
                'payment_details': payload,
            }

        if not response.get('payment_link') and payload:
            response['payment_link'] = payload.get('payment_link')

        if response.get('payment_details') is None:
            response['payment_details'] = {}

        return response

    @http.route('/api/v1/transaction/create', type='json', auth='none', methods=['POST'], csrf=False)
    def create_transaction_api(self, **kwargs):
        # 1. Validar credenciales M2M (Client Credentials)
        auth_header = request.httprequest.headers.get('Authorization', '')
        
        if not auth_header.startswith('Basic '):
            return self._bestpay_standard_response(
                status='error',
                message='Autenticación requerida. Use Basic Auth con Client ID y Client Secret.',
                transaction_status='error'
            )

        try:
            # Validamos que sea un token Basic bien formado y luego buscamos por el token codificado.
            encoded_token = auth_header[6:].strip()
            decoded_credentials = base64.b64decode(encoded_token, validate=True).decode('utf-8')
            if ':' not in decoded_credentials:
                raise ValueError('Formato inválido')
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return self._bestpay_standard_response(
                status='error',
                message='Credenciales Basic malformadas.',
                transaction_status='error'
            )

        # Al usar auth='none', necesitamos forzar el uso de una base de datos
        if not request.db:
            return self._bestpay_standard_response(
                status='error',
                message='Base de datos no especificada en la petición.',
                transaction_status='error'
            )

        # 2. Validar que el cliente exista
        partner = request.env['res.partner'].sudo().search([
            ('is_api_client', '=', True),
            ('bestpay_basic_token', '=', encoded_token),
        ], limit=1)
        
        if not partner:
            return self._bestpay_standard_response(
                status='error',
                message='Credenciales inválidas o cliente no autorizado.',
                transaction_status='error'
            )

        # 3. Leer los datos directamente de kwargs (inyectados por type='json')
        provider_code = kwargs.get('provider')
        currency_code = kwargs.get('currency')
        amount = kwargs.get('amount')
        external_reference = kwargs.get('external_reference')
        note = kwargs.get('note', '')

        # Validaciones de campos obligatorios
        if not all([provider_code, currency_code, amount, external_reference]):
            return self._bestpay_standard_response(
                status='error',
                message='Faltan campos obligatorios: (provider, currency, amount, external_reference).',
                transaction_status='error'
            )
        
        if external_reference:
            transaccion_existente = request.env['payment.transaction'].sudo().search([
                ('bestpay_client_id', '=', partner.id),
                ('external_reference', '=', external_reference),
                ('state', 'not in', ['cancel', 'error']),
            ], limit=1, order='create_date desc')

            if transaccion_existente:
                # Caso 1: ya fue pagada -> nunca se crea una nueva, reutilizamos su link.
                # (la pantalla /pago/bdv/checkout ya bloquea reprocesar un pago 'done', es seguro)
                if transaccion_existente.state == 'done':
                    return self._bestpay_standard_response(
                        tx=transaccion_existente,
                        status='success',
                        message='Esta orden ya fue pagada.',
                        transaction_status='done',
                        payment_details={'payment_link': transaccion_existente.payment_link or self._bestpay_extract_payment_link(tx=transaccion_existente)},
                        payment_link=transaccion_existente.payment_link or self._bestpay_extract_payment_link(tx=transaccion_existente),
                    )

                # Caso 2: todavía no vence (< 24h desde su creación) -> reutilizamos el mismo link.
                limite_expiracion = fields.Datetime.now() - timedelta(hours=self.LINK_EXPIRATION_HOURS)
                if transaccion_existente.create_date >= limite_expiracion:
                    return self._bestpay_standard_response(
                        tx=transaccion_existente,
                        status='success',
                        message='Reutilizando la transacción existente.',
                        transaction_status=transaccion_existente.state,
                        payment_details={'payment_link': transaccion_existente.payment_link or self._bestpay_extract_payment_link(tx=transaccion_existente)},
                        payment_link=transaccion_existente.payment_link or self._bestpay_extract_payment_link(tx=transaccion_existente),
                    )

                # Caso 3: venció y no se pagó -> la cancelamos y dejamos que el flujo normal
                # de abajo cree una transacción nueva con el mismo external_reference.
                transaccion_existente.write({
                    'state': 'cancel',
                    'state_message': f'Cancelada automáticamente: link expirado ({self.LINK_EXPIRATION_HOURS}h) sin completar el pago.',
                })
                _logger.info(
                    "[API] Transacción %s expirada, cancelada automáticamente para permitir reintento (external_reference=%s)",
                    transaccion_existente.reference, external_reference
                )
                # No hay return aquí a propósito: el código sigue hacia abajo y crea la nueva.

        # 4. Validar Proveedor de pagos y permisos
        provider = request.env['payment.provider'].sudo().search([('code', '=', provider_code),('is_bestpay_provider', '=', True)], limit=1)
        if not provider:
            return self._bestpay_standard_response(
                status='error',
                message=f'El proveedor "{provider_code}" no existe.',
                transaction_status='error'
            )
        
        if provider.id not in partner.allowed_provider_ids.ids:
            return self._bestpay_standard_response(
                status='error',
                message=f'El proveedor "{provider_code}" no está permitido para este cliente.',
                transaction_status='error'
            )
        
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

        # VALIDACIÓN ESTRICTA: Si sigue sin haber método, ¡ERROR REAL! (No más silent fails)
        if not payment_method:
            return self._bestpay_standard_response(
                status='error',
                message=f'No se encontró el método de pago "{method_code or "por defecto"}" vinculado al proveedor "{provider_code}". Verifica la configuración en Odoo.',
                transaction_status='error'
            )
        
        # 5. Validar Moneda
        currency = request.env['res.currency'].sudo().search([('name', '=', str(currency_code).upper())], limit=1)
        if not currency:
            return self._bestpay_standard_response(
                status='error',
                message=f'La moneda "{currency_code}" no existe.',
                transaction_status='error'
            )
        
        # Forzamos la verificación/búsqueda de la tasa actual
        # Creamos un registro dummy o usamos el entorno para invocar el método auxiliar
        tx_dummy = request.env['payment.transaction'].sudo().new()
        tasa_bcv = tx_dummy._get_bcv_rate_or_fetch()

        try:
            monto_recibido = float(amount)
            if monto_recibido <= 0:
                return self._bestpay_standard_response(
                    status='error',
                    message='El monto debe ser un valor mayor a cero.',
                    transaction_status='error'
                )
        except (ValueError, TypeError):
            return self._bestpay_standard_response(
                status='error',
                message='El formato del campo "amount" es inválido.',
                transaction_status='error'
            )
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
            return self._bestpay_standard_response(
                status='error',
                message=f'Error en Odoo: {str(e)}',
                transaction_status='error'
            )

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
            
            return self._bestpay_standard_response(
                tx=tx,
                status='error',
                message=error_msg,
                transaction_status='error',
                payment_details={'error': error_msg},
                payment_link=self._bestpay_extract_payment_link(tx=tx),
            )
        
        if datos_banco and 'error' in datos_banco:
            try:
                tx._set_error(datos_banco['error'])
            except Exception as tx_err:
                tx.write({'state': 'error', 'payment_request_response': f"Fallo crítico: {datos_banco['error']}. Error interno: {str(tx_err)}"})
            
            return self._bestpay_standard_response(
                tx=tx,
                status='error',
                message=datos_banco['error'],
                transaction_status='error',
                payment_details=datos_banco,
                payment_link=self._bestpay_extract_payment_link(tx=tx, payment_details=datos_banco),
            )

        # 8. Respuesta exitosa
        payment_link = self._bestpay_extract_payment_link(tx=tx, payment_details=datos_banco)
        response = self._bestpay_standard_response(
            tx=tx,
            status='success',
            message='Transacción creada correctamente.',
            transaction_status=tx.state,
            payment_details=datos_banco,
            payment_link=payment_link,
        )
        tx.write({'payment_request_response': json.dumps(response, ensure_ascii=False, indent=4)})
        return response