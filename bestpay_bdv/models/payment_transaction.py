# -*- coding: utf-8 -*-
import json
import logging
import requests
import hmac
import hashlib
import secrets
from datetime import datetime, timedelta
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

    # CAMPOS PARA DESBLOQUEAR LA BASE DE DATOS (Deben estar dentro de la clase)
    c2p_otp_request_log = fields.Text(string="C2P OTP Request", readonly=True)
    c2p_otp_response_log = fields.Text(string="C2P OTP Response", readonly=True)
    c2p_payment_request_log = fields.Text(string="C2P Payment Request", readonly=True)
    c2p_payment_response_log = fields.Text(string="C2P Payment Response", readonly=True)
    c2p_annulment_request_log = fields.Text(string="C2P Annulment Request", readonly=True)
    c2p_annulment_response_log = fields.Text(string="C2P Annulment Response", readonly=True)

        # =================================================================
    # 🔔 CAMPOS DE CONTROL DEL WEBHOOK AL TERCERO
    # =================================================================
    # TODO [MIGRACIÓN FASE 7]: Mover estos campos al módulo base 'bestpay'
    # junto con los campos del partner, para que sean comunes a BDV y Mercantil.
    bestpay_webhook_state = fields.Selection([
        ('pending', 'Pendiente'),
        ('sent', 'Enviado'),
        ('failed', 'Fallido'),
        ('done', 'Confirmado por el tercero'),
    ], string="Estado Webhook 3ro", copy=False, index=True,
       help="Estado del webhook saliente hacia el sistema del tercero.")
    bestpay_webhook_attempts = fields.Integer(
        string="Intentos de Webhook", default=0, copy=False,
        help="Número de veces que se ha intentado enviar el webhook.")
    bestpay_webhook_last_error = fields.Text(
        string="Último Error del Webhook", copy=False, readonly=True)
    bestpay_webhook_event_id = fields.Char(
        string="ID de Evento Webhook", copy=False, index=True,
        help="Identificador único del evento (para idempotencia del tercero).")
    bestpay_webhook_sent_at = fields.Datetime(
        string="Enviado a las", copy=False, readonly=True)
    bestpay_webhook_next_retry = fields.Datetime(
        string="Próximo Reintento", copy=False, index=True,
        help="Fecha/hora del próximo intento de envío si falló.")
    MAX_WEBHOOK_ATTEMPTS = 5  # Constante: máximo de reintentos
    # Backoff exponencial en minutos: 2, 5, 30, 120, 360
    WEBHOOK_BACKOFF_MINUTES = [2, 5, 30, 120, 360]

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
        creds = provider.bdv_get_api_credentials(partner=self.partner_id)
        if not creds['api_key']:
            raise UserError("Falta configurar la API Key del BDV en el proveedor de pago.")

        # 2. Construir el payload que pide el BDV
        # CORRECCIÓN CRÍTICA: Forzar fecha real en Producción
        env_type = creds.get('environment', 'qa')
        if env_type == 'prod':
            # En PRODUCCIÓN: Ignorar test_date y usar la fecha real del pago o la de hoy
            fecha_pago = str(self.bdv_fecha_pago or fields.Date.today())
            _logger.info(f"[BDV] 🔵 PRODUCCIÓN: Usando fecha real: {fecha_pago}")
        else:
            # En QA: Usar la fecha de prueba si existe
            test_date = creds.get('test_date')
            fecha_pago = test_date if test_date else str(self.bdv_fecha_pago or fields.Date.today())
            _logger.info(f"[BDV] 🟢 QA: Usando fecha: {fecha_pago}")

        payload = {
            "cedulaPagador": self.bdv_cedula_pagador or '',
            "telefonoPagador": self.bdv_telefono_pagador or '',
            "telefonoDestino": creds['telefono_destino'],
            "referencia": self.bdv_referencia or '',
            "fechaPago": fecha_pago,  # ← AHORA USARÁ LA VARIABLE CORREGIDA
            "importe": f"{self.bdv_importe:.2f}",
            "bancoOrigen": self.bdv_banco_origen or '',
            "reqCed": self.bdv_req_ced, # ← ASEGÚRATE DE QUE ESTO ESTÉ MARCADO EN ODOO
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
                })
                _logger.info(f"[BDV] ✅ TX {self.id} APROBADA por el BDV")
                self._bestpay_trigger_webhook_3ro()
                return True
            elif code == 1010:
                # ❌ PAGO RECHAZADO
                self.write({
                    'bdv_conciliation_state': 'rejected',
                    'bdv_conciliation_message': message,
                    'state': 'cancel',
                })
                _logger.warning(f"[BDV] ❌ TX {self.id} RECHAZADA: {message}")
                return False
            else:
                # ⚠️ CÓDIGO DESCONOCIDO
                self.write({
                    'bdv_conciliation_state': 'error',
                    'bdv_conciliation_message': f"Código inesperado: {code} - {message}",
                })
                _logger.error(f"[BDV] ⚠️ TX {self.id} código inesperado: {code}")
                return False

        except requests.exceptions.RequestException as e:
            _logger.error(f"[BDV] Error de conexión: {str(e)}")
            self.write({
                'bdv_conciliation_state': 'error',
                'bdv_conciliation_message': f"Error de conexión: {str(e)}",
                'state': 'error',
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

        datos_banco = {
            'payment_link': payment_link,
            'uuid_hash': self.uuid_hash,
            'flow_type': 'redirect',
        }

        # Guardar la respuesta que se le enviará al tercero cuando el proveedor es BDV.
        # Wilson indicó que este log debe contener los datos relevantes para el 3ro:
        # link de pago, referencia Odoo y hash de la operación.
        respuesta_cliente = {
            'status': 'success',
            'transaction_id': self.id,
            'odoo_reference': self.reference,
            'external_reference': self.external_reference,
            'uuid_hash': self.uuid_hash,
            'flow_type': self.bestpay_flow_type or 'redirect',
            'payment_details': datos_banco,
        }

        self.write({
            'payment_request_response': json.dumps(
                respuesta_cliente,
                ensure_ascii=False,
                indent=4
            )
        })

        return datos_banco

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

    def _bdv_c2p_log_stage(self, stage, request_payload=None, response_payload=None):
        """Agrega (append) un bloque de log por etapa C2P con encabezado.
        Salida (peticion) -> bank_out_log | Entrada (respuesta) -> bank_in_log."""
        def dump(data):
            try:
                return json.dumps(data, indent=2, ensure_ascii=False, default=str)
            except Exception:
                return str(data)
        vals = {}
        if request_payload is not None:
            vals['bank_out_log'] = (self.bank_out_log or '') + \
                f"=== SALIDA · {stage} ===\n{dump(request_payload)}\n\n"
        if response_payload is not None:
            vals['bank_in_log'] = (self.bank_in_log or '') + \
                f"=== ENTRADA · {stage} ===\n{dump(response_payload)}\n\n"
        if vals:
            self.write(vals)

    def bdv_c2p_generate_otp(self):
        """Paso 1: Solicita al BDV el envío del OTP al cliente."""
        self.ensure_one()
        url = f"{self._bdv_get_base_url()}/BankMobilePaymentC2P/MultipleAccounts/paymentkey/v2"
        payload = {
            "customerDocumentId": self.bdv_c2p_customer_document_id
        }
        
        _logger.info(f"BDV C2P OTP Request: {url} | Payload: {payload}")
        
        # Guardar el request en bank_out_log (salida al banco)
        self._bdv_c2p_log_stage("GENERATE OTP", request_payload=payload)
        
        try:
            response = requests.post(url, json=payload, headers=self._bdv_get_c2p_headers(), timeout=15)
            data = response.json()
            _logger.info(f"BDV C2P OTP Response: {data}")
            
            # Guardar la respuesta en bank_in_log (entrada del banco)
            self._bdv_c2p_log_stage("GENERATE OTP", response_payload=data)
            
            if data.get('code') == '1000':
                self.write({'bdv_c2p_status': 'otp_sent'})
                return {'success': True, 'message': data.get('message', 'OTP generado correctamente')}
            
            self.write({'bdv_c2p_status': 'error', 'state_message': data.get('message')})
            raise UserError(f"Error generando OTP: {data.get('message')}")
        except Exception as e:
            self._bdv_c2p_log_stage("GENERATE OTP · ERROR", response_payload={'error': str(e)})
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
        self._bdv_c2p_log_stage("PROCESS PAYMENT (OTP)", request_payload=payload)
        
        try:
            response = requests.post(url, json=payload, headers=self._bdv_get_c2p_headers(), timeout=20)
            data = response.json()
            _logger.info(f"BDV C2P Process Response: {data}")
            
            # Guardar la respuesta
            self._bdv_c2p_log_stage("PROCESS PAYMENT (OTP)", response_payload=data)

            if data.get('code') == '1000' and data.get('data'):
                response_data = data['data']
                self.write({
                    'bdv_c2p_status': 'done',
                    'bdv_c2p_end_to_end_id': response_data.get('endToEndId'),
                    'bdv_c2p_reference_generated': response_data.get('referencia'),
                    'state': 'done',
                    'state_message': 'Pago C2P aprobado por el BDV'
                })

                # 🔔 Disparar webhook al tercero (asíncrono, lo procesa el cron)
                self._bestpay_trigger_webhook_3ro()
                return {'success': True, 'data': response_data}
            
            self.write({
                'bdv_c2p_status': 'error',
                'state': 'error',
                'state_message': data.get('message', 'Error desconocido en el proceso de cobro')
            })
            return {'success': False, 'message': data.get('message')}
        except Exception as e:
            self._bdv_c2p_log_stage("PROCESS PAYMENT (OTP) · ERROR", response_payload={'error': str(e)})
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
        self._bdv_c2p_log_stage("ANULACION", request_payload=payload)
        
        try:
            response = requests.post(url, json=payload, headers=self._bdv_get_c2p_headers(), timeout=15)
            data = response.json()
            _logger.info(f"BDV C2P Annul Response: {data}")
            
            # Guardar la respuesta
            self._bdv_c2p_log_stage("ANULACION", response_payload=data)

            if data.get('code') == '1000':
                self.write({'bdv_c2p_status': 'annulled', 'state': 'cancel'})
                return True
            return False
        except Exception as e:
            self._bdv_c2p_log_stage("ANULACION · ERROR", response_payload={'error': str(e)})
            raise
    
        # =================================================================
    # 🔔 MÉTODOS DE WEBHOOK AL TERCERO (BestPay → Koole/ecommerce)
    # =================================================================
    # TODO [MIGRACIÓN FASE 7]: Mover toda esta lógica al módulo base 'bestpay'
    # para que sea reutilizada por Mercantil. Al migrar, estos métodos deben
    # vivir en 'bestpay/models/payment_transaction.py'.
    def _bestpay_trigger_webhook_3ro(self):
        """
        Dispara el webhook al tercero. Intenta envío inmediato; si falla, lo deja para el cron.
        Esto mejora la UX: el padre ve la deuda pagada al instante en el 95% de los casos.
        """
        self.ensure_one()
        partner = self.bestpay_client_id or self.partner_id
        if not partner or not partner.webhook_url_3ro or not partner.bestpay_webhook_active:
            _logger.info("[BESTPAY WEBHOOK] TX %s: no configurado o desactivado.", self.id)
            return
        
        # Generar event_id si no existe
        event_id = self.bestpay_webhook_event_id or f"evt_{secrets.token_urlsafe(24)}"
        self.write({
            'bestpay_webhook_event_id': event_id,
            'bestpay_webhook_state': 'pending',  # Temporal durante el intento
            'bestpay_webhook_attempts': 0,
            'bestpay_webhook_next_retry': False,
        })
        
        # 🚀 INTENTO SÍNCRONO INMEDIATO
        _logger.info("[BESTPAY WEBHOOK] TX %s: Intento síncrono inmediato...", self.id)
        self._bestpay_send_webhook_3ro()

    def _bestpay_build_webhook_payload_3ro(self):
        """Construye el payload JSON que se enviará al tercero."""
        self.ensure_one()
        # Recolectar datos bancarios según el método usado
        bank_data = {}
        if self.payment_method_id.code == 'bdv_c2p':
            bank_data = {
                'end_to_end_id': self.bdv_c2p_end_to_end_id or False,
                'bank_reference': self.bdv_c2p_reference_generated or False,
                'approval_code': False,
            }
        else:
            # Pago Móvil: los datos están en la respuesta del banco (bank_in_log)
            try:
                bank_response = json.loads(self.bank_in_log or '{}')
                data = bank_response.get('data', {}) if isinstance(bank_response, dict) else {}
                bank_data = {
                    'end_to_end_id': data.get('endToEndId') or data.get('end_to_end_id') or False,
                    'bank_reference': data.get('referencia') or self.bdv_referencia or False,
                    'approval_code': data.get('approvalCode') or False,
                }
            except (ValueError, AttributeError):
                bank_data = {'bank_reference': self.bdv_referencia or False}

        return {
            'event_id': self.bestpay_webhook_event_id,
            'event_type': 'payment.done',
            'timestamp': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'transaction': {
                'uuid_hash': self.uuid_hash,
                'reference': self.reference,
                'provider': self.provider_id.code,
                'payment_method': self.payment_method_id.code if self.payment_method_id else False,
                'external_reference': self.external_reference,
                'amount': float(self.amount),
                'currency': self.currency_id.name if self.currency_id else False,
                'amount_ves': float(self.amount_ves or 0.0),
                'exchange_rate_bcv': float(self.exchange_rate_bcv or 0.0),
                'state': self.state,
                'client_note': self.client_note or '',
            },
            'bank_data': bank_data,
        }

    def _bestpay_compute_webhook_signature_3ro(self, payload_str, secret):
        """Calcula la firma HMAC-SHA256 del payload usando el secreto compartido."""
        return hmac.new(
            key=secret.encode('utf-8'),
            msg=payload_str.encode('utf-8'),
            digestmod=hashlib.sha256,
        ).hexdigest()

    @api.model
    def _bestpay_process_pending_webhooks(self):
        """
        Método invocado por el CRON cada 2 minutos.
        Procesa todas las transacciones con webhook pendiente o con reintento programado.
        """
        now = fields.Datetime.now()
        # Buscar transacciones candidatas:
        # - webhook pendiente O fallido con intentos < MAX
        # - próxima hora de reintento ya alcanzada
        candidates = self.search([
            ('bestpay_webhook_state', 'in', ['pending', 'failed']),
            ('bestpay_webhook_attempts', '<', self.MAX_WEBHOOK_ATTEMPTS),
            '|',
            ('bestpay_webhook_next_retry', '<=', now),
            ('bestpay_webhook_next_retry', '=', False),
        ], limit=20)  # Procesamos en lotes de 20 por ejecución de cron

        _logger.info(
            "[BESTPAY WEBHOOK CRON] Procesando %d transacciones candidatas.",
            len(candidates)
        )

        for tx in candidates:
            tx._bestpay_send_webhook_3ro()

    def _bestpay_send_webhook_3ro(self):
        """Envía el webhook al tercero (un intento). Actualiza el estado."""
        self.ensure_one()
        partner = self.bestpay_client_id or self.partner_id
        if not partner or not partner.webhook_url_3ro or not partner.bestpay_webhook_secret:
            _logger.warning(
                "[BESTPAY WEBHOOK] TX %s sin URL o secreto. Se marca como fallido permanente.",
                self.id
            )
            self.write({
                'bestpay_webhook_state': 'failed',
                'bestpay_webhook_last_error': 'URL o secreto no configurados en el partner.',
                'bestpay_webhook_attempts': self.MAX_WEBHOOK_ATTEMPTS,  # No reintenta más
            })
            return

        try:
            payload = self._bestpay_build_webhook_payload_3ro()
            # Serialización estable: sort_keys para garantizar misma firma siempre
            payload_str = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
            signature = self._bestpay_compute_webhook_signature_3ro(payload_str, partner.bestpay_webhook_secret)

            headers = {
                'Content-Type': 'application/json; charset=utf-8',
                'X-BestPay-Signature': f'sha256={signature}',
                'X-BestPay-Event-ID': self.bestpay_webhook_event_id or '',
                'User-Agent': 'BestPay-Webhook/1.0',
            }

            _logger.info(
                "[BESTPAY WEBHOOK] TX %s → %s (intento %d/%d)",
                self.id, partner.webhook_url_3ro,
                self.bestpay_webhook_attempts + 1, self.MAX_WEBHOOK_ATTEMPTS
            )

            response = requests.post(
                partner.webhook_url_3ro,
                data=payload_str,
                headers=headers,
                timeout=10,
            )

            # Guardar respuesta en el campo existente de logs
            response_snapshot = json.dumps({
                'status_code': response.status_code,
                'headers': dict(response.headers),
                'body': response.text[:2000],
                'timestamp': datetime.utcnow().isoformat(),
            }, ensure_ascii=False, indent=2)

            if 200 <= response.status_code < 300:
                # ✅ ÉXITO
                self.write({
                    'bestpay_webhook_state': 'done',
                    'bestpay_webhook_attempts': self.bestpay_webhook_attempts + 1,
                    'bestpay_webhook_sent_at': fields.Datetime.now(),
                    'bestpay_webhook_last_error': False,
                })
                _logger.info("[BESTPAY WEBHOOK] ✅ TX %s confirmada por tercero (HTTP %s).",
                             self.id, response.status_code)
            else:
                # ⚠️ Respuesta no-2xx → reintento
                error_msg = f"HTTP {response.status_code}: {response.text[:500]}"
                self._bestpay_schedule_retry(error_msg)

        except requests.exceptions.Timeout:
            self._bestpay_schedule_retry("Timeout (10s) esperando respuesta del tercero.")
        except requests.exceptions.ConnectionError as e:
            self._bestpay_schedule_retry(f"Error de conexión: {str(e)[:200]}")
        except Exception as e:
            _logger.exception("[BESTPAY WEBHOOK] Error inesperado en TX %s", self.id)
            self._bestpay_schedule_retry(f"Error inesperado: {str(e)[:200]}")

    def _bestpay_schedule_retry(self, error_msg):
        """Programa el próximo reintento con backoff exponencial."""
        self.ensure_one()
        attempts = self.bestpay_webhook_attempts + 1
        if attempts >= self.MAX_WEBHOOK_ATTEMPTS:
            # Ya no reintenta más
            self.write({
                'bestpay_webhook_state': 'failed',
                'bestpay_webhook_attempts': attempts,
                'bestpay_webhook_last_error': error_msg,
            })
            _logger.warning(
                "[BESTPAY WEBHOOK] ❌ TX %s agotó %d intentos. Último error: %s",
                self.id, attempts, error_msg
            )
            return

        # Calcular próximo reintento usando backoff
        backoff_idx = min(attempts - 1, len(self.WEBHOOK_BACKOFF_MINUTES) - 1)
        minutes = self.WEBHOOK_BACKOFF_MINUTES[backoff_idx]
        next_retry = datetime.utcnow() + timedelta(minutes=minutes)

        self.write({
            'bestpay_webhook_state': 'failed',
            'bestpay_webhook_attempts': attempts,
            'bestpay_webhook_last_error': error_msg,
            'bestpay_webhook_next_retry': next_retry,
        })
        _logger.info(
            "[BESTPAY WEBHOOK] ⏱️ TX %s reintentará en %d min (intento %d/%d). Error: %s",
            self.id, minutes, attempts, self.MAX_WEBHOOK_ATTEMPTS, error_msg
        )

        # =====================================================
    # LÓGICA DE PROCESAMIENTO DE NOTIFICACIÓN (WEBHOOK)
    # Busca por Teléfono del pagador (Recomendación BDV)
    # =====================================================
    def bdv_process_notification_webhook(self, payload, partner):
        """
        Procesa la notificación del BDV buscando la transacción por el 
        número de teléfono del pagador (numeroCliente).
        """
        # 1. Extraer y normalizar datos
        telefono_pagador = payload.get('numeroCliente', '').strip()
        monto_str = payload.get('monto', '0.0')
        referencia = payload.get('referenciaBancoOrdenante', '').strip()
        cedula_pagador = payload.get('idCliente', '').strip()
        banco_origen = payload.get('bancoOrdenante', '').strip()
        
        try:
            monto = float(monto_str)
        except (ValueError, TypeError):
            monto = 0.0

        # Normalizar teléfono (asegurar que empiece con 0)
        if telefono_pagador and not telefono_pagador.startswith('0'):
            telefono_pagador = '0' + telefono_pagador

        # ==========================================================
        # BÚSQUEDA PRINCIPAL: Por Teléfono + Monto (Recomendación BDV)
        # ==========================================================
        tx = self.search([
            ('bdv_telefono_pagador', '=', telefono_pagador),
            ('bdv_importe', '=', monto),
            # Eliminamos el filtro de estado para encontrar también las 'done'
        ], limit=1).filtered(lambda t: t.state in ['draft', 'pending', 'done'])

        # ==========================================================
        # ESCENARIO A: Transacción encontrada
        # ==========================================================
        if tx:

            if tx.state == 'done':
                _logger.info(f"[BDV NOTIFICACIÓN] TX {tx.id} ya está completada → Código 01 (Re-notificación)")
                return {'codigo': '01'}  # ✅ Retorna 01 para re-notificaciones
            
            # Actualizar con los datos reales del banco y marcar como pagada
            vals_to_write = {
                'state': 'done',
                'bdv_conciliation_state': 'approved',
                'bdv_conciliation_message': 'Aprobado vía webhook notificación BDV',
                'bdv_referencia': referencia, # Guardamos la referencia real del banco
            }
            if not tx.bdv_banco_origen and banco_origen:
                vals_to_write['bdv_banco_origen'] = banco_origen
            
            # Nota: En pagos interbancarios, el BDV manda "V" + RIF del comercio en idCliente.
            # Solo guardamos la cédula si parece ser una cédula real de persona natural.
            if not tx.bdv_cedula_pagador and cedula_pagador and not cedula_pagador.startswith('V'):
                vals_to_write['bdv_cedula_pagador'] = cedula_pagador

            tx.write(vals_to_write)
            _logger.info(f"[BDV NOTIFICACIÓN] ✅ TX {tx.id} marcada como pagada (Buscada por Tel: {telefono_pagador})")
            
            # Disparar webhook al tercero (Koole/Ecommerce)
            try:
                tx._bestpay_trigger_webhook_3ro()
            except Exception as e:
                _logger.error(f"[BDV NOTIFICACIÓN] Error disparando webhook al tercero: {e}")
            
            return {'codigo': '00'}

        # ==========================================================
        # ESCENARIO B: Pago Directo (No existe en Odoo)
        # Crear transacción en 'draft' para conciliación manual posterior
        # ==========================================================
        _logger.warning(f"[BDV NOTIFICACIÓN] ️ Pago directo detectado. Creando TX en draft. Tel: {telefono_pagador}, Monto: {monto}")
        
        # Buscar el proveedor BDV por defecto
        provider = self.env['payment.provider'].sudo().search([
            ('code', '=', 'bdv'),
            ('is_bestpay_provider', '=', True),
        ], limit=1)
        
        # Buscar moneda VES
        currency_ves = self.env['res.currency'].sudo().search([('name', '=', 'VES')], limit=1)

        try:
            new_tx = self.create({
                'provider_id': provider.id if provider else False,
                'partner_id': partner.id if partner else False, # El comercio que recibió el dinero
                'amount': monto,
                'currency_id': currency_ves.id if currency_ves else False,
                'amount_ves': monto,
                'state': 'draft',
                'bdv_conciliation_state': 'draft',
                'bdv_conciliation_message': 'Creado automáticamente por Webhook (Pago Directo)',
                'bdv_referencia': referencia,
                'bdv_importe': monto,
                'bdv_telefono_pagador': telefono_pagador,
                'bdv_banco_origen': banco_origen,
                'bdv_cedula_pagador': cedula_pagador if cedula_pagador else '', 
                'client_note': f"Pago directo notificado por BDV. Teléfono pagador: {telefono_pagador}",
            })
            _logger.info(f"[BDV NOTIFICACIÓN] ✅ Transacción en draft creada exitosamente: ID {new_tx.id}")
            return {'codigo': '00'}
            
        except Exception as e:
            _logger.error(f"[BDV NOTIFICACIÓN] ❌ Error creando transacción en draft: {e}", exc_info=True)
            return {'codigo': '00'}