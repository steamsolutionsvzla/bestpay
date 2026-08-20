# -*- coding: utf-8 -*-
import base64
import json
import hashlib
import logging
import werkzeug
import requests
from typing import Dict, Any
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

def _decrypt_mercantil_data(encrypted_data: str, secret_key: str) -> Dict[str, Any] | None:
    """
    Intenta descifrar un payload de Mercantil usando una clave secreta específica (AES-ECB).
    """
    try:
        key_hash = hashlib.sha256(secret_key.encode('utf-8')).digest()[:16]
        encrypted_bytes = base64.b64decode(encrypted_data)

        cipher = AES.new(key_hash, AES.MODE_ECB)
        decrypted_padded = cipher.decrypt(encrypted_bytes)

        decrypted = unpad(decrypted_padded, AES.block_size, style='pkcs7')
        return json.loads(decrypted.decode('utf-8'))
    except Exception:
        return None


def _decrypt_with_available_keys(env, encrypted_data: str):
    """
    Prueba las claves secretas de Mercantil registradas en BestPay de forma
    iterativa hasta descifrar el payload.
    """
    api_clients = env['res.partner'].sudo().search([
        ('is_api_client', '=', True),
        ('mercantil_secret_key', '!=', False)
    ])
    
    candidate_keys = [client.mercantil_secret_key for client in api_clients if client.mercantil_secret_key]

    # Respaldo: si existe configuración global a nivel de proveedor
    if not candidate_keys:
        provider = env['payment.provider'].sudo().search([('code', '=', 'mercantil')], limit=1)
        if provider and getattr(provider, 'mercantil_secret_key', False):
            candidate_keys.append(provider.mercantil_secret_key)

    for key in candidate_keys:
        decrypted_json = _decrypt_mercantil_data(encrypted_data, key)
        if decrypted_json:
            return decrypted_json, key

    return None, None

class BestPayWebhookController(http.Controller):

    def _json_response(self, obj: Dict[str, Any], status: int):
            return request.make_response(json.dumps(obj), status=status)
    
    def _build_mercantil_response(
        self,
        info_msg: Dict[str, Any],
        code: int,
        codigo: str,
        mensaje_cliente: str,
        mensaje_sistema: str,
        id_registro: str = ""
    ) -> Dict[str, Any]:
        """
        Construye una respuesta estandarizada para el servicio de Mercantil.
        """
        info_msg = info_msg or {}
        return {
            "infoMsg": {
                "guId": info_msg.get('guId', ''),
                "channel": info_msg.get('channel', ''),
                "subchannel": info_msg.get('subchannel', ''),
                "applId": info_msg.get('applId', ''),
                "personId": info_msg.get('personId', ''),
                "userId": info_msg.get('userId', ''),
                "token": info_msg.get('token', ''),
                "action": info_msg.get('action', '')
            },
            "code": code,
            "codigo": codigo,
            "mensajeCliente": mensaje_cliente,
            "mensajeSistema": mensaje_sistema,
            "idRegistro": id_registro
        }

    @http.route('/payment/mercantil/processing', auth='public', website=True)
    def payment_processing(self, **kwargs):
        return request.render('bestpay_mer.payment_processing_page')
    
    @http.route('/api/v1/transaction/mer/redirect/<string:uuid_hash>', type='http', auth='public', csrf=False)
    def bestpay_mercantil_redirect(self, uuid_hash, **kwargs):
        """
        Endpoint que valida la transacción y redirige al usuario final 
        al Botón de Pago de Mercantil con la data encriptada.
        """
        _logger.info("[BESTPAY MERCANTIL API] Redirección solicitada para hash: %s", uuid_hash)

        # 1. Buscar la transacción por uuid_hash
        tx = request.env['payment.transaction'].sudo().search([
            ('uuid_hash', '=', uuid_hash)
        ], limit=1)

        if not tx.exists():
            return request.not_found()
        
        if tx.provider_id.code != 'mer':
            _logger.warning(
                "[BESTPAY MERCANTIL API] Intento de redirección inválido. "
                "La Tx %s pertenece al proveedor '%s', no a 'mercantil'.", 
                tx.reference, tx.provider_id.code
            )
            return request.make_response(
                "Error: Esta transacción no puede ser procesada a través de Mercantil.", 
                status=400
            )

        # 2. Evitar doble pago
        if tx.state == 'done':
            return request.make_response("Error: Esta transacción ya fue pagada exitosamente.", status=400)

        partner = tx.bestpay_client_id
        if not partner or not partner.mercantil_payment_url or not partner.mercantil_merchant_id:
            return request.make_response("Error interno: Configuración de pasarela incompleta.", status=500)

        # 3. Reutilizar el link vigente o generar uno nuevo "Justo a Tiempo"
        try:
            # Solo regenera si no existe, expiró (>1 día) o el monto VES cambió.
            custom_link = tx._bestpay_get_or_generate_mer_link()

            _logger.info("\n" + "="*80 + f"\n[BESTPAY MERCANTIL DEBUG] LINK DE PAGO:\n{custom_link}\n" + "="*80)
            
            # Redireccionar de inmediato
            return werkzeug.utils.redirect(custom_link, code=303)

        except Exception as e:
            _logger.error("[BESTPAY MERCANTIL API] Error generando link: %s", str(e))
            return request.make_response("Error interno al inicializar el cifrado seguro.", status=500)

    @http.route('/api/v1/webhooks/mercantil/payment/confirmation', type='http', auth='public', methods=['POST'], csrf=False)
    def mercantil_confirm_payment(self, **kwargs):
        raw_data = request.httprequest.data
        try:
            data = json.loads(raw_data)
            encrypted_data = data.get('data')

            if not encrypted_data:
                _logger.error("[BESTPAY MERCANTIL WEBHOOK] Campo 'data' ausente.")
                response = self._build_mercantil_response({}, 0, "06", "Payload ausente", "El campo 'data' no está presente")
                return self._json_response(response, status=400)

            # 1. Intentar descifrar
            decrypted_data, matched_key = _decrypt_with_available_keys(request.env, encrypted_data)

            if not decrypted_data:
                _logger.error("[BESTPAY MERCANTIL WEBHOOK] No se pudo descifrar el payload.")
                response = self._build_mercantil_response({}, 0, "06", "Error descifrado", "No se pudo descifrar con las credenciales activas")
                return self._json_response(response, status=400)

            webhook_info = decrypted_data.get('webhookNotificationIn', {})
            info_msg = decrypted_data.get('infoMsg', {})
            guid = info_msg.get('guId', '')

            if not webhook_info:
                _logger.info("[BESTPAY MERCANTIL WEBHOOK] Notificación vacía recibida.")
                response = self._build_mercantil_response(info_msg, 0, "06", "Notificación vacía", "Webhook vacío recibido", guid)
                return self._json_response(response, status=200)

            numero_factura = webhook_info.get('numeroFactura')
            if not numero_factura:
                _logger.error("[BESTPAY MERCANTIL WEBHOOK] No se encontró 'numeroFactura'.")
                response = self._build_mercantil_response(info_msg, 0, "06", "Factura ausente", "No se encontró el campo numeroFactura", guid)
                return self._json_response(response, status=400)

            # 2. Buscar Transacción por ID
            try:
                tx_id = int(numero_factura)
                tx = request.env['payment.transaction'].sudo().browse(tx_id)
            except (ValueError, TypeError):
                tx = request.env['payment.transaction']

            if not tx.exists():
                _logger.warning(f"[BESTPAY MERCANTIL WEBHOOK] Transacción {numero_factura} no encontrada.")
                response = self._build_mercantil_response(info_msg, 0, "06", "Registro no encontrado", f"La transacción {numero_factura} no existe", guid)
                return self._json_response(response, status=200)

            # Prepare los strings en JSON con indentación para legibilidad en la vista
            incoming_raw_json = json.dumps(decrypted_data, ensure_ascii=False, indent=2)

            # 3. Control de Duplicados
            if tx.state == 'done':
                _logger.info(f"[BESTPAY MERCANTIL WEBHOOK] Transacción {tx.reference} (ID: {tx.id}) ya procesada previamente.")
                response = self._build_mercantil_response(
                    info_msg=info_msg,
                    code=0,
                    codigo="06",
                    mensaje_cliente="Notificación duplicada recibida",
                    mensaje_sistema="La transacción ya fue procesada anteriormente",
                    id_registro=guid
                )
                outgoing_response_json = json.dumps(response, ensure_ascii=False, indent=2)

                # Concatenar en caso de reintentos
                tx.write({
                    'bank_in_log': (tx.bank_in_log or '') + f"\n\n--- [REINTENTO DUPLICADO] ---\n{incoming_raw_json}",
                    'bank_out_log': (tx.bank_out_log or '') + f"\n\n--- [RESPUESTA DUPLICADO] ---\n{outgoing_response_json}"
                })

                return self._json_response(response, status=200)

            # 4. Construir respuesta exitosa para Mercantil
            response = self._build_mercantil_response(
                info_msg=info_msg,
                code=0,
                codigo="00",
                mensaje_cliente="Notificación recibida con éxito!",
                mensaje_sistema="Notificación recibida con éxito!!",
                id_registro=guid
            )
            outgoing_response_json = json.dumps(response, ensure_ascii=False, indent=2)

            # 5. Guardar cada log en su campo correspondiente
            tx.write({
                'acquirer_reference': webhook_info.get('referencia'),
                'provider_reference': guid,
                'bank_in_log': incoming_raw_json,
                'bank_out_log': outgoing_response_json,
            })

            # 6. Marcar Transacción como Exitosa
            tx._set_done()
            _logger.info(f"[BESTPAY MERCANTIL WEBHOOK] ✓ Transacción {tx.reference} (ID: {tx.id}) procesada como DONE.")

            # 7. Notificar al Cliente API (Tercero)
            if hasattr(tx, '_notify_api_client_webhook'):
                tx._notify_api_client_webhook()

            return self._json_response(response, status=200)

        except json.JSONDecodeError as e:
            _logger.error(f"[BESTPAY MERCANTIL WEBHOOK] JSON inválido: {str(e)}")
            return self._json_response({"error": "Invalid JSON"}, status=400)
        except Exception as e:
            _logger.exception(f"[BESTPAY MERCANTIL WEBHOOK] Error inesperado: {str(e)}")
            return self._json_response({"error": "Internal server error"}, status=500)
        