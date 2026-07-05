# -*- coding: utf-8 -*-
import json
import logging
import secrets
from odoo import http, fields
from odoo.http import request
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
            return request.make_json_response({"error": "JSON inválido"}, status=400)

        # 1. Autenticación por Token
        auth_header = request.httprequest.headers.get('Authorization', '')
        token = auth_header.replace('Bearer ', '').strip() if auth_header else ''
        
        if not token:
            return request.make_json_response({"error": "Token no proporcionado"}, status=401)

        client = request.env['res.partner'].sudo().search([('bestpay_token', '=', token)], limit=1)
        if not client:
            return request.make_json_response({"error": "Token inválido"}, status=401)

        # 2. Validar datos mínimos
        amount = data.get('amount')
        currency_name = data.get('currency', 'USD')
        external_ref = data.get('external_reference', '')

        if not amount or amount <= 0:
            return request.make_json_response({"error": "Monto inválido"}, status=400)

        # 3. Buscar moneda y proveedor BDV activo
        currency = request.env['res.currency'].sudo().search([('name', '=', currency_name)], limit=1)
        if not currency:
            return request.make_json_response({"error": f"Moneda {currency_name} no encontrada"}, status=400)

        provider = request.env['payment.provider'].sudo().search([
            ('code', '=', 'bdv'),
            ('state', '=', 'enabled')  # Solo proveedores activos
        ], limit=1)

        if not provider:
            return request.make_json_response({"error": "No hay proveedor BDV configurado y activo"}, status=500)

        # 4. Generar token de acceso para el link de pago (seguridad)
        access_token = secrets.token_urlsafe(32)

        # 5. Crear la transacción en Odoo
        try:
            transaction = request.env['payment.transaction'].sudo().create({
                'provider_id': provider.id,
                'partner_id': client.id,  # El ecommerce es el "cliente" de BestPay
                'amount': amount,
                'currency_id': currency.id,
                'external_reference': external_ref,
                'client_note': data.get('client_note', ''),
                'reference': f"BP-{external_ref or 'NOREF'}-{fields.Date.today()}",
                'access_token': access_token,
                'state': 'draft',
            })
            
            _logger.info(f"[API] Transacción creada: ID {transaction.id} para cliente {client.name}")

            # 6. Construir el link de pago (usamos la URL base de Odoo)
            base_url = request.httprequest.url_root.rstrip('/')
            payment_link = f"{base_url}/pago/bdv/checkout?id={transaction.id}&access_token={access_token}"

            return request.make_json_response({
                "success": True,
                "transaction_id": transaction.id,
                "payment_link": payment_link,
                "amount": amount,
                "currency": currency_name,
                "external_reference": external_ref
            })

        except Exception as e:
            _logger.error(f"[API] Error creando transacción: {str(e)}")
            return request.make_json_response({"error": "Error interno del servidor"}, status=500)

    # =====================================================
    # 2. WEB: FORMULARIO DE PAGO (Para el usuario final)
    # =====================================================
    @http.route('/pago/bdv/checkout', type='http', auth='none', website=False)
    def bdv_checkout(self, id=None, access_token=None, **kw):
        """
        Muestra el formulario de pago móvil. 
        website=False asegura que no cargue el layout de Odoo (si estuviera instalado).
        """
        if not id or not access_token:
            return request.not_found()

        # Buscar transacción y validar token
        transaction = request.env['payment.transaction'].sudo().browse(int(id))
        if not transaction.exists() or transaction.access_token != access_token:
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
            'access_token': access_token,
        })

    # =====================================================
    # 3. WEB: PROCESAR EL PAGO (Envío al BDV)
    # =====================================================
    @http.route('/pago/bdv/procesar', type='http', auth='none', methods=['POST'], csrf=False)
    def bdv_process_payment(self, **post):
        """
        Recibe los datos del formulario, los guarda en la transacción 
        y llama al método de conciliación del BDV.
        """
        tx_id = post.get('transaction_id')
        access_token = post.get('access_token')

        if not tx_id or not access_token:
            return request.not_found()

        transaction = request.env['payment.transaction'].sudo().browse(int(tx_id))
        if not transaction.exists() or transaction.access_token != access_token:
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

        try:
            importe = f"{float(str(post.get('importe', '0')).replace(',', '.')):.2f}"
        except ValueError:
            importe = "0.00"

        # Actualizar la transacción con los datos del pagador
        transaction.write({
            'bdv_cedula_pagador': cedula,
            'bdv_telefono_pagador': telefono_pagador,
            'bdv_banco_origen': post.get('banco', '0102'),
            'bdv_referencia': post.get('referencia', ''),
            'bdv_fecha_pago': fields.Date.today(),
            'bdv_importe': float(importe),
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