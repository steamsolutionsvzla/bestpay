# -*- coding: utf-8 -*-
import json
import logging
import re
from decimal import Decimal, InvalidOperation

import requests
from odoo import fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class BestPayMercantilOperationSearch(models.Model):
    _name = 'bestpay.mercantil.operation.search'
    _description = 'Consulta de operación Mercantil'
    _order = 'create_date desc, id desc'

    transaction_id = fields.Many2one(
        'payment.transaction',
        string="Transacción",
        required=True,
        ondelete='cascade',
        index=True,
    )
    user_id = fields.Many2one(
        'res.users',
        string="Consultado por",
        required=True,
        default=lambda self: self.env.user,
        readonly=True,
    )
    operation_type = fields.Selection(
        [('transfer', "Transferencia"), ('mobile_payment', "Pago móvil")],
        string="Tipo de operación",
        required=True,
    )
    customer_id = fields.Char(string="Identificación del pagador", required=True)
    customer_phone_number = fields.Char(string="Teléfono del pagador")
    bank_code = fields.Char(string="Código del banco")
    merchant_account_number = fields.Char(string="Cuenta del comercio")
    merchant_phone_number = fields.Char(string="Teléfono del comercio")
    payment_reference = fields.Char(string="Referencia de pago", required=True)
    amount = fields.Monetary(
        string="Monto (VES)",
        currency_field='currency_id',
        required=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string="Moneda",
        required=True,
        default=lambda self: self.env['res.currency'].search([('name', '=', 'VES')], limit=1),
    )
    operation_date = fields.Date(string="Fecha de la operación", required=True)
    result = fields.Selection(
        [
            ('approved', "Aprobada"),
            ('not_found', "No encontrada"),
            ('rejected', "No aprobada"),
            ('mismatch', "Datos no coincidentes"),
            ('error', "Error"),
        ],
        string="Resultado",
        required=True,
    )
    applied_offered_amount = fields.Monetary(
        string="Monto VES aplicado",
        currency_field='currency_id',
    )
    applied_exchange_rate = fields.Float(
        string="Tasa aplicada",
        digits=(12, 2),
    )
    message = fields.Char(string="Detalle")
    http_status = fields.Integer(string="HTTP")
    request_payload = fields.Text(string="Solicitud enviada")
    response_payload = fields.Text(string="Respuesta del banco")


class BestPayMercantilOperationSearchWizard(models.TransientModel):
    _name = 'bestpay.mercantil.operation.search.wizard'
    _description = 'Asistente para consultar operación Mercantil'

    transaction_id = fields.Many2one(
        'payment.transaction',
        string="Transacción",
        required=True,
        readonly=True,
    )
    operation_type = fields.Selection(
        [('transfer', "Transferencia"), ('mobile_payment', "Pago móvil")],
        string="Tipo de operación",
        required=True,
        default='transfer',
    )
    customer_id = fields.Char(
        string="Identificación del pagador",
        required=True,
        help="Tipo y número sin guiones ni caracteres especiales. Ej.: V12388088.",
    )
    customer_phone_number = fields.Char(string="Teléfono del pagador")
    bank_code = fields.Char(string="Código del banco")
    payment_reference = fields.Char(string="Referencia de pago", required=True)
    amount = fields.Monetary(
        string="Monto (VES)",
        currency_field='currency_id',
        required=True,
        help="Monto exacto de la operación, con máximo dos decimales.",
    )
    currency_id = fields.Many2one(
        'res.currency',
        string="Moneda",
        required=True,
        default=lambda self: self.env['res.currency'].search([('name', '=', 'VES')], limit=1),
    )
    operation_date = fields.Date(string="Fecha de la operación", required=True)

    def _get_search_partner(self, transaction):
        partner = transaction.bestpay_client_id
        if not partner:
            raise UserError(_("La transacción no tiene un cliente API asociado."))
        required_values = {
            'URL de consulta': partner.mercantil_search_url,
            'X-IBM-Client-Id': partner.mercantil_search_client_id,
            'Terminal ID': partner.mercantil_terminal_id,
            'ID de comercio': partner.mercantil_merchant_id,
            'ID de integrador': partner.mercantil_integrator_id,
            'clave secreta': partner.mercantil_secret_key,
            'Browser Agent': partner.mercantil_search_browser_agent,
            'IP de consulta': partner.mercantil_search_ip_address,
            'fabricante': partner.mercantil_search_manufacturer,
        }
        missing = [label for label, value in required_values.items() if not value]
        if missing:
            raise UserError(
                _("Falta completar la configuración Mercantil: %s.") % ', '.join(missing)
            )
        return partner

    def _build_search_payload(self, transaction, partner):
        self.ensure_one()
        customer_id = (self.customer_id or '').strip().upper()
        if not re.fullmatch(r'[VEP]\d+', customer_id):
            raise UserError(
                _("La identificación debe comenzar con V, E o P y continuar solo con números.")
            )

        reference = (self.payment_reference or '').strip()
        if not reference:
            raise UserError(_("La referencia de pago es obligatoria."))

        try:
            amount = Decimal(str(self.amount))
        except InvalidOperation as error:
            raise UserError(_("El monto debe ser un número válido.")) from error
        if amount <= 0 or amount.as_tuple().exponent < -2:
            raise UserError(_("El monto debe ser mayor que cero y tener máximo dos decimales."))

        search_info = {
            'customerId': transaction._mercantil_encrypt_search_value(
                customer_id, partner.mercantil_secret_key
            ),
            'paymentReference': reference,
            'amount': format(amount, '.2f'),
            'date': self.operation_date.strftime('%Y-%m-%d'),
        }

        if self.operation_type == 'transfer':
            if not partner.mercantil_account_number:
                raise UserError(_("Configure la cuenta Mercantil del comercio antes de consultar transferencias."))
            if not (self.bank_code or '').strip():
                raise UserError(_("El código del banco es obligatorio para consultar transferencias."))
            search_info.update({
                'merchantAccountNumber': transaction._mercantil_encrypt_search_value(
                    partner.mercantil_account_number.strip(), partner.mercantil_secret_key
                ),
                'bankCode': self.bank_code.strip(),
            })
        else:
            if not partner.mercantil_merchant_phone:
                raise UserError(_("Configure el teléfono Mercantil del comercio antes de consultar pagos móviles."))
            if not (self.customer_phone_number or '').strip():
                raise UserError(_("El teléfono del pagador es obligatorio para consultar pagos móviles."))
            search_info.update({
                'merchantPhoneNumber': transaction._mercantil_encrypt_search_value(
                    partner.mercantil_merchant_phone.strip(), partner.mercantil_secret_key
                ),
                'customerPhoneNumber': transaction._mercantil_encrypt_search_value(
                    self.customer_phone_number.strip(), partner.mercantil_secret_key
                ),
            })

        return {
            'merchantIdentify': {
                'integratorId': partner.mercantil_integrator_id,
                'merchantId': partner.mercantil_merchant_id,
                'terminalId': partner.mercantil_terminal_id,
            },
            'clientIdentify': {
                'browserAgent': partner.mercantil_search_browser_agent,
                'ipAddress': partner.mercantil_search_ip_address,
                'mobile': {
                    'manufacturer': partner.mercantil_search_manufacturer,
                },
            },
            'searchInfo': search_info,
        }, customer_id, amount

    @staticmethod
    def _normalize_reference(value):
        text = str(value).strip()
        if text.isdigit():
            return str(int(text))
        return text

    def _check_operation_match(self, response_data, amount, reference):
        info = response_data.get('transactionInfo') or {}
        if self._normalize_reference(info.get('reference', '')) != self._normalize_reference(reference):
            return _("La referencia devuelta por Mercantil no coincide con la consultada.")
        try:
            bank_amount = Decimal(str(info.get('amount')))
        except (InvalidOperation, TypeError):
            return _("Mercantil no devolvió un monto válido para verificar la operación.")
        if bank_amount != amount:
            return _("El monto devuelto por Mercantil no coincide con el consultado.")

        bank_date = str(info.get('trxDate', ''))
        expected_dates = {self.operation_date.strftime('%Y%m%d'), self.operation_date.strftime('%Y-%m-%d')}
        if bank_date not in expected_dates:
            return _("La fecha devuelta por Mercantil no coincide con la consultada.")
        return False

    def action_search_operation(self):
        self.ensure_one()
        transaction = self.transaction_id
        if (
            not transaction
            or transaction.provider_id.code != 'mer'
            or transaction.state not in ('draft', 'pending')
        ):
            raise UserError(
                _("Solo se pueden consultar transacciones Mercantil en borrador o pendientes.")
            )

        partner = self._get_search_partner(transaction)
        payload, customer_id, amount = self._build_search_payload(transaction, partner)
        request_payload = json.dumps(payload, ensure_ascii=False, indent=2)

        http_status = 0
        response_text = ''
        response_data = {}
        try:
            response = requests.post(
                partner.mercantil_search_url,
                json=payload,
                headers={
                    'Content-Type': 'application/json',
                    'X-IBM-Client-Id': partner.mercantil_search_client_id,
                },
                timeout=20,
            )
            http_status = response.status_code
            response_text = response.text
            try:
                parsed_response = response.json()
                response_data = parsed_response if isinstance(parsed_response, dict) else {}
            except ValueError:
                response_data = {}
        except requests.exceptions.RequestException as error:
            response_text = _("Error de conexión: %s") % str(error)

        bank_error_codes = {
            str(item.get('errorCode'))
            for item in (response_data.get('errorList') or [])
            if isinstance(item, dict)
        }
        transaction_info = response_data.get('transactionInfo')
        if not isinstance(transaction_info, dict):
            transaction_info = {}
        message = ''
        if http_status and 200 <= http_status < 300 and '330' in bank_error_codes:
            result = 'not_found'
            message = _("Mercantil no encontró operaciones que coincidan con los datos.")
        elif not http_status or not 200 <= http_status < 300:
            result = 'error'
            message = _("No fue posible completar la consulta. Revise el detalle y la configuración.")
        elif str(transaction_info.get('status', '')).strip().casefold() != 'aprobado':
            result = 'rejected' if transaction_info else 'error'
            message = _("Mercantil no devolvió una operación aprobada.")
        else:
            mismatch = self._check_operation_match(response_data, amount, self.payment_reference)
            if mismatch:
                result = 'mismatch'
                message = mismatch
            else:
                matched_offer = transaction._find_mercantil_offered_amount(amount)
            if not mismatch and not matched_offer:
                result = 'mismatch'
                message = _(
                    "El monto aprobado no coincide con ninguno de los montos VES ofrecidos "
                    "en los enlaces de esta transacción."
                )
            elif not mismatch:
                result = 'approved'
                message = _("Operación aprobada y coincidente con un monto ofrecido.")

        response_payload = (
            json.dumps(response_data, ensure_ascii=False, indent=2)
            if response_data else response_text
        )
        attempt = self.env['bestpay.mercantil.operation.search'].create({
            'transaction_id': transaction.id,
            'user_id': self.env.user.id,
            'operation_type': self.operation_type,
            'customer_id': customer_id,
            'customer_phone_number': self.customer_phone_number,
            'bank_code': self.bank_code,
            'merchant_account_number': (
                partner.mercantil_account_number if self.operation_type == 'transfer' else False
            ),
            'merchant_phone_number': (
                partner.mercantil_merchant_phone if self.operation_type == 'mobile_payment' else False
            ),
            'payment_reference': self.payment_reference.strip(),
            'amount': float(amount),
            'currency_id': self.currency_id.id,
            'operation_date': self.operation_date,
            'result': result,
            'applied_offered_amount': (
                float(matched_offer[1]) if result == 'approved' and matched_offer else 0.0
            ),
            'applied_exchange_rate': (
                float(matched_offer[2]) if result == 'approved' and matched_offer else 0.0
            ),
            'message': message,
            'http_status': http_status,
            'request_payload': request_payload,
            'response_payload': response_payload,
        })
        _logger.info(
            "[BESTPAY MERCANTIL SEARCH] Consulta %s para transacción %s: %s",
            attempt.id, transaction.reference, result,
        )

        webhook_sent = False
        if result == 'approved' and transaction.state in ('draft', 'pending'):
            previous_amount = Decimal(str(transaction.amount_ves or 0)).quantize(Decimal('0.01'))
            applied_amount = matched_offer[1]
            applied_rate = matched_offer[2]
            transaction.write({
                'amount_ves': float(applied_amount),
                'exchange_rate_bcv': float(applied_rate),
                'provider_reference': str(transaction_info.get('reference', '')),
                'mercantil_paid_by_manual_search': True,
            })
            if previous_amount != applied_amount:
                message = _(
                    "Pago confirmado con un monto ofrecido anteriormente (%s VES, tasa %s). "
                    "La transacción se actualizó antes de notificar al tercero."
                ) % (applied_amount, applied_rate)
                attempt.write({'message': message})
            transaction._set_done()
            webhook_sent = transaction._notify_api_client_webhook()
            if webhook_sent:
                message = _("Pago confirmado. El tercero fue notificado correctamente.")
                notification_type = 'success'
            else:
                message = _(
                    "Pago confirmado. La notificación al tercero no fue confirmada; "
                    "revise su estado en el formulario."
                )
                notification_type = 'warning'
        elif result == 'approved':
            message = _("La operación coincide, pero la transacción ya no está pendiente; no se procesó nuevamente.")
            notification_type = 'warning'
        else:
            notification_type = 'warning' if result in ('not_found', 'rejected', 'mismatch') else 'danger'
            message = message or attempt.message

        action = {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Consulta Mercantil"),
                'message': message,
                'type': notification_type,
                'sticky': result != 'approved' or not webhook_sent,
            },
        }
        if result == 'approved':
            action['params']['next'] = {'type': 'ir.actions.client', 'tag': 'reload'}
        return action
