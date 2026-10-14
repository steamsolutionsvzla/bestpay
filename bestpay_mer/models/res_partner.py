# -*- coding: utf-8 -*-
from odoo import models, fields, _
from odoo.exceptions import UserError
from odoo.http import request

class ResPartner(models.Model):
    _inherit = 'res.partner'

    # Credenciales exclusivas Mercantil por cada cliente de la API
    mercantil_payment_url = fields.Char(
        string="URL Botón de Pago Mercantil",
        help="URL del endpoint del banco (Sandbox o Producción) asignada a este cliente."
    )
    mercantil_integrator_id = fields.Char(
        string="ID Integrador Mercantil",
        help="Identificador del integrador proporcionado por el banco."
    )
    mercantil_merchant_id = fields.Char(
        string="ID Comercio Mercantil (Merchant ID)",
        help="Identificador único del comercio en Mercantil."
    )
    mercantil_secret_key = fields.Char(
        string="Clave Secreta Mercantil (Secret Key)",
        help="Clave secreta utilizada para la encriptación AES-ECB de las transacciones."
    )
    mercantil_search_url = fields.Char(
        string="URL de Consulta de Operaciones",
        help="URL del endpoint integrated-search de Mercantil para sandbox o producción."
    )
    mercantil_search_client_id = fields.Char(
        string="X-IBM-Client-Id",
        help="Client ID requerido por el API de consulta de operaciones Mercantil."
    )
    mercantil_terminal_id = fields.Char(
        string="Terminal ID Mercantil",
        help="Identificador del terminal enviado en merchantIdentify."
    )
    mercantil_account_number = fields.Char(
        string="Cuenta Mercantil del Comercio",
        help="Cuenta del comercio usada para consultar transferencias bancarias."
    )
    mercantil_merchant_phone = fields.Char(
        string="Teléfono Mercantil del Comercio",
        help="Teléfono del comercio usado para consultar pagos móviles."
    )
    mercantil_search_browser_agent = fields.Char(
        string="Browser Agent de Consulta",
        default="Chrome",
        help="Valor clientIdentify.browserAgent enviado al API. Ejemplo: Chrome."
    )
    mercantil_search_ip_address = fields.Char(
        string="IP de Consulta",
        help="Valor clientIdentify.ipAddress enviado al API. Debe configurarse según los requisitos del banco."
    )
    mercantil_search_manufacturer = fields.Char(
        string="Fabricante del dispositivo de la Consulta",
        default="Samsung",
        help="Valor clientIdentify.mobile.manufacturer enviado al API. Ejemplo: Samsung."
    )

    # Configuración de flujos y URLs del tercero
    mercantil_payment_concepts = fields.Char(
        string="Conceptos de Pago por Defecto",
        default='["b2b","c2p","tdd"]',
        help="Formatos de pago permitidos en formato JSON string. Ej: ['b2b','c2p','tdd']"
    )
    default_return_url_3ro = fields.Char(
        string="URL de Retorno por Defecto (3ro)",
        help="URL del ecommerce del tercero a donde el banco redirigirá al cliente final si no se envía una en la API."
    )
    webhook_url_3ro = fields.Char(
        string="URL Webhook del Tercero",
        help="URL del sistema del tercero donde Odoo notificará de forma asíncrona cuando el banco confirme el pago."
    )

    def action_capture_mercantil_search_ip(self):
        self.ensure_one()
        try:
            detected_ip = request.httprequest.remote_addr
        except RuntimeError as error:
            raise UserError(_("La IP solo se puede detectar desde una sesión web activa.")) from error
        if not detected_ip:
            raise UserError(_("No se pudo detectar la IP de la sesión actual."))
        self.mercantil_search_ip_address = detected_ip
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("IP detectada"),
                "message": _(
                    "Se guardó la IP de conexión al backend (%s). Verifique que sea el valor requerido "
                    "por Mercantil; detrás de un proxy podría no ser la IP esperada."
                ) % detected_ip,
                "type": "warning",
                "sticky": True,
            },
        }