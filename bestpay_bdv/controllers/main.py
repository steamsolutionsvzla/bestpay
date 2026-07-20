# -*- coding: utf-8 -*-
import json
import logging
import secrets
import time
from odoo import http, fields
from odoo.http import request, Response
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class BestpayBDVController(http.Controller):

    # =====================================================
    # 1. API: CREAR LINK DE PAGO (Para el Ecommerce)
    # =====================================================
    @http.route('/api/bestpay/v1/payment/create', type='http', auth='none', methods=['POST'], csrf=False)
    def api_create_payment(self, **kwargs):
        """
        Endpoint REST para que el ecommerce cree una transacción.
        Headers esperados: Authorization: Bearer <bestpay_token>
        Body JSON esperado: {
            "amount": 100.00,
            "currency": "USD",
            "external_reference": "ORD-12345",
            "client_note": "Pago de bicicleta",
            "payer_email": "cliente@email.com"
        }
        """
        try:
            data = json.loads(request.httprequest.data)
        except ValueError:
            return Response(
                json.dumps({"error": "JSON inválido"}),
                status=400,
                content_type='application/json'
            )

        # 1. Autenticación por Token
        auth_header = request.httprequest.headers.get('Authorization', '')
        token = auth_header.replace('Bearer ', '').strip() if auth_header else ''
        
        if not token:
            return Response(
                json.dumps({"error": "Token no proporcionado"}),
                status=401,
                content_type='application/json'
            )

        client = request.env['res.partner'].sudo().search([('bestpay_token', '=', token)], limit=1)
        if not client:
            return Response(
                json.dumps({"error": "Token inválido"}),
                status=401,
                content_type='application/json'
            )

        # 2. Validar datos mínimos
        amount = data.get('amount')
        currency_name = data.get('currency', 'USD')
        external_ref = data.get('external_reference', '')

        if not amount or amount <= 0:
            return Response(
                json.dumps({"error": "Monto inválido"}),
                status=400,
                content_type='application/json'
            )

        # 3. Buscar moneda y proveedor BDV activo
        currency = request.env['res.currency'].sudo().search([('name', '=', currency_name)], limit=1)
        if not currency:
            return Response(
                json.dumps({"error": f"Moneda {currency_name} no encontrada"}),
                status=400,
                content_type='application/json'
            )

        provider = request.env['payment.provider'].sudo().search([
            ('code', '=', 'bdv'),
            ('state', '=', 'enabled')
        ], limit=1)

        if not provider:
            return Response(
                json.dumps({"error": "No hay proveedor BDV configurado y activo"}),
                status=500,
                content_type='application/json'
            )

        # 4. Buscar o crear automáticamente el método de pago
        payment_method = request.env['payment.method'].sudo().search([
            ('active', '=', True)
        ], limit=1)
        if not payment_method:
            payment_method = request.env['payment.method'].sudo().create({
                'name': 'Pago Móvil BDV',
                'code': 'bdv_pagomovil',
                'active': True,
            })
            _logger.info(f"✅ Método de pago creado automáticamente: {payment_method.name}")

        seq_ref = request.env['ir.sequence'].sudo().next_by_code('bestpay.transaction.seq')
        if not seq_ref:
            import time
            seq_ref = str(int(time.time()))[-6:]
            _logger.warning(f"[API] Secuencia no disponible, usando fallback: {seq_ref}")

        reference = f"BP-{external_ref or 'NOREF'}-{seq_ref}"

        # 5. Crear la transacción en Odoo (El uuid_hash se genera automáticamente por el módulo bestpay)
        try:
            transaction = request.env['payment.transaction'].sudo().create({
                'provider_id': provider.id,
                'payment_method_id': payment_method.id,
                'partner_id': client.id,
                'amount': amount,
                'currency_id': currency.id,
                'external_reference': external_ref,
                'client_note': data.get('client_note', ''),
                'reference': reference,
                'state': 'draft',
            })

            # Llamar al método del banco para que genere el link oficial con el uuid_hash
            datos_banco = transaction._bestpay_process_transaction_with_bank(data)
            payment_link = datos_banco.get('payment_link')

            return Response(
                json.dumps({
                    "success": True,
                    "transaction_id": transaction.id,
                    "payment_link": payment_link,
                    "uuid_hash": transaction.uuid_hash,
                    "amount": amount,
                    "currency": currency_name,
                    "external_reference": external_ref
                }),
                status=200,
                content_type='application/json'
            )

        except Exception as e:
            _logger.error(f"[API] Error creando transacción: {str(e)}", exc_info=True)
            return Response(
                json.dumps({"error": "Error interno del servidor"}),
                status=500,
                content_type='application/json'
            )

    # =====================================================
    # 2. WEB: FORMULARIO DE PAGO (Para el usuario final)
    # =====================================================
    @http.route('/pago/bdv/checkout', type='http', auth='none', website=False)
    def bdv_checkout(self, hash=None, **kw):
        """
        Muestra el formulario de pago móvil usando el uuid_hash.
        """
        if not hash:
            return request.not_found()
        
        # Buscar por uuid_hash en lugar de ID
        transaction = request.env['payment.transaction'].sudo().search([
            ('uuid_hash', '=', hash)
        ], limit=1)
        
        if not transaction.exists():
            return request.not_found()
            
        if transaction.state == 'done':
            return request.render('bestpay_bdv.bdv_checkout_result', {
                'transaction': transaction,
                'status': 'success',
                'message': 'Este pago ya fue procesado exitosamente.'
            })
        
        # Renderizar el formulario standalone
        return request.render('bestpay_bdv.bdv_checkout_form', {
            'transaction': transaction,
            'hash': hash,
        })

    # =====================================================
    # 3. WEB: PROCESAR EL PAGO (Envío al BDV)
    # =====================================================
    @http.route('/pago/bdv/procesar', type='http', auth='none', methods=['POST'], csrf=False)
    def bdv_process_payment(self, **post):
        tx_hash = post.get('hash')
        if not tx_hash:
            return request.not_found()
            
        transaction = request.env['payment.transaction'].sudo().search([
            ('uuid_hash', '=', tx_hash)
        ], limit=1)
        
        if not transaction.exists():
            return request.not_found()

        # Mapear datos del formulario a los campos del modelo
        cedula_raw = post.get('cedula', '')
        tipo_cedula = post.get('tipo_cedula', 'V')
        cedula = f"{tipo_cedula}{cedula_raw}"

        telefono_raw = post.get('telefono', '')
        if telefono_raw and not telefono_raw.startswith('0'):
            telefono_pagador = f"0{telefono_raw}"
        else:
            telefono_pagador = telefono_raw

        # =====================================================
        # LÓGICA DE MONTO: QA (Hardcodeado) vs PRODUCCIÓN (Real)
        # =====================================================
        # Leemos el entorno directamente del provider de la transacción
        env_type = transaction.provider_id.bdv_environment.strip().lower() if transaction.provider_id.bdv_environment else 'qa'
        
        if env_type == 'qa':
            # AMBIENTE DE CALIDAD: Monto fijo solicitado por el banco
            importe_float = 120.00
            _logger.info(f"[BDV] 🟢 Ambiente QA detectado (desde provider). Forzando monto a enviar: {importe_float} Bs")
        else:
            # AMBIENTE DE PRODUCCIÓN: Tomar el monto real que viene del e-commerce
            # 1. Intentamos usar amount_ves si existe y es válido
            importe_float = getattr(transaction, 'amount_ves', 0.0)
            
            # 2. Si amount_ves es 0, None o no existe, usamos el amount base de la transacción
            if not importe_float or float(importe_float) <= 0:
                importe_float = float(transaction.amount)
                
            _logger.info(f"[BDV] 🔵 Ambiente PRODUCCIÓN detectado (desde provider). Monto a enviar al banco: {importe_float} Bs (ID Transacción: {transaction.id})")
        
        # Formateamos a 2 decimales como string para el payload del banco
        importe_str = f"{importe_float:.2f}"
        # =====================================================
        # =====================================================

        # Actualizar la transacción con los datos del pagador y el monto determinado
        transaction.write({
            'bdv_cedula_pagador': cedula,
            'bdv_telefono_pagador': telefono_pagador,
            'bdv_banco_origen': post.get('banco', '0102'),
            'bdv_referencia': post.get('referencia', ''),
            'bdv_fecha_pago': fields.Date.today(),
            'bdv_importe': importe_float,  # ← Se usa el monto calculado con seguridad
        })

        # Llamar al método de conciliación (el que creamos en el modelo)
        try:
            is_approved = transaction.bdv_send_conciliation()
            
            if is_approved:
                return request.render('bestpay_bdv.bdv_checkout_result', {
                    'transaction': transaction,
                    'status': 'success',
                    'message': '¡Pago aprobado exitosamente por el Banco de Venezuela!'
                })
            else:
                return request.render('bestpay_bdv.bdv_checkout_result', {
                    'transaction': transaction,
                    'status': 'error',
                    'message': f"Pago rechazado: {transaction.bdv_conciliation_message or 'Revise los datos e intente nuevamente.'}"
                })

        except UserError as e:
            _logger.error(f"[BDV] Error de usuario: {str(e)}")
            return request.render('bestpay_bdv.bdv_checkout_result', {
                'transaction': transaction,
                'status': 'error',
                'message': str(e)
            })
        except Exception as e:
            _logger.error(f"[BDV] Error inesperado: {str(e)}")
            return request.render('bestpay_bdv.bdv_checkout_result', {
                'transaction': transaction,
                'status': 'error',
                'message': 'Ocurrió un error inesperado al procesar el pago.'
            })
    
        # =====================================================
    # 4. WEBHOOK: NOTIFICACIÓN DEL BANCO (Server-to-Server)
    # =====================================================
    @http.route('/api/bestpay/v1/webhook/bdv', type='http', auth='none', methods=['POST'], csrf=False)
    def bdv_webhook_notify(self, **post):
        """
        Endpoint para recibir notificaciones automáticas del BDV sobre el estado de un pago.
        """
        try:
            # Registrar TODO lo que nos envía el banco para auditoría y pruebas
            raw_data = request.httprequest.data.decode('utf-8')
            _logger.info("="*50)
            _logger.info("[BDV WEBHOOK] Notificación recibida del Banco de Venezuela")
            _logger.info(f"[BDV WEBHOOK] Headers: {dict(request.httprequest.headers)}")
            _logger.info(f"[BDV WEBHOOK] Body (Raw): {raw_data}")
            _logger.info(f"[BDV WEBHOOK] Form Data: {post}")
            _logger.info("="*50)

            # Aquí puedes agregar la lógica para buscar la transacción por referencia 
            # y cambiar su estado a 'done' si el banco confirma el pago.
            # Ejemplo básico de respuesta exitosa al banco:
            
            return Response(
                json.dumps({"status": "success", "message": "Notificación recibida correctamente"}),
                status=200,
                content_type='application/json'
            )

        except Exception as e:
            _logger.error(f"[BDV WEBHOOK] Error procesando notificación: {str(e)}", exc_info=True)
            # Siempre devolver 200 al banco para que no siga reintentando, 
            # aunque haya fallado nuestro procesamiento interno.
            return Response(
                json.dumps({"status": "error", "message": "Error interno, pero recibido"}),
                status=200,
                content_type='application/json'
            )

        # =====================================================
    # MÉTODO AUXILIAR: GENERAR REFERENCIA SECUENCIAL
    # =====================================================
    def _generate_bestpay_reference(self, external_ref):
        """
        Genera una referencia única usando ir.sequence de Odoo.
        Formato: BP-{AÑO}-{EXTERNA}-{SECUENCIAL}
        """
        from datetime import datetime
        year = datetime.now().year
        ext_clean = (external_ref or 'NOREF').replace(' ', '-').upper()
        
        # Intentar obtener el siguiente número de la secuencia
        seq_number = request.env['ir.sequence'].sudo().next_by_code('bestpay.transaction.reference')
        
        # Si la secuencia no existe o devuelve False, crearla al vuelo
        if not seq_number:
            _logger.warning("[API] Secuencia no encontrada, creando automáticamente...")
            seq = request.env['ir.sequence'].sudo().create({
                'name': 'BestPay Transaction Reference',
                'code': 'bestpay.transaction.reference',
                'prefix': f'BP-{year}-',
                'padding': 7,
                'number_next': 1,
                'number_increment': 1,
            })
            seq_number = seq.next_by_code('bestpay.transaction.reference')
        
        # Si aún así falla, usar timestamp como fallback
        if not seq_number:
            import time
            seq_number = str(int(time.time()))
            _logger.error(f"[API] Fallback a timestamp: {seq_number}")
        
        return f"BP-{year}-{ext_clean}-{seq_number}"       