# -*- coding: utf-8 -*-
import base64
import json
import logging
import hashlib
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from odoo import models, fields, api, _
from odoo.exceptions import UserError

MER_LINK_VALIDITY = timedelta(days=1)

# Backoff progresivo entre reintentos de notificación al tercero:
# 5 min → 15 min → 1 h → 6 h  (4 reintentos; 5 intentos en total contando el inicial)
WEBHOOK_3RO_RETRY_DELAYS = [
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
]

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

    payment_link_client_mer = fields.Char(
        string="Link de Pago para el Cliente",
        help="Link de pago que se le envía al cliente final para que complete la transacción. Este link es generado internamente y es único para cada transacción.",
        readonly=True
    )

    payment_link_bank_mer = fields.Char(
        string="Link de boton de Pago Mercantil",
        help="Link generado para redirigir al cliente final al endpoint del banco Mercantil.",
        readonly=True
    )

    payment_link_bank_mer_date = fields.Datetime(
        string="Fecha de Generación del Link Bancario",
        readonly=True
    )

    payment_link_bank_mer_amount_ves = fields.Monetary(
        string="Monto VES al Generar el Link",
        currency_field='currency_ves_id',
        readonly=True
    )

    mercantil_offered_amount_history = fields.Text(
        string="Historial de montos ofrecidos Mercantil",
        default='[]',
        readonly=True,
        copy=False,
        help="Montos VES y tasas usados al generar enlaces bancarios de esta transacción."
    )

    mercantil_paid_by_manual_search = fields.Boolean(
        string="Pago confirmado por consulta manual Mercantil",
        default=False,
        readonly=True,
        copy=False,
        help="Indica que una consulta manual de Mercantil fue la que confirmó el pago."
    )

    mercantil_search_ids = fields.One2many(
        'bestpay.mercantil.operation.search',
        'transaction_id',
        string="Consultas de Operación Mercantil",
        readonly=True,
    )

    # =========================================================================
    # LOGS DE NOTIFICACIÓN AL TERCERO (WEBHOOK)
    # =========================================================================
    webhook_3ro_payload = fields.Text(
        string="Payload enviado al Webhook (3ro)",
        readonly=True,
        copy=False,
        help="Cuerpo completo (JSON) enviado por Odoo al webhook del tercero para notificar el resultado del pago."
    )

    webhook_3ro_response = fields.Text(
        string="Respuesta del Webhook (3ro)",
        readonly=True,
        copy=False,
        help="Respuesta completa (código HTTP y cuerpo) devuelta por el tercero tras la notificación, o el error de conexión si no respondió."
    )

    webhook_3ro_state = fields.Selection([
        ('na', 'No aplica'),
        ('pending', 'Pendiente de reintento'),
        ('sent', 'Notificado'),
        ('failed', 'Falló (sin reintento)'),
    ], string="Estado Notificación (3ro)",
        readonly=True,
        copy=False,
        index=True,
        help="Resultado de la notificación webhook al tercero. "
             "Solo queda 'Pendiente de reintento' cuando el fallo es recuperable "
             "(error de conexión, 5xx, 408/429). Los errores 4xx son permanentes."
    )

    webhook_3ro_attempts = fields.Integer(
        string="Intentos de Notificación (3ro)",
        readonly=True,
        copy=False,
        default=0
    )

    webhook_3ro_next_retry = fields.Datetime(
        string="Próximo Reintento Webhook (3ro)",
        readonly=True,
        copy=False,
        index=True,
        help="Fecha/hora en que el cron volverá a intentar notificar al tercero."
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
                'mercantil_payment_concepts': payment_concepts,
                'payment_link_client_mer': redirect_url_odoo
            })
            
            # 4. Al tercero solo le devolvemos el link seguro de nuestro Odoo
            return {
                "payment_link": redirect_url_odoo
            }
            
        return super()._bestpay_process_transaction_with_bank(api_kwargs)

    # =========================================================================
    # GENERACIÓN / CACHÉ DEL LINK BANCARIO
    # =========================================================================
    def _bestpay_get_or_generate_mer_link(self):
        """
        Recalcula el monto VES y reutiliza el link bancario ya generado salvo que
        no exista, haya expirado (> 1 día) o el monto VES recalculado haya cambiado.
        """
        self.ensure_one()
        partner = self.bestpay_client_id

        if not self.mercantil_offered_amount_history and self.payment_link_bank_mer_amount_ves:
            legacy_history = self._get_mercantil_offered_amount_history()
            self.write({
                'mercantil_offered_amount_history': json.dumps(legacy_history, ensure_ascii=False),
            })

        self._bestpay_action_recalculate_jit_ves()

        link_expired = (
            not self.payment_link_bank_mer_date
            or fields.Datetime.now() - self.payment_link_bank_mer_date >= MER_LINK_VALIDITY
        )
        amount_changed = self.payment_link_bank_mer_amount_ves != self.amount_ves

        if self.payment_link_bank_mer and not link_expired and not amount_changed:
            return self.payment_link_bank_mer

        mercantil_payment_url = partner.mercantil_payment_url.rstrip('/')
        merchant_id = partner.mercantil_merchant_id
        integrator_id = partner.mercantil_integrator_id or ""

        encrypted_data = self._encrypt_transaction_data()
        custom_link = f"{mercantil_payment_url}/?merchantid={merchant_id}&transactiondata={encrypted_data}&integratorid={integrator_id}"

        self._record_mercantil_offered_amount()
        self.write({
            'payment_link_bank_mer': custom_link,
            'payment_link_bank_mer_date': fields.Datetime.now(),
            'payment_link_bank_mer_amount_ves': self.amount_ves,
        })

        return custom_link

    def _get_mercantil_offered_amount_history(self):
        self.ensure_one()
        try:
            history = json.loads(self.mercantil_offered_amount_history or '[]')
        except (TypeError, json.JSONDecodeError):
            _logger.warning(
                "[BESTPAY MERCANTIL] Historial de montos inválido en transacción %s.",
                self.reference,
            )
            history = []
        if not isinstance(history, list):
            history = []

        if not history and self.payment_link_bank_mer_amount_ves:
            history = [{
                'amount_ves': f"{self.payment_link_bank_mer_amount_ves:.2f}",
                'exchange_rate_bcv': str(self.exchange_rate_bcv or 0.0),
                'offered_at': fields.Datetime.to_string(
                    self.payment_link_bank_mer_date or fields.Datetime.now()
                ),
            }]
        return [
            entry for entry in history
            if isinstance(entry, dict) and entry.get('amount_ves') is not None
        ]

    def _record_mercantil_offered_amount(self):
        self.ensure_one()
        history = self._get_mercantil_offered_amount_history()
        amount = f"{self.amount_ves:.2f}"
        rate = str(self.exchange_rate_bcv or 0.0)
        if not any(
            str(entry.get('amount_ves')) == amount
            and str(entry.get('exchange_rate_bcv', '0.0')) == rate
            for entry in history
        ):
            history.append({
                'amount_ves': amount,
                'exchange_rate_bcv': rate,
                'offered_at': fields.Datetime.to_string(fields.Datetime.now()),
            })
        self.write({'mercantil_offered_amount_history': json.dumps(history, ensure_ascii=False)})

    def _find_mercantil_offered_amount(self, amount):
        self.ensure_one()
        requested_amount = Decimal(str(amount)).quantize(Decimal('0.01'))
        matches = []
        for entry in self._get_mercantil_offered_amount_history():
            try:
                offered_amount = Decimal(str(entry['amount_ves'])).quantize(Decimal('0.01'))
                offered_rate = Decimal(str(entry.get('exchange_rate_bcv', '0.0')))
            except (InvalidOperation, TypeError, KeyError):
                continue
            if offered_amount == requested_amount:
                matches.append((entry, offered_amount, offered_rate))
        return matches[-1] if matches else False

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

        transaction_data = {
            "amount": float(self.amount_ves),
            "customerName": self.partner_id.name or partner.name or "Cliente General",
            "returnUrl": final_return_url,
            "merchantId": partner.mercantil_merchant_id or "",
            "invoiceNumber": {
                "number": str(self.id).zfill(12), 
                "invoiceCreationDate": fields.Date.today().strftime("%Y-%m-%d"),
                "invoiceCancelledDate": fields.Date.today().strftime("%Y-%m-%d"),
            },
            "contract": {
                "contractNumber": str(self.id), # Pasamos el ID de la transacción como número de contrato
                "contractDate": fields.Date.today().strftime("%Y-%m-%d")
            },
            "trxType": self.trx_type,
            "currency": "VES",
            "paymentConcepts": payment_concepts
        }

        _logger.info("=== TRANSACTION DATA (REQUEST) FOR MERCANTIL ===")
        _logger.info(json.dumps(transaction_data, indent=4, ensure_ascii=False))
        return transaction_data

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

    @api.model
    def _mercantil_encrypt_search_value(self, value, secret_key):
        key_hash = hashlib.sha256(secret_key.encode('utf-8')).digest()[:16]
        cipher = AES.new(key_hash, AES.MODE_ECB)
        encrypted = cipher.encrypt(pad(value.encode('utf-8'), AES.block_size))
        return base64.b64encode(encrypted).decode('ascii')

    def action_open_mercantil_operation_search(self):
        self.ensure_one()
        if self.provider_id.code != 'mer' or self.state not in ('draft', 'pending'):
            raise UserError(_("Solo se pueden consultar transacciones Mercantil en borrador o pendientes."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Consultar operación Mercantil"),
            'res_model': 'bestpay.mercantil.operation.search.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_transaction_id': self.id,
            },
        }

    def _webhook_3ro_event_name(self):
        """Nombre del evento según el estado (se mantiene el histórico de pagos)."""
        if self.state == 'done':
            return 'payment.completed'
        if self.state == 'no_pagado':
            return 'payment.expired'
        return f"payment.{self.state}"

    def _bestpay_schedule_unpaid_notification(self):
        """Programa el aviso 'payment.expired' para el cron de reintentos de Mercantil.

        Se usa el sistema de webhooks propio de Mercantil (webhook_3ro_*), que ya
        tiene backoff y cron cada 5 minutos.
        """
        if self.provider_id.code != 'mer':
            return super()._bestpay_schedule_unpaid_notification()

        partner = self.bestpay_client_id or self.partner_id
        if not partner or not partner.webhook_url_3ro or not partner.bestpay_webhook_active:
            _logger.info(
                "[WEBHOOK TERCERO] Tx %s: aviso de vencimiento omitido "
                "(webhook no configurado o desactivado).", self.reference
            )
            return False

        self.write({
            'webhook_3ro_state': 'pending',
            'webhook_3ro_attempts': 0,
            'webhook_3ro_next_retry': fields.Datetime.now(),
        })
        _logger.info(
            "[WEBHOOK TERCERO] Tx %s: aviso de link vencido programado.", self.reference
        )
        return True

    @api.model
    def _webhook_3ro_is_retryable(self, status_code):
        """
        Determina si tiene sentido reintentar la notificación según el resultado.
        - Sin status_code (error de conexión/timeout): el servidor del tercero
          pudo estar caído o inalcanzable → reintentar.
        - 5xx, 408 (timeout) o 429 (rate-limit): fallo transitorio → reintentar.
        - Resto de 4xx (400, 401, 403, 404...): error permanente de la petición,
          reintentar no lo resolvería → no reintentar.
        """
        if status_code is None:
            return True
        return status_code >= 500 or status_code in (408, 429)

    def _notify_api_client_webhook(self):
        """
        Notifica al tercero (cliente API) el resultado de la transacción
        llamando a la URL de webhook configurada en su ficha (webhook_url_3ro).

        Guarda el payload enviado y la última respuesta recibida, y gestiona el
        estado de la notificación: 'sent' en éxito; 'pending' (programando el
        próximo reintento con backoff) solo si el fallo es recuperable; 'failed'
        si el fallo es permanente o se agotaron los reintentos; 'na' si el
        cliente no tiene webhook configurada.

        :return: True si el tercero confirmó recepción (HTTP 2xx), False en caso contrario.
        """
        self.ensure_one()

        # Verificar si el cliente tiene configurada una URL de webhook
        partner = self.bestpay_client_id or self.partner_id
        if not partner or not partner.webhook_url_3ro:
            _logger.info(
                "[WEBHOOK TERCERO] El cliente %s no tiene configurada una webhook_url_3ro.",
                partner.name if partner else "desconocido",
            )
            if self.webhook_3ro_state != 'na':
                self.write({'webhook_3ro_state': 'na'})
            return False

        webhook_url = partner.webhook_url_3ro
        attempts = self.webhook_3ro_attempts + 1

        # Construir el payload a enviar
        payload = {
            "event": self._webhook_3ro_event_name(),
            "external_reference": self.external_reference,
            "odoo_reference": self.reference,
            "transaction_id": self.id,
            "uuid_hash": self.uuid_hash,
            "status": self.state,
            "amount": self.amount,
            "currency": self.currency_id.name,
            "amount_ves": self.amount_ves,
            "exchange_rate": self.exchange_rate_bcv,
            # "bank_reference": self.acquirer_reference or '',
        }

        # El aviso de vencimiento explica al cliente por qué se cerró el link.
        if self.state == 'no_pagado':
            payload["reason"] = self.bestpay_unpaid_reason or ''
            if self.create_date:
                payload["expired_at"] = (
                    self.create_date + timedelta(hours=self.BESTPAY_LINK_EXPIRATION_HOURS)
                ).strftime('%Y-%m-%dT%H:%M:%SZ')

        payload_json = json.dumps(payload, ensure_ascii=False, indent=4)
        _logger.info(f"[WEBHOOK TERCERO] Intento #{attempts} hacia {webhook_url} para transacción {self.reference}")
        result = self._bestpay_send_encrypted_webhook(payload, partner)
        status_code = result.get('status_code')
        success = result.get('success', False)
        if status_code is not None:
            response_body = f"HTTP {status_code}\n\n{result.get('response', '')}"
        else:
            response_body = f"ERROR DE CONEXIÓN O CIFRADO:\n\n{result.get('response', '')}"

        vals = {
            'webhook_3ro_payload': payload_json,
            'webhook_3ro_response': f"[Intento #{attempts} - {fields.Datetime.now()}]\n{response_body}",
            'webhook_3ro_attempts': attempts,
        }

        if success:
            vals.update({'webhook_3ro_state': 'sent', 'webhook_3ro_next_retry': False})
        elif not self._webhook_3ro_is_retryable(status_code):
            # Fallo permanente (p. ej. 400/401/403/404): reintentar no tiene sentido
            _logger.warning(
                f"[WEBHOOK TERCERO] Fallo permanente (HTTP {status_code}) notificando Tx {self.reference}. "
                "No se programará reintento automático."
            )
            vals.update({'webhook_3ro_state': 'failed', 'webhook_3ro_next_retry': False})
        elif attempts <= len(WEBHOOK_3RO_RETRY_DELAYS):
            next_retry = fields.Datetime.now() + WEBHOOK_3RO_RETRY_DELAYS[attempts - 1]
            _logger.warning(
                f"[WEBHOOK TERCERO] Fallo recuperable notificando Tx {self.reference} "
                f"(intento #{attempts}). Próximo reintento: {next_retry}"
            )
            vals.update({'webhook_3ro_state': 'pending', 'webhook_3ro_next_retry': next_retry})
        else:
            _logger.error(
                f"[WEBHOOK TERCERO] Se agotaron los reintentos para Tx {self.reference} "
                f"tras {attempts} intentos. Marcada como fallida."
            )
            vals.update({'webhook_3ro_state': 'failed', 'webhook_3ro_next_retry': False})

        self.write(vals)
        return success

    def action_retry_webhook_3ro(self):
        """
        Botón manual: fuerza un reintento inmediato de la notificación al tercero.
        El propio método de notificación vuelve a evaluar si el fallo es
        recuperable, por lo que un fallo permanente (4xx) quedará en 'failed'.
        """
        self.ensure_one()
        if self.webhook_3ro_state not in ('pending', 'failed'):
            raise UserError(_("Esta transacción no tiene una notificación de webhook que reintentar."))

        success = self._notify_api_client_webhook()

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Webhook al tercero"),
                'message': _("Notificación enviada correctamente.") if success else _(
                    "La notificación volvió a fallar. Revisa el estado y los logs del webhook."
                ),
                'type': 'success' if success else 'warning',
                'sticky': False,
            }
        }

    @api.model
    def _cron_retry_webhooks_3ro(self):
        """
        Cron: reintenta las notificaciones webhook al tercero marcadas como
        pendientes cuyo próximo reintento ya venció.
        """
        now = fields.Datetime.now()
        txs = self.search([
            ('webhook_3ro_state', '=', 'pending'),
            ('webhook_3ro_next_retry', '!=', False),
            ('webhook_3ro_next_retry', '<=', now),
        ], limit=50)

        if not txs:
            return

        _logger.info(f"[WEBHOOK TERCERO] Cron de reintentos: {len(txs)} transacción(es) por notificar.")

        for tx in txs:
            try:
                with self.env.cr.savepoint():
                    tx._notify_api_client_webhook()
            except Exception as e:
                _logger.error(
                    f"[WEBHOOK TERCERO] Error inesperado reintentando notificación de Tx {tx.reference}: {str(e)}"
                )