# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request
import json
import logging
import base64
import binascii

_logger = logging.getLogger(__name__)


class BestPayApiController(http.Controller):

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
                # Campos monetarios (misma lógica que el webhook de Mercantil)
                'amount': tx.amount,
                'currency': tx.currency_id.name,
                'amount_ves': tx.amount_ves,
                'exchange_rate': tx.exchange_rate_bcv,
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
        # ========================================================================
        # 1. VERIFICAR QUE EL PAYLOAD ESTÉ CIFRADO (OBLIGATORIO)
        # ========================================================================
        is_encrypted = request.httprequest.headers.get('X-BestPay-Encrypted') == 'true'
        if not is_encrypted:
            return self._bestpay_standard_response(
                status='error',
                message='Payload debe estar cifrado con AES-256-GCM. Agregar header X-BestPay-Encrypted: true',
                transaction_status='error'
            )

        # ========================================================================
        # 2. VALIDAR CREDENCIALES M2M (Client Credentials) PARA OBTENER EL PARTNER
        # ========================================================================
        auth_header = request.httprequest.headers.get('Authorization', '')
        if not auth_header.startswith('Basic '):
            return self._bestpay_standard_response(
                status='error',
                message='Autenticación requerida. Use Basic Auth con Client ID y Client Secret.',
                transaction_status='error'
            )

        try:
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

        if not request.db:
            return self._bestpay_standard_response(
                status='error',
                message='Base de datos no especificada en la petición.',
                transaction_status='error'
            )

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

        # ========================================================================
        # 3. DESCIFRAR EL PAYLOAD USANDO LA CLAVE DEL PARTNER
        # ========================================================================
        try:
            from ..lib.crypto_utils import decrypt_payload
            
            # En type='json', Odoo ya puso el body parseado en kwargs
            encrypted_data = kwargs
            
            # Desciframos usando la clave del partner que acabamos de validar
            decrypted_data = decrypt_payload(encrypted_data, partner.bestpay_encryption_key)
            
            # TRUCO: Actualizamos kwargs con los datos descifrados. 
            # Así el resto de tu código sigue funcionando sin cambiar nada.
            kwargs.update(decrypted_data)
            
        except Exception as e:
            return self._bestpay_standard_response(
                status='error',
                message=f'Error descifrando payload: {str(e)}',
                transaction_status='error'
            )

        # ========================================================================
        # 4. RESTO DE TU LÓGICA (INTACTA, AHORA USA LOS DATOS DESCIFRADOS)
        # ========================================================================
        # 3. Leer los datos directamente de kwargs (ahora descifrados)
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

            if transaccion_existente.state == 'done':
                response = self._bestpay_standard_response(
                    tx=transaccion_existente,
                    status='success',
                    message='Esta orden ya fue pagada.',
                    transaction_status='done',
                    payment_details={'payment_link': transaccion_existente.payment_link or self._bestpay_extract_payment_link(tx=transaccion_existente)},
                    payment_link=transaccion_existente.payment_link or self._bestpay_extract_payment_link(tx=transaccion_existente),
                )
                # CIFRAMOS ANTES DE RETORNAR
                from ..lib.crypto_utils import encrypt_payload
                return encrypt_payload(response, partner.bestpay_encryption_key)

            # --- NUEVO: VALIDACIÓN DEFENSIVA PARA EVITAR EL ERROR DE FECHA ---
            if not transaccion_existente.create_date:
                _logger.warning(
                    "[API] Transacción ID=%s (Ref: %s) encontrada sin fecha de creación (corrupta). Se cancela para permitir reintento.",
                    transaccion_existente.id, external_reference
                )
                transaccion_existente.write({
                    'state': 'cancel',
                    'state_message': 'Cancelada: fecha de creación inválida/corrupta.',
                })
            else:
                # --- LÓGICA DE EXPIRACIÓN DEL LINK ---
                # El vencimiento se calcula con create_date + 48h (ver modelo).
                if not transaccion_existente._bestpay_is_link_expired():
                    response = self._bestpay_standard_response(
                        tx=transaccion_existente,
                        status='success',
                        message='Reutilizando la transacción existente.',
                        transaction_status=transaccion_existente.state,
                        payment_details={'payment_link': transaccion_existente.payment_link or self._bestpay_extract_payment_link(tx=transaccion_existente)},
                        payment_link=transaccion_existente.payment_link or self._bestpay_extract_payment_link(tx=transaccion_existente),
                    )
                    # CIFRAMOS ANTES DE RETORNAR
                    from ..lib.crypto_utils import encrypt_payload
                    return encrypt_payload(response, partner.bestpay_encryption_key)

                transaccion_existente._bestpay_mark_as_unpaid()
                _logger.info(
                    "[API] Transacción %s expirada, pasada a No pagado para permitir reintento (external_reference=%s)",
                    transaccion_existente.reference, external_reference
                )

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
        
        if method_code:
            _logger.info(f"🔍 DEBUG: Buscando método '{method_code}' para proveedor ID: {provider.id if provider else 'NO HAY PROVEEDOR'}")
            payment_method = request.env['payment.method'].sudo().search([
                ('code', '=', str(method_code).lower()),
                ('provider_ids', 'in', provider.id)
            ], limit=1)
            _logger.info(f"🔍 DEBUG: Resultado de la búsqueda: {payment_method}")

        if not payment_method:
            payment_method = provider.payment_method_ids[:1]

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
        tasa_para_guardar = tasa_bcv
        currency_usd = request.env['res.currency'].sudo().search([('name', '=', 'USD')], limit=1)
        monto_final_odoo = monto_recibido
        currency_final = currency

        if currency_code.upper() == 'VES':
            monto_calculado_ves = monto_recibido
            monto_odoo_usd = round(monto_recibido / tasa_bcv, 2)
            recalcular = False
            tasa_para_guardar = tasa_bcv 
            monto_final_odoo = monto_odoo_usd
            currency_final = currency_usd
            
        elif currency_code.upper() == 'USD':
            monto_calculado_ves = round(monto_recibido * tasa_bcv, 2)
            recalcular = True
            tasa_para_guardar = tasa_bcv
            
        elif currency_code.upper() == 'EUR':
            currency_eur = request.env['res.currency'].sudo().search([('name', '=', 'EUR')], limit=1)
            latest_rate_eur = request.env['res.currency.rate'].sudo().search([
                ('currency_id', '=', currency_eur.id)
            ], order='name desc', limit=1)
            
            tasa_eur_en_usd = latest_rate_eur.rate if latest_rate_eur else 1.0
            monto_odoo_usd = round(monto_recibido / tasa_eur_en_usd, 2)
            monto_calculado_ves = round(monto_odoo_usd * tasa_bcv, 2)
            recalcular = True
            tasa_para_guardar = round(monto_calculado_ves / monto_recibido, 4)

        payload_recibido = decrypted_data # Guardamos SOLO los datos limpios y descifrados
        
        # [Paso 6 - Creación del registro]
        try:
            internal_reference = f"BPAY-{external_reference}-{fields.Datetime.now().strftime('%Y%m%d%H%M%S')}"
            
            tx = request.env['payment.transaction'].sudo().create({
                'reference': internal_reference,
                'amount': monto_final_odoo,
                'currency_id': currency_final.id,
                'provider_id': provider.id,
                'payment_method_id': payment_method.id,
                'partner_id': partner.id,                  
                'bestpay_client_id': partner.id,           
                'external_reference': external_reference,
                'client_note': note,
                'payment_request_payload': payload_recibido,
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
            try:
                tx._set_error(error_msg)
            except Exception as tx_err:
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

        # 8. El link ya fue emitido: la transacción queda pendiente de pago.
        # 'pending' permite que el cron de expiración y los filtros de estado
        # reflejen que el link está vigente esperando pago.
        if tx.state == 'draft':
            tx._set_pending(state_message='Link de pago emitido, pendiente de pago.')

         # 9. Respuesta exitosa
        payment_link = self._bestpay_extract_payment_link(tx=tx, payment_details=datos_banco)
        response = self._bestpay_standard_response(
            tx=tx,
            status='success',
            message='Transacción creada correctamente.',
            transaction_status=tx.state,
            payment_details=datos_banco,
            payment_link=payment_link,
        )
        
        # Guardamos en la DB la respuesta en texto plano para auditoría interna
        tx.write({'payment_request_response': json.dumps(response, ensure_ascii=False, indent=4)})

        # ========================================================================
        # CIFRADO DE LA RESPUESTA (PATRÓN ESTANDARIZADO)
        # ========================================================================
        try:
            from ..lib.crypto_utils import encrypt_payload
            # Ciframos el diccionario de respuesta antes de enviarlo por la red
            response = encrypt_payload(response, partner.bestpay_encryption_key)
            _logger.info("✅ [API] Respuesta cifrada exitosamente para tx: %s", tx.reference)
        except Exception as e:
            _logger.error("❌ [API] Error cifrando respuesta: %s. Retornando en texto plano.", str(e))
            # Si falla el cifrado, retornamos en texto plano para no romper el flujo
        
        return response

    def _bestpay_auth_and_decrypt(self):
        """Autenticación, validación de partner y descifrado común para APIs BestPay."""
        # 1. VERIFICAR QUE EL PAYLOAD ESTÉ CIFRADO
        is_encrypted = request.httprequest.headers.get('X-BestPay-Encrypted') == 'true'
        if not is_encrypted:
            return None, self._bestpay_standard_response(
                status='error',
                message='Payload debe estar cifrado con AES-256-GCM. Agregar header X-BestPay-Encrypted: true',
                transaction_status='error'
            )

        # 2. VALIDAR CREDENCIALES M2M
        auth_header = request.httprequest.headers.get('Authorization', '')
        if not auth_header.startswith('Basic '):
            return None, self._bestpay_standard_response(
                status='error',
                message='Autenticación requerida. Use Basic Auth con Client ID y Client Secret.',
                transaction_status='error'
            )

        try:
            encoded_token = auth_header[6:].strip()
            decoded_credentials = base64.b64decode(encoded_token, validate=True).decode('utf-8')
            if ':' not in decoded_credentials:
                raise ValueError('Formato inválido')
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return None, self._bestpay_standard_response(
                status='error',
                message='Credenciales Basic malformadas.',
                transaction_status='error'
            )

        if not request.db:
            return None, self._bestpay_standard_response(
                status='error',
                message='Base de datos no especificada en la petición.',
                transaction_status='error'
            )

        partner = request.env['res.partner'].sudo().search([
            ('is_api_client', '=', True),
            ('bestpay_basic_token', '=', encoded_token),
        ], limit=1)

        if not partner:
            return None, self._bestpay_standard_response(
                status='error',
                message='Credenciales inválidas o cliente no autorizado.',
                transaction_status='error'
            )

        # 3. DESCIFRAR
        try:
            from ..lib.crypto_utils import decrypt_payload
            encrypted_data = request.httprequest.get_json(silent=True) or {}
            if not encrypted_data and hasattr(request, 'get_json'):
                try:
                    encrypted_data = request.get_json(force=True, silent=True) or {}
                except Exception:
                    encrypted_data = {}
            if not encrypted_data:
                encrypted_data = kwargs if kwargs else {}
            decrypted_data = decrypt_payload(encrypted_data, partner.bestpay_encryption_key)
            return (partner, decrypted_data), None
        except Exception as e:
            return None, self._bestpay_standard_response(
                status='error',
                message=f'Error descifrando payload: {str(e)}',
                transaction_status='error'
            )

    @http.route('/api/v1/transactions/search', type='json', auth='none', methods=['POST'], csrf=False)
    def search_transactions_api(self, **kwargs):
        auth_result, error_resp = self._bestpay_auth_and_decrypt()
        if error_resp is not None:
            return error_resp
        partner, decrypted_data = auth_result
        
        try:
            from ..services.transaction_service import BestPayTransactionQueryService
            service = BestPayTransactionQueryService(request.env)
            result = service.search_transactions(partner, decrypted_data or {})
        except ValueError as ve:
            resp = self._bestpay_standard_response(
                status='error',
                message=str(ve),
                transaction_status='error'
            )
            try:
                from ..lib.crypto_utils import encrypt_payload
                return encrypt_payload(resp, partner.bestpay_encryption_key)
            except Exception:
                return resp
        except Exception as e:
            _logger.error("[API][SEARCH] Error: %s", e)
            resp = self._bestpay_standard_response(
                status='error',
                message='Error interno al consultar transacciones.',
                transaction_status='error'
            )
            try:
                from ..lib.crypto_utils import encrypt_payload
                return encrypt_payload(resp, partner.bestpay_encryption_key)
            except Exception:
                return resp
        
        try:
            from ..lib.crypto_utils import encrypt_payload
            return encrypt_payload(result, partner.bestpay_encryption_key)
        except Exception as e:
            _logger.error("[API][SEARCH] Error cifrando respuesta: %s", e)
            return result

    @http.route('/api/v1/transaction/get', type='json', auth='none', methods=['POST'], csrf=False)
    @http.route('/api/v1/transaction/detail', type='json', auth='none', methods=['POST'], csrf=False)
    def get_transaction_api(self, **kwargs):
        auth_result, error_resp = self._bestpay_auth_and_decrypt()
        if error_resp is not None:
            return error_resp
        partner, decrypted_data = auth_result
        
        try:
            from ..services.transaction_service import BestPayTransactionQueryService
            service = BestPayTransactionQueryService(request.env)
            external_reference = decrypted_data.get('external_reference') if isinstance(decrypted_data, dict) else None
            result = service.get_transaction(partner, external_reference)
        except ValueError as ve:
            resp = self._bestpay_standard_response(
                status='error',
                message=str(ve),
                transaction_status='error'
            )
            try:
                from ..lib.crypto_utils import encrypt_payload
                return encrypt_payload(resp, partner.bestpay_encryption_key)
            except Exception:
                return resp
        except Exception as e:
            _logger.error("[API][GET] Error: %s", e)
            resp = self._bestpay_standard_response(
                status='error',
                message='Error interno al consultar transacción.',
                transaction_status='error'
            )
            try:
                from ..lib.crypto_utils import encrypt_payload
                return encrypt_payload(resp, partner.bestpay_encryption_key)
            except Exception:
                return resp
        
        try:
            from ..lib.crypto_utils import encrypt_payload
            return encrypt_payload(result, partner.bestpay_encryption_key)
        except Exception as e:
            _logger.error("[API][GET] Error cifrando respuesta: %s", e)
            return result

    @http.route('/api/v1/transaction/cancel', type='json', auth='none', methods=['POST'], csrf=False)
    def cancel_transaction_api(self, **kwargs):
        auth_result, error_resp = self._bestpay_auth_and_decrypt()
        if error_resp is not None:
            return error_resp
        partner, decrypted_data = auth_result
        
        try:
            from ..services.transaction_service import BestPayTransactionQueryService
            service = BestPayTransactionQueryService(request.env)
            external_reference = decrypted_data.get('external_reference') if isinstance(decrypted_data, dict) else None
            reason = decrypted_data.get('reason') if isinstance(decrypted_data, dict) else None
            result = service.cancel_transaction(partner, external_reference, reason)
        except ValueError as ve:
            resp = self._bestpay_standard_response(
                status='error',
                message=str(ve),
                transaction_status='error'
            )
            try:
                from ..lib.crypto_utils import encrypt_payload
                return encrypt_payload(resp, partner.bestpay_encryption_key)
            except Exception:
                return resp
        except Exception as e:
            _logger.error("[API][CANCEL] Error: %s", e)
            resp = self._bestpay_standard_response(
                status='error',
                message='Error interno al cancelar transacción.',
                transaction_status='error'
            )
            try:
                from ..lib.crypto_utils import encrypt_payload
                return encrypt_payload(resp, partner.bestpay_encryption_key)
            except Exception:
                return resp
        
        try:
            from ..lib.crypto_utils import encrypt_payload
            return encrypt_payload(result, partner.bestpay_encryption_key)
        except Exception as e:
            _logger.error("[API][CANCEL] Error cifranda respuesta: %s", e)
            return result