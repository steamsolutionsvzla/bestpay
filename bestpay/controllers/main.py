# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request
import json
import logging
import base64
import binascii
from datetime import datetime, timezone

from ..utils.api_logger import api_call_logger
from ..utils.business_logger import log_business_error

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

    def _bestpay_close_superseded_transactions(self, txs, reason):
        """Cierra intentos anteriores sin disparar el aviso de link vencido.

        Se usa cuando el cliente cambia de proveedor, método, monto o moneda
        antes de pagar. El estado 'no_pagado' bloquea el checkout del link
        anterior en BDV y Mercantil, pero permite conciliar un pago tardío si
        el banco llegara a confirmarlo.
        """
        for tx in txs.sudo():
            try:
                tx.write({
                    'state': 'no_pagado',
                    'bestpay_unpaid_reason': reason,
                    'state_message': reason,
                })
                _logger.info(
                    "[API] Transacción previa %s cerrada: %s",
                    tx.reference, reason,
                )
            except Exception as e:
                _logger.error(
                    "[API] No se pudo cerrar la transacción previa %s: %s",
                    tx.reference, e,
                )
                try:
                    log_business_error(
                        request.env,
                        '_bestpay_close_superseded_transactions',
                        input_data={'transaction': tx.reference},
                        output_data='No se pudo cerrar la transacción previa: %s' % e,
                        exc=e,
                        is_error=True,
                        http=True,
                        meta={'tx': tx.reference},
                    )
                except Exception:
                    _logger.exception(
                        '[API] No se pudo registrar el fallo al cerrar la TX previa.'
                    )

    @staticmethod
    def _bestpay_key_is_usable(encryption_key):
        """Valida que la clave sea base64 de 32 bytes (AES-256)."""
        if not encryption_key:
            return False
        try:
            return len(base64.b64decode(encryption_key, validate=True)) == 32
        except Exception:
            return False

    def _bestpay_encrypt_or_error(self, payload, partner):
        """Cifra la respuesta de la API. Si no se puede, devuelve un error.

        Nunca devuelve un 'success' en texto plano: eso rompe el contrato de
        seguridad y el cliente no puede verificarlo. El error sí puede ir plano
        (el cliente ya lo acepta así) y el reintento reutiliza la transacción.
        """
        try:
            from ..lib.crypto_utils import encrypt_payload
            return encrypt_payload(payload, partner.bestpay_encryption_key)
        except Exception as e:
            _logger.error(
                "[API] Error cifrando respuesta: %s. Se devuelve error al cliente.", e
            )
            try:
                log_business_error(
                    request.env,
                    '_bestpay_encrypt_or_error',
                    input_data={'partner_id': partner.id if partner else False},
                    output_data='No se pudo cifrar la respuesta para el cliente: %s' % e,
                    exc=e,
                    is_error=True,
                    http=True,
                    meta={
                        'cliente': partner.name if partner else False,
                        'proveedor': 'bestpay',
                    },
                    dedupe_key='bestpay_encrypt_response:%s' % (
                        partner.id if partner else 0),
                    dedupe_minutes=60,
                )
            except Exception:
                _logger.exception('[API] No se pudo registrar el fallo de cifrado.')
            return self._bestpay_standard_response(
                status='error',
                message='No se pudo cifrar la respuesta. Reintenta con la misma referencia.',
                transaction_status='error',
            )

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
    @api_call_logger
    def create_transaction_api(self, **kwargs):
        # ========================================================================
        # 1-3. AUTENTICACIÓN, PARTNER Y DESCIFRADO (lógica común de la API)
        # ========================================================================
        auth_result, error_resp = self._bestpay_auth_and_decrypt(kwargs)
        if error_resp is not None:
            return error_resp

        partner, decrypted_data = auth_result

        # Actualizamos kwargs con los datos descifrados para que el resto de la
        # lógica siga leyéndolos igual que antes.
        kwargs.update(decrypted_data)

        # ========================================================================
        # 4. RESTO DE LA LÓGICA (usa los datos descifrados)
        # ========================================================================
        # Leer los datos directamente de kwargs (ahora descifrados)
        provider_code = kwargs.get('provider')
        currency_code = kwargs.get('currency')
        amount = kwargs.get('amount')
        external_reference = kwargs.get('external_reference')
        note = kwargs.get('note', '')

        # Validaciones de campos obligatorios
        if not all([provider_code, currency_code, amount, external_reference]):
            response = self._bestpay_standard_response(
                status='error',
                message='Faltan campos obligatorios: (provider, currency, amount, external_reference).',
                transaction_status='error'
            )
            return self._bestpay_encrypt_or_error(response, partner)
        
        # Si la orden ya fue pagada, no se permite iniciar otro pago ni cambiar
        # de método. Se devuelve la transacción ganadora para que el cliente
        # pueda mostrar el comprobante correcto.
        if external_reference:
            paid_tx = request.env['payment.transaction'].sudo().search([
                ('bestpay_client_id', '=', partner.id),
                ('external_reference', '=', external_reference),
                ('state', '=', 'done'),
            ], limit=1, order='create_date desc, id desc')
            if paid_tx:
                response = self._bestpay_standard_response(
                    tx=paid_tx,
                    status='success',
                    message='Esta orden ya fue pagada.',
                    transaction_status='done',
                    payment_details={'payment_link': paid_tx.payment_link or self._bestpay_extract_payment_link(tx=paid_tx)},
                    payment_link=paid_tx.payment_link or self._bestpay_extract_payment_link(tx=paid_tx),
                )
                return self._bestpay_encrypt_or_error(response, partner)

        # 4. Validar Proveedor de pagos y permisos
        provider = request.env['payment.provider'].sudo().search([('code', '=', provider_code),('is_bestpay_provider', '=', True)], limit=1)
        if not provider:
            response = self._bestpay_standard_response(
                status='error',
                message=f'El proveedor "{provider_code}" no existe.',
                transaction_status='error'
            )
            return self._bestpay_encrypt_or_error(response, partner)
        
        if provider.id not in partner.allowed_provider_ids.ids:
            response = self._bestpay_standard_response(
                status='error',
                message=f'El proveedor "{provider_code}" no está permitido para este cliente.',
                transaction_status='error'
            )
            return self._bestpay_encrypt_or_error(response, partner)
        
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
            try:
                log_business_error(
                    request.env,
                    'create_transaction_api',
                    input_data={
                        'provider': provider_code,
                        'payment_method': method_code,
                    },
                    output_data=(
                        'No se encontró el método de pago "%s" vinculado al '
                        'proveedor "%s".' % (method_code or 'por defecto', provider_code)
                    ),
                    is_error=False,
                    http=True,
                    meta={
                        'proveedor': provider_code,
                        'cliente': partner.name,
                    },
                )
            except Exception:
                _logger.exception('[API] No se pudo registrar el método de pago faltante.')
            response = self._bestpay_standard_response(
                status='error',
                message=f'No se encontró el método de pago "{method_code or "por defecto"}" vinculado al proveedor "{provider_code}". Verifica la configuración en Odoo.',
                transaction_status='error'
            )
            return self._bestpay_encrypt_or_error(response, partner)
        
        # 5. Validar Moneda
        currency = request.env['res.currency'].sudo().search([('name', '=', str(currency_code).upper())], limit=1)
        if not currency:
            response = self._bestpay_standard_response(
                status='error',
                message=f'La moneda "{currency_code}" no existe.',
                transaction_status='error'
            )
            return self._bestpay_encrypt_or_error(response, partner)
        
        tx_dummy = request.env['payment.transaction'].sudo().new()
        tasa_bcv = tx_dummy._get_bcv_rate_or_fetch()

        try:
            monto_recibido = float(amount)
            if monto_recibido <= 0:
                response = self._bestpay_standard_response(
                    status='error',
                    message='El monto debe ser un valor mayor a cero.',
                    transaction_status='error'
                )
                return self._bestpay_encrypt_or_error(response, partner)
        except (ValueError, TypeError):
            response = self._bestpay_standard_response(
                status='error',
                message='El formato del campo "amount" es inválido.',
                transaction_status='error'
            )
            return self._bestpay_encrypt_or_error(response, partner)
            
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

        # ====================================================================
        # 5.b. Intentos previos de la misma orden
        # ====================================================================
        # Un mismo external_reference puede tener varios intentos: el cliente
        # elige un método, se arrepiente y elige otro antes de pagar. Solo se
        # reutiliza el link si el intento es idéntico (proveedor, método,
        # monto y moneda). Si cambió algo, se emite un link nuevo y el
        # anterior se cierra DESPUÉS de que el nuevo link salga bien.
        open_txs = request.env['payment.transaction'].sudo().search([
            ('bestpay_client_id', '=', partner.id),
            ('external_reference', '=', external_reference),
            ('state', 'in', ['draft', 'pending']),
        ], order='create_date desc, id desc')

        # Importante: ambos recordsets deben ser sudo. Bajo auth='none' no hay
        # usuario en el entorno y un write sin sudo falla en check_access.
        tx_to_reuse = request.env['payment.transaction'].sudo()
        txs_to_supersede = request.env['payment.transaction'].sudo()
        for open_tx in open_txs:
            if not open_tx.create_date:
                _logger.warning(
                    "[API] Transacción ID=%s (Ref: %s) sin fecha de creación. "
                    "Se cancela para permitir reintento.",
                    open_tx.id, external_reference,
                )
                open_tx.write({
                    'state': 'cancel',
                    'state_message': 'Cancelada: fecha de creación inválida/corrupta.',
                })
                continue

            if open_tx._bestpay_is_link_expired():
                open_tx._bestpay_mark_as_unpaid()
                _logger.info(
                    "[API] Transacción %s expirada, pasada a No pagado "
                    "(external_reference=%s)",
                    open_tx.reference, external_reference,
                )
                continue

            same_intent = (
                open_tx.provider_id.id == provider.id
                and open_tx.payment_method_id.id == payment_method.id
                and open_tx.currency_id.id == currency_final.id
                and abs((open_tx.amount or 0.0) - monto_final_odoo) <= 0.01
                # Un intento sin link no sirve para reintentar: se emite uno nuevo.
                and bool(self._bestpay_extract_payment_link(tx=open_tx))
            )
            if same_intent and not tx_to_reuse:
                tx_to_reuse = open_tx
            else:
                txs_to_supersede |= open_tx

        if tx_to_reuse:
            # Reintento del mismo método: se conserva el link (idempotencia).
            # Cualquier otro intento abierto de la orden ya no debe usarse.
            self._bestpay_close_superseded_transactions(
                txs_to_supersede,
                'Cerrada porque se reutilizó un link vigente del mismo método de pago.',
            )
            response = self._bestpay_standard_response(
                tx=tx_to_reuse,
                status='success',
                message='Reutilizando la transacción existente.',
                transaction_status=tx_to_reuse.state,
                payment_details={'payment_link': tx_to_reuse.payment_link or self._bestpay_extract_payment_link(tx=tx_to_reuse)},
                payment_link=tx_to_reuse.payment_link or self._bestpay_extract_payment_link(tx=tx_to_reuse),
            )
            return self._bestpay_encrypt_or_error(response, partner)

        # [Paso 6 - Creación del registro]
        try:
            # Microsegundos reales: fields.Datetime.now() trunca a segundos y dos
            # create con la misma referencia externa en el mismo segundo chocaban
            # contra la restricción única de 'reference'.
            reference_stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
            internal_reference = f"BPAY-{external_reference}-{reference_stamp}"
            
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
            try:
                log_business_error(
                    request.env,
                    'create_transaction_api',
                    input_data={
                        'external_reference': external_reference,
                        'provider': provider_code,
                        'currency': currency_code,
                        'amount': amount,
                    },
                    output_data='No se pudo crear la transacción en Odoo: %s' % e,
                    exc=e,
                    is_error=True,
                    http=True,
                    meta={
                        'cliente': partner.name,
                        'proveedor': provider_code,
                        'referencia_externa': external_reference,
                    },
                )
            except Exception:
                _logger.exception('[API] No se pudo registrar el fallo al crear la TX.')
            response = self._bestpay_standard_response(
                status='error',
                message=f'Error en Odoo: {str(e)}',
                transaction_status='error'
            )
            return self._bestpay_encrypt_or_error(response, partner)

        # 7. Delegar el procesamiento al banco
        try:
            datos_banco = tx._bestpay_process_transaction_with_bank(kwargs)
        except Exception as e:
            error_msg = f'Error al comunicar con el banco: {str(e)}'
            try:
                tx._set_error(error_msg)
            except Exception as tx_err:
                tx.write({'state': 'error', 'payment_request_response': f"Fallo crítico: {error_msg}. Error interno: {str(tx_err)}"})

            try:
                log_business_error(
                    request.env,
                    'create_transaction_api',
                    input_data={
                        'external_reference': external_reference,
                        'provider': provider_code,
                    },
                    output_data=error_msg,
                    exc=e,
                    is_error=True,
                    http=True,
                    meta={
                        'cliente': partner.name,
                        'proveedor': provider_code,
                        'tx': tx.reference,
                        'referencia_externa': external_reference,
                    },
                )
            except Exception:
                _logger.exception('[API] No se pudo registrar el fallo con el banco.')

            response = self._bestpay_standard_response(
                tx=tx,
                status='error',
                message=error_msg,
                transaction_status='error',
                payment_details={'error': error_msg},
                payment_link=self._bestpay_extract_payment_link(tx=tx),
            )
            return self._bestpay_encrypt_or_error(response, partner)
        
        if datos_banco and 'error' in datos_banco:
            try:
                tx._set_error(datos_banco['error'])
            except Exception as tx_err:
                tx.write({'state': 'error', 'payment_request_response': f"Fallo crítico: {datos_banco['error']}. Error interno: {str(tx_err)}"})

            try:
                log_business_error(
                    request.env,
                    'create_transaction_api',
                    input_data={
                        'external_reference': external_reference,
                        'provider': provider_code,
                    },
                    output_data='El banco devolvió un error: %s' % datos_banco['error'],
                    is_error=True,
                    http=True,
                    meta={
                        'cliente': partner.name,
                        'proveedor': provider_code,
                        'tx': tx.reference,
                        'referencia_externa': external_reference,
                    },
                )
            except Exception:
                _logger.exception('[API] No se pudo registrar el error devuelto por el banco.')

            response = self._bestpay_standard_response(
                tx=tx,
                status='error',
                message=datos_banco['error'],
                transaction_status='error',
                payment_details=datos_banco,
                payment_link=self._bestpay_extract_payment_link(tx=tx, payment_details=datos_banco),
            )
            return self._bestpay_encrypt_or_error(response, partner)

        # 8. El link ya fue emitido: la transacción queda pendiente de pago.
        # 'pending' permite que el cron de expiración y los filtros de estado
        # reflejen que el link está vigente esperando pago.
        if tx.state == 'draft':
            tx._set_pending(state_message='Link de pago emitido, pendiente de pago.')

        # El link nuevo ya está emitido: recién ahora se cierran los intentos
        # anteriores con otro método/proveedor/monto. Si la creación hubiera
        # fallado, el cliente conserva el link anterior para reintentar.
        if txs_to_supersede:
            self._bestpay_close_superseded_transactions(
                txs_to_supersede,
                'Método de pago cambiado por el cliente antes de pagar. '
                'Se emitió el nuevo link %s.' % tx.reference,
            )

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
        return self._bestpay_encrypt_or_error(response, partner)

    def _bestpay_auth_and_decrypt(self, params):
        """Autenticación, validación de partner y descifrado común para APIs BestPay.

        El payload cifrado debe venir dentro de ``params`` del sobre JSON-RPC,
        igual que en ``/api/v1/transaction/create``.
        """
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

        if not self._bestpay_key_is_usable(partner.bestpay_encryption_key):
            _logger.error(
                "[API] Cliente %s sin clave de cifrado válida. No se procesa la consulta.",
                partner.name,
            )
            try:
                log_business_error(
                    request.env,
                    '_bestpay_auth_and_decrypt',
                    input_data={'partner': partner.name},
                    output_data=(
                        'El cliente no tiene una clave de cifrado válida '
                        'configurada. No se procesa la consulta.'
                    ),
                    is_error=False,
                    http=True,
                    meta={'cliente': partner.name},
                )
            except Exception:
                _logger.exception('[API] No se pudo registrar la clave inválida.')
            return None, self._bestpay_standard_response(
                status='error',
                message='El cliente no tiene una clave de cifrado válida configurada. Contacta a BestPay.',
                transaction_status='error'
            )

        # 3. DESCIFRAR (el payload viene dentro de params, igual que en create)
        if not isinstance(params, dict) or not params:
            return None, self._bestpay_standard_response(
                status='error',
                message='Falta params en el sobre JSON-RPC o el payload cifrado está vacío.',
                transaction_status='error'
            )

        try:
            from ..lib.crypto_utils import decrypt_payload
            decrypted_data = decrypt_payload(params, partner.bestpay_encryption_key)
            return (partner, decrypted_data), None
        except Exception as e:
            return None, self._bestpay_standard_response(
                status='error',
                message=f'Error descifrando payload: {str(e)}',
                transaction_status='error'
            )

    @http.route('/api/v1/transactions/search', type='json', auth='none', methods=['POST'], csrf=False)
    @api_call_logger
    def search_transactions_api(self, **kwargs):
        auth_result, error_resp = self._bestpay_auth_and_decrypt(kwargs)
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
            return self._bestpay_encrypt_or_error(resp, partner)
        except Exception as e:
            _logger.error("[API][SEARCH] Error: %s", e)
            try:
                log_business_error(
                    request.env, 'search_transactions_api',
                    input_data={'partner': partner.name},
                    output_data='Error interno al consultar transacciones: %s' % e,
                    exc=e, is_error=True, http=True,
                    meta={'cliente': partner.name},
                )
            except Exception:
                _logger.exception('[API][SEARCH] No se pudo registrar el error.')
            resp = self._bestpay_standard_response(
                status='error',
                message='Error interno al consultar transacciones.',
                transaction_status='error'
            )
            return self._bestpay_encrypt_or_error(resp, partner)

        return self._bestpay_encrypt_or_error(result, partner)

    @http.route('/api/v1/transaction/get', type='json', auth='none', methods=['POST'], csrf=False)
    @http.route('/api/v1/transaction/detail', type='json', auth='none', methods=['POST'], csrf=False)
    @api_call_logger
    def get_transaction_api(self, **kwargs):
        auth_result, error_resp = self._bestpay_auth_and_decrypt(kwargs)
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
            return self._bestpay_encrypt_or_error(resp, partner)
        except Exception as e:
            _logger.error("[API][GET] Error: %s", e)
            try:
                log_business_error(
                    request.env, 'get_transaction_api',
                    input_data={'partner': partner.name},
                    output_data='Error interno al consultar transacción: %s' % e,
                    exc=e, is_error=True, http=True,
                    meta={'cliente': partner.name},
                )
            except Exception:
                _logger.exception('[API][GET] No se pudo registrar el error.')
            resp = self._bestpay_standard_response(
                status='error',
                message='Error interno al consultar transacción.',
                transaction_status='error'
            )
            return self._bestpay_encrypt_or_error(resp, partner)

        return self._bestpay_encrypt_or_error(result, partner)

    @http.route('/api/v1/transaction/cancel', type='json', auth='none', methods=['POST'], csrf=False)
    @api_call_logger
    def cancel_transaction_api(self, **kwargs):
        auth_result, error_resp = self._bestpay_auth_and_decrypt(kwargs)
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
            return self._bestpay_encrypt_or_error(resp, partner)
        except Exception as e:
            _logger.error("[API][CANCEL] Error: %s", e)
            try:
                log_business_error(
                    request.env, 'cancel_transaction_api',
                    input_data={'partner': partner.name},
                    output_data='Error interno al cancelar transacción: %s' % e,
                    exc=e, is_error=True, http=True,
                    meta={'cliente': partner.name},
                )
            except Exception:
                _logger.exception('[API][CANCEL] No se pudo registrar el error.')
            resp = self._bestpay_standard_response(
                status='error',
                message='Error interno al cancelar transacción.',
                transaction_status='error'
            )
            return self._bestpay_encrypt_or_error(resp, partner)

        return self._bestpay_encrypt_or_error(result, partner)