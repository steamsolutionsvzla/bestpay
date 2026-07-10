# -*- coding: utf-8 -*-
import logging
import werkzeug
import requests
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

class BestPayWebhookController(http.Controller):

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

        # 2. Evitar doble pago
        if tx.state == 'done':
            return request.make_response("Error: Esta transacción ya fue pagada exitosamente.", status=400)

        partner = tx.bestpay_client_id
        if not partner or not partner.mercantil_payment_url or not partner.mercantil_merchant_id:
            return request.make_response("Error interno: Configuración de pasarela incompleta.", status=500)

        # 3. Generar el link "Justo a Tiempo"
        try:
            # =================================================================
            # LLAMADA ULTRA LIMPIA AL CORE PRINCIPAL
            # =================================================================
            # Invoca el método unificado del core que ya maneja de forma segura 
            # las conversiones Base USD.
            tx._bestpay_action_recalculate_jit_ves()

            # 3. Generar el paquete criptográfico con la data recién actualizada
            mercantil_payment_url = partner.mercantil_payment_url.rstrip('/')
            merchant_id = partner.mercantil_merchant_id
            integrator_id = partner.mercantil_integrator_id or ""
            
            # Criptografía AES
            encrypted_data = tx._encrypt_transaction_data()

            custom_link = f"{mercantil_payment_url}/?merchantid={merchant_id}&transactiondata={encrypted_data}&integratorid={integrator_id}"
            
            _logger.info("\n" + "="*80 + f"\n[BESTPAY MERCANTIL DEBUG] LINK GENERADO CON NUEVA TASA:\n{custom_link}\n" + "="*80)
            
            # Redireccionar de inmediato
            return werkzeug.utils.redirect(custom_link, code=303)

        except Exception as e:
            _logger.error("[BESTPAY MERCANTIL API] Error generando link: %s", str(e))
            return request.make_response("Error interno al inicializar el cifrado seguro.", status=500)