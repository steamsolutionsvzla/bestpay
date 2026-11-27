# -*- coding: utf-8 -*-
import json
import logging
import requests
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from odoo import api, fields, models
from odoo.exceptions import UserError

from odoo.addons.bestpay.utils.business_logger import is_logged, log_business_error
from odoo.addons.bestpay.utils.identity import (
    document_variants,
    normalize_document,
    normalize_phone,
    only_digits,
    parse_amount,
    parse_bank_datetime,
    phone_variants,
    reference_matches,
)

_logger = logging.getLogger(__name__)


class PaymentTransactionBDV(models.Model):
    _inherit = 'payment.transaction'

    payment_link = fields.Char(
        string="Link de Pago",
        help="URL completa del formulario de pago que se envía al cliente final.",
        copy=False,
        readonly=True,
    )

    bdv_return_url_3ro = fields.Char(
        string="URL de Retorno (3ro) BDV",
        help="URL del portal del cliente (ej. Koole) a la que se devuelve al pagador "
             "al terminar el checkout. Si se deja vacía se usa la URL por defecto "
             "configurada en la ficha del cliente API.",
        copy=False,
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

    bdv_id_comercio = fields.Char(string="RIF Comercio (BDV)")
    bdv_numero_comercio = fields.Char(string="Teléfono Comercio (BDV)")
    bdv_hora_pago = fields.Char(string="Hora del Pago BDV")
    bdv_raw_payload = fields.Text(string="Payload Original BDV (JSON)")

    bank_notification_id = fields.Many2one(
        'bestpay.bank.notification',
        string="Notificación Bancaria Casada",
        readonly=True,
        copy=False,
        help="Notificación recibida del banco que originó o validó este pago"
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
            log_business_error(
                self.env,
                'bdv_send_conciliation',
                input_data={'tx': self.reference},
                output_data=(
                    'Falta configurar la API Key del BDV para el comercio %s.'
                    % (self.partner_id.name or '-')
                ),
                is_error=True,
                error_type='BDVMissingApiKey',
                dedupe_key='bdv_missing_api_key:%s' % (self.partner_id.id or 0),
                dedupe_minutes=60,
                meta={
                    'proveedor': 'bdv',
                    'tx': self.reference,
                    'comercio': self.partner_id.name,
                },
            )
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

        # Determinar el teléfono destino según el ambiente (QA vs Producción)
        if env_type == 'qa' and self.partner_id.bdv_telefono_destino_qa:
            telefono_destino = self.partner_id.bdv_telefono_destino_qa
        else:
            telefono_destino = self.partner_id.bdv_telefono_destino or creds.get('telefono_destino', '')
            
        _logger.info(f"[BDV] Teléfono destino seleccionado ({env_type.upper()}): {telefono_destino}")

        payload = {
            "cedulaPagador": self.bdv_cedula_pagador or '',
            "telefonoPagador": self.bdv_telefono_pagador or '',
            "telefonoDestino": telefono_destino,  # ← AHORA USA LA VARIABLE DINÁMICA
            "referencia": self.bdv_referencia or '',
            "fechaPago": fecha_pago,
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
        except requests.exceptions.Timeout:
            mensaje = 'El BDV tardó demasiado en responder. Intenta nuevamente.'
            _logger.error(f"[BDV] Timeout en conciliación TX {self.id}")
            self.write({
                'bdv_conciliation_state': 'error',
                'bdv_conciliation_message': mensaje,
                'state': 'error',
            })
            log_business_error(
                self.env,
                'bdv_send_conciliation',
                input_data={'tx': self.reference, 'url': creds.get('api_url')},
                output_data='Timeout esperando la respuesta del BDV.',
                is_error=True,
                error_type='BDVTimeout',
                dedupe_key='bdv_conciliation_timeout',
                dedupe_minutes=15,
                meta={
                    'proveedor': 'bdv',
                    'tx': self.reference,
                    'ambiente': env_type,
                },
            )
            raise UserError(mensaje)
        except requests.exceptions.RequestException as e:
            mensaje = 'No se pudo conectar con el BDV. Intenta nuevamente.'
            _logger.error(f"[BDV] Error de conexión en TX {self.id}: {str(e)}")
            self.write({
                'bdv_conciliation_state': 'error',
                'bdv_conciliation_message': f"Error de conexión: {str(e)}",
                'state': 'error',
            })
            log_business_error(
                self.env,
                'bdv_send_conciliation',
                input_data=payload,
                output_data='Error de conexión con el BDV: %s' % e,
                exc=e,
                is_error=True,
                dedupe_key='bdv_conciliation_connection',
                dedupe_minutes=15,
                meta={
                    'proveedor': 'bdv',
                    'tx': self.reference,
                    'ambiente': env_type,
                    'url': creds.get('api_url'),
                },
            )
            raise UserError(mensaje)

        # 5. Guardar la respuesta cruda (incluye el cuerpo de los errores HTTP)
        try:
            respuesta = response.json()
        except ValueError:
            respuesta = {'raw': (response.text or '')[:2000]}

        _logger.info(f"[BDV] Respuesta recibida (HTTP {response.status_code}): {respuesta}")
        self.bank_in_log = json.dumps(respuesta, indent=2, ensure_ascii=False)

        # 6. Errores HTTP (400, 401, 404, 5xx, etc.)
        if not response.ok:
            mensaje = self._bdv_conciliation_http_error(response.status_code, respuesta)
            _logger.error(f"[BDV] HTTP {response.status_code} en TX {self.id}: {mensaje}")
            self.write({
                'bdv_conciliation_state': 'error',
                'bdv_conciliation_message': mensaje,
                'state': 'error',
            })
            log_business_error(
                self.env,
                'bdv_send_conciliation',
                input_data={'tx': self.reference, 'url': creds.get('api_url')},
                output_data='El BDV respondió HTTP %s: %s' % (
                    response.status_code, mensaje),
                is_error=True,
                error_type='BDVHttpError',
                dedupe_key='bdv_conciliation_http:%s' % response.status_code,
                dedupe_minutes=15,
                meta={
                    'proveedor': 'bdv',
                    'tx': self.reference,
                    'http_status': response.status_code,
                },
            )
            raise UserError(mensaje)

        # 7. Procesar el código de respuesta del BDV
        code = str(respuesta.get("code") or '')
        message = respuesta.get("message", "")

        if code == '1000':
            # ✅ PAGO APROBADO
            self.write({
                'bdv_conciliation_state': 'approved',
                'bdv_conciliation_message': message,
                'state': 'done',
            })
            _logger.info(f"[BDV] ✅ TX {self.id} APROBADA por el BDV")
            self._bestpay_trigger_webhook_3ro()
            return True
        elif code == '1010':
            # ❌ PAGO RECHAZADO / DATOS INVÁLIDOS / YA CONCILIADO
            # La doc del BDV usa el mismo código para todos los casos; el detalle
            # viene en `data` (Campo X invalido) o en `message`.
            mensaje = self._bdv_conciliation_error_message(respuesta)
            self.write({
                'bdv_conciliation_state': 'rejected',
                'bdv_conciliation_message': mensaje,
                'state': 'cancel',
            })
            _logger.warning(f"[BDV] ❌ TX {self.id} RECHAZADA: {mensaje}")
            # Rechazo de negocio: se registra para auditoría, pero no
            # despierta al grupo (puede ser un dato mal tecleado).
            log_business_error(
                self.env,
                'bdv_send_conciliation',
                input_data={'tx': self.reference, 'referencia': self.bdv_referencia},
                output_data='Pago rechazado por el BDV: %s' % mensaje,
                is_error=False,
                meta={'proveedor': 'bdv', 'tx': self.reference},
            )
            return False
        else:
            # ⚠️ CÓDIGO DESCONOCIDO
            mensaje = f"Código inesperado: {code} - {message}"
            self.write({
                'bdv_conciliation_state': 'error',
                'bdv_conciliation_message': mensaje,
            })
            _logger.error(f"[BDV] ⚠️ TX {self.id} código inesperado: {code}")
            log_business_error(
                self.env,
                'bdv_send_conciliation',
                input_data={'tx': self.reference},
                output_data=(
                    'El BDV devolvió un código inesperado %s: %s'
                    % (code, message)
                ),
                is_error=True,
                error_type='BDVUnexpectedCode',
                dedupe_key='bdv_unexpected_code:%s' % (code or 'sin-codigo'),
                dedupe_minutes=30,
                meta={
                    'proveedor': 'bdv',
                    'tx': self.reference,
                    'codigo': code,
                },
            )
            return False

    # Mensajes de la API de Conciliación del BDV.
    # Todos los rechazos de negocio llegan con code 1010; el detalle real viene
    # en `data` ("Campo X invalido") o en `message` (ya conciliado, monto, etc.).
    _BDV_CONCILIATION_FIELD_ERRORS = {
        'Campo FechaPago invalido': 'La fecha del pago no es válida.',
        'Campo CedulaPagador invalido': 'La cédula del pagador no es válida.',
        'Campo TelefonoPagador invalido': 'El teléfono del pagador no es válido.',
        'Campo TelefonoDestino invalido': 'El teléfono destino del comercio no es válido.',
        'Campo Referencia invalido': 'El número de referencia no es válido (debe tener entre 4 y 12 dígitos).',
        'Campo Importe invalido': 'El importe del pago no es válido.',
        'Campo BancoOrigen invalido': 'El banco de origen no es válido.',
    }

    def _bdv_conciliation_error_message(self, respuesta):
        """Traduce el código 1010 del BDV a un mensaje claro para el cliente."""
        message = str(respuesta.get('message') or '').strip()
        data_key = ' '.join(str(respuesta.get('data') or '').split())

        if data_key in self._BDV_CONCILIATION_FIELD_ERRORS:
            return self._BDV_CONCILIATION_FIELD_ERRORS[data_key]

        lower = message.lower()
        if 'ya fue conciliado' in lower:
            return 'Este pago ya fue conciliado anteriormente.'
        if 'no afiliado al producto' in lower:
            return 'La API Key del comercio no está afiliada al servicio de conciliación del BDV.'
        if 'importe no coincide' in lower:
            return 'El monto reportado no coincide con el pago realizado.'
        if lower.startswith('monto:') and 'transacc' in lower and 'realizada' in lower:
            return 'El monto reportado no coincide con el pago realizado.'
        if 'no existe' in lower:
            return ('No se encontró un pago con esos datos. Verifica referencia, '
                    'teléfono, cédula, monto y fecha.')

        return message or 'No se pudo validar el pago con el BDV.'

    def _bdv_conciliation_http_error(self, status_code, respuesta):
        """Mensaje para los errores HTTP de la API de Conciliación."""
        body = respuesta if isinstance(respuesta, dict) else {}
        text = str(body.get('message') or body.get('error') or body.get('raw') or '')

        if 'no Route matched' in text:
            return 'La URL del servicio de conciliación del BDV no es correcta.'
        if status_code == 400:
            return ('El banco rechazó los datos del pago (formato inválido). '
                    'Verifica teléfono, cédula, referencia y fecha.')
        if status_code in (401, 403):
            return 'La API Key del comercio no es válida o no tiene permisos para conciliar.'
        if status_code == 404:
            return 'El servicio de conciliación del BDV no está disponible (URL incorrecta).'
        if status_code >= 500:
            return 'El BDV no está disponible en este momento. Intenta más tarde.'
        return f'Error del BDV al conciliar el pago (HTTP {status_code}).'

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

        # URL de retorno del tercero enviada en caliente por la API (opcional).
        return_url_3ro = (data.get('return_url_3ro') or '').strip() if isinstance(data, dict) else ''

        vals = {
            'payment_link': payment_link,
            'bestpay_flow_type': 'redirect',
        }
        if return_url_3ro:
            vals['bdv_return_url_3ro'] = return_url_3ro

        self.write(vals)

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
    # RETORNO AL PORTAL DEL TERCERO (Koole / e-commerce)
    # =========================================================================
    def _bdv_get_return_url(self, status=None):
        """Construye la URL a la que se devuelve al pagador al terminar el checkout.

        Prioridad:
        1. URL enviada en caliente por el tercero para esta transacción.
        2. URL por defecto configurada en la ficha del cliente API (Koole).
        3. Sin URL: el checkout se queda en la pantalla de resultado.

        Solo se aceptan esquemas http/https para evitar redirecciones abiertas.
        """
        self.ensure_one()
        partner = self.bestpay_client_id or self.partner_id
        raw_url = (self.bdv_return_url_3ro or '').strip()
        if not raw_url and partner:
            raw_url = (partner.default_return_url_3ro or '').strip()
        if not raw_url:
            return False

        parts = urlsplit(raw_url)
        if parts.scheme.lower() not in ('http', 'https') or not parts.netloc:
            _logger.warning(
                "[BDV] URL de retorno inválida para TX %s (se ignora): %s",
                self.id, raw_url,
            )
            return False

        # Se conservan los parámetros que ya traiga la URL y se reemplazan los
        # nuestros para que un reintento no duplique valores viejos.
        params = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key not in ('bestpay_status', 'external_reference')
        ]
        if status:
            params.append(('bestpay_status', status))
            if self.external_reference:
                params.append(('external_reference', self.external_reference))

        return urlunsplit((
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(params),
            parts.fragment,
        ))

    # =========================================================================
    # MÉTODOS API C2P CUENTAS MÚLTIPLES (BDV)
    # =========================================================================

    def _bdv_get_c2p_base_url(self):
        """Obtiene la URL base específica para C2P según el entorno (QA o Prod)."""
        self.ensure_one()
        env_type = self.provider_id.bdv_environment.strip().lower() if self.provider_id.bdv_environment else 'qa'
        
        if env_type == 'prod':
            # PRODUCCIÓN: URL oficial fija según el PDF de Producción (pág. 2, 4, 6)
            return "https://bdvconciliacion.banvenez.com:443/BankMobilePaymentC2P/MultipleAccounts"
        else:
            # QA: Deriva la URL base desde la URL de Pago Móvil QA que ya tienes configurada
            base_qa = self.provider_id.bdv_api_url or 'https://bdvconciliacionqa.banvenez.com:444'
            base_clean = base_qa.replace('/getMovement/v2', '').replace('/getMovement', '').rstrip('/')
            return f"{base_clean}/BankMobilePaymentC2P/MultipleAccounts"

    def _bdv_get_c2p_headers(self):
        """Construye los headers C2P usando la API Key correcta según el entorno."""
        self.ensure_one()
        env_type = self.provider_id.bdv_environment.strip().lower() if self.provider_id.bdv_environment else 'qa'
        
        if env_type == 'prod':
            # PRODUCCIÓN: Usa la API Key maestra de producción (la misma de Pago Móvil)
            api_key = getattr(self.partner_id, 'bdv_api_key_prod', '')
        else:
            # QA: Usa la key específica de C2P, o la general de QA como respaldo
            api_key = getattr(self.partner_id, 'bdv_api_key_c2p', '') or getattr(self.partner_id, 'bdv_api_key', '')
            
        if not api_key:
            _logger.error(f"BDV C2P: Falta API Key para entorno {env_type} en partner {self.partner_id.name}")
            log_business_error(
                self.env,
                '_bdv_get_c2p_headers',
                input_data={'partner': self.partner_id.name, 'ambiente': env_type},
                output_data=(
                    'Falta la API Key C2P para el entorno %s del comercio %s.'
                    % (env_type.upper(), self.partner_id.name or '-')
                ),
                is_error=True,
                error_type='BDVMissingC2PApiKey',
                dedupe_key='bdv_c2p_missing_api_key:%s:%s' % (
                    self.partner_id.id or 0, env_type),
                dedupe_minutes=60,
                meta={
                    'proveedor': 'bdv',
                    'comercio': self.partner_id.name,
                    'ambiente': env_type,
                },
            )
            raise UserError(f"Error de configuración: Falta la API Key para el entorno {env_type.upper()} en los datos del comercio.")
            
        return {
            "X-API-Key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def _bdv_get_c2p_commerce_phone(self):
        """Obtiene el teléfono del comercio (commerceNumberInstrument) según el entorno."""
        self.ensure_one()
        env_type = self.provider_id.bdv_environment.strip().lower() if self.provider_id.bdv_environment else 'qa'
        
        if env_type == 'prod':
            # PRODUCCIÓN: Usa el teléfono destino principal del comercio (mismo que Pago Móvil)
            phone = getattr(self.partner_id, 'bdv_telefono_destino', '')
        else:
            # QA: Usa el teléfono específico de C2P o el de destino QA como respaldo
            phone = getattr(self.partner_id, 'bdv_phone_destino_c2p', '') or \
                    getattr(self.partner_id, 'bdv_telefono_destino_qa', '') or \
                    getattr(self.partner_id, 'bdv_telefono_destino', '')
                    
        if not phone:
            log_business_error(
                self.env,
                '_bdv_get_c2p_commerce_phone',
                input_data={'partner': self.partner_id.name, 'ambiente': env_type},
                output_data=(
                    'Falta el Teléfono Destino del comercio %s para el '
                    'entorno %s.' % (self.partner_id.name or '-', env_type.upper())
                ),
                is_error=True,
                error_type='BDVMissingC2PPhone',
                dedupe_key='bdv_c2p_missing_phone:%s:%s' % (
                    self.partner_id.id or 0, env_type),
                dedupe_minutes=60,
                meta={
                    'proveedor': 'bdv',
                    'comercio': self.partner_id.name,
                    'ambiente': env_type,
                },
            )
            raise UserError(f"Error de configuración: Falta el Teléfono Destino del comercio para el entorno {env_type.upper()}.")
        return phone

    def _bdv_c2p_log_stage(self, stage, request_payload=None, response_payload=None):
        """Agrega (append) un bloque de log por etapa C2P con encabezado."""
        def dump(data):
            try:
                return json.dumps(data, indent=2, ensure_ascii=False, default=str)
            except Exception:
                return str(data)
        vals = {}
        if request_payload is not None:
            vals['bank_out_log'] = (self.bank_out_log or '') + f"=== SALIDA · {stage} ===\n{dump(request_payload)}\n\n"
        if response_payload is not None:
            vals['bank_in_log'] = (self.bank_in_log or '') + f"=== ENTRADA · {stage} ===\n{dump(response_payload)}\n\n"
        if vals:
            self.write(vals)

    def bdv_c2p_generate_otp(self):
        """Paso 1: Solicita al BDV el envío del OTP al cliente."""
        self.ensure_one()
        env_type = self.provider_id.bdv_environment.strip().lower() if self.provider_id.bdv_environment else 'qa'
        
        base_url = self._bdv_get_c2p_base_url()
        # QA mantiene /v2 (según tu doc actual), Producción va sin /v2 (según PDF Prod)
        action = "paymentkey/v2" if env_type == 'qa' else "paymentkey"
        url = f"{base_url}/{action}"
        
        payload = {"customerDocumentId": self.bdv_c2p_customer_document_id}
        _logger.info(f"BDV C2P OTP Request ({env_type.upper()}): {url} | Payload: {payload}")
        
        self._bdv_c2p_log_stage("GENERATE OTP", request_payload=payload)
        try:
            response = requests.post(url, json=payload, headers=self._bdv_get_c2p_headers(), timeout=15)
            data = response.json()
            _logger.info(f"BDV C2P OTP Response: {data}")
            self._bdv_c2p_log_stage("GENERATE OTP", response_payload=data)
            
            if data.get('code') == '1000':
                self.write({'bdv_c2p_status': 'otp_sent'})
                return {'success': True, 'message': data.get('message', 'OTP generado correctamente')}
            
            self.write({'bdv_c2p_status': 'error', 'state_message': data.get('message')})
            log_business_error(
                self.env,
                'bdv_c2p_generate_otp',
                input_data={'tx': self.reference, 'codigo': data.get('code')},
                output_data='El BDV no generó el OTP: %s' % data.get('message'),
                is_error=False,
                meta={
                    'proveedor': 'bdv',
                    'tx': self.reference,
                    'codigo': data.get('code'),
                },
            )
            raise UserError(f"Error generando OTP: {data.get('message')}")
        except Exception as e:
            self._bdv_c2p_log_stage("GENERATE OTP · ERROR", response_payload={'error': str(e)})
            if not is_logged(e) and not isinstance(e, UserError):
                log_business_error(
                    self.env,
                    'bdv_c2p_generate_otp',
                    input_data={'tx': self.reference},
                    output_data='Error generando OTP C2P: %s' % e,
                    exc=e,
                    is_error=True,
                    dedupe_key='bdv_c2p_otp_exception',
                    dedupe_minutes=15,
                    meta={
                        'proveedor': 'bdv',
                        'tx': self.reference,
                        'ambiente': env_type,
                    },
                )
            raise

    def bdv_c2p_process_payment(self):
        """Paso 2: Procesa el cobro real utilizando el OTP proporcionado."""
        self.ensure_one()
        env_type = self.provider_id.bdv_environment.strip().lower() if self.provider_id.bdv_environment else 'qa'
        
        base_url = self._bdv_get_c2p_base_url()
        action = "process/v2" if env_type == 'qa' else "process"
        url = f"{base_url}/{action}"
        
        # Obtiene el teléfono correcto según el entorno (Prod = principal)
        phone_destino = self._bdv_get_c2p_commerce_phone()
        
        # Lógica de Monto (QA fijo, Prod real)
        if env_type == 'qa':
            amount_str = "1000.6"
            concept_str = "Pago"
            _logger.info(f"[BDV C2P] 🟢 Ambiente QA detectado. Forzando monto: {amount_str} Bs")
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
        
        _logger.info(f"BDV C2P Process Request ({env_type.upper()}): {url} | Payload: {payload}")
        self._bdv_c2p_log_stage("PROCESS PAYMENT (OTP)", request_payload=payload)
        
        try:
            response = requests.post(url, json=payload, headers=self._bdv_get_c2p_headers(), timeout=20)
            data = response.json()
            _logger.info(f"BDV C2P Process Response: {data}")
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
                self._bestpay_trigger_webhook_3ro()
                return {'success': True, 'data': response_data}
                
            self.write({
                'bdv_c2p_status': 'error',
                'state_message': data.get('message', 'Error desconocido en el proceso de cobro')
            })
            log_business_error(
                self.env,
                'bdv_c2p_process_payment',
                input_data={'tx': self.reference, 'codigo': data.get('code')},
                output_data='El BDV rechazó el cobro C2P: %s' % data.get('message'),
                is_error=False,
                meta={
                    'proveedor': 'bdv',
                    'tx': self.reference,
                    'codigo': data.get('code'),
                },
            )
            return {'success': False, 'code': data.get('code'), 'message': data.get('message')}
        except Exception as e:
            self._bdv_c2p_log_stage("PROCESS PAYMENT (OTP) · ERROR", response_payload={'error': str(e)})
            if not is_logged(e) and not isinstance(e, UserError):
                log_business_error(
                    self.env,
                    'bdv_c2p_process_payment',
                    input_data={'tx': self.reference},
                    output_data='Error procesando el cobro C2P: %s' % e,
                    exc=e,
                    is_error=True,
                    dedupe_key='bdv_c2p_process_exception',
                    dedupe_minutes=15,
                    meta={
                        'proveedor': 'bdv',
                        'tx': self.reference,
                        'ambiente': env_type,
                    },
                )
            raise

    def bdv_c2p_annul(self):
        """Paso 3: Anula la transacción en caso de error posterior o reversión."""
        self.ensure_one()
        if not self.bdv_c2p_end_to_end_id:
            _logger.warning("BDV C2P Annul: No hay endToEndId para anular.")
            return False
            
        env_type = self.provider_id.bdv_environment.strip().lower() if self.provider_id.bdv_environment else 'qa'
        
        base_url = self._bdv_get_c2p_base_url()
        action = "annulment/v2" if env_type == 'qa' else "annulment"
        url = f"{base_url}/{action}"
        
        # PRODUCCIÓN: para operaciones interbancarias (BDV – otros bancos) el PDF
        # exige enviar la referencia de origen devuelta por el cobro. En BDV–BDV
        # debe ir vacía. QA mantiene None (sin cambios respecto al dummy).
        bank_code = (self.bdv_c2p_customer_bank_code or '0102').strip()
        if env_type == 'prod':
            reference_origin = (
                (self.bdv_c2p_reference_generated or '')
                if bank_code != '0102' else ''
            )
        else:
            reference_origin = None

        payload = {
            "endToEndId": self.bdv_c2p_end_to_end_id,
            "referenceOrigin": reference_origin,
        }
        
        _logger.info(f"BDV C2P Annul Request ({env_type.upper()}): {url} | Payload: {payload}")
        self._bdv_c2p_log_stage("ANULACION", request_payload=payload)
        
        try:
            response = requests.post(url, json=payload, headers=self._bdv_get_c2p_headers(), timeout=15)
            data = response.json()
            _logger.info(f"BDV C2P Annul Response: {data}")
            self._bdv_c2p_log_stage("ANULACION", response_payload=data)
            
            if data.get('code') == '1000':
                self.write({'bdv_c2p_status': 'annulled', 'state': 'cancel'})
                return True
            log_business_error(
                self.env,
                'bdv_c2p_annul',
                input_data={'tx': self.reference, 'codigo': data.get('code')},
                output_data='El BDV no anuló el cobro C2P: %s' % data.get('message'),
                is_error=False,
                meta={
                    'proveedor': 'bdv',
                    'tx': self.reference,
                    'codigo': data.get('code'),
                },
            )
            return False
        except Exception as e:
            self._bdv_c2p_log_stage("ANULACION · ERROR", response_payload={'error': str(e)})
            if not is_logged(e) and not isinstance(e, UserError):
                log_business_error(
                    self.env,
                    'bdv_c2p_annul',
                    input_data={'tx': self.reference},
                    output_data='Error anulando el cobro C2P: %s' % e,
                    exc=e,
                    is_error=True,
                    dedupe_key='bdv_c2p_annul_exception',
                    dedupe_minutes=15,
                    meta={
                        'proveedor': 'bdv',
                        'tx': self.reference,
                        'ambiente': env_type,
                    },
                )
            raise
        # =================================================================
    # 🔔 MÉTODOS DE WEBHOOK AL TERCERO (BestPay → Koole/ecommerce)
    # =================================================================
    # TODO [MIGRACIÓN FASE 7]: Mover toda esta lógica al módulo base 'bestpay'
    # para que sea reutilizada por Mercantil. Al migrar, estos métodos deben
    # vivir en 'bestpay/models/payment_transaction.py'.
    def _bestpay_schedule_unpaid_notification(self):
        """Programa el aviso 'payment.expired' para el cron de webhooks (sin envío síncrono).

        No se envía en el acto porque este método se ejecuta desde el cron de
        expiración: un timeout por transacción retrasaría todo el barrido.
        """
        if self.provider_id.code != 'bdv':
            return super()._bestpay_schedule_unpaid_notification()

        partner = self.bestpay_client_id or self.partner_id
        if not partner or not partner.webhook_url_3ro or not partner.bestpay_webhook_active:
            _logger.info(
                "[BESTPAY WEBHOOK] TX %s: aviso de vencimiento omitido "
                "(webhook no configurado o desactivado).", self.id
            )
            return False

        self.write({
            'bestpay_webhook_event_id': f"evt_{secrets.token_urlsafe(24)}",
            'bestpay_webhook_state': 'pending',
            'bestpay_webhook_attempts': 0,
            'bestpay_webhook_next_retry': False,
            'bestpay_webhook_last_error': False,
        })
        _logger.info("[BESTPAY WEBHOOK] TX %s: aviso de link vencido programado.", self.id)
        return True

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
        
        # Cada evento es independiente (pago, vencimiento, pago tardío): el event_id
        # debe ser nuevo para que el tercero no lo descarte como duplicado.
        event_id = f"evt_{secrets.token_urlsafe(24)}"
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

        payload = {
            'event_id': self.bestpay_webhook_event_id,
            'event_type': 'payment.expired' if self.state == 'no_pagado' else 'payment.done',
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

        # El aviso de vencimiento explica al cliente por qué se cerró el link.
        if self.state == 'no_pagado':
            payload['reason'] = self.bestpay_unpaid_reason or ''
            if self.create_date:
                payload['expired_at'] = (
                    self.create_date + timedelta(hours=self.BESTPAY_LINK_EXPIRATION_HOURS)
                ).strftime('%Y-%m-%dT%H:%M:%SZ')

        return payload

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
            try:
                tx._bestpay_send_webhook_3ro()
            except Exception as e:
                _logger.error(
                    "[BESTPAY WEBHOOK CRON] Error inesperado en TX %s: %s",
                    tx.reference, e,
                )
                log_business_error(
                    self.env,
                    '_bestpay_process_pending_webhooks',
                    input_data={'tx': tx.reference},
                    output_data='Error inesperado procesando el webhook de %s: %s'
                                % (tx.reference, e),
                    exc=e,
                    is_error=False,
                    meta={'tx': tx.reference, 'proveedor': 'bdv'},
                )

    def _bestpay_send_webhook_3ro(self):
        """Envía el webhook al tercero (un intento). Actualiza el estado."""
        self.ensure_one()
        partner = self.bestpay_client_id or self.partner_id
        if not partner or not partner.webhook_url_3ro:
            _logger.warning(
                "[BESTPAY WEBHOOK] TX %s sin URL de webhook configurada. Se marca como fallido permanente.",
                self.id
            )
            self.write({
                'bestpay_webhook_state': 'failed',
                'bestpay_webhook_last_error': 'URL de webhook no configurada en el partner.',
                'bestpay_webhook_attempts': self.MAX_WEBHOOK_ATTEMPTS,  # No reintenta más
            })
            log_business_error(
                self.env,
                '_bestpay_send_webhook_3ro',
                input_data={'tx': self.reference},
                output_data=(
                    'La TX %s no tiene URL de webhook configurada en el cliente; '
                    'no se puede notificar el pago al tercero.' % self.reference
                ),
                is_error=False,
                meta={'tx': self.reference, 'proveedor': 'bdv'},
            )
            return

        try:
            payload = self._bestpay_build_webhook_payload_3ro()

            headers = {
                'Content-Type': 'application/json; charset=utf-8',
                'X-BestPay-Event-ID': self.bestpay_webhook_event_id or '',
                'User-Agent': 'BestPay-Webhook/1.0',
            }

            _logger.info(
                "[BESTPAY WEBHOOK] TX %s → %s (intento %d/%d)",
                self.id, partner.webhook_url_3ro,
                self.bestpay_webhook_attempts + 1, self.MAX_WEBHOOK_ATTEMPTS
            )
            
            # 1. Llamar al método del core (que ya cifra y envía)
            result = self._bestpay_send_encrypted_webhook(payload, partner)
            
            # 2. Extraer datos directamente del diccionario 
            status_code = result.get('status_code') or 500
            response_text = result.get('response', '')
            success = result.get('success', False)

            # 3. Guardar respuesta en el campo existente de logs
            response_snapshot = json.dumps({
                'status_code': status_code,
                'headers': {},  # El core ya maneja los headers internamente
                'body': response_text[:2000],
                'timestamp': fields.Datetime.now().isoformat(),
            }, ensure_ascii=False, indent=2)

            # 4. Evaluar resultado y actualizar estado
            if success:
                self.write({
                    'bestpay_webhook_state': 'done',
                    'bestpay_webhook_attempts': self.bestpay_webhook_attempts + 1,
                    'bestpay_webhook_sent_at': fields.Datetime.now(),
                    'bestpay_webhook_last_error': False,
                })
                _logger.info("[BESTPAY WEBHOOK] ✅ TX %s confirmada por tercero (HTTP %s).", self.id, status_code)
            else:
                error_msg = f"HTTP {status_code}: {response_text[:500]}"
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
            log_business_error(
                self.env,
                '_bestpay_schedule_retry',
                input_data={'tx': self.reference, 'intentos': attempts},
                output_data=(
                    'La notificación al tercero de la TX %s agotó %s intentos. '
                    'Último error: %s' % (self.reference, attempts, error_msg)
                ),
                is_error=True,
                error_type='Webhook3roExhausted',
                dedupe_key='bdv_webhook_final_failure',
                dedupe_minutes=10,
                meta={
                    'tx': self.reference,
                    'proveedor': 'bdv',
                    'intentos': attempts,
                },
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

    # ==================================================================
    # CASADO DE NOTIFICACIONES BANCARIAS
    # ==================================================================
    def _bdv_notification_payment_date(self, payload):
        """Fecha del pago reportada por el banco (o la de hoy)."""
        fecha_raw = str((payload or {}).get('fecha') or '').strip()
        for date_format in ('%Y%m%d', '%Y-%m-%d'):
            try:
                return datetime.strptime(fecha_raw, date_format).date()
            except (TypeError, ValueError):
                continue
        return fields.Date.today()

    def _bdv_candidate_partner_domain(self, candidate_partners):
        """Restringe la búsqueda a los partners candidatos del comercio.

        Si no hay candidatos no se agrega filtro: el casado por referencia debe
        poder encontrar la transacción aunque la configuración del comercio
        esté incompleta.
        """
        if not candidate_partners:
            return []
        return [
            '|',
            ('bestpay_client_id', 'in', candidate_partners.ids),
            ('partner_id', 'in', candidate_partners.ids),
        ]

    def _bdv_open_candidate_transactions(self, amount, notification_dt, candidate_partners=None):
        """Transacciones abiertas que pudieron originar el pago notificado.

        Filtra por monto exacto y por una ventana de tiempo coherente:
        la transacción no puede ser posterior al pago (salvo un pequeño
        desfase de reloj) ni anterior a las 48h de vigencia del link.
        """
        amount = round(float(amount or 0.0), 2)
        common_domain = [
            ('provider_id.code', '=', 'bdv'),
            ('state', 'not in', ['done', 'cancel', 'error']),
            ('create_date', '<=', notification_dt + timedelta(minutes=5)),
            (
                'create_date',
                '>=',
                notification_dt - timedelta(hours=self.BESTPAY_LINK_EXPIRATION_HOURS),
            ),
        ]
        common_domain += self._bdv_candidate_partner_domain(candidate_partners)

        candidates = self.sudo().search(
            common_domain + [
                ('amount_ves', '>=', round(amount - 0.01, 2)),
                ('amount_ves', '<=', round(amount + 0.01, 2)),
            ],
            order='create_date asc, id asc',
        )
        if candidates:
            return candidates

        # Respaldo para transacciones antiguas en VES sin amount_ves.
        return self.sudo().search(
            common_domain + [
                ('currency_id.name', '=', 'VES'),
                ('amount', '>=', round(amount - 0.01, 2)),
                ('amount', '<=', round(amount + 0.01, 2)),
            ],
            order='create_date asc, id asc',
        )

    def _bdv_tx_amount_matches(self, tx, amount):
        """True si el monto notificado coincide con el monto VES de la TX."""
        amount = round(float(amount or 0.0), 2)
        expected = tx.amount_ves
        if not expected and tx.currency_id.name == 'VES':
            expected = tx.amount
        return abs((expected or 0.0) - amount) <= 0.01

    def _bdv_reference_needs_identity(self, stored_reference):
        """True si la referencia es tan corta que necesita validar pagador.

        Un sufijo de 4 a 7 dígitos puede coincidir con varios pagos. Para esos
        casos no se enlaza solo por referencia: se exige que el teléfono/cédula
        del pagador también coincida con la transacción.
        """
        digits = only_digits(stored_reference)
        return 4 <= len(digits) < 8

    def _bdv_tx_matches_payer(self, tx, phone_key, document_key):
        """True si la transacción pertenece al pagador del aviso."""
        tx_phone, tx_document = tx._bestpay_get_payer_identity()

        stored_phones = {
            key for key in (
                normalize_phone(tx_phone),
                normalize_phone(tx.bdv_telefono_pagador),
            ) if key
        }
        stored_documents = set()
        for document in (tx_document, normalize_document(tx.bdv_cedula_pagador)):
            if document:
                stored_documents.update(document_variants(document))

        if phone_key and set(phone_variants(phone_key)) & stored_phones:
            return True

        if document_key and set(document_variants(document_key)) & stored_documents:
            return True

        return False

    def _bdv_find_transaction_for_notification(self, payload, candidate_partners=None):
        """Busca la transacción que corresponde a un aviso del BDV.

        Orden de confianza:
        1. Referencia bancaria (solo existe si pasó por checkout o ya se casó).
        2. Teléfono/cédula del pagador + monto + ventana de tiempo.
        3. Monto único entre las transacciones abiertas.

        Devuelve un dict con ``tx`` (vacío si no hay match), ``method``,
        ``candidates`` y ``ambiguous``/``details`` cuando hay que revisar.
        """
        payload = payload or {}
        referencia = str(payload.get('referenciaBancoOrdenante') or '').strip()

        try:
            monto = parse_amount(payload.get('monto'))
        except (TypeError, ValueError):
            monto = 0.0

        notification_dt = parse_bank_datetime(
            payload.get('fecha'), payload.get('hora')
        ) or fields.Datetime.now()

        phone_key = normalize_phone(payload.get('numeroCliente'))
        document_key = normalize_document(payload.get('idCliente'))

        result = {
            'tx': self.browse(),
            'method': False,
            'candidates': 0,
            'ambiguous': False,
        }

        # 1. Referencia bancaria: exacta primero. Si el pagador escribió solo
        # una parte (la conciliación acepta desde los 4 últimos dígitos), se
        # compara el sufijo con monto y unicidad. Si no hay referencia en la
        # transacción, este paso no encuentra nada y sigue el casado normal.
        if referencia:
            tx = self.sudo().search([
                ('provider_id.code', '=', 'bdv'),
                ('bdv_referencia', '=', referencia),
            ], limit=1)
            if tx and self._bdv_tx_amount_matches(tx, monto):
                result.update({
                    'tx': tx,
                    'method': 'reference',
                    'candidates': 1,
                })
                return result
            if tx:
                _logger.warning(
                    "[BDV NOTIFICACIÓN] La referencia %s coincide con la TX %s "
                    "pero el monto notificado %s no coincide con %s; se ignora "
                    "la referencia y se sigue con pagador/monto.",
                    referencia, tx.reference, monto, tx.amount_ves,
                )

            ref_candidates = self.sudo().search([
                ('provider_id.code', '=', 'bdv'),
                ('bdv_referencia', '!=', False),
                ('create_date', '<=', notification_dt + timedelta(minutes=5)),
                (
                    'create_date',
                    '>=',
                    notification_dt - timedelta(
                        hours=self.BESTPAY_LINK_EXPIRATION_HOURS),
                ),
            ], order='create_date desc')
            ref_matches = ref_candidates.filtered(
                lambda tx: reference_matches(referencia, tx.bdv_referencia)
                and self._bdv_tx_amount_matches(tx, monto)
                and (
                    not self._bdv_reference_needs_identity(tx.bdv_referencia)
                    or self._bdv_tx_matches_payer(tx, phone_key, document_key)
                )
            )
            if len(ref_matches) == 1:
                result.update({
                    'tx': ref_matches,
                    'method': 'reference',
                    'candidates': len(ref_candidates),
                })
                return result
            if len(ref_matches) > 1:
                result.update({
                    'ambiguous': True,
                    'candidates': len(ref_matches),
                    'details': ref_matches,
                })
                return result

        # 2. Identidad del pagador + monto + tiempo.
        candidates = self._bdv_open_candidate_transactions(
            monto, notification_dt, candidate_partners)
        result['candidates'] = len(candidates)
        if not candidates:
            return result

        strong_matches = candidates.filtered(
            lambda tx: self._bdv_tx_matches_payer(tx, phone_key, document_key))

        if len(strong_matches) == 1:
            result.update({
                'tx': strong_matches,
                'method': 'payer_identity',
                'candidates': len(candidates),
            })
            return result

        if len(strong_matches) > 1:
            result.update({
                'ambiguous': True,
                'candidates': len(strong_matches),
                'details': strong_matches,
            })
            return result

        # 3. Sin coincidencia de identidad. El monto único solo se usa si el
        # aviso trae identidad del pagador y la transacción no tiene identidad
        # registrada (el tercero no la envió). Si la transacción ya tiene
        # identidad y no coincidió, no se adivina por monto: se deja para
        # revisión.
        if not phone_key and not document_key:
            return result

        if len(candidates) == 1:
            candidate = candidates
            tx_phone, tx_document = candidate._bestpay_get_payer_identity()
            has_identity = bool(
                tx_phone
                or tx_document
                or candidate.bdv_telefono_pagador
                or candidate.bdv_cedula_pagador
            )
            if has_identity:
                result.update({
                    'ambiguous': True,
                    'candidates': 1,
                    'details': candidate,
                })
                return result

            result.update({
                'tx': candidate,
                'method': 'amount',
                'candidates': 1,
            })
        else:
            result.update({
                'ambiguous': True,
                'details': candidates,
            })
        return result

    def _bdv_apply_notification_match(self, notification, tx, payload, method, candidate_count):
        """Confirma la transacción (o enlaza el aviso tardío) y sella el aviso."""
        payload = payload or {}

        referencia = str(payload.get('referenciaBancoOrdenante') or '').strip()
        payer_phone_raw = str(payload.get('numeroCliente') or '').strip()
        payer_phone_key = normalize_phone(payer_phone_raw)
        if payer_phone_key:
            payer_phone_raw = '0' + payer_phone_key

        payer_document_raw = str(payload.get('idCliente') or '').strip()
        payer_document_key = normalize_document(payer_document_raw)

        try:
            monto = parse_amount(payload.get('monto'))
        except (TypeError, ValueError):
            monto = 0.0

        partner = tx.bestpay_client_id or tx.partner_id
        now = fields.Datetime.now()

        # La transacción ya estaba pagada (por checkout u otro aviso): el aviso
        # llega tarde. Solo se enlaza para auditoría; no se vuelve a confirmar
        # ni se dispara otro webhook al tercero.
        if tx.state == 'done':
            notification.write({
                'transaction_id': tx.id,
                'partner_id': partner.id if partner else False,
                'status': 'matched_late',
                'match_method': method or 'reference',
                'candidate_count': candidate_count or 1,
                'matched_at': now,
                'error_message': False,
            })
            _logger.info(
                "[BDV NOTIFICACIÓN] TX %s ya completada. Notificación tardía enlazada (Ref: %s)",
                tx.id, referencia,
            )
            return {'codigo': '01', 'matched': True, 'late': True}

        vals_to_update = {
            'state': 'done',
            'bdv_conciliation_state': 'approved',
            'bdv_conciliation_message': 'Aprobado vía webhook notificación BDV',
            'bdv_referencia': referencia,
            'bdv_importe': monto,
            'bdv_telefono_pagador': payer_phone_raw,
            'bdv_cedula_pagador': payer_document_raw,
            'bdv_banco_origen': str(payload.get('bancoOrdenante') or ''),
            'bdv_fecha_pago': self._bdv_notification_payment_date(payload),
            'bdv_hora_pago': str(payload.get('hora') or ''),
            'bdv_id_comercio': str(payload.get('idComercio') or ''),
            'bdv_numero_comercio': str(payload.get('numeroComercio') or ''),
            'bdv_raw_payload': json.dumps(payload, ensure_ascii=False),
        }
        if payer_phone_key and not tx.bestpay_payer_phone:
            vals_to_update['bestpay_payer_phone'] = payer_phone_key
        if payer_document_key and not tx.bestpay_payer_document:
            vals_to_update['bestpay_payer_document'] = payer_document_key

        tx.write(vals_to_update)
        tx.write({'bank_notification_id': notification.id})

        notification.write({
            'transaction_id': tx.id,
            'partner_id': partner.id if partner else False,
            'status': 'matched',
            'match_method': method or 'reference',
            'candidate_count': candidate_count or 1,
            'matched_at': now,
            'error_message': False,
        })

        _logger.info(
            "[BDV NOTIFICACIÓN] ✅ TX %s confirmada (Ref: %s, método: %s)",
            tx.id, referencia, method or 'reference',
        )

        try:
            tx._bestpay_trigger_webhook_3ro()
        except Exception as e:
            _logger.error(
                "[BDV NOTIFICACIÓN] Error disparando webhook al tercero: %s", e)

        return {'codigo': '00', 'matched': True}

    def _bdv_link_pending_notifications_by_reference(self):
        """Enlaza avisos pendientes con la referencia ya conocida de la TX.

        Se llama después de una conciliación aprobada. No cambia el estado de
        la transacción, no vuelve a confirmar y no dispara webhooks al tercero:
        solo deja el aviso enlazado para auditoría (``matched_late``).

        Acepta referencia exacta o parcial (últimos 4+ dígitos) cuando el monto
        coincide y hay un único candidato.
        """
        self.ensure_one()
        stored_reference = str(self.bdv_referencia or '').strip()
        if not stored_reference:
            return 0

        Notification = self.env['bestpay.bank.notification'].sudo()
        base_domain = [
            ('provider_code', '=', 'bdv'),
            ('transaction_id', '=', False),
            ('status', 'in', ('received', 'ambiguous', 'orphan')),
        ]

        notifications = Notification.search(
            base_domain + [('external_ref', '=', stored_reference)])
        if notifications:
            notifications = notifications.filtered(
                lambda notification: self._bdv_tx_amount_matches(
                    self, notification.amount or 0.0))
        if not notifications:
            recent = fields.Datetime.now() - timedelta(
                hours=self.BESTPAY_LINK_EXPIRATION_HOURS)
            candidates = Notification.search(
                base_domain + [('received_at', '>=', recent)],
                order='received_at DESC',
                limit=50,
            )
            notifications = candidates.filtered(
                lambda notification: reference_matches(
                    notification.external_ref, stored_reference)
                and self._bdv_tx_amount_matches(self, notification.amount or 0.0)
                and (
                    not self._bdv_reference_needs_identity(stored_reference)
                    or self._bdv_tx_matches_payer(
                        self,
                        notification.payer_phone_key
                        or normalize_phone(notification.payer_phone),
                        notification.payer_document_key
                        or normalize_document(notification.payer_document),
                    )
                )
            )
            if len(notifications) > 1:
                _logger.warning(
                    "[BDV CONCILIACIÓN] La referencia '%s' coincide con %s "
                    "avisos; no se enlaza automáticamente.",
                    stored_reference, len(notifications),
                )
                return 0

        if not notifications:
            return 0

        partner = self.bestpay_client_id or self.partner_id
        now = fields.Datetime.now()
        for notification in notifications:
            notification.write({
                'transaction_id': self.id,
                'partner_id': partner.id if partner else False,
                'status': 'matched_late',
                'match_method': 'reference',
                'candidate_count': 1,
                'matched_at': now,
                'error_message': False,
            })
            _logger.info(
                "[BDV CONCILIACIÓN] Aviso %s enlazado tardíamente a TX %s.",
                notification.external_ref, self.reference,
            )

        if not self.bank_notification_id:
            self.write({'bank_notification_id': notifications[:1].id})

        return len(notifications)

    def bdv_process_notification_webhook(self, payload, candidate_partners=None, notification=None):
        """Procesa un aviso del BDV y lo casa con su transacción.

        ``candidate_partners`` es el conjunto de clientes que comparten el
        comercio (puede ser uno o varios). La decisión final NO se toma por el
        teléfono del comercio: se toma por la transacción encontrada.
        """
        payload = payload or {}

        if candidate_partners is None:
            candidate_partners = self.env['res.partner']
        elif isinstance(candidate_partners, (list, tuple)):
            candidate_partners = self.env['res.partner'].browse(candidate_partners)

        referencia = str(payload.get('referenciaBancoOrdenante') or '').strip()

        if not notification:
            notification = self.env['bestpay.bank.notification'].sudo().search([
                ('external_ref', '=', referencia),
                ('provider_code', '=', 'bdv'),
            ], limit=1, order='received_at DESC')

        if not notification:
            notification, _created = (
                self.env['bestpay.bank.notification'].sudo()
                ._bestpay_get_or_create_from_bdv_payload(payload)
            )

        # Re-notificación de un aviso ya casado: no se reprocesa.
        if notification.transaction_id and notification.status in ('matched', 'matched_late'):
            return {
                'codigo': '01',
                'matched': True,
                'already_matched': True,
                'status': notification.status,
            }

        match = self._bdv_find_transaction_for_notification(
            payload, candidate_partners)

        if not match['tx']:
            if match.get('ambiguous'):
                details = match.get('details') or self.browse()
                candidate_refs = ', '.join(details.mapped('reference')[:10])
                notification.write({
                    'status': 'ambiguous',
                    'match_method': False,
                    'candidate_count': match.get('candidates', 0),
                    'matched_at': False,
                    'error_message': (
                        'Varias transacciones abiertas coinciden con el aviso. '
                        'Candidatas: %s' % (candidate_refs or '-')
                    ),
                })
                _logger.warning(
                    "[BDV NOTIFICACIÓN] ⚠️ Aviso ambiguo. Referencia: %s, "
                    "candidatas: %s", referencia, candidate_refs or '-',
                )
                log_business_error(
                    self.env,
                    'bdv_process_notification_webhook',
                    input_data={
                        'referencia': referencia,
                        'monto': payload.get('monto'),
                        'candidatas': candidate_refs,
                    },
                    output_data=(
                        'Llegó un pago del BDV que coincide con varias '
                        'transacciones abiertas (referencia %s, monto %s). '
                        'Quedó como notificación ambigua para revisión.'
                        % (referencia or '-', payload.get('monto') or '-')
                    ),
                    is_error=True,
                    error_type='BDVAmbiguousNotification',
                    dedupe_key='bdv_ambiguous_notification:%s' % (
                        referencia or 'sin-referencia'),
                    dedupe_minutes=1440,
                    meta={
                        'proveedor': 'bdv',
                        'referencia': referencia,
                        'monto': payload.get('monto'),
                        'candidatas': candidate_refs,
                    },
                )
                return {
                    'codigo': '00',
                    'matched': False,
                    'status': 'ambiguous',
                }

            notification.write({
                'status': 'orphan',
                'match_method': False,
                'candidate_count': match.get('candidates', 0),
                'matched_at': False,
                'error_message': (
                    'No se encontró una transacción abierta que coincida con '
                    'el aviso (monto, teléfono/cédula y fecha).'
                ),
            })
            _logger.warning(
                "[BDV NOTIFICACIÓN] ⚠️ No se encontró una transacción previa "
                "para casar. Referencia: %s, Tel: %s, Monto: %s. Queda como "
                "notificación huérfana.",
                referencia, payload.get('numeroCliente'),
                payload.get('monto'),
            )
            log_business_error(
                self.env,
                'bdv_process_notification_webhook',
                input_data={
                    'referencia': referencia,
                    'monto': payload.get('monto'),
                    'comercio': notification.merchant_phone or False,
                },
                output_data=(
                    'Llegó una notificación de pago del BDV sin transacción '
                    'que casar (referencia %s, monto %s). Quedó como '
                    'notificación huérfana.' % (
                        referencia or '-', payload.get('monto') or '-')
                ),
                is_error=True,
                error_type='BDVOrphanNotification',
                dedupe_key='bdv_orphan_notification:%s' % (
                    referencia or 'sin-referencia'),
                dedupe_minutes=1440,
                meta={
                    'proveedor': 'bdv',
                    'referencia': referencia,
                    'monto': payload.get('monto'),
                    'comercio': notification.merchant_phone or False,
                },
            )
            return {
                'codigo': '00',
                'matched': False,
                'status': 'orphan',
            }

        return self._bdv_apply_notification_match(
            notification,
            match['tx'],
            payload,
            match['method'],
            match['candidates'],
        )

    def _bdv_attempt_auto_match_notification(self, phone=None, amount=None):
        """Casa un aviso pendiente con esta transacción desde el checkout.

        El pagador está activo en el checkout de esta transacción, así que el
        teléfono del pagador + monto es una señal fuerte incluso si el aviso
        había quedado ambiguo.
        """
        self.ensure_one()
        if self.state == 'done':
            return True

        target_amount = round(float(amount or self.amount_ves or self.amount), 2)
        target_phone_key = normalize_phone(phone or self.bdv_telefono_pagador or '')
        if not target_phone_key:
            target_phone_key, _document = self._bestpay_get_payer_identity()

        # 1. Referencia: si el checkout ya la tiene, es la señal más fuerte.
        if self.bdv_referencia:
            recent = fields.Datetime.now() - timedelta(
                hours=self.BESTPAY_LINK_EXPIRATION_HOURS)
            reference_notifications = self.env['bestpay.bank.notification'].sudo().search([
                ('provider_code', '=', 'bdv'),
                ('status', 'in', ('received', 'ambiguous', 'orphan')),
                ('transaction_id', '=', False),
                ('received_at', '>=', recent),
            ], order='received_at DESC', limit=50)
            reference_matches_found = reference_notifications.filtered(
                lambda notification: reference_matches(
                    notification.external_ref, self.bdv_referencia)
                and abs((notification.amount or 0.0) - target_amount) <= 0.01
                and (
                    not self._bdv_reference_needs_identity(self.bdv_referencia)
                    or self._bdv_tx_matches_payer(
                        self,
                        notification.payer_phone_key
                        or normalize_phone(notification.payer_phone),
                        notification.payer_document_key
                        or normalize_document(notification.payer_document),
                    )
                )
            )
            if len(reference_matches_found) == 1:
                payload = {}
                try:
                    payload = json.loads(
                        reference_matches_found.raw_payload or '{}')
                except (TypeError, ValueError):
                    payload = {}
                result = self._bdv_apply_notification_match(
                    reference_matches_found, self, payload,
                    'checkout_polling', 1)
                return bool(result.get('matched'))

        # 2. Teléfono del pagador + monto.
        if not target_phone_key:
            return False

        notifications = self.env['bestpay.bank.notification'].sudo().search([
            ('provider_code', '=', 'bdv'),
            ('status', 'in', ('received', 'ambiguous')),
            ('transaction_id', '=', False),
            ('amount', '>=', round(target_amount - 0.01, 2)),
            ('amount', '<=', round(target_amount + 0.01, 2)),
        ], order='received_at DESC', limit=20)

        notification = None
        for candidate in notifications:
            candidate_phone = (
                candidate.payer_phone_key
                or normalize_phone(candidate.payer_phone)
            )
            if candidate_phone and candidate_phone == target_phone_key:
                notification = candidate
                break

        if not notification:
            return False

        payload = {}
        try:
            payload = json.loads(notification.raw_payload or '{}')
        except (TypeError, ValueError):
            payload = {}

        result = self._bdv_apply_notification_match(
            notification, self, payload, 'checkout_polling', 1)
        return bool(result.get('matched'))
