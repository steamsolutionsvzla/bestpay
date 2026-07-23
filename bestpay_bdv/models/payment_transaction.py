# -*- coding: utf-8 -*-
import json
import logging
import requests
from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PaymentTransactionBDV(models.Model):
    _inherit = 'payment.transaction'

    payment_link = fields.Char(
        string="Link de Pago",
        help="URL completa del formulario de pago que se envía al cliente final.",
        copy=False,
        readonly=True,
    )

    # =====================================================
    # CAMPOS ESPECÍFICOS DEL FLUJO BDV (Pago Móvil)
    # =====================================================
    # Estos son los datos que el BDV pide para conciliar el pago
    bdv_cedula_pagador = fields.Char(
        string="Cédula Pagador BDV",
        help="Cédula de quien realiza el pago móvil (ej: V12345678).",
    )
    bdv_telefono_pagador = fields.Char(
        string="Teléfono Pagador BDV",
        help="Teléfono de quien realiza el pago móvil (ej: 04121234567).",
    )
    bdv_banco_origen = fields.Char(
        string="Banco Origen BDV",
        help="Código del banco de donde sale el pago (ej: 0102 = BDV, 0105 = Mercantil).",
    )
    bdv_referencia = fields.Char(
        string="Referencia BDV",
        help="Número de referencia del pago móvil proporcionado por el pagador.",
    )
    bdv_fecha_pago = fields.Date(
        string="Fecha del Pago BDV",
        help="Fecha en que se realizó el pago móvil.",
    )
    bdv_importe = fields.Float(
        string="Importe BDV (VES)",
        help="Monto en Bolívares que el pagador dice haber transferido.",
        digits=(12, 2),
    )
    bdv_req_ced = fields.Boolean(
        string="Requiere Cédula BDV",
        default=False,
    )

    # =====================================================
    # ESTADO DEL PROCESO DE CONCILIACIÓN
    # =====================================================
    bdv_conciliation_state = fields.Selection([
        ('draft', 'Borrador'),
        ('sent', 'Enviado al BDV'),
        ('approved', 'Aprobado (1000)'),
        ('rejected', 'Rechazado (1010)'),
        ('error', 'Error de conexión'),
    ], string="Estado Conciliación BDV", default='draft')

    bdv_conciliation_message = fields.Char(
        string="Mensaje del BDV",
        help="Mensaje devuelto por el BDV en la última conciliación.",
    )

    # ============================================
    # CAMPOS ESPECÍFICOS C2P CUENTAS MÚLTIPLES
    # ============================================
    
    bdv_c2p_customer_document_id = fields.Char(
        string="Cédula Cliente (C2P)",
        help="Documento de identidad del cliente pagador (ej: V12345678)"
    )
    
    bdv_c2p_customer_phone = fields.Char(
        string="Teléfono Cliente (C2P)",
        help="Número de teléfono del cliente (instrumento de pago)"
    )
    
    bdv_c2p_customer_bank_code = fields.Char(
        string="Código Banco Cliente (C2P)",
        help="Código del banco del cliente (ej: 0102 para Venezuela)"
    )
    
    bdv_c2p_otp = fields.Char(
        string="OTP (C2P)",
        groups="base.group_system",
        help="Código OTP generado por el banco (solo visible para administradores)"
    )
    
    bdv_c2p_end_to_end_id = fields.Char(
        string="EndToEnd ID (C2P)",
        readonly=True,
        help="Identificador único de la transacción devuelto por el BDV"
    )
    
    bdv_c2p_reference_generated = fields.Char(
        string="Referencia Generada (C2P)",
        readonly=True,
        help="Número de referencia generado por el banco"
    )
    
    bdv_c2p_status = fields.Selection([
        ('pending', 'Pendiente'),
        ('otp_sent', 'OTP Enviado'),
        ('processing', 'Procesando'),
        ('done', 'Completado'),
        ('error', 'Error'),
        ('annulled', 'Anulado'),
    ], string="Estado C2P", default='pending',
       help="Estado actual del flujo multi-paso C2P")
    
    bdv_c2p_coin_type = fields.Char(
        string="Tipo de Moneda (C2P)",
        default='VES',
        help="Moneda de la transacción (VES, USD, etc.)"
    )
    
    bdv_c2p_operation_type = fields.Char(
        string="Tipo de Operación (C2P)",
        default='CELE',
        help="Tipo de operación (CELE=pago, REV=reversa)"
    )
    
    bdv_c2p_concept = fields.Char(
        string="Concepto (C2P)",
        help="Descripción del pago (concepto)"
    )

    def bdv_send_conciliation(self):
        """
        Envía los datos del pago móvil al BDV para conciliación.
        Devuelve True si fue aprobado, False en caso contrario.
        """
        self.ensure_one()
        provider = self.provider_id

        if provider.code != 'bdv':
            raise UserError("Este método solo aplica para el proveedor BDV.")

        # 1. Obtener credenciales del provider, pasando el partner (comercio)
        # para que tome la API Key y teléfono destino del partner, no del provider
        creds = provider.bdv_get_api_credentials(partner=self.partner_id)
        
        if not creds['api_key']:
            raise UserError("Falta configurar la API Key del BDV en el proveedor de pago.")

        # 2. Construir el payload que pide el BDV
        # Usar fecha de prueba si está configurada, sino usar fecha actual
        test_date = creds.get('test_date')
        fecha_pago = test_date if test_date else str(self.bdv_fecha_pago or fields.Date.today())
        
        payload = {
            "cedulaPagador": self.bdv_cedula_pagador or '',
            "telefonoPagador": self.bdv_telefono_pagador or '',
            "telefonoDestino": creds['telefono_destino'],
            "referencia": self.bdv_referencia or '',
            "fechaPago": fecha_pago,  # ← CAMBIAR ESTA LÍNEA
            "importe": f"{self.bdv_importe:.2f}",
            "bancoOrigen": self.bdv_banco_origen or '',
            "reqCed": self.bdv_req_ced,
        }

        headers = {
            "X-API-Key": creds['api_key'],
            "Content-Type": "application/json",
        }

        _logger.info(f"[BDV] Enviando conciliación para TX {self.id}: {json.dumps(payload)}")

        # 3. Marcar como "enviado"
        self.write({
            'bdv_conciliation_state': 'sent',
            'bank_out_log': json.dumps(payload, indent=2),
        })

        # 4. Hacer la petición HTTP al BDV
        try:
            response = requests.post(
                creds['api_url'],
                json=payload,
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            respuesta = response.json()

            _logger.info(f"[BDV] Respuesta recibida: {respuesta}")

            # 5. Guardar la respuesta cruda
            self.bank_in_log = json.dumps(respuesta, indent=2, ensure_ascii=False)

            # 6. Procesar el código de respuesta del BDV
            code = respuesta.get("code")
            message = respuesta.get("message", "")

            if code == 1000:
                # ✅ PAGO APROBADO
                self.write({
                    'bdv_conciliation_state': 'approved',
                    'bdv_conciliation_message': message,
                    'state': 'done',
                    'bank_in_log': json.dumps(respuesta, indent=2, ensure_ascii=False),
                })
                _logger.info(f"[BDV] ✅ TX {self.id} APROBADA por el BDV")
                return True

            elif code == 1010:
                # ❌ PAGO RECHAZADO
                self.write({
                    'bdv_conciliation_state': 'rejected',
                    'bdv_conciliation_message': message,
                    'state': 'cancel',
                    'bank_in_log': json.dumps(respuesta, indent=2, ensure_ascii=False),
                })
                _logger.warning(f"[BDV] ❌ TX {self.id} RECHAZADA: {message}")
                return False

            else:
                # ⚠️ CÓDIGO DESCONOCIDO
                self.write({
                    'bdv_conciliation_state': 'error',
                    'bdv_conciliation_message': f"Código inesperado: {code} - {message}",
                    'bank_in_log': json.dumps(respuesta, indent=2, ensure_ascii=False),
                })
                _logger.error(f"[BDV] ⚠️ TX {self.id} código inesperado: {code}")
                return False

        except requests.exceptions.RequestException as e:
            # ❌ ERROR DE CONEXIÓN - Sin simulación, solo registro del error
            _logger.error(f"[BDV] Error de conexión: {str(e)}")
            
            self.write({
                'bdv_conciliation_state': 'error',
                'bdv_conciliation_message': f"Error de conexión: {str(e)}",
                'state': 'error',
                'bank_in_log': json.dumps({
                    'error': str(e),
                    'payload_sent': payload,
                }, indent=2, ensure_ascii=False),
            })
            raise UserError(f"Error de conexión con el BDV: {str(e)}")
    
    @api.model_create_multi
    def create(self, vals_list):
        """
        Sobrescribe create() para asignar automáticamente payment_method_id
        cuando el proveedor es BDV. Resuelve la restricción NOT NULL de Odoo 19
        sin modificar el módulo base de BestPay.
        """
        for vals in vals_list:
            provider_id = vals.get('provider_id')
            if provider_id and not vals.get('payment_method_id'):
                provider = self.env['payment.provider'].sudo().browse(provider_id)
                if provider.code == 'bdv':
                    # Buscar o crear el método de pago BDV
                    pm = self.env['payment.method'].sudo().search([
                        ('code', '=', 'bdv_pagomovil'),
                        ('active', '=', True)
                    ], limit=1)
                    
                    if not pm:
                        pm = self.env['payment.method'].sudo().create({
                            'name': 'Pago Móvil BDV',
                            'code': 'bdv_pagomovil',
                            'active': True,
                        })
                        _logger.info(f"[BDV] Método de pago creado: {pm.name}")
                    
                    vals['payment_method_id'] = pm.id
                    _logger.info(f"[BDV] payment_method_id asignado automáticamente: {pm.id}")
        
        return super().create(vals_list) 
    
    def _bestpay_process_transaction_with_bank(self, data):
        """
        Método llamado por el endpoint base de BestPay.
        Usa el uuid_hash existente para construir el link de pago.
        """
        self.ensure_one()

        # Si NO es BDV, le pasamos el control al siguiente módulo en la cadena (Mercantil)
        if self.provider_id.code != 'bdv':
            return super()._bestpay_process_transaction_with_bank(data)

        # Si SÍ es BDV, procesamos nuestra lógica
        _logger.info("[BDV] Procesando transacción %s", self.id)

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        payment_link = f"{base_url}/pago/bdv/checkout?hash={self.uuid_hash}"

        self.write({
            'payment_link': payment_link,
            'bestpay_flow_type': 'redirect',
        })

        _logger.info("[BDV] Link generado con UUID_HASH para TX %s: %s", self.id, payment_link)

        return {
            'payment_link': payment_link,
            'uuid_hash': self.uuid_hash,
            'flow_type': 'redirect',
        }

    # =========================================================================
    # MÉTODOS API C2P CUENTAS MÚLTIPLES (BDV)
    # =========================================================================

    def _bdv_get_base_url(self):
        """Obtiene la URL base del proveedor, eliminando el endpoint específico de Pago Móvil."""
        self.ensure_one()
        url = self.provider_id.bdv_api_url or 'https://bdvconciliacionqa.banvenez.com:444'
        # Limpia la URL para dejar solo el host y puerto base
        return url.replace('/getMovement/v2', '').replace('/getMovement', '').rstrip('/')

    def _bdv_get_c2p_headers(self):
        """Construye los headers necesarios para las peticiones C2P."""
        self.ensure_one()
        # Lee la API Key específica de C2P del partner (comercio)
        api_key = getattr(self.partner_id, 'bdv_api_key_c2p', '')
        if not api_key:
            _logger.error(f"BDV C2P: Falta bdv_api_key_c2p en el partner {self.partner_id.name}")
            raise UserError("Error de configuración: Falta la API Key de C2P en los datos del comercio.")
        
        return {
            "X-API-Key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def bdv_c2p_generate_otp(self):
        """Paso 1: Solicita al BDV el envío del OTP al cliente."""
        self.ensure_one()
        url = f"{self._bdv_get_base_url()}/BankMobilePaymentC2P/MultipleAccounts/paymentkey/v2"
        payload = {
            "customerDocumentId": self.bdv_c2p_customer_document_id
        }
        
        _logger.info(f"BDV C2P OTP Request: {url} | Payload: {payload}")
        
        # Guardar el request en bank_out_log (salida al banco)
        self.write({'bank_out_log': json.dumps(payload, indent=2)})
        
        try:
            response = requests.post(url, json=payload, headers=self._bdv_get_c2p_headers(), timeout=15)
            data = response.json()
            _logger.info(f"BDV C2P OTP Response: {data}")
            
            # Guardar la respuesta en bank_in_log (entrada del banco)
            self.write({'bank_in_log': json.dumps(data, indent=2, ensure_ascii=False)})
            
            if data.get('code') == '1000':
                self.write({'bdv_c2p_status': 'otp_sent'})
                return {'success': True, 'message': data.get('message', 'OTP generado correctamente')}
            
            self.write({'bdv_c2p_status': 'error', 'state_message': data.get('message')})
            raise UserError(f"Error generando OTP: {data.get('message')}")
        except Exception as e:
            self.write({'bank_in_log': json.dumps({'error': str(e)}, indent=2)})
            raise

    def bdv_c2p_process_payment(self):
        """Paso 2: Procesa el cobro real utilizando el OTP proporcionado."""
        self.ensure_one()
        url = f"{self._bdv_get_base_url()}/BankMobilePaymentC2P/MultipleAccounts/process/v2"
        
        phone_destino = getattr(self.partner_id, 'bdv_phone_destino_c2p', '')
        if not phone_destino:
            raise UserError("Falta configurar el 'bdv_phone_destino_c2p' en el contacto del comercio.")

        # LÓGICA DE QA vs PRODUCCIÓN (Monto y Concepto)
        env_type = self.provider_id.bdv_environment.strip().lower() if self.provider_id.bdv_environment else 'qa'
        
        if env_type == 'qa':
            amount_str = "1000.6"
            concept_str = "Pago"
            _logger.info(f"[BDV C2P] 🟢 Ambiente QA detectado. Forzando monto: {amount_str} Bs y concepto: '{concept_str}'")
        else:
            amount_float = getattr(self, 'amount_ves', 0.0)
            if not amount_float or float(amount_float) <= 0:
                amount_float = float(self.amount)
            amount_str = f"{amount_float:.2f}"
            concept_str = self.bdv_c2p_concept or f"Pago BestPay Ref: {self.reference}"
            _logger.info(f"[BDV C2P] 🔵 Ambiente PRODUCCIÓN. Monto: {amount_str} Bs")

        payload = {
            "customerDocumentId": self.bdv_c2p_customer_document_id,
            "customerNumberInstrument": self.bdv_c2p_customer_phone,
            "amount": amount_str,
            "customerBankCode": self.bdv_c2p_customer_bank_code,
            "concept": concept_str,
            "otp": self.bdv_c2p_otp,
            "coinType": self.bdv_c2p_coin_type or "VES",
            "operationType": self.bdv_c2p_operation_type or "CELE",
            "commerceNumberInstrument": phone_destino
        }
        
        _logger.info(f"BDV C2P Process Request: {url} | Payload: {payload}")
        
        # Guardar el request
        self.write({'bank_out_log': json.dumps(payload, indent=2, ensure_ascii=False)})
        
        try:
            response = requests.post(url, json=payload, headers=self._bdv_get_c2p_headers(), timeout=20)
            data = response.json()
            _logger.info(f"BDV C2P Process Response: {data}")
            
            # Guardar la respuesta
            self.write({'bank_in_log': json.dumps(data, indent=2, ensure_ascii=False)})

            if data.get('code') == '1000' and data.get('data'):
                response_data = data['data']
                self.write({
                    'bdv_c2p_status': 'done',
                    'bdv_c2p_end_to_end_id': response_data.get('endToEndId'),
                    'bdv_c2p_reference_generated': response_data.get('referencia'),
                    'state': 'done',
                    'state_message': 'Pago C2P aprobado por el BDV'
                })
                return {'success': True, 'data': response_data}
            
            self.write({
                'bdv_c2p_status': 'error',
                'state': 'error',
                'state_message': data.get('message', 'Error desconocido en el proceso de cobro')
            })
            return {'success': False, 'message': data.get('message')}
        except Exception as e:
            self.write({'bank_in_log': json.dumps({'error': str(e)}, indent=2)})
            raise

    def bdv_c2p_annul(self):
        """Paso 3: Anula la transacción en caso de error posterior o reversión."""
        self.ensure_one()
        if not self.bdv_c2p_end_to_end_id:
            _logger.warning("BDV C2P Annul: No hay endToEndId para anular.")
            return False
            
        url = f"{self._bdv_get_base_url()}/BankMobilePaymentC2P/MultipleAccounts/annulment/v2"
        payload = {
            "endToEndId": self.bdv_c2p_end_to_end_id,
            "referenceOrigin": None
        }
        
        _logger.info(f"BDV C2P Annul Request: {url} | Payload: {payload}")
        
        # Guardar el request
        self.write({'bank_out_log': json.dumps(payload, indent=2)})
        
        try:
            response = requests.post(url, json=payload, headers=self._bdv_get_c2p_headers(), timeout=15)
            data = response.json()
            _logger.info(f"BDV C2P Annul Response: {data}")
            
            # Guardar la respuesta
            self.write({'bank_in_log': json.dumps(data, indent=2, ensure_ascii=False)})

            if data.get('code') == '1000':
                self.write({'bdv_c2p_status': 'annulled', 'state': 'cancel'})
                return True
            return False
        except Exception as e:
            self.write({'bank_in_log': json.dumps({'error': str(e)}, indent=2)})
            raise