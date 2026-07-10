# -*- coding: utf-8 -*-
import base64
import json
import logging
import hashlib
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    # =========================================================================
    # CAMPOS DEL MÓDULO SATÉLITE
    # =========================================================================
    trx_type = fields.Selection([
        ('compra', 'Compra'),
        ('venta', 'Venta')
    ], string="Tipo de Transacción Mercantil", default='compra', required=True)

    mercantil_payment_concepts = fields.Char(
        string="Conceptos para esta Transacción",
        help="Conceptos específicos guardados para esta operación. Si se deja vacío, se usarán los del partner."
    )

    return_url_3ro = fields.Char(
        string="URL Retorno Específica (3ro)",
        help="Dirección de redirección final que el comercio 3ro provee en caliente para su pasarela."
    )

    # =========================================================================
    # ORQUESTADOR PRINCIPAL (ENTRADA DESDE LA API)
    # =========================================================================
    def _bestpay_process_transaction_with_bank(self, api_kwargs):
        """
        Punto de entrada llamado por el controlador de la API de BestPay.
        Prepara el flujo de redirección web de forma transparente para el tercero,
        guardando las preferencias enviadas en la petición.
        """
        self.ensure_one()
        
        if self.provider_id.code == 'mer':
            _logger.info("[BESTPAY MERCANTIL] Registrando flujo de redirección para Tx ID %s", self.id)
            
            # 1. Obtener la URL intermedia de redirección propia de este registro en Odoo usando el uuid_hash
            base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
            redirect_url_odoo = f"{base_url}/api/v1/transaction/mer/redirect/{self.uuid_hash}"
            
            # 2. Extraer parámetros específicos de Mercantil enviados por el tercero en la API
            trx_type = api_kwargs.get('trx_type', 'compra')
            return_url_3ro = api_kwargs.get('return_url_3ro', False)
            payment_concepts = api_kwargs.get('payment_concepts', False) # Por si mandan una lista/string específico

            # Si pasaron payment_concepts como lista/diccionario, lo convertimos a string para guardarlo en el Char
            if payment_concepts and isinstance(payment_concepts, (list, dict)):
                payment_concepts = json.dumps(payment_concepts)

            # 3. Guardar la configuración en la transacción actual
            self.write({
                'bestpay_flow_type': 'redirect',
                'trx_type': trx_type,
                'return_url_3ro': return_url_3ro,
                'mercantil_payment_concepts': payment_concepts
            })
            
            # 4. Al tercero solo le devolvemos el link seguro de nuestro Odoo
            return {
                "redirect_url": redirect_url_odoo
            }
            
        return {}

    # =========================================================================
    # FUNCIONES DE CONSTRUCCIÓN Y ENCRIPTACIÓN (MERCANTIL)
    # =========================================================================
    def _build_transaction_data(self):
        """
        Build dict for bank encryption utilizando los datos dinámicos 
        del Hub BestPay y las prioridades de redirección del tercero.
        """
        self.ensure_one()
        partner = self.bestpay_client_id

        if not partner:
            raise UserError("La transacción no tiene un cliente API (BestPay) asociado para extraer credenciales.")

        if not self.amount_ves or self.amount_ves <= 0:
            raise UserError("El importe calculado en VES debe ser mayor que cero para proceder con el pago.")

        # Manejo seguro de los conceptos de pago (prioriza la transacción, luego el partner)
        concepts_str = self.mercantil_payment_concepts or partner.mercantil_payment_concepts or '["b2b","c2p","tdd"]'
        try:
            payment_concepts = json.loads(concepts_str)
        except Exception:
            payment_concepts = ["b2b", "c2p", "tdd"]

        # LÓGICA DE REDIRECCIÓN DIRECTA DEL BANCO (Prioridades de URL de Retorno):
        # 1. URL enviada en caliente en la transacción actual (return_url_3ro)
        # 2. URL guardada por defecto en la ficha del Partner (default_return_url_3ro)
        # 3. URL genérica interna de Odoo (/payment/processing)
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        
        final_return_url = self.return_url_3ro or partner.default_return_url_3ro or f"{base_url}/payment/mercantil/processing"

        return {
            "amount": float(self.amount_ves),
            "customerName": self.partner_id.name or partner.name or "Cliente General",
            "returnUrl": final_return_url,
            "merchantId": partner.mercantil_merchant_id or "",
            "invoiceNumber": {
                "number": str(self.id).zfill(12), 
                "invoiceCreationDate": fields.Date.today().strftime("%Y-%m-%d"),
                "invoiceCancelledDate": ""
            },
            "contract": {
                "contractNumber": str(self.id), # Pasamos el ID de la transacción como número de contrato
                "contractDate": fields.Date.today().strftime("%Y-%m-%d")
            },
            "trxType": self.trx_type,
            "currency": "VES",
            "paymentConcepts": payment_concepts
        }

    def _encrypt_transaction_data(self):
        """
        Cifra la data construida con el estándar AES-ECB 128-bit 
        utilizando la clave secreta única del tercero (res.partner).
        """
        self.ensure_one()
        partner = self.bestpay_client_id
        
        if not partner or not partner.mercantil_secret_key:
            raise UserError("Configuración incompleta: Falta la Clave Secreta de Mercantil en el registro del cliente.")

        # 1. Ejecutar el build adaptado
        transaction_data = self._build_transaction_data()
        
        # 2. Generar string plano JSON
        json_str = json.dumps(transaction_data, ensure_ascii=False)
        
        # 3. Derivar llave hash de 16 bytes (AES-128) basándose en la llave del partner
        key = partner.mercantil_secret_key
        key_hash = hashlib.sha256(key.encode('utf-8')).digest()[:16]
        
        # 4. Proceso criptográfico AES ECB + PKCS7 Padding
        cipher = AES.new(key_hash, AES.MODE_ECB)
        encrypted = cipher.encrypt(
            pad(json_str.encode('utf-8'), AES.block_size)
        )
        
        # 5. Salida final en Base64 plano para el parámetro del botón
        return base64.b64encode(encrypted).decode('utf-8')