# -*- coding: utf-8 -*-
import base64
import json
import logging
import hashlib
import requests
from datetime import timedelta
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
                "redirect_url": redirect_url_odoo
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

        self.write({
            'payment_link_bank_mer': custom_link,
            'payment_link_bank_mer_date': fields.Datetime.now(),
            'payment_link_bank_mer_amount_ves': self.amount_ves,
        })

        return custom_link

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
            _logger.info(f"[WEBHOOK TERCERO] El cliente {partner.name} no tiene configurada una webhook_url_3ro.")
            if self.webhook_3ro_state != 'na':
                self.write({'webhook_3ro_state': 'na'})
            return False

        webhook_url = partner.webhook_url_3ro
        attempts = self.webhook_3ro_attempts + 1

        # Construir el payload a enviar
        payload = {
            "event": "payment.completed" if self.state == 'done' else f"payment.{self.state}",
            "external_reference": self.external_reference,
            "odoo_reference": self.reference,
            "transaction_id": self.id,
            "uuid_hash": self.uuid_hash,
            "status": self.state,
            "amount": self.amount,
            "currency": self.currency_id.name,
            "amount_ves": self.amount_ves,
            "exchange_rate": self.exchange_rate_bcv,
            "bank_reference": self.acquirer_reference or '',
        }

        payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
        status_code = None
        success = False

        try:
            _logger.info(f"[WEBHOOK TERCERO] Intento #{attempts} hacia {webhook_url} para transacción {self.reference}")
            response = requests.post(
                webhook_url,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=10
            )
            status_code = response.status_code
            response_body = f"HTTP {response.status_code}\n\n{response.text}"
            success = response.status_code in (200, 201, 204)
        except requests.exceptions.RequestException as e:
            _logger.error(f"[WEBHOOK TERCERO] Error de conexión al notificar Tx {self.reference}: {str(e)}")
            response_body = f"ERROR DE CONEXIÓN (sin respuesta del tercero):\n\n{str(e)}"

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